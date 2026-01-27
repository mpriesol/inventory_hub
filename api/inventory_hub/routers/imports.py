from __future__ import annotations

import csv
import io
import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from fastapi import APIRouter, Body, HTTPException, Query

# Reuse helpers & settings from invoices router
from .invoices import (
    settings,
    _imports_upgates_dir,
    _norm_header,
    _decode_bytes_auto,
    _detect_delimiter,
)

router = APIRouter()

# ---------- interné helpery ----------

def _supplier_root(supplier: str) -> Path:
    return settings.INVENTORY_DATA_ROOT / "suppliers" / supplier

def _selected_dir(supplier: str) -> Path:
    p = _supplier_root(supplier) / "imports" / "upgates_selected"
    p.mkdir(parents=True, exist_ok=True)
    return p

def _sent_dir(supplier: str) -> Path:
    p = _supplier_root(supplier) / "imports" / "sent"
    p.mkdir(parents=True, exist_ok=True)
    return p

def _history_dir(supplier: str) -> Path:
    p = _supplier_root(supplier) / "invoices" / "history"
    p.mkdir(parents=True, exist_ok=True)
    return p

def _now_ts() -> str:
    return time.strftime("%Y%m%d_%H%M%S")

def _parse_invoice_no(invoice_id: str) -> str:
    # supports "supplier:INVNO" or "INVNO"
    try:
        return invoice_id.split(":", 1)[1]
    except Exception:
        return invoice_id

def _find_latest_for(inv_no: str, upg_dir: Path, tab: str) -> Path | None:
    # tab in {"updates","new"}
    patt = None
    if tab == "updates":
        patt = f"{inv_no}_updates_existing_*.csv"
    elif tab == "new":
        patt = f"{inv_no}_new_products_*.csv"
    elif tab == "unmatched":
        patt = f"{inv_no}_unmatched_*.csv"
    else:
        return None
    cands = sorted(upg_dir.glob(patt))
    return cands[-1] if cands else None

def _read_csv(path: Path) -> Tuple[List[str], List[List[str]], str]:
    b = path.read_bytes()
    text = _decode_bytes_auto(b)
    delim = _detect_delimiter(text)
    r = csv.reader(io.StringIO(text), delimiter=delim)
    headers = next(r, []) or []
    rows = list(r)
    return headers, rows, delim

def _write_csv(path: Path, headers: List[str], rows: List[List[str]], delimiter: str) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=delimiter)
        w.writerow(headers)
        for row in rows:
            w.writerow(row)

def _ensure_col(headers: List[str], idx_map: Dict[str, int], colname: str) -> int:
    """Ensure column exists (exact name) and return its index."""
    if colname in headers:
        return headers.index(colname)
    headers.append(colname)
    idx_map[_norm_header(colname)] = len(headers) - 1
    return len(headers) - 1

def _find_col_idx(headers: List[str], norm_map: Dict[str, int], key: str) -> int:
    """Find index by exact or normalized header; -1 if not found."""
    if key in headers:
        return headers.index(key)
    nk = _norm_header(key)
    if nk in norm_map:
        return norm_map[nk]
    # build on the fly
    for i, h in enumerate(headers):
        if _norm_header(h) == nk:
            norm_map[nk] = i
            return i
    return -1

def _get_cell(row: List[str], i: int) -> str:
    return row[i] if 0 <= i < len(row) else ""

def _set_cell(row: List[str], i: int, value: Any) -> None:
    while len(row) < i + 1:
        row.append("")
    row[i] = "" if value is None else str(value)

# ---------- APPLY: výber + edity → *_selected.csv + história ----------

