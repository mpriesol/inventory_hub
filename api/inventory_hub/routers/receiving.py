# -*- coding: utf-8 -*-
from __future__ import annotations
from fastapi import APIRouter, HTTPException, Body
from typing import Any, Dict, Optional, List, Tuple
from pathlib import Path
from datetime import datetime
import csv, json, io, re

from inventory_hub.settings import settings
from inventory_hub.config_io import load_supplier as load_supplier_config

router = APIRouter()

# ---------- Paths ----------
def _supplier_root(supplier: str) -> Path:
    return Path(settings.INVENTORY_DATA_ROOT).expanduser() / "suppliers" / supplier

def _invoice_csv_path(supplier: str, invoice_no: str) -> Path:
    return _supplier_root(supplier) / "invoices" / "csv" / f"{invoice_no}.csv"

def _receiving_dir(supplier: str, invoice_no: str) -> Path:
    return _supplier_root(supplier) / "receiving" / invoice_no

def _session_path(supplier: str, invoice_no: str, session_id: str) -> Path:
    return _receiving_dir(supplier, invoice_no) / f"session_{session_id}.json"

def _summary_path(supplier: str, invoice_no: str) -> Path:
    return _receiving_dir(supplier, invoice_no) / "summary_latest.json"


# ---------- Helpers ----------
_HDR_EAN = ["EAN", "[EAN]", "EAN13", "EAN_13", "Čiarový kód", "Ciarovy kod", "Barcode", "BARCODE", "Kód EAN"]
_HDR_SCM = ["SCM", "SČM", "[SCM]", "[SČM]", "SKU", "Supplier SKU", "Kat. číslo", "Katalógové číslo"]
_HDR_TITLE = ["TITLE", "Názov", "Nazov", "Product name", "Name"]
_HDR_QTY = ["QTY", "Mnozstvo", "Množstvo", "Počet", "Pocet", "Počet kusov", "Kusy"]

def _norm_text(s: str) -> str:
    return (s or "").strip()

def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def _invoice_no_from_id(invoice_id: str) -> str:
    try:
        return invoice_id.split(":", 1)[1]
    except Exception:
        return invoice_id

def _cfg_try_get(obj: Any, path: Tuple[str, ...]) -> Optional[Any]:
    cur = obj
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            cur = getattr(cur, key, None)
        if cur is None:
            return None
    return cur

def _product_code_prefix_for_supplier(supplier: str) -> str:
    cfg = load_supplier_config(supplier)
    candidates: List[Tuple[str, ...]] = [
        ("adapter_settings", "mapping", "postprocess", "product_code_prefix"),
        ("adapter_settings", "product_code_prefix"),
        ("product_code_prefix",),
    ]
    for path in candidates:
        v = _cfg_try_get(cfg, path)
        if v is not None and str(v).strip():
            return str(v).strip()
    if supplier in ("paul-lange", "paul_lange"):
        return "PL-"
    return ""

def _product_code_for_supplier(supplier: str, scm: str) -> str:
    scm = _norm_text(scm)
    prefix = _product_code_prefix_for_supplier(supplier)
    return f"{prefix}{scm}" if (prefix and scm) else scm

def _status_for(received: float, ordered: float) -> str:
    if received <= 0: return "pending"
    if received < ordered: return "partial"
    if received == ordered: return "matched"
    return "overage"


# ---- Robust CSV reading ----
def _decode_bytes_auto(b: bytes) -> str:
    try:
        return b.decode("utf-8-sig")
    except Exception:
        try:
            return b.decode("cp1250")
        except Exception:
            return b.decode("utf-8", errors="ignore")

def _detect_delimiter_from_header_line(line: str) -> str:
    counts = { ';': line.count(';'), '\t': line.count('\t'), ',': line.count(',') }
    delim = max(counts, key=lambda k: counts[k])
    return delim if counts[delim] > 0 else ';'

def _norm_hdr_name(h: str) -> str:
    s = (h or "").strip().strip('"').strip("'")
    s = s.replace("„", '"').replace("“", '"').replace("”", '"')
    s = s.strip("[]").lower()
    s = re.sub(r"\s+", "", s)
    return s

