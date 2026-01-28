# -*- coding: utf-8 -*-
from __future__ import annotations

import io, csv, json, hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, List, Tuple

from fastapi import APIRouter, HTTPException, Body, Query

from inventory_hub.settings import settings
from inventory_hub.config_io import load_supplier as load_supplier_config
from inventory_hub.adapters.paul_lange_web import (
    LoginConfig, 
    refresh_invoices_web as paul_lange_refresh_invoices_web, 
    prepare_from_invoice
)
from inventory_hub.adapters import northfinder_web

router = APIRouter(tags=["invoices"])

# -----------------------------
# Paths / index helpers
# -----------------------------
def _supplier_root(supplier: str) -> Path:
    return settings.INVENTORY_DATA_ROOT / "suppliers" / supplier

def _invoices_dir(supplier: str) -> Path:
    return _supplier_root(supplier) / "invoices"

def _history_dir(supplier: str) -> Path:
    return _invoices_dir(supplier) / "history"

def _index_latest_path(supplier: str) -> Path:
    return _invoices_dir(supplier) / "index.latest.json"

def _imports_upgates_dir(supplier: str) -> Path:
    return settings.INVENTORY_DATA_ROOT / "suppliers" / supplier / "imports" / "upgates"

def _strip_data_prefix(p: str) -> str:
    # index.latest.json často drží cesty s prefixom "data/"
    return p[5:] if p.startswith("data/") else p

def _cfg_get(cfg: Any, *keys: str, default=None):
    cur = cfg
    for k in keys:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            cur = getattr(cur, k, None)
    return default if cur is None else cur
# -----------------------------
# Basic utils
# -----------------------------

def _to_number(s: Any) -> float:
    if s is None:
        return 0.0
    ss = str(s).strip()
    if not ss:
        return 0.0
    ss = ss.replace(" ", "").replace("\u00A0", "").replace(",", ".")
    ss = (ss.replace("€","").replace("EUR","").replace("eur","")
             .replace("Kč","").replace("CZK","").replace("czk",""))
    try:
        return float(ss)
    except Exception:
        return 0.0

