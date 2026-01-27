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


# Manual quantity edit for a single line
@router.post("/suppliers/{supplier}/receiving/sessions/{session_id}/set-qty")
def set_line_quantity(
    supplier: str,
    session_id: str,
    line_index: int = Body(..., embed=True),
    received_qty: float = Body(..., embed=True),
    note: Optional[str] = Body(None, embed=True),
) -> Dict[str, Any]:
    """
    Manually set received quantity for a specific line.
    line_index: index in lines array
    received_qty: new received quantity (can be 0 to reset)
    note: optional note for manual edit
    """
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
    
    if line_index < 0 or line_index >= len(lines):
        raise HTTPException(400, detail="Invalid line_index")
    
    line = lines[line_index]
    old_qty = float(line.get("received_qty") or 0)
    ordered_qty = float(line.get("ordered_qty") or 0)
    
    # Update quantity
    line["received_qty"] = received_qty
    line["status"] = _status_for(received_qty, ordered_qty)
    
    # Record manual edit in scans log
    scans = data.get("scans", [])
    scans.append({
        "ts": _now_iso(),
        "type": "manual_edit",
        "line_index": line_index,
        "code": line.get("ean") or line.get("scm") or "",
        "old_qty": old_qty,
        "new_qty": received_qty,
        "note": note,
        "status": line["status"],
    })
    data["scans"] = scans
    data["lines"] = lines
    
    target_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    
    summary = {
        "matched": sum(1 for ln in lines if ln.get("status")=="matched"),
        "partial": sum(1 for ln in lines if ln.get("status")=="partial"),
        "pending": sum(1 for ln in lines if ln.get("status")=="pending"),
        "overage": sum(1 for ln in lines if ln.get("status")=="overage"),
        "unexpected": len([s for s in scans if s.get("status")=="unexpected"]),
    }
    
    return {
        "success": True,
        "line": line,
        "summary": summary,
    }


# Bulk action: Accept all remaining items
@router.post("/suppliers/{supplier}/receiving/sessions/{session_id}/accept-all")
def accept_all_items(
    supplier: str,
    session_id: str,
    only_pending: bool = Body(True, embed=True),
) -> Dict[str, Any]:
    """
    Mark all items as fully received (received_qty = ordered_qty).
    only_pending: if True, only update items with status 'pending'
    """
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
    
    updated_count = 0
    for i, line in enumerate(lines):
        if only_pending and line.get("status") != "pending":
            continue
        
        ordered_qty = float(line.get("ordered_qty") or 0)
        old_qty = float(line.get("received_qty") or 0)
        
        if old_qty < ordered_qty:
            line["received_qty"] = ordered_qty
            line["status"] = "matched"
            updated_count += 1
            
            # Log the bulk action
            scans.append({
                "ts": _now_iso(),
                "type": "bulk_accept",
                "line_index": i,
                "code": line.get("ean") or line.get("scm") or "",
                "old_qty": old_qty,
                "new_qty": ordered_qty,
                "status": "matched",
            })
    
    data["scans"] = scans
    data["lines"] = lines
    target_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    
    summary = {
        "matched": sum(1 for ln in lines if ln.get("status")=="matched"),
        "partial": sum(1 for ln in lines if ln.get("status")=="partial"),
        "pending": sum(1 for ln in lines if ln.get("status")=="pending"),
        "overage": sum(1 for ln in lines if ln.get("status")=="overage"),
        "unexpected": len([s for s in scans if s.get("status")=="unexpected"]),
    }
    
    return {
        "success": True,
        "updated_count": updated_count,
        "lines": lines,
        "summary": summary,
        "message": f"Označených {updated_count} položiek ako prijaté.",
    }


# Reset all quantities to 0
@router.post("/suppliers/{supplier}/receiving/sessions/{session_id}/reset-all")
def reset_all_items(
    supplier: str,
    session_id: str,
) -> Dict[str, Any]:
    """Reset all received quantities to 0."""
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
    
    # Log the reset
    scans.append({
        "ts": _now_iso(),
        "type": "bulk_reset",
        "note": "All quantities reset to 0",
    })
    
    for line in lines:
        line["received_qty"] = 0
        line["status"] = "pending"
    
    data["scans"] = scans
    data["lines"] = lines
    target_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    
    return {
        "success": True,
        "lines": lines,
        "summary": {
            "matched": 0,
            "partial": 0,
            "pending": len(lines),
            "overage": 0,
            "unexpected": 0,
        },
        "message": "Všetky množstvá boli vynulované.",
    }


