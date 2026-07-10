# inventory_hub/routers/receiving_db.py
"""
Receiving Router - PostgreSQL-backed implementation for v12 FINAL.

Replaces JSON-based receiving.py with proper database operations.
"""
from __future__ import annotations
from fastapi import APIRouter, HTTPException, Body, Depends, Query
from typing import Any, Dict, Optional, List
from pathlib import Path
from datetime import datetime
from decimal import Decimal
import csv, io, re

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from inventory_hub.database import get_session
from inventory_hub.settings import settings
from inventory_hub.db_models import (
    Product, ProductIdentifier, IdentifierType, Supplier, Warehouse,
    ReceivingStatus
)
from inventory_hub.db_models_ext import (
    ReceivingSession, ReceivingLine, ScanEvent, ScanSessionType, ScanStatus
)
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.config_io import load_supplier as load_supplier_config
from inventory_hub.routers.receiving import _update_invoice_status

router = APIRouter(tags=["Receiving"])

# ============================================================================
# CSV Parsing Helpers (from original receiving.py)
# ============================================================================

_HDR_EAN = ["EAN", "[EAN]", "EAN13", "EAN_13", "Čiarový kód", "Ciarovy kod", "Barcode", "BARCODE", "Kód EAN"]
_HDR_SCM = ["SCM", "SČM", "[SCM]", "[SČM]", "SKU", "Supplier SKU", "Kat. číslo", "Katalógové číslo"]
_HDR_TITLE = ["TITLE", "Názov", "Nazov", "Product name", "Name"]
_HDR_QTY = ["QTY", "Mnozstvo", "Množstvo", "Počet", "Pocet", "Počet kusov", "Kusy"]
_HDR_PRICE = ["PRICE", "Cena", "Unit price", "Jednotková cena"]


def _decode_bytes_auto(b: bytes) -> str:
    try:
        return b.decode("utf-8-sig")
    except Exception:
        try:
            return b.decode("cp1250")
        except Exception:
            return b.decode("utf-8", errors="ignore")


def _detect_delimiter(line: str) -> str:
    counts = {';': line.count(';'), '\t': line.count('\t'), ',': line.count(',')}
    delim = max(counts, key=lambda k: counts[k])
    return delim if counts[delim] > 0 else ';'


def _norm_hdr_name(h: str) -> str:
    s = (h or "").strip().strip('"').strip("'")
    s = s.replace("„", '"').replace(""", '"').replace(""", '"')
    s = s.strip("[]").lower()
    s = re.sub(r"\s+", "", s)
    return s


def _build_index(headers: List[str]) -> Dict[str, int]:
    return {_norm_hdr_name(h): i for i, h in enumerate(headers)}


def _get_val(row: List[str], idx: Dict[str, int], candidates: List[str]) -> str:
    for c in candidates:
        k = _norm_hdr_name(c)
        j = idx.get(k)
        if j is not None and j < len(row):
            v = (row[j] or "").strip()
            if v:
                return v
    return ""


def _parse_invoice_csv(p: Path) -> List[Dict[str, Any]]:
    """Parse invoice CSV and return list of line dicts."""
    if not p.is_file():
        raise FileNotFoundError(str(p))

    text = _decode_bytes_auto(p.read_bytes())
    first_line = next((ln for ln in text.splitlines() if ln.strip()), "")
    delim = _detect_delimiter(first_line)

    r = csv.reader(io.StringIO(text), delimiter=delim)
    headers: List[str] = []
    for row in r:
        if row and any(str(x or "").strip() for x in row):
            headers = row
            break

    rows = list(r)
    idx = _build_index(headers)
    out: List[Dict[str, Any]] = []

    for row in rows:
        if not row or all(not str(x or "").strip() for x in row):
            continue

        ean = _get_val(row, idx, _HDR_EAN)
        scm = _get_val(row, idx, _HDR_SCM)
        title = _get_val(row, idx, _HDR_TITLE)
        qty_raw = _get_val(row, idx, _HDR_QTY)
        price_raw = _get_val(row, idx, _HDR_PRICE)

        try:
            qty = Decimal(str(qty_raw).replace(",", ".") or "0")
        except Exception:
            qty = Decimal("0")

        try:
            price = Decimal(str(price_raw).replace(",", ".")) if price_raw else None
        except Exception:
            price = None

        out.append({
            "ean": ean,
            "supplier_sku": scm,
            "description": title,
            "ordered_qty": qty,
            "unit_price": price,
        })

    return out


