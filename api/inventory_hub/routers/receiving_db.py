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

router = APIRouter(prefix="/receiving", tags=["Receiving"])

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


class CreateSessionResponse(BaseModel):
    session_id: int
    invoice_number: str
    total_lines: int
    lines: List[Dict[str, Any]]


class ScanRequest(BaseModel):
    code: str
    qty: float = 1.0
    scanned_by: str = "scanner"


class ScanResponse(BaseModel):
    status: str
    line: Optional[Dict[str, Any]]
    summary: Dict[str, int]


class SessionSummaryResponse(BaseModel):
    session_id: int
    invoice_number: str
    status: str
    lines: List[Dict[str, Any]]
    summary: Dict[str, int]


# ============================================================================
# Endpoints
# ============================================================================

@router.post("/suppliers/{supplier_code}/sessions", response_model=CreateSessionResponse)
async def create_session(
    supplier_code: str,
    request: CreateSessionRequest,
    db: AsyncSession = Depends(get_session),
):
    """
    Create a new receiving session from invoice CSV.
    
    Parses the invoice, creates session and lines in database.
    """
    invoice_no = _invoice_no_from_id(request.invoice_id)
    invoice_csv = _invoice_csv_path(supplier_code, invoice_no)
    
    if not invoice_csv.exists():
        raise HTTPException(404, detail=f"Invoice CSV not found: {invoice_csv}")
    
    # Get supplier
    stmt = select(Supplier).where(Supplier.code == supplier_code)
    result = await db.execute(stmt)
    supplier = result.scalar_one_or_none()
    if not supplier:
        raise HTTPException(404, detail=f"Supplier not found: {supplier_code}")
    
    # Get warehouse
    if request.warehouse_code:
        stmt = select(Warehouse).where(Warehouse.code == request.warehouse_code)
    else:
        stmt = select(Warehouse).where(Warehouse.is_default == True)
    result = await db.execute(stmt)
    warehouse = result.scalar_one_or_none()
    if not warehouse:
        raise HTTPException(404, detail="Warehouse not found")
    
    # Check if session already exists
    stmt = select(ReceivingSession).where(
        ReceivingSession.supplier_id == supplier.id,
        ReceivingSession.invoice_number == invoice_no,
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(409, detail=f"Session already exists for invoice {invoice_no}")
    
    # Parse CSV
    try:
        rows = _parse_invoice_csv(invoice_csv)
    except Exception as e:
        raise HTTPException(400, detail=f"Failed to parse CSV: {e}")
    
    # Create session
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
    
    # Create lines
    prefix = _product_code_prefix(supplier_code)
    identifier_service = ProductIdentifierService(db)
    
    lines_data = []
    for line_number, row in enumerate(rows, start=1):
        # Try to match product
        product = None
        match_method = None
        
        if row["ean"]:
            product = await identifier_service.find_product_by_barcode(row["ean"])
            if product:
                match_method = "ean"
        
        if not product and row["supplier_sku"]:
            product = await identifier_service.find_product_by_identifier(
                row["supplier_sku"],
                IdentifierType.supplier_sku,
                supplier.id
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
        
        lines_data.append({
            "line_number": line_number,
            "ean": row["ean"],
            "supplier_sku": row["supplier_sku"],
            "product_code": f"{prefix}{row['supplier_sku']}" if row["supplier_sku"] else None,
            "description": row["description"],
            "ordered_qty": float(row["ordered_qty"]),
            "received_qty": 0.0,
            "unit_price": float(row["unit_price"]) if row["unit_price"] else None,
            "status": "pending",
            "matched": product is not None,
            "match_method": match_method,
        })
    
    await db.flush()
    
    return CreateSessionResponse(
        session_id=session.id,
        invoice_number=invoice_no,
        total_lines=len(rows),
        lines=lines_data,
    )


@router.post("/sessions/{session_id}/scan", response_model=ScanResponse)
async def scan_code(
    session_id: int,
    request: ScanRequest,
    db: AsyncSession = Depends(get_session),
):
    """
    Process barcode scan during receiving.
    
    Matches scanned code to invoice line and updates received quantity.
    """
    # Get session with lines
    stmt = (
        select(ReceivingSession)
        .options(selectinload(ReceivingSession.lines))
        .where(ReceivingSession.id == session_id)
    )
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, detail="Session not found")
    
    # Update status if first scan
    if session.status == ReceivingStatus.new:
        session.status = ReceivingStatus.in_progress
        session.started_at = datetime.utcnow()
    
    code = (request.code or "").strip()
    qty = Decimal(str(request.qty))
    
    # Find matching line
    matched_line: Optional[ReceivingLine] = None
    for line in session.lines:
        if code and line.ean and line.ean.strip() == code:
            matched_line = line
            break
        if code and line.supplier_sku and line.supplier_sku.strip() == code:
            matched_line = line
            break
    
    # Create scan event
    identifier_service = ProductIdentifierService(db)
    product = await identifier_service.find_product_by_barcode(code)
    
    scan_status = "unexpected"
    if matched_line:
        matched_line.received_qty += qty
        
        if matched_line.received_qty <= Decimal("0"):
            scan_status = "pending"
            matched_line.status = "pending"
        elif matched_line.received_qty < matched_line.ordered_qty:
            scan_status = "partial"
            matched_line.status = "partial"
        elif matched_line.received_qty == matched_line.ordered_qty:
            scan_status = "matched"
            matched_line.status = "matched"
        else:
            scan_status = "overage"
            matched_line.status = "overage"
    
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
    
    # Calculate summary
    summary = {
        "matched": sum(1 for ln in session.lines if ln.status == "matched"),
        "partial": sum(1 for ln in session.lines if ln.status == "partial"),
        "pending": sum(1 for ln in session.lines if ln.status == "pending"),
        "overage": sum(1 for ln in session.lines if ln.status == "overage"),
        "unexpected": 0,  # Will count from scan events
    }
    
    # Count unexpected scans
    stmt = select(func.count()).where(
        ScanEvent.receiving_session_id == session.id,
        ScanEvent.receiving_line_id == None,
        ScanEvent.status == ScanStatus.active,
    )
    result = await db.execute(stmt)
    summary["unexpected"] = result.scalar() or 0
    
    return ScanResponse(
        status=scan_status,
        line={
            "line_number": matched_line.line_number,
            "ean": matched_line.ean,
            "supplier_sku": matched_line.supplier_sku,
            "description": matched_line.description,
            "ordered_qty": float(matched_line.ordered_qty),
            "received_qty": float(matched_line.received_qty),
            "status": matched_line.status,
        } if matched_line else None,
        summary=summary,
    )


@router.get("/sessions/{session_id}/summary", response_model=SessionSummaryResponse)
async def get_session_summary(
    session_id: int,
    db: AsyncSession = Depends(get_session),
):
    """Get receiving session summary with all lines."""
    stmt = (
        select(ReceivingSession)
        .options(selectinload(ReceivingSession.lines))
        .options(selectinload(ReceivingSession.supplier))
        .where(ReceivingSession.id == session_id)
    )
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, detail="Session not found")
    
    prefix = _product_code_prefix(session.supplier.code)
    
    lines_data = []
    for line in sorted(session.lines, key=lambda x: x.line_number):
        lines_data.append({
            "line_number": line.line_number,
            "ean": line.ean,
            "supplier_sku": line.supplier_sku,
            "product_code": f"{prefix}{line.supplier_sku}" if line.supplier_sku else None,
            "description": line.description,
            "ordered_qty": float(line.ordered_qty),
            "received_qty": float(line.received_qty),
            "unit_price": float(line.unit_price) if line.unit_price else None,
            "status": line.status,
            "product_id": line.product_id,
        })
    
    summary = {
        "matched": sum(1 for ln in session.lines if ln.status == "matched"),
        "partial": sum(1 for ln in session.lines if ln.status == "partial"),
        "pending": sum(1 for ln in session.lines if ln.status == "pending"),
        "overage": sum(1 for ln in session.lines if ln.status == "overage"),
        "unexpected": 0,
    }
    
    # Count unexpected scans
    stmt = select(func.count()).where(
        ScanEvent.receiving_session_id == session.id,
        ScanEvent.receiving_line_id == None,
        ScanEvent.status == ScanStatus.active,
    )
    result = await db.execute(stmt)
    summary["unexpected"] = result.scalar() or 0
    
    return SessionSummaryResponse(
        session_id=session.id,
        invoice_number=session.invoice_number,
        status=session.status.value,
        lines=lines_data,
        summary=summary,
    )