@router.post("/suppliers/{supplier}/imports/apply")
def apply_imports(
    supplier: str,
    payload: Dict[str, Any] = Body(...)
):
    """
    Body:
    {
      "invoice_id": "paul-lange:F2025100902",
      "shop": "biketrek",
      "tab": "updates" | "new",
      "selected_product_codes": ["PL-ABC", "PL-XYZ"],
      "edits": { "PL-ABC": {"AVAILABILITY":"Na sklade", "PRICE_BUY":"12.90"} },
      "meta": { "append_invoice_ref": true },
      "send_now": false
    }
    """
    invoice_id = str(payload.get("invoice_id") or "").strip()
    if not invoice_id:
        raise HTTPException(400, detail="Missing invoice_id")
    inv_no = _parse_invoice_no(invoice_id)

    tab = str(payload.get("tab") or "updates").strip().lower()
    if tab not in ("updates", "new"):
        raise HTTPException(400, detail="Only tabs 'updates' or 'new' are supported for apply.")

    shop = str(payload.get("shop") or "").strip()
    selected_codes = list(payload.get("selected_product_codes") or [])
    if not selected_codes:
        raise HTTPException(400, detail="No selected_product_codes.")

    edits: Dict[str, Dict[str, Any]] = payload.get("edits") or {}
    meta_cfg = payload.get("meta") or {}
    append_invoice_ref: bool = bool(meta_cfg.get("append_invoice_ref", True))
    send_now: bool = bool(payload.get("send_now", False))

    upg_dir = _imports_upgates_dir(supplier)
    if not upg_dir.is_dir():
        raise HTTPException(404, detail="imports/upgates not found")

    src_path = _find_latest_for(inv_no, upg_dir, tab)
    if not src_path:
        raise HTTPException(404, detail=f"Source CSV for tab '{tab}' not found.")

    headers, rows, delim = _read_csv(src_path)
    norm_idx = { _norm_header(h): i for i, h in enumerate(headers) }

    # locate PRODUCT_CODE column (or fallback)
    pc_idx = _find_col_idx(headers, norm_idx, "PRODUCT_CODE")
    if pc_idx < 0:
        # fallback for "new" might still have PRODUCT_CODE (per your adapter), else try SCM
        pc_idx = _find_col_idx(headers, norm_idx, "SCM")
        if pc_idx < 0:
            raise HTTPException(400, detail="CSV does not contain PRODUCT_CODE nor SCM column.")

    selected_set = set(str(c).strip() for c in selected_codes if str(c).strip())

    # filter rows by selected PRODUCT_CODE/SCM
    filtered: List[List[str]] = []
    row_map_by_code: Dict[str, List[str]] = {}
    for row in rows:
        code = _get_cell(row, pc_idx).strip()
        if not code:
            continue
        if code in selected_set:
            filtered.append(row.copy())
            row_map_by_code[code] = filtered[-1]

    if not filtered:
        raise HTTPException(400, detail="No matching rows found for selected_product_codes.")

    # apply edits
    added_columns: List[str] = []
    for code, changes in edits.items():
        row = row_map_by_code.get(code)
        if row is None:
            continue
        for colname_raw, value in (changes or {}).items():
            # nájdi existujúci stĺpec podľa exact alebo normalized; ak nie je, pridáme ho (s presným menom z payloadu)
            idx = _find_col_idx(headers, norm_idx, colname_raw)
            if idx < 0:
                # vyskúšaj nájsť stĺpec pod jeho "norm" variantom v existujúcich headers
                nk = _norm_header(colname_raw)
                idx = _find_col_idx(headers, norm_idx, nk)
            if idx < 0:
                # nový stĺpec
                idx = _ensure_col(headers, norm_idx, colname_raw)
                added_columns.append(colname_raw)
                # doplň prázdne bunky pre už vložené riadky (okrem aktuálneho, ten vyplníme nižšie)
                for r in filtered:
                    if r is row:
                        continue
                    _set_cell(r, idx, _get_cell(r, idx))  # no-op, len rozšíri riadok ak treba
            # nastav hodnotu v riadku
            _set_cell(row, idx, value)

    # meta stĺpce
    if append_invoice_ref:
        col_refs = "[META 'invoice_refs']"
        col_last = "[META 'last_invoice_processed']"
        ci_refs = _find_col_idx(headers, norm_idx, col_refs)
        if ci_refs < 0:
            ci_refs = _ensure_col(headers, norm_idx, col_refs)
        ci_last = _find_col_idx(headers, norm_idx, col_last)
        if ci_last < 0:
            ci_last = _ensure_col(headers, norm_idx, col_last)

        for row in filtered:
            # append invoice number to refs (semicolon-separated, unique)
            cur = _get_cell(row, ci_refs)
            parts = [p.strip() for p in cur.split(";") if p.strip()] if cur else []
            if inv_no not in parts:
                parts.append(inv_no)
            _set_cell(row, ci_refs, ";".join(parts))
            _set_cell(row, ci_last, inv_no)

    # write selected CSV
    ts = _now_ts()
    if tab == "updates":
        out_name = f"{inv_no}_updates_existing_selected_{ts}.csv"
    else:
        out_name = f"{inv_no}_new_products_selected_{ts}.csv"
    out_path = _selected_dir(supplier) / out_name
    _write_csv(out_path, headers, filtered, delimiter=delim)

    # history (apply)
    hist = {
        "type": "apply",
        "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "supplier": supplier,
        "shop": shop,
        "invoice_id": invoice_id,
        "invoice_no": inv_no,
        "tab": tab,
        "source_file": str(src_path),
        "output_file": str(out_path),
        "selected_count": len(filtered),
        "selected_product_codes": sorted(list(selected_set)),
        "edits": edits,
        "added_columns": added_columns,
    }
    hist_path = _history_dir(supplier) / f"{inv_no}_apply_{ts}.json"
    hist_path.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {
        "selected_files": {
            tab: str(out_path)
        },
        "history_entry": str(hist_path),
        "sent": False
    }

    # optional send
    if send_now:
        sent = _send_selected_file(supplier, out_path, inv_no, tab)
        result["sent"] = True
        result["send_history_entry"] = sent["history_entry"]
        result["sent_file"] = sent["sent_file"]

    return result