def _product_code_prefix(supplier_code: str) -> str:
    """Get product code prefix from supplier config."""
    try:
        cfg = load_supplier_config(supplier_code)
        paths = [
            ("adapter_settings", "mapping", "postprocess", "product_code_prefix"),
            ("adapter_settings", "product_code_prefix"),
            ("product_code_prefix",),
        ]
        for path in paths:
            cur = cfg
            for key in path:
                cur = getattr(cur, key, None) if hasattr(cur, key) else (cur.get(key) if isinstance(cur, dict) else None)
                if cur is None:
                    break
            if cur and str(cur).strip():
                return str(cur).strip()
    except Exception:
        pass

    if supplier_code in ("paul-lange", "paul_lange"):
        return "PL-"
    return ""


def _invoice_csv_path(supplier_code: str, invoice_no: str) -> Path:
    return Path(settings.INVENTORY_DATA_ROOT) / "suppliers" / supplier_code / "invoices" / "csv" / f"{invoice_no}.csv"


def _invoice_no_from_id(invoice_id: str) -> str:
    try:
        return invoice_id.split(":", 1)[1]
    except Exception:
        return invoice_id


# ============================================================================
# Pydantic Models for API
# ============================================================================

from pydantic import BaseModel


class CreateSessionRequest(BaseModel):
    invoice_id: str
    warehouse_code: Optional[str] = None


class ScanRequest(BaseModel):
    code: str
    qty: float = 1.0
    scanned_by: str = "scanner"


class SetQtyRequest(BaseModel):
    line_index: int
    received_qty: float
    note: Optional[str] = None


class AcceptAllRequest(BaseModel):
    only_pending: bool = True


class FinalizeRequest(BaseModel):
    force: bool = False


# ============================================================================
# Shared helpers
# ============================================================================

def _status_for(received: Decimal, ordered: Decimal) -> str:
    if received <= 0:
        return "pending"
    if received < ordered:
        return "partial"
    if received == ordered:
        return "matched"
    return "overage"


def _line_dict(line: ReceivingLine, prefix: str) -> Dict[str, Any]:
    """
    Serialize ReceivingLine.

    Canonical names: supplier_sku, description, line_number.
    Backward-compatible aliases for the frontend contract: scm, title.
    """
    product_code = f"{prefix}{line.supplier_sku}" if line.supplier_sku else None
    return {
        "line_number": line.line_number,
        "ean": line.ean or "",
        "supplier_sku": line.supplier_sku or "",
        "scm": line.supplier_sku or "",              # alias (frontend)
        "product_code": product_code,
        "description": line.description or "",
        "title": line.description or "",             # alias (frontend)
        "ordered_qty": float(line.ordered_qty),
        "received_qty": float(line.received_qty),
        "unit_price": float(line.unit_price) if line.unit_price is not None else None,
        "status": line.status,
        "product_id": line.product_id,
        "match_method": line.match_method,
    }


def _lines_sorted(session: ReceivingSession) -> List[ReceivingLine]:
    return sorted(session.lines, key=lambda x: x.line_number)


def _summary_counts(session: ReceivingSession, unexpected: int = 0) -> Dict[str, int]:
    return {
        "matched": sum(1 for ln in session.lines if ln.status == "matched"),
        "partial": sum(1 for ln in session.lines if ln.status == "partial"),
        "pending": sum(1 for ln in session.lines if ln.status == "pending"),
        "overage": sum(1 for ln in session.lines if ln.status == "overage"),
        "unexpected": unexpected,
    }