def _build_index(headers: List[str]) -> Dict[str, int]:
    idx: Dict[str, int] = {}
    for i, h in enumerate(headers):
        idx[_norm_hdr_name(h)] = i
    return idx

def _get_val(row: List[str], idx: Dict[str,int], candidates: List[str]) -> str:
    for c in candidates:
        k = _norm_hdr_name(c)
        j = idx.get(k)
        if j is not None and j < len(row):
            v = (row[j] or "").strip()
            if v != "":
                return v
    return ""

def _parse_invoice_rows(p: Path) -> List[Dict[str, str]]:
    if not p.is_file():
        raise FileNotFoundError(str(p))
    text = _decode_bytes_auto(p.read_bytes())

    # find first non-empty line to guess delimiter
    first_non_empty = ""
    for ln in text.splitlines():
        if ln.strip():
            first_non_empty = ln
            break
    delim = _detect_delimiter_from_header_line(first_non_empty or text)

    r = csv.reader(io.StringIO(text), delimiter=delim)
    # find header (skip empty lines)
    headers: List[str] = []
    for row in r:
        if not row or all((str(x or "").strip() == "" for x in row)):
            continue
        headers = row
        break
    # remaining rows
    rows = [row for row in r]

    idx = _build_index(headers)
    out: List[Dict[str, str]] = []
    for row in rows:
        if not row or all((str(x or "").strip() == "" for x in row)):
            continue
        ean = _get_val(row, idx, _HDR_EAN)
        scm = _get_val(row, idx, _HDR_SCM)
        title = _get_val(row, idx, _HDR_TITLE)
        qty_raw = _get_val(row, idx, _HDR_QTY)
        try:
            qty_f = float(str(qty_raw).replace(",", ".") or "0")
        except Exception:
            qty_f = 0.0
        out.append({"ean": ean, "scm": scm, "title": title, "ordered_qty": qty_f})
    return out


# ---------- API ----------
@router.post("/suppliers/{supplier}/receiving/sessions")
def create_session(
    supplier: str,
    invoice_id: str = Body(..., embed=True),
) -> Dict[str, Any]:
    inv_no = _invoice_no_from_id(invoice_id)
    inv_csv = _invoice_csv_path(supplier, inv_no)
    rows = _parse_invoice_rows(inv_csv)

    lines: List[Dict[str, Any]] = []
    for r in rows:
        lines.append({
            "ean": r["ean"],
            "scm": r["scm"],
            "product_code": _product_code_for_supplier(supplier, r["scm"]),
            "title": r["title"],
            "ordered_qty": r["ordered_qty"],
            "received_qty": 0.0,
            "status": "pending",
        })

    sess_id = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    d = _receiving_dir(supplier, inv_no)
    d.mkdir(parents=True, exist_ok=True)
    s_path = _session_path(supplier, inv_no, sess_id)

    payload = {
        "supplier": supplier,
        "invoice_id": invoice_id,
        "invoice_no": inv_no,
        "created_at": _now_iso(),
        "lines": lines,
        "scans": []
    }
    s_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"session_id": sess_id, "invoice_no": inv_no, "lines": lines}