@router.post("/sessions/{session_id}/finalize")
async def finalize_session(
    session_id: int,
    db: AsyncSession = Depends(get_session),
):
    """
    Finalize receiving session.
    
    Marks session as completed and prepares data for stock movements.
    """
    stmt = (
        select(ReceivingSession)
        .options(selectinload(ReceivingSession.lines))
        .options(selectinload(ReceivingSession.supplier))
        .where(ReceivingSession.id == session_id)
    )
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, detail="Session not found")
    
    if session.status == ReceivingStatus.completed:
        raise HTTPException(400, detail="Session already finalized")
    
    session.status = ReceivingStatus.completed
    session.finished_at = datetime.utcnow()
    
    # Prepare output
    prefix = _product_code_prefix(session.supplier.code)
    selected_codes = []
    edits = {}
    
    for line in session.lines:
        if line.received_qty > 0:
            pc = f"{prefix}{line.supplier_sku}" if line.supplier_sku else None
            if pc:
                selected_codes.append(pc)
                edits[pc] = {"INVOICE_QTY": str(int(line.received_qty) if line.received_qty == int(line.received_qty) else line.received_qty)}
    
    return {
        "session_id": session.id,
        "invoice_number": session.invoice_number,
        "status": "completed",
        "selected_product_codes": selected_codes,
        "edits": edits,
    }


