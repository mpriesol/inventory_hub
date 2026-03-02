# -*- coding: utf-8 -*-
from __future__ import annotations

import io, csv, json, hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, List, Tuple

from fastapi import APIRouter, HTTPException, Body, Query
from pydantic import BaseModel

from inventory_hub.settings import settings
from inventory_hub.config_io import load_supplier as load_supplier_config
from inventory_hub.adapters.paul_lange_web import (
    LoginConfig,
    refresh_invoices_web as paul_lange_refresh_invoices_web,
    prepare_from_invoice,
)
from inventory_hub.adapters import northfinder_web

router = APIRouter(tags=["invoices"])

# ── Models ────────────────────────────────────────────────────────────────────

class NotePayload(BaseModel):
    note: str
    status: Optional[str] = None

class ReceivedItemsPayload(BaseModel):
    # scm (raw, bez PL-) -> {"qty": int, "done": bool}
    items: Dict[str, Any]

# ── Path helpers ──────────────────────────────────────────────────────────────

def _supplier_root(supplier: str) -> Path:
    return settings.INVENTORY_DATA_ROOT / "suppliers" / supplier

def _invoices_dir(supplier: str) -> Path:
    return _supplier_root(supplier) / "invoices"

def _index_latest_path(supplier: str) -> Path:
    return _invoices_dir(supplier) / "index.latest.json"

def _imports_upgates_dir(supplier: str) -> Path:
    return settings.INVENTORY_DATA_ROOT / "suppliers" / supplier / "imports" / "upgates"

def _strip_data_prefix(p: str) -> str:
    return p[5:] if p.startswith("data/") else p

def _cfg_get(cfg: Any, *keys: str, default=None):
    cur = cfg
    for k in keys:
        if cur is None:
            return default
        cur = cur.get(k) if isinstance(cur, dict) else getattr(cur, k, None)
    return default if cur is None else cur

# ── Basic utils ───────────────────────────────────────────────────────────────

def _to_number(s: Any) -> float:
    if s is None:
        return 0.0
    ss = str(s).strip()
    if not ss:
        return 0.0
    ss = ss.replace(" ", "").replace("\u00A0", "").replace(",", ".")
    ss = ss.replace("€","").replace("EUR","").replace("eur","").replace("Kč","").replace("CZK","").replace("czk","")
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

def _get1(rec: dict, variants: list, default: str = "") -> str:
    for v in variants:
        vv = rec.get(_norm_header(v))
        if vv is not None and str(vv).strip() != "":
            return str(vv)
    return default

def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

# ── Supplier config helpers ───────────────────────────────────────────────────

def _supplier_cfg(supplier: str) -> dict:
    return _load_json(settings.INVENTORY_DATA_ROOT / "suppliers" / supplier / "config.json")

def _supplier_vat_rate(supplier: str) -> float:
    cfg = _supplier_cfg(supplier)
    try:
        return float(cfg.get("adapter_settings", {}).get("vat_rate") or cfg.get("invoices", {}).get("vat_rate") or 23.0)
    except Exception:
        return 23.0

def _supplier_invoice_currency(supplier: str) -> str:
    cfg = _supplier_cfg(supplier)
    cur = cfg.get("adapter_settings", {}).get("currency") or cfg.get("invoices", {}).get("currency") or "EUR"
    return str(cur).upper()

def _supplier_mapping(supplier: str) -> dict:
    cfg = _supplier_cfg(supplier)
    return cfg.get("adapter_settings", {}).get("mapping") or cfg.get("invoices", {}).get("mapping") or {}

def _console_czk_eur_rate() -> float:
    root = settings.INVENTORY_DATA_ROOT
    for p in [root / "configs" / "console.json", root / "console" / "config.json", root / "console.json"]:
        cfg = _load_json(p)
        try:
            rate = cfg.get("currency_rates", {}).get("CZK", {}).get("EUR")
            if rate:
                return float(rate)
        except Exception:
            pass
    return 0.0

def _console_rate(from_ccy: str, to_ccy: str) -> float:
    f, t = (from_ccy or "").upper(), (to_ccy or "").upper()
    if f == t:
        return 1.0
    if f == "CZK" and t == "EUR":
        return _console_czk_eur_rate()
    return 1.0