async def _count_unexpected(db: AsyncSession, session_id: int) -> int:
    # flush first so scan events added in this transaction are visible
    await db.flush()
    stmt = select(func.count()).where(
        ScanEvent.receiving_session_id == session_id,
        ScanEvent.receiving_line_id == None,  # noqa: E711
        ScanEvent.status == ScanStatus.active,
        ScanEvent.match_method.is_distinct_from("manual"),
    )
    result = await db.execute(stmt)
    return result.scalar() or 0


async def _count_scans(db: AsyncSession, session_id: int) -> int:
    await db.flush()
    stmt = select(func.count()).where(
        ScanEvent.receiving_session_id == session_id,
        ScanEvent.status == ScanStatus.active,
    )
    result = await db.execute(stmt)
    return result.scalar() or 0


async def _get_supplier(db: AsyncSession, supplier_code: str) -> Supplier:
    stmt = select(Supplier).where(Supplier.code == supplier_code)
    result = await db.execute(stmt)
    supplier = result.scalar_one_or_none()
    if not supplier:
        raise HTTPException(404, detail=f"Supplier not found: {supplier_code}")
    return supplier


async def _get_session_for_supplier(
    db: AsyncSession, supplier_code: str, session_id: int
) -> ReceivingSession:
    stmt = (
        select(ReceivingSession)
        .options(selectinload(ReceivingSession.lines))
        .options(selectinload(ReceivingSession.supplier))
        .where(ReceivingSession.id == session_id)
    )
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()
    if not session or session.supplier.code != supplier_code:
        raise HTTPException(404, detail="Session not found")
    return session


ACTIVE_STATUSES = (ReceivingStatus.new, ReceivingStatus.in_progress, ReceivingStatus.paused)


# ============================================================================
# Endpoints — canonical contract: /suppliers/{supplier_code}/receiving/...
# ============================================================================