def _decode_bytes_auto(b: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1250", "cp1252", "latin-1"):
        try:
            return b.decode(enc)
        except Exception:
            continue
    return b.decode("latin-1", errors="ignore")

def _detect_delimiter(sample_text: str) -> str:
    head = "\n".join(sample_text.splitlines()[:5])
    return ";" if head.count(";") > head.count(",") else ","

def _norm_header(h: str) -> str:
    s = str(h or "").strip().strip('"').strip("'")
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return s

def _get1(rec: dict[str, str], variants: list[str], default: str = "") -> str:
    for v in variants:
        vv = rec.get(_norm_header(v))
        if vv is not None and str(vv).strip() != "":
            return str(vv)
    return default

def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    import json
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

# -----------------------------
# Console config (currency rates) + supplier invoice currency
# -----------------------------
def _console_czk_eur_rate() -> float:
    root = settings.INVENTORY_DATA_ROOT
    candidates = [
        root / "configs" / "console.json",
        root / "console" / "config.json",
        root / "console.json",
    ]
    for p in candidates:
        cfg = _load_json(p)
        try:
            rate = cfg.get("currency_rates", {}).get("CZK", {}).get("EUR")
            if rate:
                return float(rate)
        except Exception:
            pass
    return 0.000  # no fallback

def _supplier_invoice_currency(supplier: str) -> str:
    cfg = _supplier_cfg(supplier)
    # prefer adapter_settings.currency, fallback invoices.currency, default EUR
    cur = (
        cfg.get("adapter_settings", {}).get("currency")
        or cfg.get("invoices", {}).get("currency")
        or "EUR"
    )
    return str(cur).upper()


def _supplier_cfg(supplier: str) -> dict:
    p = settings.INVENTORY_DATA_ROOT / "suppliers" / supplier / "config.json"
    return _load_json(p)

def _supplier_vat_rate(supplier: str) -> float:
    cfg = _supplier_cfg(supplier)
    try:
        return float(
            cfg.get("adapter_settings", {}).get("vat_rate")
            or cfg.get("invoices", {}).get("vat_rate")
            or 23.0
        )
    except Exception:
        return 23.0
    
def _supplier_mapping(supplier: str) -> dict:
    cfg = _supplier_cfg(supplier)
    return (
        cfg.get("adapter_settings", {}).get("mapping")
        or cfg.get("invoices", {}).get("mapping")
        or {}
    )

def _console_rate(from_ccy: str, to_ccy: str) -> float:
    f = (from_ccy or "").upper()
    t = (to_ccy or "").upper()
    if f == t:
        return 1.0
    if f == "CZK" and t == "EUR":
        return _console_czk_eur_rate()
    return 1.0

def _canon_from_invoice(inv_rec: dict, supplier: str) -> dict:
    """
    Supplier-deklaratívne mapovanie: adapter_settings.mapping.invoice_to_canon + postprocess.
    Vypočíta UNIT_PRICE_INC_EUR podľa vat_rate a currency.
    """
    mp = _supplier_mapping(supplier)
    inv2can = mp.get("invoice_to_canon", {}) or {}
    postp   = mp.get("postprocess", {}) or {}

    vat_rate = _supplier_vat_rate(supplier)
    supplier_ccy = _supplier_invoice_currency(supplier)  # EUR/CZK
    rate_to_eur = _console_rate(supplier_ccy, "EUR")

    def get_src(name: str) -> str:
        src = inv2can.get(name)
        if not src:
            return ""
        return str(inv_rec.get(_norm_header(src), "")).strip()

    scm   = get_src("SCM")
    ean   = get_src("EAN")
    title = get_src("TITLE")
    qty   = _to_number(get_src("QTY"))

    # jednotková cena – EX alebo INC podľa unit_price_source
    unit_ex = None
    unit_inc = None
    src_kind = (postp.get("unit_price_source") or "ex").lower()  # "ex"|"inc"
    if src_kind == "inc":
        unit_inc = _to_number(get_src("UNIT_PRICE_INC"))
        if unit_inc and vat_rate:
            unit_ex = unit_inc / (1.0 + float(vat_rate)/100.0)
    else:
        unit_ex = _to_number(get_src("UNIT_PRICE_EX"))
        if unit_ex and vat_rate:
            unit_inc = unit_ex * (1.0 + float(vat_rate)/100.0)

    unit_inc_eur = (unit_inc or 0.0) * rate_to_eur

    prefix = postp.get("product_code_prefix") or ""
    product_code = f"{prefix}{scm}" if (prefix and scm) else scm

    return {
        "SCM": scm,
        "EAN": ean,
        "TITLE": title,
        "QTY": qty,
        "UNIT_PRICE_EX": unit_ex or 0.0,
        "UNIT_PRICE_INC": unit_inc or 0.0,
        "UNIT_PRICE_INC_EUR": unit_inc_eur,
        "CURRENCY": supplier_ccy,
        "PRODUCT_CODE": product_code
    }

# -----------------------------
# Index carry-over helpers
# -----------------------------
def _load_prev_index_map(supplier: str) -> Dict[str, Dict[str, Any]]:
    mp: Dict[str, Dict[str, Any]] = {}
    idx = _index_latest_path(supplier)
    if not idx.exists():
        return mp
    try:
        data = json.loads(idx.read_text(encoding="utf-8"))
        for it in data.get("invoices", []):
            key = it.get("invoice_id") or f"{supplier}:{it.get('number')}"
            if key:
                mp[str(key)] = it
    except Exception:
        pass
    return mp

def _enrich_with_history(invoices: List[Dict[str, Any]], supplier: str) -> None:
    hdir = _history_dir(supplier)
    if not hdir.is_dir():
        return
    for it in invoices:
        inv_no = str(it.get("number") or "").strip()
        if not inv_no:
            continue
        files = sorted(hdir.glob(f"{inv_no}_*.json"))
        it["history_count"] = len(files)
        if files:
            try:
                latest = files[-1]
                it["last_processed_at"] = json.loads(latest.read_text(encoding="utf-8")).get("processed_at")
            except Exception:
                it["last_processed_at"] = None




# -----------------------------
# Routes: refresh / prepare / index / reindex / history
# -----------------------------

# Strategy dispatch table
INVOICE_STRATEGIES = {
    "paul-lange-web": "paul_lange",
    "northfinder-web": "northfinder",
    "manual": "manual",
}


def _dispatch_paul_lange(cfg: Dict, data_root: Path, supplier: str, months_back: int):
    """Paul-Lange web scraping strategy"""
    web = _cfg_get(cfg, "invoices", "download", "web") or None
    if not web:
        raise HTTPException(400, detail="Missing invoices.download.web config")

    login = _cfg_get(web, "login") or None
    if not login:
        raise HTTPException(400, detail="Missing invoices.download.web.login config")
    lc = LoginConfig(
        mode=_cfg_get(login, "mode", default="form"),
        login_url=_cfg_get(login, "login_url", default="") or "",
        user_field=_cfg_get(login, "user_field", default="login") or "login",
        pass_field=_cfg_get(login, "pass_field", default="password") or "password",
        username=_cfg_get(login, "username", default="") or "",
        password=_cfg_get(login, "password", default="") or "",
        cookie=_cfg_get(login, "cookie", default="") or "",
        insecure_all=bool(_cfg_get(login, "insecure_all", default=False)),
    )
    return paul_lange_refresh_invoices_web(data_root, supplier, lc, months_back=months_back)


def _dispatch_northfinder(cfg: Dict, data_root: Path, supplier: str, months_back: int):
    """Northfinder Playwright-based web strategy"""
    return northfinder_web.refresh_invoices_web(
        data_root=data_root,
        supplier_code=supplier,
        supplier_config=cfg,
        months_back=months_back,
    )


@router.post("/suppliers/{supplier}/invoices/refresh")
def invoices_refresh(
    supplier: str,
    months_back: Optional[int] = Query(None),
) -> Dict[str, Any]:
    cfg = load_supplier_config(supplier)
    strat = _cfg_get(cfg, "invoices", "download", "strategy")
    if not strat:
        raise HTTPException(status_code=400, detail="Supplier config missing invoices.download.strategy")

    mb = int(months_back or _cfg_get(cfg, "invoices", "months_back_default", default=6) or 3)
    data_root = Path(settings.INVENTORY_DATA_ROOT).expanduser()

    # Strategy dispatch
    if strat == "paul-lange-web":
        res = _dispatch_paul_lange(cfg, data_root, supplier, mb)
    elif strat == "northfinder-web":
        res = _dispatch_northfinder(cfg, data_root, supplier, mb)
    elif strat == "manual":
        # Manual strategy - just return empty result, no auto-download
        return {
            "ok": True,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
            "pages": 0,
            "log_files": [],
            "message": "Manual strategy - no auto-download"
        }
    else:
        raise HTTPException(400, detail=f"Unsupported strategy: {strat}")

    # carry-over processed
    prev_map = _load_prev_index_map(supplier)
    idx_path = _index_latest_path(supplier)
    if idx_path.is_file():
        try:
            idx_data = json.loads(idx_path.read_text(encoding="utf-8"))
        except Exception:
            idx_data = {}

        if isinstance(idx_data, dict):
            items = list(idx_data.values())
        elif isinstance(idx_data, list):
            items = idx_data
        else:
            items = []

        for it in items:
            if not isinstance(it, dict): 
                continue
            inv_id = it.get("invoice_id") or f"{supplier}:{it.get('number')}"
            prev_it = prev_map.get(inv_id) or (prev_map.get(f"{supplier}:{it.get('number')}") if it.get("number") else None)
            if prev_it and prev_it.get("status") == "processed":
                it["status"] = "processed"
                if prev_it.get("processed_at"):
                    it["processed_at"] = prev_it["processed_at"]
                if prev_it.get("stats"):
                    it["stats"] = prev_it["stats"]
                if prev_it.get("outputs"):
                    it["outputs"] = prev_it["outputs"]

        if isinstance(idx_data, dict):
            new_payload = {}
            for it in items:
                if isinstance(it, dict):
                    key = it.get("invoice_id") or f"{supplier}:{it.get('number')}"
                    if key:
                        new_payload[key] = it
            idx_path.write_text(json.dumps(new_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            idx_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

    # Extract errors and log_files from result
    errors = getattr(res, "errors", []) or []
    log_files = getattr(res, "log_files", []) or []
    
    # ok is false if there were failures or errors
    is_ok = (res.failed == 0 and len(errors) == 0)
    
    return {
        "ok": is_ok,
        "downloaded": res.downloaded,
        "skipped": res.skipped,
        "failed": res.failed,
        "pages": getattr(res, "pages", 0),
        "errors": errors,
        "log_files": log_files,
    }

@router.post("/runs/prepare")
def run_prepare(
    payload: Dict[str, Any] = Body(..., example={
        "supplier_ref": "paul-lange",
        "shop_ref": "biketrek",
        "invoice_relpath": "invoices/csv/F2025100902.csv",
        "use_invoice_qty": True
    })
) -> Dict[str, Any]:
    supplier = payload.get("supplier_ref")
    shop     = payload.get("shop_ref")
    relpath  = payload.get("invoice_relpath")
    use_qty  = bool(payload.get("use_invoice_qty", True))
    if not (supplier and shop and relpath):
        raise HTTPException(400, detail="supplier_ref, shop_ref and invoice_relpath are required.")

    data_root = Path(settings.INVENTORY_DATA_ROOT).expanduser()
    out = prepare_from_invoice(data_root, supplier, shop, relpath, use_invoice_qty=use_qty)

    # normalize stats (ensure unmatched present)
    stats = {
        "existing": int(out.get("existing", 0)),
        "new": int(out.get("new", 0)),
        "unmatched": int(out.get("unmatched", 0)),
        "invoice_items": int(out.get("invoice_items", 0)),
    }
    outputs = out.get("outputs", {}) or {}

    # persist to index.latest.json
    sup_root = _supplier_root(supplier)
    inv_dir = sup_root / "invoices"
    inv_dir.mkdir(parents=True, exist_ok=True)
    idx_path = inv_dir / "index.latest.json"
    try:
        idx = json.loads(idx_path.read_text(encoding="utf-8")) if idx_path.is_file() else {}
    except Exception:
        idx = {}

    inv_stem = Path(relpath).stem  # F2025100902
    invoice_id = f"{supplier}:{inv_stem}"
    entry = idx.get(invoice_id) or {}
    csv_rel = entry.get("csv_path") or relpath
    rel_rel = entry.get("rel_path") or csv_rel

    entry.update({
        "supplier": supplier,
        "invoice_id": invoice_id,
        "number": inv_stem,
        "csv_path": csv_rel,
        "rel_path": rel_rel,
        "status": "processed",
        "processed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stats": stats,
        "outputs": outputs,
    })
    idx[invoice_id] = entry

    tmp = idx_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(idx_path)

    # write history JSON
    history_dir = _history_dir(supplier)
    history_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    hist_name = f"{inv_stem}_{ts}.json"
    hist_payload = {
        'supplier': supplier,
        'invoice_id': invoice_id,
        'number': inv_stem,
        'inputs': {
            'shop_ref': shop,
            'invoice_relpath': relpath,
            'use_invoice_qty': use_qty,
        },
        'stats': stats,
        'outputs': outputs,
        'processed_at': entry['processed_at'],
        'version': 1,
    }
    (history_dir / hist_name).write_text(json.dumps(hist_payload, ensure_ascii=False, indent=2), encoding='utf-8')

    return {"ok": True, "stats": stats, "outputs": outputs}

@router.get("/suppliers/{supplier}/invoices/index")
def invoices_index(supplier: str):
    sup = _supplier_root(supplier)
    idx = sup / "invoices" / "index.latest.json"

    if idx.is_file():
        try:
            raw = json.loads(idx.read_text(encoding="utf-8"))
        except Exception as e:
            raise HTTPException(500, detail=f"Cannot read index: {e}")
        if isinstance(raw, dict):
            invoices = list(raw.values())
        elif isinstance(raw, list):
            invoices = raw
        else:
            invoices = []
        # add rel_path fallback + keep stats/outputs if present
        for it in invoices:
            if isinstance(it, dict) and "rel_path" not in it and "csv_path" in it:
                it["rel_path"] = it["csv_path"]
        # enrich with history_count and last_processed_at
        hdir = _history_dir(supplier)
        if hdir.is_dir():
            for it in invoices:
                if isinstance(it, dict):
                    inv_no = str(it.get("number") or "").strip()
                    if inv_no:
                        files = sorted(hdir.glob(f"{inv_no}_*.json"))
                        it["history_count"] = len(files)
                        if files:
                            latest = files[-1]
                            try:
                                it["last_processed_at"] = json.loads(latest.read_text(encoding="utf-8")).get("processed_at")
                            except Exception:
                                it["last_processed_at"] = None
        return {"supplier": supplier, "count": len(invoices), "invoices": invoices}

    # fallback – build index from files (status=new)
    csv_dir = sup / "invoices" / "csv"
    if not csv_dir.exists():
        raise HTTPException(404, detail="Index not found")
    found = []
    for p in csv_dir.rglob("*.csv"):
        rel = p.relative_to(sup).as_posix()
        inv_id = p.stem
        found.append({
            "supplier": supplier,
            "invoice_id": f"{supplier}:{inv_id}",
            "number": inv_id,
            "csv_path": f"invoices/csv/{p.name}" if p.parent == csv_dir else rel,
            "rel_path": f"invoices/csv/{p.name}" if p.parent == csv_dir else rel,
            "status": "new",
        })
    found.sort(key=lambda x: x.get("number",""))
    _enrich_with_history(found, supplier)
    return {"supplier": supplier, "count": len(found), "invoices": found}

@router.post("/suppliers/{supplier}/invoices/reindex")
def invoices_reindex(
    supplier: str,
    flatten_to_root: bool = Query(False),
):
    sup = _supplier_root(supplier)
    inv_dir = sup / "invoices"
    csv_root = inv_dir / "csv"
    csv_root.mkdir(parents=True, exist_ok=True)

    # keep previous status
    idx_json = inv_dir / "index.latest.json"
    prev_status = {}
    if idx_json.is_file():
        try:
            raw = json.loads(idx_json.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, dict) and "status" in v:
                        prev_status[k] = v["status"]
        except Exception:
            pass

    files = list(csv_root.rglob("*.csv"))

    moved = []
    if flatten_to_root:
        for p in files:
            if p.parent != csv_root:
                dst = csv_root / p.name
                i = 1
                while dst.exists():
                    dst = csv_root / f"{p.stem}_{i}{p.suffix}"
                    i += 1
                p.replace(dst)
                moved.append({"from": p.as_posix(), "to": dst.as_posix()})
        files = list(csv_root.glob("*.csv"))

    entries = {}
    for p in files:
        rel_to_sup = p.relative_to(sup).as_posix()
        inv_no = p.stem
        inv_id = f"{supplier}:{inv_no}"
        # sha1
        h = hashlib.sha1()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        sha1 = h.hexdigest()

        entries[inv_id] = {
            "supplier": supplier,
            "invoice_id": inv_id,
            "number": inv_no,
            "issue_date": None,
            "issue_date_source": "reindex",
            "downloaded_at": None,
            "csv_path": rel_to_sup,
            "rel_path": rel_to_sup,
            "status": prev_status.get(inv_id, "new"),
            "sha1": sha1,
            "layout_used": "flat",
        }

    if not entries:
        raise HTTPException(404, detail="No CSVs found under invoices/csv")

    idx_tmp = idx_json.with_suffix(".tmp")
    idx_tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    idx_tmp.replace(idx_json)

    jsonl = inv_dir / "index.jsonl"
    jsonl_tmp = jsonl.with_suffix(".tmp")
    with jsonl_tmp.open("w", encoding="utf-8", newline="\n") as f:
        for k in sorted(entries.keys()):
            f.write(json.dumps(entries[k], ensure_ascii=False))
            f.write("\n")
    jsonl_tmp.replace(jsonl)

    return {
        "ok": True,
        "supplier": supplier,
        "count": len(entries),
        "moved": moved,
        "index_json": str(idx_json.relative_to(sup).as_posix()),
        "index_jsonl": str(jsonl.relative_to(sup).as_posix()),
    }

@router.get("/suppliers/{supplier}/invoices/history")
def invoices_history(supplier: str, invoice_id: Optional[str] = Query(None)):
    if not invoice_id:
        raise HTTPException(400, detail="invoice_id is required")
    try:
        inv_no = str(invoice_id).split(":", 1)[1]
    except Exception:
        inv_no = str(invoice_id)
    hdir = _history_dir(supplier)
    items = []
    if hdir.is_dir():
        for p in sorted(hdir.glob(f"{inv_no}_*.json")):
            try:
                st = p.stat()
                items.append({
                    "name": p.name,
                    "relpath": str((Path("suppliers")/supplier/"invoices"/"history"/p.name).as_posix()),
                    "size": st.st_size,
                    "processed_at": p.stem.split("_")[-1]
                })
            except Exception:
                pass
    return {"supplier": supplier, "invoice_id": invoice_id, "count": len(items), "items": items}

@router.get("/suppliers/{supplier}/invoices/{invoice}/csv-outputs")
def invoices_csv_outputs(supplier: str, invoice: str):
    try:
        inv_no = invoice.split(":", 1)[1]
    except Exception:
        inv_no = invoice

    upg_dir = _imports_upgates_dir(supplier)
    if not upg_dir.is_dir():
        raise HTTPException(404, detail="imports/upgates not found")

    idx_path = _index_latest_path(supplier)
    prefer_rel_updates = prefer_rel_new = prefer_rel_unmatched = None
    if idx_path.is_file():
        try:
            idx_raw = json.loads(idx_path.read_text(encoding="utf-8"))
            if isinstance(idx_raw, dict):
                entry = idx_raw.get(f"{supplier}:{inv_no}") or {}
            elif isinstance(idx_raw, list):
                entry = next((e for e in idx_raw if str(e.get("number")) == inv_no), {})
            else:
                entry = {}
            outs = entry.get("outputs") or {}
            if isinstance(outs, dict):
                if outs.get("existing"):
                    prefer_rel_updates = _strip_data_prefix(str(outs["existing"]))
                if outs.get("new"):
                    prefer_rel_new = _strip_data_prefix(str(outs["new"]))
                if outs.get("unmatched"):
                    prefer_rel_unmatched = _strip_data_prefix(str(outs["unmatched"]))
        except Exception:
            pass

    root = Path(settings.INVENTORY_DATA_ROOT).expanduser().resolve()

    def to_abs(rel: str | None) -> Path | None:
        if not rel:
            return None
        p = (root / rel).resolve()
        try:
            p.relative_to(root)
        except Exception:
            return None
        return p if p.is_file() else None

    def _find_latest_for(inv: str, dirp: Path, pattern_suffix: str) -> Path | None:
        candidates = sorted(dirp.glob(f"{inv}_{pattern_suffix}"), key=lambda p: p.stat().st_mtime)
        return candidates[-1] if candidates else None

    p_updates = to_abs(prefer_rel_updates) or _find_latest_for(inv_no, upg_dir, "updates_existing_*.csv")
    p_new     = to_abs(prefer_rel_new)     or _find_latest_for(inv_no, upg_dir, "new_products_*.csv")
    p_unmatch = to_abs(prefer_rel_unmatched) or _find_latest_for(inv_no, upg_dir, "unmatched_*.csv")

    def _csv_header_and_count(path: Path) -> Tuple[list[str], int]:
        headers: list[str] = []
        count = 0
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            r = csv.reader(f)
            headers = next(r, []) or []
            for _ in r:
                count += 1
        return headers, count

    def build_entry(p: Path | None):
        if not p:
            return None
        headers, rows = _csv_header_and_count(p)
        rel = p.relative_to(root).as_posix()
        return {"relpath": rel, "headers": headers, "rows": rows}

    return {
        "invoice": inv_no,
        "updates": build_entry(p_updates),
        "new": build_entry(p_new),
        "unmatched": build_entry(p_unmatch),
    }

def _choose_hdr(cands, existing):
    # vyberie prvý názov z cands, ktorý už v src_headers existuje (aj po normalizácii),
    # inak vráti prvý z cands
    existing_norm = {_norm_header(h): h for h in existing}
    for c in cands:
        n = _norm_header(c)
        if n in existing_norm:
            return existing_norm[n]
    return cands[0]


# -----------------------------
# Enriched preview (merge with shop export & invoice)
# -----------------------------
# kandidáti na polia
_PRICE_CANDIDATES = [
    "INVOICE_UNIT_PRICE_WITH_VAT", "CENA_S_DPH_KS", "CENA S DPH/KS", "Cena s DPH/ks",
    "Cena s DPH ks", "Price VAT per unit", "Jednotková cena"
]
_QTY_CANDIDATES = ["QTY", "Mnozstvo", "Množstvo", "Pocet", "Počet", "Počet kusov"]
_TOTAL_VAT_CANDIDATES = ["SUMA_S_DPH", "Celkovo s DPH", "Total VAT", "TOTAL_S_DPH"]

# e-shop (Upgates) cena – širší zoznam názvov
_SHOP_PRICE_CANDIDATES = [
    "PRICE_WITH_VAT „Predvolené“", "[PRICE_WITH_VAT „Predvolené“]"
]

_SHOP_BUY_CANDIDATES = [
    "PRICE_BUY", "[PRICE_BUY]", "BUY_PRICE", "CENA_NAKUP"
]

def _shops_latest_csv(shop: str) -> Path:
    return settings.INVENTORY_DATA_ROOT / "shops" / shop / "latest.csv"

def _read_csv_map(path: Path, key_candidates: list[str]) -> tuple[list[str], dict[str, dict[str, str]]]:
    if not path.is_file():
        return [], {}

    b = path.read_bytes()
    text = _decode_bytes_auto(b)
    delim = _detect_delimiter(text)

    r = csv.reader(io.StringIO(text), delimiter=delim)

    # preskoč prázdne / whitespace riadky a nájdi skutočnú hlavičku
    norm_cands = [_norm_header(c) for c in key_candidates]
    headers: list[str] = []
    norm_headers: list[str] = []

    for row in r:
        cells = [_norm_header(x) for x in row]
        if not any(cells):  # celý riadok prázdny
            continue
        # hlavička = radšej riadok, ktorý obsahuje aspoň jeden kandidátsky stĺpec
        if any(c in cells for c in norm_cands):
            headers = row
            norm_headers = cells
            break
        # fallback: ak nič lepšie, zapamätaj prvý „rozumný“ riadok, ale pokračuj v hľadaní
        if not headers and sum(1 for c in cells if c) >= 2:
            headers = row
            norm_headers = cells

    if not headers:
        return [], {}

    idx_by_norm = {h: i for i, h in enumerate(norm_headers)}
    key_idx = None
    for cand in norm_cands:
        if cand in idx_by_norm:
            key_idx = idx_by_norm[cand]
            break
    if key_idx is None:
        return headers, {}

    data: dict[str, dict[str, str]] = {}
    for row in r:
        if key_idx >= len(row):
            continue
        key = str(row[key_idx]).strip()
        if not key:
            continue
        rec = {}
        for i, nh in enumerate(norm_headers):
            rec[nh] = row[i] if i < len(row) else ""
        data[key] = rec

    return norm_headers, data

@router.get("/suppliers/{supplier}/invoices/{invoice}/enriched-preview")
def enriched_preview(
    supplier: str,
    invoice: str,
    shop: str = Query(...),
    tab: str = Query("updates"),        # "updates" | "new" | "unmatched"
    offset: int = 0,
    limit: int = 200
):
    try:
        inv_no = invoice.split(":", 1)[1]
    except Exception:
        inv_no = invoice

    upg_dir = _imports_upgates_dir(supplier)
    if not upg_dir.is_dir():
        raise HTTPException(404, detail="imports/upgates not found")
    p = None
    if tab == "updates":
        p = sorted(upg_dir.glob(f"{inv_no}_updates_existing_*.csv"))[-1] if list(upg_dir.glob(f"{inv_no}_updates_existing_*.csv")) else None
    elif tab == "new":
        p = sorted(upg_dir.glob(f"{inv_no}_new_products_*.csv"))[-1] if list(upg_dir.glob(f"{inv_no}_new_products_*.csv")) else None
    elif tab == "unmatched":
        p = sorted(upg_dir.glob(f"{inv_no}_unmatched_*.csv"))[-1] if list(upg_dir.glob(f"{inv_no}_unmatched_*.csv")) else None
    if not p:
        return {"columns": [], "rows": []}

    # načítaj zdrojové CSV
    b = p.read_bytes()
    text = _decode_bytes_auto(b)
    delim = _detect_delimiter(text)
    r = csv.reader(io.StringIO(text), delimiter=delim)
    src_headers = next(r, []) or []
    src_rows_raw = list(r)

    src_headers_norm = [_norm_header(h) for h in src_headers]
    idx_norm = {h: i for i, h in enumerate(src_headers_norm)}

    # shop export map & invoice map
    _, shop_map = _read_csv_map(_shops_latest_csv(shop), ["PRODUCT_CODE", "[PRODUCT_CODE]"])
    inv_csv = settings.INVENTORY_DATA_ROOT / "suppliers" / supplier / "invoices" / "csv" / f"{inv_no}.csv"
    _, inv_map = _read_csv_map(inv_csv, ["SCM", "SČM", "[SCM]", "[SČM]"])

    # EAN budeme mať vždy pod názvom [EAN] (aby sme neprepisovali cudzie EAN stĺpce)
    ean_hdr = "[EAN]"
    if ean_hdr in src_headers:
        # keby sa náhodou v source už vyskytol [EAN], nebudeme ho duplikovať – radšej preplníme renderom
        pass

    add_cols = [
        "TITLE",
        "IMAGES",
        "[PRICE_WITH_VAT „Predvolené“]",
        "PRICE_BUY",
        "INVOICE_UNIT_PRICE_EUR",
        "BUY_DELTA_EUR",
        "PRICE_DELTA_EUR",
        "PROFIT_VS_INVOICE_EUR",
        "PROFIT_VS_INVOICE_PCT",
        ean_hdr,
    ]
    add_cols += ["SHOP_STOCK_CURRENT", "INVOICE_QTY", "STOCK_DELTA", "STOCK_AFTER"]
    out_headers = src_headers + [c for c in add_cols if c not in src_headers]

    def _pc_to_scm(pc: str) -> str:
        pc = str(pc or "")
        return pc[3:] if pc.startswith("PL-") else pc

    def _only_digits(s: str) -> str:
        s = str(s or "").strip().replace(" ", "")
        # EAN-13/8 sú len číslice; ak príde iný formát, necháme pôvodný
        if s and all(ch.isdigit() for ch in s):
            return s
        return s

    rows_paged = src_rows_raw[offset: offset + limit]
    out_rows = []

    for row in rows_paged:
        rec = {h: (row[i] if i < len(row) else "") for h, i in idx_norm.items()}
        pc = rec.get("PRODUCT_CODE", "")

        # --- shop data
        shop_rec = shop_map.get(pc, {})
        title = _get1(shop_rec, ["TITLE"])
        images = _get1(shop_rec, ["IMAGES"])
        shop_price_text = _get1(shop_rec, _SHOP_PRICE_CANDIDATES)
        shop_price_eur = _to_number(shop_price_text)
        shop_buy_text = _get1(shop_rec, _SHOP_BUY_CANDIDATES)
        shop_buy_eur = _to_number(shop_buy_text)
        shop_stock_cur = _to_number(_get1(shop_rec, ["STOCK", "[STOCK]"]))

        # --- invoice → canonical
        scm = _pc_to_scm(pc)
        inv_rec = inv_map.get(scm, {})
        canon = _canon_from_invoice(inv_rec, supplier)
        inv_unit_eur = float(canon.get("UNIT_PRICE_INC_EUR") or 0.0)
        inv_qty = float(canon.get("QTY") or 0.0)

        # --- EAN: shop → invoice; normalizácia
        ean = _get1(shop_rec, ["EAN", "[EAN]"])
        if not ean:
            ean = _get1(inv_rec, ["EAN","[EAN]","EAN13","Čiarový kód","Ciarovy kod","Barcode","BARCODE","Kód EAN"])
        ean = _only_digits(ean)

        # --- odvodené polia
        stock_delta = inv_qty
        stock_after = shop_stock_cur + inv_qty
        buy_delta_eur = inv_unit_eur - shop_buy_eur if (inv_unit_eur or shop_buy_eur) else 0.0
        price_delta_eur = inv_unit_eur - shop_price_eur if (inv_unit_eur or shop_price_eur) else 0.0
        profit_vs_invoice_eur = max(shop_price_eur - inv_unit_eur, 0.0) if (shop_price_eur and inv_unit_eur) else 0.0
        profit_vs_invoice_pct = (profit_vs_invoice_eur / shop_price_eur * 100.0) if shop_price_eur else 0.0

        base = list(row)
        for c in add_cols:
            if c in src_headers:
                continue
            if c == "TITLE": base.append(title)
            elif c == "IMAGES": base.append(images)
            elif c == "[PRICE_WITH_VAT „Predvolené“]": base.append(shop_price_text or "")
            elif c == "PRICE_BUY": base.append(shop_buy_text or "")
            elif c == "INVOICE_UNIT_PRICE_EUR": base.append(f"{inv_unit_eur:.2f} €" if inv_unit_eur else "")
            elif c == "BUY_DELTA_EUR":
                sign = "+" if buy_delta_eur > 0 else ""
                base.append(f"{sign}{buy_delta_eur:.2f} €" if (inv_unit_eur or shop_buy_eur) else "")
            elif c == "PRICE_DELTA_EUR":
                sign = "+" if price_delta_eur > 0 else ""
                base.append(f"{sign}{price_delta_eur:.2f} €" if (inv_unit_eur or shop_price_eur) else "")
            elif c == "PROFIT_VS_INVOICE_EUR":
                base.append(f"{profit_vs_invoice_eur:.2f} €" if profit_vs_invoice_eur or inv_unit_eur else "")
            elif c == "PROFIT_VS_INVOICE_PCT":
                base.append(f"{profit_vs_invoice_pct:.1f} %" if shop_price_eur else "")
            elif c == "SHOP_STOCK_CURRENT":
                base.append(str(int(shop_stock_cur)) if float(shop_stock_cur).is_integer() else f"{shop_stock_cur:.2f}")
            elif c == "INVOICE_QTY":
                base.append(str(int(inv_qty)) if float(inv_qty).is_integer() else f"{inv_qty:.2f}")
            elif c == "STOCK_DELTA":
                base.append(str(int(stock_delta)) if float(stock_delta).is_integer() else f"{stock_delta:.2f}")
            elif c == "STOCK_AFTER":
                base.append(str(int(stock_after)) if float(stock_after).is_integer() else f"{stock_after:.2f}")
            elif c == ean_hdr:
                base.append(ean)
        out_rows.append(base)

    return {"columns": out_headers, "rows": out_rows}