def _canon_from_invoice(inv_rec: dict, supplier: str) -> dict:
    mp = _supplier_mapping(supplier)
    inv2can = mp.get("invoice_to_canon", {}) or {}
    postp   = mp.get("postprocess", {}) or {}
    vat_rate = _supplier_vat_rate(supplier)
    supplier_ccy = _supplier_invoice_currency(supplier)
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

    src_kind = (postp.get("unit_price_source") or "ex").lower()
    unit_ex = unit_inc = None
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
        "SCM": scm, "EAN": ean, "TITLE": title, "QTY": qty,
        "UNIT_PRICE_EX": unit_ex or 0.0, "UNIT_PRICE_INC": unit_inc or 0.0,
        "UNIT_PRICE_INC_EUR": unit_inc_eur, "CURRENCY": supplier_ccy, "PRODUCT_CODE": product_code,
    }

# ── Received items helpers ────────────────────────────────────────────────────

def _received_items_path(supplier: str, invoice_no: str) -> Path:
    return _supplier_root(supplier) / "invoices" / "received" / f"{invoice_no}.json"

def _load_received_items(supplier: str, invoice_no: str) -> Optional[Dict[str, Any]]:
    path = _received_items_path(supplier, invoice_no)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("items")
    except Exception:
        return None

# ── Index helpers ─────────────────────────────────────────────────────────────

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

def _save_index(idx_path: Path, idx: dict) -> None:
    tmp = idx_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(idx_path)

# ── Routes: refresh ───────────────────────────────────────────────────────────

INVOICE_STRATEGIES = {
    "paul-lange-web": "paul_lange",
    "northfinder-web": "northfinder",
    "manual": "manual",
}

def _dispatch_paul_lange(cfg: Dict, data_root: Path, supplier: str, months_back: int):
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
    return northfinder_web.refresh_invoices_web(
        data_root=data_root, supplier_code=supplier, supplier_config=cfg, months_back=months_back,
    )

@router.post("/suppliers/{supplier}/invoices/refresh")
def invoices_refresh(supplier: str, months_back: Optional[int] = Query(None)) -> Dict[str, Any]:
    cfg = load_supplier_config(supplier)
    strat = _cfg_get(cfg, "invoices", "download", "strategy")
    if not strat:
        raise HTTPException(status_code=400, detail="Supplier config missing invoices.download.strategy")

    mb = int(months_back or _cfg_get(cfg, "invoices", "months_back_default", default=6) or 3)
    data_root = Path(settings.INVENTORY_DATA_ROOT).expanduser()

    if strat == "paul-lange-web":
        res = _dispatch_paul_lange(cfg, data_root, supplier, mb)
    elif strat == "northfinder-web":
        res = _dispatch_northfinder(cfg, data_root, supplier, mb)
    elif strat == "manual":
        return {"ok": True, "downloaded": 0, "skipped": 0, "failed": 0, "pages": 0, "log_files": [], "message": "Manual strategy - no auto-download"}
    else:
        raise HTTPException(400, detail=f"Unsupported strategy: {strat}")

    # carry-over processed status from previous index
    prev_map = _load_prev_index_map(supplier)
    idx_path = _index_latest_path(supplier)
    if idx_path.is_file():
        try:
            idx_data = json.loads(idx_path.read_text(encoding="utf-8"))
        except Exception:
            idx_data = {}

        items = list(idx_data.values()) if isinstance(idx_data, dict) else (idx_data if isinstance(idx_data, list) else [])

        for it in items:
            if not isinstance(it, dict):
                continue
            inv_id = it.get("invoice_id") or f"{supplier}:{it.get('number')}"
            prev_it = prev_map.get(inv_id) or (prev_map.get(f"{supplier}:{it.get('number')}") if it.get("number") else None)
            if prev_it and prev_it.get("status") == "processed":
                it["status"] = "processed"
                for k in ("processed_at", "stats", "outputs", "note"):
                    if prev_it.get(k):
                        it[k] = prev_it[k]

        if isinstance(idx_data, dict):
            new_payload = {(it.get("invoice_id") or f"{supplier}:{it.get('number')}"): it for it in items if isinstance(it, dict)}
            _save_index(idx_path, new_payload)
        else:
            _save_index(idx_path, items)

    errors   = getattr(res, "errors", []) or []
    log_files = getattr(res, "log_files", []) or []
    return {
        "ok": res.failed == 0 and len(errors) == 0,
        "downloaded": res.downloaded,
        "skipped": res.skipped,
        "failed": res.failed,
        "pages": getattr(res, "pages", 0),
        "errors": errors,
        "log_files": log_files,
    }