@router.post("/suppliers/{supplier}/receiving/sessions/{session_id}/finalize")
def finalize_session(
    supplier: str,
    session_id: str,
    force: bool = Body(False, embed=True),
) -> Dict[str, Any]:
    """
    Finalize receiving session:
    1. Save completed receiving record
    2. Update invoice status to 'processed' in index
    3. Return summary for frontend
    """
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

    # Calculate stats
    stats = {
        "total_lines": len(lines),
        "received_complete": sum(1 for ln in lines if ln.get("status") == "matched"),
        "received_partial": sum(1 for ln in lines if ln.get("status") == "partial"),
        "received_overage": sum(1 for ln in lines if ln.get("status") == "overage"),
        "not_received": sum(1 for ln in lines if ln.get("status") == "pending"),
        "total_scans": len(scans),
        "unexpected_scans": len([s for s in scans if s.get("status") == "unexpected"]),
    }

    # Calculate totals
    total_ordered = sum(float(ln.get("ordered_qty") or 0) for ln in lines)
    total_received = sum(float(ln.get("received_qty") or 0) for ln in lines)

    # Build received items list (for stock movements later)
    received_items: List[Dict[str, Any]] = []
    for ln in lines:
        rq = float(ln.get("received_qty") or 0)
        if rq > 0:
            pc = ln.get("product_code") or ""
            if not pc:
                scm = ln.get("scm") or ""
                pc = _product_code_for_supplier(supplier, scm)
            received_items.append({
                "product_code": pc,
                "scm": ln.get("scm", ""),
                "ean": ln.get("ean", ""),
                "title": ln.get("title", ""),
                "ordered_qty": float(ln.get("ordered_qty") or 0),
                "received_qty": rq,
                "status": ln.get("status", ""),
            })

    # 1. Save completed receiving record
    completed_at = _now_iso()
    completed_record = {
        "supplier": supplier,
        "invoice_no": inv_no,
        "session_id": session_id,
        "completed_at": completed_at,
        "created_at": data.get("created_at"),
        "stats": stats,
        "total_ordered": total_ordered,
        "total_received": total_received,
        "received_items": received_items,
        "all_lines": lines,
        "scans": scans,
    }

    receiving_dir = _receiving_dir(supplier, inv_no)
    completed_path = receiving_dir / f"completed_{session_id}.json"
    completed_path.write_text(
        json.dumps(completed_record, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    # Also save as summary_latest for backwards compatibility
    _summary_path(supplier, inv_no).write_text(
        json.dumps(completed_record, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    # 2. Update invoice status in index.latest.json
    _update_invoice_status(
        supplier=supplier,
        invoice_no=inv_no,
        new_status="processed",
        extra_data={
            "processed_at": completed_at,
            "receiving_session_id": session_id,
            "receiving_stats": stats,
        }
    )

    # 3. Return summary for frontend
    return {
        "success": True,
        "invoice_no": inv_no,
        "session_id": session_id,
        "completed_at": completed_at,
        "stats": stats,
        "total_ordered": total_ordered,
        "total_received": total_received,
        "received_items_count": len(received_items),
        "message": f"Príjem faktúry {inv_no} dokončený. Prijaté {len(received_items)} položiek ({int(total_received)} ks).",
    }


def _update_invoice_status(
    supplier: str,
    invoice_no: str,
    new_status: str,
    extra_data: Optional[Dict[str, Any]] = None
) -> bool:
    """Update invoice status in index.latest.json"""
    sup_root = _supplier_root(supplier)
    idx_path = sup_root / "invoices" / "index.latest.json"

    if not idx_path.is_file():
        return False

    try:
        raw = json.loads(idx_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return False

        # Find the invoice by number
        invoice_key = f"{supplier}:{invoice_no}"
        if invoice_key not in raw:
            # Try to find by number field
            for key, val in raw.items():
                if isinstance(val, dict) and val.get("number") == invoice_no:
                    invoice_key = key
                    break

        if invoice_key in raw and isinstance(raw[invoice_key], dict):
            raw[invoice_key]["status"] = new_status
            if extra_data:
                raw[invoice_key].update(extra_data)

            # Write back
            idx_tmp = idx_path.with_suffix(".tmp")
            idx_tmp.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            idx_tmp.replace(idx_path)
            return True

    except Exception as e:
        print(f"Error updating invoice status: {e}")

    return False


# Additional endpoint: Get receiving history for an invoice
@router.get("/suppliers/{supplier}/receiving/{invoice_no}/history")
def get_receiving_history(supplier: str, invoice_no: str) -> Dict[str, Any]:
    """Get all receiving sessions/completions for an invoice"""
    recv_dir = _receiving_dir(supplier, invoice_no)
    
    if not recv_dir.is_dir():
        return {"invoice_no": invoice_no, "sessions": [], "completed": []}

    sessions = []
    completed = []

    for f in recv_dir.iterdir():
        if f.name.startswith("session_") and f.suffix == ".json":
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                sessions.append({
                    "session_id": f.stem.replace("session_", ""),
                    "created_at": data.get("created_at"),
                    "paused_at": data.get("paused_at"),
                    "lines_count": len(data.get("lines", [])),
                    "scans_count": len(data.get("scans", [])),
                    "is_paused": data.get("is_paused", False),
                })
            except Exception:
                pass
        elif f.name.startswith("completed_") and f.suffix == ".json":
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                completed.append({
                    "session_id": data.get("session_id"),
                    "completed_at": data.get("completed_at"),
                    "stats": data.get("stats"),
                    "total_received": data.get("total_received"),
                })
            except Exception:
                pass

    return {
        "invoice_no": invoice_no,
        "sessions": sorted(sessions, key=lambda x: x.get("created_at", ""), reverse=True),
        "completed": sorted(completed, key=lambda x: x.get("completed_at", ""), reverse=True),
    }


# Endpoint to pause/save session without finalizing
@router.post("/suppliers/{supplier}/receiving/sessions/{session_id}/pause")
def pause_session(
    supplier: str,
    session_id: str,
) -> Dict[str, Any]:
    """
    Pause receiving session - save current state and mark invoice as in_progress.
    User can resume later.
    """
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

    # Load and update session
    data = json.loads(target_path.read_text(encoding="utf-8"))
    data["is_paused"] = True
    data["paused_at"] = _now_iso()
    
    # Calculate current stats
    lines = data.get("lines", [])
    scans = data.get("scans", [])
    stats = {
        "total_lines": len(lines),
        "received_complete": sum(1 for ln in lines if ln.get("status") == "matched"),
        "received_partial": sum(1 for ln in lines if ln.get("status") == "partial"),
        "not_received": sum(1 for ln in lines if ln.get("status") == "pending"),
        "total_scans": len(scans),
    }
    data["pause_stats"] = stats
    
    # Save session
    target_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    
    # Update invoice status to in_progress
    _update_invoice_status(
        supplier=supplier,
        invoice_no=inv_no,
        new_status="in_progress",
        extra_data={
            "current_session_id": session_id,
            "paused_at": data["paused_at"],
            "pause_stats": stats,
        }
    )
    
    return {
        "success": True,
        "invoice_no": inv_no,
        "session_id": session_id,
        "paused_at": data["paused_at"],
        "stats": stats,
        "message": f"Príjem faktúry {inv_no} uložený. Môžete pokračovať neskôr.",
    }


# Endpoint to resume a paused session
@router.post("/suppliers/{supplier}/receiving/sessions/{session_id}/resume")
def resume_session(
    supplier: str,
    session_id: str,
) -> Dict[str, Any]:
    """
    Resume a paused receiving session.
    Returns full session data so frontend can restore state.
    """
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
    
    # Mark as resumed
    data["is_paused"] = False
    data["resumed_at"] = _now_iso()
    
    target_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    
    return {
        "session_id": session_id,
        "invoice_no": inv_no,
        "lines": data.get("lines", []),
        "scans": data.get("scans", []),
        "created_at": data.get("created_at"),
        "resumed_at": data["resumed_at"],
    }


# Get active/paused session for an invoice (if any)
@router.get("/suppliers/{supplier}/invoices/{invoice_no}/active-session")
def get_active_session(supplier: str, invoice_no: str) -> Dict[str, Any]:
    """
    Check if invoice has an active or paused session.
    Returns session details if found, null otherwise.
    """
    recv_dir = _receiving_dir(supplier, invoice_no)
    
    if not recv_dir.is_dir():
        return {"has_session": False, "session": None}
    
    # Find most recent session
    sessions = []
    for f in recv_dir.iterdir():
        if f.name.startswith("session_") and f.suffix == ".json":
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                sessions.append({
                    "path": f,
                    "session_id": f.stem.replace("session_", ""),
                    "created_at": data.get("created_at", ""),
                    "is_paused": data.get("is_paused", False),
                    "data": data,
                })
            except Exception:
                pass
    
    if not sessions:
        return {"has_session": False, "session": None}
    
    # Get most recent
    sessions.sort(key=lambda x: x["created_at"], reverse=True)
    latest = sessions[0]
    
    lines = latest["data"].get("lines", [])
    scans = latest["data"].get("scans", [])
    
    return {
        "has_session": True,
        "session": {
            "session_id": latest["session_id"],
            "created_at": latest["created_at"],
            "is_paused": latest["is_paused"],
            "paused_at": latest["data"].get("paused_at"),
            "lines_count": len(lines),
            "scans_count": len(scans),
            "stats": {
                "matched": sum(1 for ln in lines if ln.get("status") == "matched"),
                "partial": sum(1 for ln in lines if ln.get("status") == "partial"),
                "pending": sum(1 for ln in lines if ln.get("status") == "pending"),
            }
        }
    }


# Endpoint to reopen a processed invoice (for corrections)
@router.post("/suppliers/{supplier}/invoices/{invoice_no}/reopen")
def reopen_invoice(supplier: str, invoice_no: str) -> Dict[str, Any]:
    """Change invoice status back to 'new' for re-processing"""
    success = _update_invoice_status(supplier, invoice_no, "new", {
        "reopened_at": _now_iso(),
        "processed_at": None,
        "receiving_session_id": None,
        "receiving_stats": None,
        "current_session_id": None,
        "paused_at": None,
        "pause_stats": None,
    })
    
    if success:
        return {"success": True, "message": f"Faktúra {invoice_no} znovu otvorená pre príjem."}
    else:
        raise HTTPException(404, detail="Invoice not found in index")