@router.post("/suppliers/{supplier_code}/receiving/sessions")
async def create_session(
    supplier_code: str,
    request: CreateSessionRequest,
    db: AsyncSession = Depends(get_session),
):
    """Create a new receiving session from invoice CSV (lines stored in DB)."""
    invoice_no = _invoice_no_from_id(request.invoice_id)
    invoice_csv = _invoice_csv_path(supplier_code, invoice_no)

    if not invoice_csv.exists():
        raise HTTPException(404, detail=f"Invoice CSV not found: {invoice_csv}")

    supplier = await _get_supplier(db, supplier_code)

    # Warehouse
    if request.warehouse_code:
        stmt = select(Warehouse).where(Warehouse.code == request.warehouse_code)
    else:
        stmt = select(Warehouse).where(Warehouse.is_default == True)  # noqa: E712
    result = await db.execute(stmt)
    warehouse = result.scalar_one_or_none()
    if not warehouse:
        raise HTTPException(404, detail="Warehouse not found")

    # Schema enforces ONE session per (supplier, invoice) via uq_receiving_invoice.
    # Active session -> resume it; completed session -> use the reopen endpoint.
    stmt = select(ReceivingSession).where(
        ReceivingSession.supplier_id == supplier.id,
        ReceivingSession.invoice_number == invoice_no,
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing:
        if existing.status in ACTIVE_STATUSES:
            raise HTTPException(
                409,
                detail=f"Active session already exists for invoice {invoice_no} (session_id={existing.id}) — resume it",
            )
        raise HTTPException(
            409,
            detail=f"Invoice {invoice_no} already has a finished session (session_id={existing.id}) — use reopen",
        )

    try:
        rows = _parse_invoice_csv(invoice_csv)
    except Exception as e:
        raise HTTPException(400, detail=f"Failed to parse CSV: {e}")

    session = ReceivingSession(
        supplier_id=supplier.id,
        warehouse_id=warehouse.id,
        invoice_number=invoice_no,
        invoice_file_path=str(invoice_csv),
        total_lines=len(rows),
        status=ReceivingStatus.new,
    )
    db.add(session)
    await db.flush()

    prefix = _product_code_prefix(supplier_code)
    identifier_service = ProductIdentifierService(db)

    lines_out: List[Dict[str, Any]] = []
    for line_number, row in enumerate(rows, start=1):
        product = None
        match_method = None

        if row["ean"]:
            product = await identifier_service.find_product_by_barcode(row["ean"])
            if product:
                match_method = "ean"

        if not product and row["supplier_sku"]:
            product = await identifier_service.find_product_by_identifier(
                row["supplier_sku"], IdentifierType.supplier_sku, supplier.id
            )
            if product:
                match_method = "supplier_sku"

        line = ReceivingLine(
            session_id=session.id,
            line_number=line_number,
            product_id=product.id if product else None,
            supplier_sku=row["supplier_sku"],
            ean=row["ean"],
            description=row["description"],
            ordered_qty=row["ordered_qty"],
            received_qty=Decimal("0"),
            unit_price=row["unit_price"],
            status="pending",
            match_method=match_method,
        )
        db.add(line)
        lines_out.append(_line_dict(line, prefix))

    await db.flush()

    return {
        "session_id": session.id,
        "invoice_number": invoice_no,   # canonical
        "invoice_no": invoice_no,       # alias (frontend)
        "total_lines": len(rows),
        "lines": lines_out,
    }


@router.post("/suppliers/{supplier_code}/receiving/sessions/{session_id}/scan")
async def scan_code(
    supplier_code: str,
    session_id: int,
    request: ScanRequest,
    db: AsyncSession = Depends(get_session),
):
    """Process a barcode scan: match EAN first, then supplier SKU."""
    session = await _get_session_for_supplier(db, supplier_code, session_id)

    if session.status == ReceivingStatus.paused:
        raise HTTPException(400, detail="Session is paused — resume it first")
    if session.status == ReceivingStatus.completed:
        raise HTTPException(400, detail="Session already finalized")

    if session.status == ReceivingStatus.new:
        session.status = ReceivingStatus.in_progress
        session.started_at = datetime.utcnow()

    code = (request.code or "").strip()
    qty = Decimal(str(request.qty))

    # 1) EAN match, 2) supplier SKU match
    matched_line: Optional[ReceivingLine] = None
    for line in _lines_sorted(session):
        if code and line.ean and line.ean.strip() == code:
            matched_line = line
            break
    if not matched_line:
        for line in _lines_sorted(session):
            if code and line.supplier_sku and line.supplier_sku.strip() == code:
                matched_line = line
                break

    identifier_service = ProductIdentifierService(db)
    product = await identifier_service.find_product_by_barcode(code)

    scan_status = "unexpected"
    if matched_line:
        matched_line.received_qty += qty
        matched_line.status = _status_for(matched_line.received_qty, matched_line.ordered_qty)
        scan_status = matched_line.status

    scan_event = ScanEvent(
        session_type=ScanSessionType.receiving,
        receiving_session_id=session.id,
        receiving_line_id=matched_line.id if matched_line else None,
        scanned_code=code,
        scanned_code_type=identifier_service.classify_barcode(code) if code else None,
        product_id=product.id if product else None,
        match_method="ean" if matched_line and matched_line.ean == code else (
            "supplier_sku" if matched_line else None
        ),
        quantity=qty,
        status=ScanStatus.active,
        scanned_by=request.scanned_by,
    )
    db.add(scan_event)

    unexpected = await _count_unexpected(db, session.id)
    prefix = _product_code_prefix(supplier_code)

    return {
        "status": scan_status,
        "line": _line_dict(matched_line, prefix) if matched_line else None,
        "summary": _summary_counts(session, unexpected),
    }


@router.get("/suppliers/{supplier_code}/receiving/sessions/{session_id}/summary")
async def get_session_summary(
    supplier_code: str,
    session_id: int,
    db: AsyncSession = Depends(get_session),
):
    """Get receiving session summary with all lines."""
    session = await _get_session_for_supplier(db, supplier_code, session_id)
    prefix = _product_code_prefix(supplier_code)
    unexpected = await _count_unexpected(db, session.id)

    return {
        "session_id": session.id,
        "invoice_number": session.invoice_number,  # canonical
        "invoice_no": session.invoice_number,      # alias (frontend)
        "status": session.status.value,
        "lines": [_line_dict(ln, prefix) for ln in _lines_sorted(session)],
        "summary": _summary_counts(session, unexpected),
    }


@router.post("/suppliers/{supplier_code}/receiving/sessions/{session_id}/set-qty")
async def set_line_quantity(
    supplier_code: str,
    session_id: int,
    request: SetQtyRequest,
    db: AsyncSession = Depends(get_session),
):
    """Manually set received quantity for a line. line_index is 0-based index in the sorted lines array."""
    session = await _get_session_for_supplier(db, supplier_code, session_id)
    if session.status == ReceivingStatus.completed:
        raise HTTPException(400, detail="Session already finalized")

    lines = _lines_sorted(session)
    if request.line_index < 0 or request.line_index >= len(lines):
        raise HTTPException(400, detail="Invalid line_index")

    line = lines[request.line_index]
    old_qty = line.received_qty
    new_qty = Decimal(str(request.received_qty))
    line.received_qty = new_qty
    line.status = _status_for(new_qty, line.ordered_qty)

    if session.status == ReceivingStatus.new:
        session.status = ReceivingStatus.in_progress
        session.started_at = datetime.utcnow()

    db.add(ScanEvent(
        session_type=ScanSessionType.receiving,
        receiving_session_id=session.id,
        receiving_line_id=line.id,
        scanned_code=line.ean or line.supplier_sku or "",
        quantity=new_qty - old_qty,
        match_method="manual",
        status=ScanStatus.active,
        scanned_by="manual_edit",
    ))

    unexpected = await _count_unexpected(db, session.id)
    prefix = _product_code_prefix(supplier_code)
    return {
        "success": True,
        "line": _line_dict(line, prefix),
        "summary": _summary_counts(session, unexpected),
    }


@router.post("/suppliers/{supplier_code}/receiving/sessions/{session_id}/accept-all")
async def accept_all_items(
    supplier_code: str,
    session_id: int,
    request: AcceptAllRequest = Body(default=AcceptAllRequest()),
    db: AsyncSession = Depends(get_session),
):
    """Mark items as fully received (received_qty = ordered_qty)."""
    session = await _get_session_for_supplier(db, supplier_code, session_id)
    if session.status == ReceivingStatus.completed:
        raise HTTPException(400, detail="Session already finalized")

    updated = 0
    for line in _lines_sorted(session):
        if request.only_pending and line.status != "pending":
            continue
        if line.received_qty != line.ordered_qty and line.ordered_qty > 0:
            line.received_qty = line.ordered_qty
            line.status = "matched"
            updated += 1

    if updated and session.status == ReceivingStatus.new:
        session.status = ReceivingStatus.in_progress
        session.started_at = datetime.utcnow()

    unexpected = await _count_unexpected(db, session.id)
    prefix = _product_code_prefix(supplier_code)
    return {
        "success": True,
        "updated_count": updated,
        "lines": [_line_dict(ln, prefix) for ln in _lines_sorted(session)],
        "summary": _summary_counts(session, unexpected),
        "message": f"Accepted {updated} items",
    }


@router.post("/suppliers/{supplier_code}/receiving/sessions/{session_id}/reset-all")
async def reset_all_items(
    supplier_code: str,
    session_id: int,
    db: AsyncSession = Depends(get_session),
):
    """Reset all received quantities to 0."""
    session = await _get_session_for_supplier(db, supplier_code, session_id)
    if session.status == ReceivingStatus.completed:
        raise HTTPException(400, detail="Session already finalized")

    updated = 0
    for line in session.lines:
        if line.received_qty != 0:
            line.received_qty = Decimal("0")
            updated += 1
        line.status = "pending"

    unexpected = await _count_unexpected(db, session.id)
    prefix = _product_code_prefix(supplier_code)
    return {
        "success": True,
        "updated_count": updated,
        "lines": [_line_dict(ln, prefix) for ln in _lines_sorted(session)],
        "summary": _summary_counts(session, unexpected),
        "message": f"Reset {updated} items",
    }


@router.post("/suppliers/{supplier_code}/receiving/sessions/{session_id}/finalize")
async def finalize_session(
    supplier_code: str,
    session_id: int,
    request: FinalizeRequest = Body(default=FinalizeRequest()),
    db: AsyncSession = Depends(get_session),
):
    """
    Finalize receiving session (idempotency guard: cannot finalize twice).

    NOTE / TODO: stock_movements + stock_balances are NOT written yet.
    This requires a product-matching policy decision (what to do with lines
    that have no product_id). See project plan.
    """
    session = await _get_session_for_supplier(db, supplier_code, session_id)

    if session.status == ReceivingStatus.completed:
        raise HTTPException(400, detail="Session already finalized")

    lines = _lines_sorted(session)
    pending = sum(1 for ln in lines if ln.status == "pending")
    if pending and not request.force:
        raise HTTPException(
            400,
            detail=f"{pending} lines not received. Use force=true to finalize anyway.",
        )

    total_scans = await _count_scans(db, session.id)
    unexpected = await _count_unexpected(db, session.id)

    session.status = ReceivingStatus.completed
    session.finished_at = datetime.utcnow()

    stats = {
        "total_lines": len(lines),
        "received_complete": sum(1 for ln in lines if ln.status == "matched"),
        "received_partial": sum(1 for ln in lines if ln.status == "partial"),
        "received_overage": sum(1 for ln in lines if ln.status == "overage"),
        "not_received": sum(1 for ln in lines if ln.status == "pending"),
        "total_scans": total_scans,
        "unexpected_scans": unexpected,
    }

    total_ordered = sum(float(ln.ordered_qty) for ln in lines)
    total_received = sum(float(ln.received_qty) for ln in lines)
    received_count = sum(1 for ln in lines if ln.received_qty > 0)

    # Sync filesystem invoice index (UI tabs Nové/Prebieha/Dokončené read it)
    try:
        _update_invoice_status(supplier_code, session.invoice_number, "processed", {
            "processed_at": session.finished_at.isoformat(),
            "receiving_session_id": str(session.id),
            "receiving_stats": {
                "total_lines": stats["total_lines"],
                "received_complete": stats["received_complete"],
                "received_partial": stats["received_partial"],
                "not_received": stats["not_received"],
            },
            "current_session_id": None,
            "paused_at": None,
            "pause_stats": None,
        })
    except Exception:
        pass

    return {
        "success": True,
        "invoice_number": session.invoice_number,  # canonical
        "invoice_no": session.invoice_number,      # alias (frontend)
        "session_id": session.id,
        "completed_at": session.finished_at.isoformat(),
        "stats": stats,
        "total_ordered": total_ordered,
        "total_received": total_received,
        "received_items_count": received_count,
        "stock_movements_created": False,  # TODO: not implemented yet
        "message": f"Príjem faktúry {session.invoice_number} dokončený",
    }


@router.post("/suppliers/{supplier_code}/receiving/sessions/{session_id}/pause")
async def pause_session(
    supplier_code: str,
    session_id: int,
    db: AsyncSession = Depends(get_session),
):
    """Pause receiving session."""
    session = await _get_session_for_supplier(db, supplier_code, session_id)

    if session.status not in (ReceivingStatus.new, ReceivingStatus.in_progress):
        raise HTTPException(400, detail=f"Cannot pause session in status {session.status.value}")

    session.status = ReceivingStatus.paused
    session.paused_at = datetime.utcnow()

    total_scans = await _count_scans(db, session.id)
    lines = list(session.lines)
    stats = {
        "total_lines": len(lines),
        "received_complete": sum(1 for ln in lines if ln.status == "matched"),
        "received_partial": sum(1 for ln in lines if ln.status == "partial"),
        "not_received": sum(1 for ln in lines if ln.status == "pending"),
        "total_scans": total_scans,
    }

    # Sync filesystem invoice index (UI tabs Nové/Prebieha/Dokončené read it)
    try:
        _update_invoice_status(supplier_code, session.invoice_number, "in_progress", {
            "current_session_id": str(session.id),
            "paused_at": session.paused_at.isoformat(),
            "pause_stats": stats,
        })
    except Exception:
        pass

    return {
        "success": True,
        "invoice_number": session.invoice_number,
        "invoice_no": session.invoice_number,
        "session_id": session.id,
        "paused_at": session.paused_at.isoformat(),
        "stats": stats,
        "message": "Session paused",
    }


@router.post("/suppliers/{supplier_code}/receiving/sessions/{session_id}/resume")
async def resume_session(
    supplier_code: str,
    session_id: int,
    db: AsyncSession = Depends(get_session),
):
    """Resume paused receiving session; returns lines + scan history."""
    session = await _get_session_for_supplier(db, supplier_code, session_id)

    if session.status != ReceivingStatus.paused:
        raise HTTPException(400, detail=f"Cannot resume session in status {session.status.value}")

    session.status = ReceivingStatus.in_progress
    resumed_at = datetime.utcnow()

    stmt = (
        select(ScanEvent)
        .where(ScanEvent.receiving_session_id == session.id, ScanEvent.status == ScanStatus.active)
        .order_by(ScanEvent.scanned_at.asc())
    )
    result = await db.execute(stmt)
    scans = [
        {
            "ts": ev.scanned_at.isoformat() if ev.scanned_at else None,
            "code": ev.scanned_code,
            "qty": float(ev.quantity),
            "status": ev.match_method or "scan",
        }
        for ev in result.scalars().all()
    ]

    prefix = _product_code_prefix(supplier_code)
    return {
        "session_id": session.id,
        "invoice_number": session.invoice_number,
        "invoice_no": session.invoice_number,
        "lines": [_line_dict(ln, prefix) for ln in _lines_sorted(session)],
        "scans": scans,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "resumed_at": resumed_at.isoformat(),
    }


@router.get("/suppliers/{supplier_code}/invoices/{invoice_no}/active-session")
async def get_active_session(
    supplier_code: str,
    invoice_no: str,
    db: AsyncSession = Depends(get_session),
):
    """Return the active (new/in_progress/paused) session for an invoice, if any."""
    supplier = await _get_supplier(db, supplier_code)

    stmt = (
        select(ReceivingSession)
        .options(selectinload(ReceivingSession.lines))
        .where(
            ReceivingSession.supplier_id == supplier.id,
            ReceivingSession.invoice_number == invoice_no,
            ReceivingSession.status.in_(ACTIVE_STATUSES),
        )
        .order_by(ReceivingSession.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()

    if not session:
        return {"has_session": False, "session": None}

    scans_count = await _count_scans(db, session.id)
    return {
        "has_session": True,
        "session": {
            "session_id": session.id,
            "created_at": session.created_at.isoformat() if session.created_at else None,
            "is_paused": session.status == ReceivingStatus.paused,
            "paused_at": session.paused_at.isoformat() if session.paused_at else None,
            "lines_count": len(session.lines),
            "scans_count": scans_count,
            "stats": {
                "matched": sum(1 for ln in session.lines if ln.status == "matched"),
                "partial": sum(1 for ln in session.lines if ln.status == "partial"),
                "pending": sum(1 for ln in session.lines if ln.status == "pending"),
            },
        },
    }


@router.post("/suppliers/{supplier_code}/invoices/{invoice_no}/reopen")
async def reopen_invoice(
    supplier_code: str,
    invoice_no: str,
    db: AsyncSession = Depends(get_session),
):
    """
    Mark a processed invoice as not finished so it can be received again.

    - If a completed DB session exists: put it into 'paused' (resumable via
      the standard resume flow) and mark the invoice index as in_progress
      with current_session_id.
    - If no DB session exists (invoice processed via legacy flow): just flip
      the invoice index back to 'new' so a fresh session can be created.

    Safe today because finalize does not write stock movements yet.
    Once stock movements are implemented, reopen must be blocked (or must
    create compensating movements) when movements exist for the session.
    """
    supplier = await _get_supplier(db, supplier_code)

    stmt = (
        select(ReceivingSession)
        .options(selectinload(ReceivingSession.lines))
        .where(
            ReceivingSession.supplier_id == supplier.id,
            ReceivingSession.invoice_number == invoice_no,
            ReceivingSession.status == ReceivingStatus.completed,
        )
        .order_by(ReceivingSession.finished_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()

    if session:
        session.status = ReceivingStatus.paused
        session.finished_at = None
        session.paused_at = datetime.utcnow()

        lines = list(session.lines)
        total_scans = await _count_scans(db, session.id)
        stats = {
            "total_lines": len(lines),
            "received_complete": sum(1 for ln in lines if ln.status == "matched"),
            "received_partial": sum(1 for ln in lines if ln.status == "partial"),
            "not_received": sum(1 for ln in lines if ln.status == "pending"),
            "total_scans": total_scans,
        }
        try:
            _update_invoice_status(supplier_code, invoice_no, "in_progress", {
                "reopened_at": datetime.utcnow().isoformat(),
                "processed_at": None,
                "receiving_stats": None,
                "current_session_id": str(session.id),
                "paused_at": session.paused_at.isoformat(),
                "pause_stats": stats,
            })
        except Exception:
            pass
        return {
            "success": True,
            "session_id": session.id,
            "message": f"Faktúra {invoice_no} znovu otvorená — pokračuj v príjme (session {session.id}).",
        }

    # No DB session — legacy-processed invoice; reset index to 'new'
    updated = _update_invoice_status(supplier_code, invoice_no, "new", {
        "reopened_at": datetime.utcnow().isoformat(),
        "processed_at": None,
        "receiving_session_id": None,
        "receiving_stats": None,
        "current_session_id": None,
        "paused_at": None,
        "pause_stats": None,
    })
    if not updated:
        raise HTTPException(404, detail="Invoice not found (no DB session, not in index)")
    return {"success": True, "message": f"Faktúra {invoice_no} znovu otvorená pre príjem."}


@router.get("/suppliers/{supplier_code}/receiving/sessions")
async def list_sessions(
    supplier_code: str,
    status: Optional[str] = None,
    limit: int = Query(default=50, le=100),
    db: AsyncSession = Depends(get_session),
):
    """List receiving sessions for supplier."""
    supplier = await _get_supplier(db, supplier_code)

    stmt = (
        select(ReceivingSession)
        .where(ReceivingSession.supplier_id == supplier.id)
        .order_by(ReceivingSession.created_at.desc())
        .limit(limit)
    )
    if status:
        try:
            stmt = stmt.where(ReceivingSession.status == ReceivingStatus(status))
        except ValueError:
            pass

    result = await db.execute(stmt)
    sessions = result.scalars().all()

    return [
        {
            "session_id": s.id,
            "invoice_number": s.invoice_number,
            "invoice_no": s.invoice_number,
            "status": s.status.value,
            "total_lines": s.total_lines,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "finished_at": s.finished_at.isoformat() if s.finished_at else None,
        }
        for s in sessions
    ]