# ── Routes: index / reindex ───────────────────────────────────────────────────

@router.get("/suppliers/{supplier}/invoices/index")
def invoices_index(supplier: str):
    sup = _supplier_root(supplier)
    idx = sup / "invoices" / "index.latest.json"

    if idx.is_file():
        try:
            raw = json.loads(idx.read_text(encoding="utf-8"))
        except Exception as e:
            raise HTTPException(500, detail=f"Cannot read index: {e}")
        invoices = list(raw.values()) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
        for it in invoices:
            if isinstance(it, dict) and "rel_path" not in it and "csv_path" in it:
                it["rel_path"] = it["csv_path"]
        return {"supplier": supplier, "count": len(invoices), "invoices": invoices}

    # fallback – build from CSV files, status=new
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
    found.sort(key=lambda x: x.get("number", ""))
    return {"supplier": supplier, "count": len(found), "invoices": found}

@router.post("/suppliers/{supplier}/invoices/reindex")
def invoices_reindex(supplier: str, flatten_to_root: bool = Query(False)):
    sup = _supplier_root(supplier)
    inv_dir = sup / "invoices"
    csv_root = inv_dir / "csv"
    csv_root.mkdir(parents=True, exist_ok=True)

    idx_json = inv_dir / "index.latest.json"
    prev_status = {}
    if idx_json.is_file():
        try:
            raw = json.loads(idx_json.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                prev_status = {k: v["status"] for k, v in raw.items() if isinstance(v, dict) and "status" in v}
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
        h = hashlib.sha1()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        entries[inv_id] = {
            "supplier": supplier, "invoice_id": inv_id, "number": inv_no,
            "issue_date": None, "downloaded_at": None,
            "csv_path": rel_to_sup, "rel_path": rel_to_sup,
            "status": prev_status.get(inv_id, "new"),
            "sha1": h.hexdigest(), "layout_used": "flat",
        }

    if not entries:
        raise HTTPException(404, detail="No CSVs found under invoices/csv")

    _save_index(idx_json, entries)
    return {"ok": True, "supplier": supplier, "count": len(entries), "moved": moved}

# ── Routes: note ──────────────────────────────────────────────────────────────

@router.get("/suppliers/{supplier}/invoices/{invoice_no}/note")
def invoice_get_note(supplier: str, invoice_no: str):
    idx = _load_json(_index_latest_path(supplier))
    entry = (idx or {}).get(f"{supplier}:{invoice_no}") or {}
    return {"invoice_id": f"{supplier}:{invoice_no}", "note": entry.get("note", ""), "status": entry.get("status", "new")}

@router.post("/suppliers/{supplier}/invoices/{invoice_no}/note")
def invoice_set_note(supplier: str, invoice_no: str, payload: NotePayload):
    idx_path = _index_latest_path(supplier)
    idx = _load_json(idx_path)
    if not isinstance(idx, dict):
        idx = {}
    invoice_id = f"{supplier}:{invoice_no}"
    entry = idx.get(invoice_id) or {"invoice_id": invoice_id, "number": invoice_no, "supplier": supplier, "status": "new"}
    entry["note"] = payload.note
    if payload.status:
        entry["status"] = payload.status
    idx[invoice_id] = entry
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    _save_index(idx_path, idx)
    return {"ok": True, "invoice_id": invoice_id, "note": payload.note, "status": entry["status"]}

# ── Routes: received-items ────────────────────────────────────────────────────

@router.post("/suppliers/{supplier}/invoices/{invoice_no}/received-items")
def invoice_save_received_items(supplier: str, invoice_no: str, payload: ReceivedItemsPayload):
    """Uloží prijaté množstvá zo session pred generovaním CSV."""
    rec_path = _received_items_path(supplier, invoice_no)
    rec_path.parent.mkdir(parents=True, exist_ok=True)
    rec_path.write_text(json.dumps({
        "invoice_no": invoice_no,
        "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "items": payload.items,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "invoice_no": invoice_no, "item_count": len(payload.items)}

# ── Routes: prepare_legacy (hlavný endpoint pre ReceivingResultsModal) ────────

@router.post("/runs/prepare_legacy")
def run_prepare_legacy(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Pripraví CSV pre Upgates import.
    Automaticky načíta received_items ak boli uložené cez /received-items.
    """
    supplier = payload.get("supplier_ref")
    shop     = payload.get("shop_ref")
    relpath  = payload.get("invoice_relpath")
    use_qty  = bool(payload.get("use_invoice_qty", True))

    if not (supplier and shop and relpath):
        raise HTTPException(400, detail="supplier_ref, shop_ref and invoice_relpath are required.")

    inv_stem = Path(relpath).stem
    received_overrides = _load_received_items(supplier, inv_stem)

    data_root = Path(settings.INVENTORY_DATA_ROOT).expanduser()
    out = prepare_from_invoice(
        data_root, supplier, shop, relpath,
        use_invoice_qty=use_qty,
        received_overrides=received_overrides,
    )

    stats = {
        "existing_rows":  int(out.get("existing", 0)),
        "new_rows":       int(out.get("new", 0)),
        "unmatched_rows": int(out.get("unmatched", 0)),
        "pending_rows":   int(out.get("pending", 0)),
        "invoice_items":  int(out.get("invoice_items", 0)),
    }
    outputs = out.get("outputs", {}) or {}

    # Persist to index
    idx_path = _index_latest_path(supplier)
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    idx = _load_json(idx_path)
    if not isinstance(idx, dict):
        idx = {}

    invoice_id = f"{supplier}:{inv_stem}"
    entry = idx.get(invoice_id) or {}
    entry.update({
        "supplier": supplier, "invoice_id": invoice_id, "number": inv_stem,
        "csv_path": entry.get("csv_path") or relpath,
        "status": "processed",
        "processed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stats": stats, "outputs": outputs,
    })
    idx[invoice_id] = entry
    _save_index(idx_path, idx)

    return {"ok": True, "run_id": inv_stem, "stats": stats, "outputs": outputs}

# ── Routes: csv-outputs (pre starý modal, zachovaj kompatibilitu) ─────────────

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
    prefer_rel_updates = prefer_rel_new = prefer_rel_unmatched = prefer_rel_pending = None
    if idx_path.is_file():
        try:
            idx_raw = _load_json(idx_path)
            entry = idx_raw.get(f"{supplier}:{inv_no}") or {} if isinstance(idx_raw, dict) else {}
            outs = entry.get("outputs") or {}
            if isinstance(outs, dict):
                if outs.get("existing"):   prefer_rel_updates  = _strip_data_prefix(str(outs["existing"]))
                if outs.get("new"):        prefer_rel_new       = _strip_data_prefix(str(outs["new"]))
                if outs.get("unmatched"):  prefer_rel_unmatched = _strip_data_prefix(str(outs["unmatched"]))
                if outs.get("pending"):    prefer_rel_pending   = _strip_data_prefix(str(outs["pending"]))
        except Exception:
            pass

    root = Path(settings.INVENTORY_DATA_ROOT).expanduser().resolve()

    def to_abs(rel):
        if not rel: return None
        p = (root / rel).resolve()
        try: p.relative_to(root)
        except Exception: return None
        return p if p.is_file() else None

    def find_latest(inv, dirp, suffix):
        cands = sorted(dirp.glob(f"{inv}_{suffix}"), key=lambda p: p.stat().st_mtime)
        return cands[-1] if cands else None

    p_updates = to_abs(prefer_rel_updates) or find_latest(inv_no, upg_dir, "updates_existing_*.csv")
    p_new     = to_abs(prefer_rel_new)     or find_latest(inv_no, upg_dir, "new_products_*.csv")
    p_unmatch = to_abs(prefer_rel_unmatched) or find_latest(inv_no, upg_dir, "unmatched_*.csv")
    p_pending = to_abs(prefer_rel_pending)   or find_latest(inv_no, upg_dir, "pending_*.csv")

    def build_entry(p):
        if not p: return None
        headers, count = [], 0
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            r = csv.reader(f)
            headers = next(r, []) or []
            for _ in r: count += 1
        return {"relpath": p.relative_to(root).as_posix(), "headers": headers, "rows": count}

    return {"invoice": inv_no, "updates": build_entry(p_updates), "new": build_entry(p_new), "unmatched": build_entry(p_unmatch), "pending": build_entry(p_pending)}

# ── Enriched preview ──────────────────────────────────────────────────────────

_SHOP_PRICE_CANDIDATES = ["PRICE_WITH_VAT „Predvolené“", "[PRICE_WITH_VAT „Predvolené“]"]
_SHOP_BUY_CANDIDATES   = ["PRICE_BUY", "[PRICE_BUY]", "BUY_PRICE", "CENA_NAKUP"]

def _shops_latest_csv(shop: str) -> Path:
    return settings.INVENTORY_DATA_ROOT / "shops" / shop / "latest.csv"

def _read_csv_map(path: Path, key_candidates: list) -> tuple:
    if not path.is_file():
        return [], {}
    b = path.read_bytes()
    text = _decode_bytes_auto(b)
    delim = _detect_delimiter(text)
    r = csv.reader(io.StringIO(text), delimiter=delim)
    norm_cands = [_norm_header(c) for c in key_candidates]
    headers = norm_headers = []
    for row in r:
        cells = [_norm_header(x) for x in row]
        if not any(cells): continue
        if any(c in cells for c in norm_cands):
            headers, norm_headers = row, cells
            break
        if not headers and sum(1 for c in cells if c) >= 2:
            headers, norm_headers = row, cells
    if not headers:
        return [], {}
    idx_by_norm = {h: i for i, h in enumerate(norm_headers)}
    key_idx = next((idx_by_norm[c] for c in norm_cands if c in idx_by_norm), None)
    if key_idx is None:
        return headers, {}
    data = {}
    for row in r:
        if key_idx >= len(row): continue
        key = str(row[key_idx]).strip()
        if not key: continue
        data[key] = {norm_headers[i]: (row[i] if i < len(row) else "") for i in range(len(norm_headers))}
    return norm_headers, data

@router.get("/suppliers/{supplier}/invoices/{invoice}/enriched-preview")
def enriched_preview(
    supplier: str, invoice: str,
    shop: str = Query(...), tab: str = Query("updates"),
    offset: int = 0, limit: int = 200,
):
    try:
        inv_no = invoice.split(":", 1)[1]
    except Exception:
        inv_no = invoice

    upg_dir = _imports_upgates_dir(supplier)
    if not upg_dir.is_dir():
        raise HTTPException(404, detail="imports/upgates not found")

    suffix_map = {
        "updates":   "updates_existing_*.csv",
        "new":       "new_products_*.csv",
        "unmatched": "unmatched_*.csv",
        "pending":   "pending_*.csv",
    }
    suffix = suffix_map.get(tab)
    if not suffix:
        return {"columns": [], "rows": []}

    candidates = sorted(upg_dir.glob(f"{inv_no}_{suffix}"))
    p = candidates[-1] if candidates else None
    if not p:
        return {"columns": [], "rows": []}

    b = p.read_bytes()
    text = _decode_bytes_auto(b)
    delim = _detect_delimiter(text)
    r = csv.reader(io.StringIO(text), delimiter=delim)
    src_headers = next(r, []) or []
    src_rows_raw = list(r)

    src_headers_norm = [_norm_header(h) for h in src_headers]
    idx_norm = {h: i for i, h in enumerate(src_headers_norm)}

    # For pending/unmatched tabs, no enrichment needed – return as-is
    if tab in ("pending", "unmatched"):
        rows_paged = src_rows_raw[offset: offset + limit]
        return {"columns": src_headers, "rows": rows_paged}

    # Enrich updates/new with shop data
    _, shop_map = _read_csv_map(_shops_latest_csv(shop), ["PRODUCT_CODE", "[PRODUCT_CODE]"])
    inv_csv = settings.INVENTORY_DATA_ROOT / "suppliers" / supplier / "invoices" / "csv" / f"{inv_no}.csv"
    _, inv_map = _read_csv_map(inv_csv, ["SCM", "SČM", "[SCM]", "[SČM]"])

    ean_hdr = "[EAN]"
    add_cols = [
        "TITLE", "IMAGES", "[PRICE_WITH_VAT „Predvolené“]", "PRICE_BUY",
        "INVOICE_UNIT_PRICE_EUR", "BUY_DELTA_EUR", "PRICE_DELTA_EUR",
        "PROFIT_VS_INVOICE_EUR", "PROFIT_VS_INVOICE_PCT",
        ean_hdr, "SHOP_STOCK_CURRENT", "INVOICE_QTY", "STOCK_DELTA", "STOCK_AFTER",
    ]
    out_headers = src_headers + [c for c in add_cols if c not in src_headers]

    def _pc_to_scm(pc: str) -> str:
        pc = str(pc or "")
        return pc[3:] if pc.startswith("PL-") else pc

    def _only_digits(s: str) -> str:
        s = str(s or "").strip().replace(" ", "")
        return s if s and all(ch.isdigit() for ch in s) else s

    out_rows = []
    for row in src_rows_raw[offset: offset + limit]:
        rec = {h: (row[i] if i < len(row) else "") for h, i in idx_norm.items()}
        pc = rec.get("PRODUCT_CODE", "")
        shop_rec = shop_map.get(pc, {})
        title          = _get1(shop_rec, ["TITLE"])
        images         = _get1(shop_rec, ["IMAGES"])
        shop_price_text= _get1(shop_rec, _SHOP_PRICE_CANDIDATES)
        shop_price_eur = _to_number(shop_price_text)
        shop_buy_text  = _get1(shop_rec, _SHOP_BUY_CANDIDATES)
        shop_buy_eur   = _to_number(shop_buy_text)
        shop_stock_cur = _to_number(_get1(shop_rec, ["STOCK", "[STOCK]"]))
        scm     = _pc_to_scm(pc)
        inv_rec = inv_map.get(scm, {})
        canon   = _canon_from_invoice(inv_rec, supplier)
        inv_unit_eur = float(canon.get("UNIT_PRICE_INC_EUR") or 0.0)
        inv_qty      = float(canon.get("QTY") or 0.0)
        ean = _only_digits(_get1(shop_rec, ["EAN", "[EAN]"]) or _get1(inv_rec, ["EAN","[EAN]","EAN13","Čiarový kód","Barcode","BARCODE"]))
        stock_delta          = inv_qty
        stock_after          = shop_stock_cur + inv_qty
        buy_delta_eur        = inv_unit_eur - shop_buy_eur   if (inv_unit_eur or shop_buy_eur) else 0.0
        price_delta_eur      = inv_unit_eur - shop_price_eur if (inv_unit_eur or shop_price_eur) else 0.0
        profit_vs_invoice_eur= max(shop_price_eur - inv_unit_eur, 0.0) if (shop_price_eur and inv_unit_eur) else 0.0
        profit_vs_invoice_pct= (profit_vs_invoice_eur / shop_price_eur * 100.0) if shop_price_eur else 0.0

        def fmt_int(v): return str(int(v)) if float(v).is_integer() else f"{v:.2f}"
        def fmt_sign(v, cond): return (("+" if v > 0 else "") + f"{v:.2f} €") if cond else ""

        base = list(row)
        for c in add_cols:
            if c in src_headers: continue
            if   c == "TITLE":                           base.append(title)
            elif c == "IMAGES":                          base.append(images)
            elif c == "[PRICE_WITH_VAT „Predvolené“]":   base.append(shop_price_text or "")
            elif c == "PRICE_BUY":                       base.append(shop_buy_text or "")
            elif c == "INVOICE_UNIT_PRICE_EUR":          base.append(f"{inv_unit_eur:.2f} €" if inv_unit_eur else "")
            elif c == "BUY_DELTA_EUR":                   base.append(fmt_sign(buy_delta_eur, inv_unit_eur or shop_buy_eur))
            elif c == "PRICE_DELTA_EUR":                 base.append(fmt_sign(price_delta_eur, inv_unit_eur or shop_price_eur))
            elif c == "PROFIT_VS_INVOICE_EUR":           base.append(f"{profit_vs_invoice_eur:.2f} €" if profit_vs_invoice_eur or inv_unit_eur else "")
            elif c == "PROFIT_VS_INVOICE_PCT":           base.append(f"{profit_vs_invoice_pct:.1f} %" if shop_price_eur else "")
            elif c == "SHOP_STOCK_CURRENT":              base.append(fmt_int(shop_stock_cur))
            elif c == "INVOICE_QTY":                     base.append(fmt_int(inv_qty))
            elif c == "STOCK_DELTA":                     base.append(fmt_int(stock_delta))
            elif c == "STOCK_AFTER":                     base.append(fmt_int(stock_after))
            elif c == ean_hdr:                           base.append(ean)
        out_rows.append(base)

    return {"columns": out_headers, "rows": out_rows}