@router.post("/sessions/{session_id}/pause")
async def pause_session(
    session_id: int,
    db: AsyncSession = Depends(get_session),
):
    """Pause receiving session."""
    stmt = select(ReceivingSession).where(ReceivingSession.id == session_id)
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, detail="Session not found")
    
    if session.status not in (ReceivingStatus.new, ReceivingStatus.in_progress):
        raise HTTPException(400, detail=f"Cannot pause session in status {session.status}")
    
    session.status = ReceivingStatus.paused
    session.paused_at = datetime.utcnow()
    
    return {"session_id": session.id, "status": "paused"}


@router.post("/sessions/{session_id}/resume")
async def resume_session(
    session_id: int,
    db: AsyncSession = Depends(get_session),
):
    """Resume paused receiving session."""
    stmt = select(ReceivingSession).where(ReceivingSession.id == session_id)
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, detail="Session not found")
    
    if session.status != ReceivingStatus.paused:
        raise HTTPException(400, detail=f"Cannot resume session in status {session.status}")
    
    session.status = ReceivingStatus.in_progress
    session.paused_at = None
    
    return {"session_id": session.id, "status": "in_progress"}


@router.get("/suppliers/{supplier_code}/sessions")
async def list_sessions(
    supplier_code: str,
    status: Optional[str] = None,
    limit: int = Query(default=50, le=100),
    db: AsyncSession = Depends(get_session),
):
    """List receiving sessions for supplier."""
    # Get supplier
    stmt = select(Supplier).where(Supplier.code == supplier_code)
    result = await db.execute(stmt)
    supplier = result.scalar_one_or_none()
    if not supplier:
        raise HTTPException(404, detail=f"Supplier not found: {supplier_code}")
    
    # Build query
    stmt = (
        select(ReceivingSession)
        .where(ReceivingSession.supplier_id == supplier.id)
        .order_by(ReceivingSession.created_at.desc())
        .limit(limit)
    )
    
    if status:
        try:
            status_enum = ReceivingStatus(status)
            stmt = stmt.where(ReceivingSession.status == status_enum)
        except ValueError:
            pass
    
    result = await db.execute(stmt)
    sessions = result.scalars().all()
    
    return [
        {
            "session_id": s.id,
            "invoice_number": s.invoice_number,
            "status": s.status.value,
            "total_lines": s.total_lines,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "finished_at": s.finished_at.isoformat() if s.finished_at else None,
        }
        for s in sessions
    ]