@router.post("/suppliers/{supplier}/receiving/sessions/{session_id}/scan")
def scan_code(
    supplier: str,
    session_id: str,
    code: str = Body(..., embed=True),
    qty: float = Body(1.0, embed=True)
) -> Dict[str, Any]:
    base = _supplier_root(supplier) / "receiving"
    target_path: Optional[Path] = None
    inv_no = None
    if base.is_dir():
        for inv_dir in base.iterdir():
            cand = inv_dir / f"session_{session_id}.json"
            if cand.is_file():
                target_path = cand
                inv_no = inv_dir.name
                break
    if not target_path:
        raise HTTPException(404, detail="Session not found")

    data = json.loads(target_path.read_text(encoding="utf-8"))
    lines = data.get("lines", [])
    code = _norm_text(code)
    try:
        qty = float(qty or 0)
    except Exception:
        qty = 0.0

    def _matches(line: Dict[str, Any]) -> bool:
        if code and _norm_text(line.get("ean","")) == code:
            return True
        if code and _norm_text(line.get("scm","")) == code:
            return True
        pc = _norm_text(line.get("product_code",""))
        if code and pc == code:
            return True
        return False

    matched = None
    for ln in lines:
        if _matches(ln):
            matched = ln
            break

    status = "unknown"
    if matched is None:
        status = "unexpected"
    else:
        matched["received_qty"] = float(matched.get("received_qty", 0) or 0) + qty
        matched["status"] = _status_for(matched["received_qty"], float(matched.get("ordered_qty", 0) or 0))
        status = matched["status"]

    scans = data.get("scans", [])
    scans.append({"ts": _now_iso(), "code": code, "qty": qty, "status": status})
    data["scans"] = scans
    data["lines"] = lines
    target_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "status": status,
        "line": matched,
        "summary": {
            "matched": sum(1 for ln in lines if ln.get("status")=="matched"),
            "partial": sum(1 for ln in lines if ln.get("status")=="partial"),
            "pending": sum(1 for ln in lines if ln.get("status")=="pending"),
            "overage": sum(1 for ln in lines if ln.get("status")=="overage"),
            "unexpected": len([s for s in scans if s.get("status")=="unexpected"]),
        }
    }


@router.get("/suppliers/{supplier}/receiving/sessions/{session_id}/summary")
def get_summary(supplier: str, session_id: str) -> Dict[str, Any]:
    base = _supplier_root(supplier) / "receiving"
    target_path: Optional[Path] = None
    inv_no = None
    if base.is_dir():
        for inv_dir in base.iterdir():
            cand = inv_dir / f"session_{session_id}.json"
            if cand.is_file():
                target_path = cand
                inv_no = inv_dir.name
                break
    if not target_path:
        raise HTTPException(404, detail="Session not found")

    data = json.loads(target_path.read_text(encoding="utf-8"))
    lines = data.get("lines", [])
    scans = data.get("scans", [])
    summary = {
        "matched": sum(1 for ln in lines if ln.get("status")=="matched"),
        "partial": sum(1 for ln in lines if ln.get("status")=="partial"),
        "pending": sum(1 for ln in lines if ln.get("status")=="pending"),
        "overage": sum(1 for ln in lines if ln.get("status")=="overage"),
        "unexpected": len([s for s in scans if s.get("status")=="unexpected"]),
    }
    _summary_path(supplier, inv_no).write_text(json.dumps({"lines": lines, "summary": summary}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"invoice_no": inv_no, "lines": lines, "summary": summary}


@router.post("/suppliers/{supplier}/receiving/sessions/{session_id}/finalize")
def finalize_session(
    supplier: str,
    session_id: str,
) -> Dict[str, Any]:
    base = _supplier_root(supplier) / "receiving"
    target_path: Optional[Path] = None
    inv_no = None
    if base.is_dir():
        for inv_dir in base.iterdir():
            cand = inv_dir / f"session_{session_id}.json"
            if cand.is_file():
                target_path = cand
                inv_no = inv_dir.name
                break
    if not target_path:
        raise HTTPException(404, detail="Session not found")

    data = json.loads(target_path.read_text(encoding="utf-8"))
    lines = data.get("lines", [])

    selected_codes: List[str] = []
    edits: Dict[str, Dict[str, str]] = {}
    for ln in lines:
        rq = float(ln.get("received_qty") or 0)
        if rq > 0:
            pc = ln.get("product_code") or ""
            if not pc:
                scm = ln.get("scm") or ""
                pc = _product_code_for_supplier(supplier, scm)
            if pc:
                selected_codes.append(pc)
                edits[pc] = {"INVOICE_QTY": str(int(rq) if rq.is_integer() else rq)}

    _summary_path(supplier, inv_no).write_text(json.dumps({"prepared_codes": selected_codes, "edits": edits}, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "invoice_no": inv_no,
        "selected_product_codes": selected_codes,
        "edits": edits,
    }