def _send_selected_file(supplier: str, selected_file: Path, inv_no: str, tab: str) -> Dict[str, Any]:
    ts = _now_ts()
    dest_name = f"{Path(selected_file).stem}_sent_{ts}.csv"
    dest = _sent_dir(supplier) / dest_name
    shutil.copyfile(selected_file, dest)
    hist = {
        "type": "send",
        "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "supplier": supplier,
        "invoice_no": inv_no,
        "tab": tab,
        "selected_file": str(selected_file),
        "sent_file": str(dest),
        "mode": "upgates-csv"
    }
    hist_path = _history_dir(supplier) / f"{inv_no}_send_{ts}.json"
    hist_path.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"history_entry": str(hist_path), "sent_file": str(dest)}

# ---------- SEND: po apply vezmeme *_selected.csv a "odošleme" ----------

@router.post("/suppliers/{supplier}/imports/send")
def send_imports(
    supplier: str,
    payload: Dict[str, Any] = Body(...)
):
    """
    Body:
    {
      "invoice_id": "paul-lange:F2025100902",
      "tab": "updates" | "new",
      "selected_files": [".../imports/upgates_selected/<file>.csv"],
      "mode": "upgates-csv"
    }
    """
    invoice_id = str(payload.get("invoice_id") or "").strip()
    if not invoice_id:
        raise HTTPException(400, detail="Missing invoice_id")
    inv_no = _parse_invoice_no(invoice_id)

    tab = str(payload.get("tab") or "updates").strip().lower()
    if tab not in ("updates", "new"):
        raise HTTPException(400, detail="Only tabs 'updates' or 'new' are supported for send.")

    selected_files = list(payload.get("selected_files") or [])
    if not selected_files:
        raise HTTPException(400, detail="No selected_files.")

    # For now, just copy files into 'sent' and log
    out = []
    for f in selected_files:
        p = Path(f)
        if not p.is_file():
            raise HTTPException(404, detail=f"Selected file not found: {f}")
        out.append(_send_selected_file(supplier, p, inv_no, tab))

    return {"status": "ok", "sent": out}

# ---------- HISTORY: timeline pre faktúru ----------

@router.get("/suppliers/{supplier}/invoices/{invoice}/history")
def invoice_history(
    supplier: str,
    invoice: str,
    limit: int = Query(100)
):
    inv_no = _parse_invoice_no(invoice)
    hdir = _history_dir(supplier)
    items: List[Dict[str, Any]] = []
    for p in sorted(hdir.glob(f"{inv_no}_*.json")):
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            obj = {"_error": "json_read_failed"}
        obj["_path"] = str(p)
        items.append(obj)
    # sort by timestamp desc if present
    def _ts(x: Dict[str, Any]) -> str:
        return str(x.get("timestamp") or "")
    items.sort(key=_ts, reverse=True)
    return {"invoice": invoice, "items": items[:limit]}
