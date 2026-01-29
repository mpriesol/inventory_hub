# inventory_hub/routers/invoices_unified.py
"""
Unified Invoice Management Router - uses uploaded_invoices table.

Upload → Save file + DB record → List in /invoices
receiving_sessions is created only when "Príjem" workflow starts.
"""
from __future__ import annotations
import os
import re
from pathlib import Path
from datetime import datetime, date
from decimal import Decimal
from enum import Enum
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Query, UploadFile, File, Form
from fastapi.responses import FileResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from inventory_hub.database import get_session
from inventory_hub.db_models import Supplier

router = APIRouter(prefix="/invoices", tags=["Invoice Management"])

# ============================================================================
# Constants
# ============================================================================

INVOICE_STORAGE_BASE = Path("/data/inventory-data/suppliers")
ALLOWED_EXTENSIONS = {'.pdf', '.csv', '.xlsx', '.xls', '.doc', '.docx', '.xml', '.txt', '.jpg', '.jpeg', '.png'}

# ============================================================================
# Pydantic Models
# ============================================================================

class PaymentStatus(str, Enum):
    unpaid = "unpaid"
    partial = "partial"
    paid = "paid"


class InvoiceUpdateRequest(BaseModel):
    invoice_number: Optional[str] = None
    invoice_date: Optional[date] = None
    due_date: Optional[date] = None
    currency: Optional[str] = None
    total_amount: Optional[Decimal] = None
    vat_rate: Optional[Decimal] = None
    vat_included: Optional[bool] = None
    items_count: Optional[int] = None
    notes: Optional[str] = None


class PaymentUpdateRequest(BaseModel):
    payment_status: PaymentStatus
    paid_amount: Optional[Decimal] = None
    paid_at: Optional[datetime] = None


# ============================================================================
# Helper Functions
# ============================================================================

def _sanitize_filename(filename: str) -> str:
    """Sanitize filename for safe filesystem storage."""
    filename = os.path.basename(filename)
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    filename = filename.strip(' .')
    return filename if filename else 'invoice'


def _extract_invoice_number(filename: str) -> Optional[str]:
    """Try to extract invoice number from filename."""
    stem = Path(filename).stem
    patterns = [
        r'F?(\d{10})',
        r'(\d{7,})',
        r'FA[-_]?(\d+)',
        r'INV[-_]?(\d+)',
        r'[Ff]akt[uú]ra[-_]?(\d+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, stem)
        if match:
            return match.group(1) if match.lastindex else match.group(0)
    return None


def _get_file_type(filename: str) -> str:
    """Get file type from filename."""
    ext = Path(filename).suffix.lower()
    type_map = {
        '.pdf': 'pdf', '.csv': 'csv', '.xlsx': 'xlsx', '.xls': 'xls',
        '.doc': 'doc', '.docx': 'docx', '.xml': 'xml', '.txt': 'txt',
    }
    return type_map.get(ext, 'other')


# ============================================================================
# Endpoints
# ============================================================================

@router.get("/suppliers")
async def list_all_suppliers(db: AsyncSession = Depends(get_session)):
    """
    List ALL suppliers (not just those with invoices).
    Uses LEFT OUTER JOIN to include suppliers with 0 invoices.
    """
    sql = text("""
        SELECT 
            s.id,
            s.code,
            s.name,
            COALESCE(COUNT(ui.id), 0) as invoice_count,
            COALESCE(SUM(ui.total_amount), 0) as total_amount
        FROM suppliers s
        LEFT OUTER JOIN uploaded_invoices ui ON s.id = ui.supplier_id
        WHERE s.is_active = true
        GROUP BY s.id, s.code, s.name
        ORDER BY s.name
    """)
    
    result = await db.execute(sql)
    suppliers = [
        {
            "id": row.id,
            "code": row.code,
            "name": row.name,
            "invoice_count": row.invoice_count,
            "total_amount": float(row.total_amount) if row.total_amount else 0,
        }
        for row in result.fetchall()
    ]
    
    return {"suppliers": suppliers}


@router.post("/upload")
async def upload_invoice(
    supplier_code: str = Form(...),
    file: UploadFile = File(...),
    invoice_number: Optional[str] = Form(None),
    invoice_date: Optional[str] = Form(None),
    due_date: Optional[str] = Form(None),
    vat_included: bool = Form(True),
    db: AsyncSession = Depends(get_session),
):
    """
    Upload invoice file.
    
    1. Validates supplier
    2. Saves file to: /data/inventory-data/suppliers/{code}/invoices/raw/{year}/{month}/
    3. Creates record in uploaded_invoices table
    4. Returns invoice ID
    """
    if not file.filename:
        raise HTTPException(400, "No filename provided")
    
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"File type {ext} not allowed. Allowed: {', '.join(ALLOWED_EXTENSIONS)}")
    
    # Get supplier
    stmt = select(Supplier).where(Supplier.code == supplier_code)
    result = await db.execute(stmt)
    supplier = result.scalar_one_or_none()
    
    if not supplier:
        raise HTTPException(404, f"Supplier '{supplier_code}' not found")
    
    # Prepare storage path
    now = datetime.now()
    storage_dir = INVOICE_STORAGE_BASE / supplier_code / "invoices" / "raw" / str(now.year) / f"{now.month:02d}"
    storage_dir.mkdir(parents=True, exist_ok=True)
    
    # Sanitize filename and handle duplicates
    safe_filename = _sanitize_filename(file.filename)
    stored_filename = safe_filename
    counter = 1
    while (storage_dir / stored_filename).exists():
        stem = Path(safe_filename).stem
        suffix = Path(safe_filename).suffix
        stored_filename = f"{stem}_{counter}{suffix}"
        counter += 1
    
    file_path = storage_dir / stored_filename
    
    # Save file
    try:
        content = await file.read()
        file_path.write_bytes(content)
        file_size = len(content)
    except Exception as e:
        raise HTTPException(500, f"Failed to save file: {e}")
    
    # Parse dates
    parsed_invoice_date = None
    parsed_due_date = None
    if invoice_date:
        try:
            parsed_invoice_date = datetime.strptime(invoice_date, "%Y-%m-%d").date()
        except ValueError:
            pass
    if due_date:
        try:
            parsed_due_date = datetime.strptime(due_date, "%Y-%m-%d").date()
        except ValueError:
            pass
    
    # Extract invoice number from filename if not provided
    final_invoice_number = invoice_number or _extract_invoice_number(file.filename)
    
    # Insert into database
    insert_sql = text("""
        INSERT INTO uploaded_invoices (
            supplier_id, original_filename, stored_filename, file_path,
            file_size_bytes, file_type, invoice_number, invoice_date, due_date,
            vat_included, payment_status, is_parsed, created_at, updated_at
        ) VALUES (
            :supplier_id, :original_filename, :stored_filename, :file_path,
            :file_size_bytes, :file_type, :invoice_number, :invoice_date, :due_date,
            :vat_included, 'unpaid', false, NOW(), NOW()
        )
        RETURNING id
    """)
    
    result = await db.execute(insert_sql, {
        "supplier_id": supplier.id,
        "original_filename": file.filename,
        "stored_filename": stored_filename,
        "file_path": str(file_path),
        "file_size_bytes": file_size,
        "file_type": _get_file_type(file.filename),
        "invoice_number": final_invoice_number,
        "invoice_date": parsed_invoice_date,
        "due_date": parsed_due_date,
        "vat_included": vat_included,
    })
    
    invoice_id = result.scalar_one()
    await db.commit()
    
    return {
        "id": invoice_id,
        "supplier_code": supplier_code,
        "original_filename": file.filename,
        "stored_filename": stored_filename,
        "file_path": str(file_path),
        "invoice_number": final_invoice_number,
        "message": "Invoice uploaded successfully"
    }


@router.get("")
async def list_invoices(
    # Filter row parameters
    f_invoice_number: Optional[str] = Query(None, description="Invoice number (partial match)"),
    f_supplier: Optional[str] = Query(None, description="Supplier name or code (partial match)"),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    due_from: Optional[date] = Query(None),
    due_to: Optional[date] = Query(None),
    f_amount_min: Optional[float] = Query(None),
    f_amount_max: Optional[float] = Query(None),
    f_items_min: Optional[int] = Query(None),
    f_items_max: Optional[int] = Query(None),
    f_currency: Optional[str] = Query(None),
    payment_status: Optional[str] = Query(None),
    receiving_status: Optional[str] = Query(None),
    is_overdue: Optional[bool] = Query(None),
    # Sorting
    sort_by: str = Query("created_at"),
    sort_order: str = Query("desc"),
    # Pagination
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_session),
):
    """List all invoices with server-side filtering."""
    # Build WHERE conditions
    conditions = []
    params = {}
    
    if f_invoice_number:
        conditions.append("(ui.invoice_number ILIKE :inv_num OR ui.original_filename ILIKE :inv_num)")
        params["inv_num"] = f"%{f_invoice_number}%"
    
    if f_supplier:
        conditions.append("(s.name ILIKE :supplier OR s.code ILIKE :supplier)")
        params["supplier"] = f"%{f_supplier}%"
    
    if date_from:
        conditions.append("ui.invoice_date >= :date_from")
        params["date_from"] = date_from
    
    if date_to:
        conditions.append("ui.invoice_date <= :date_to")
        params["date_to"] = date_to
    
    if due_from:
        conditions.append("ui.due_date >= :due_from")
        params["due_from"] = due_from
    
    if due_to:
        conditions.append("ui.due_date <= :due_to")
        params["due_to"] = due_to
    
    if f_amount_min is not None:
        conditions.append("COALESCE(ui.total_amount, 0) >= :amount_min")
        params["amount_min"] = f_amount_min
    
    if f_amount_max is not None:
        conditions.append("COALESCE(ui.total_amount, 0) <= :amount_max")
        params["amount_max"] = f_amount_max
    
    if f_items_min is not None:
        conditions.append("COALESCE(ui.items_count, 0) >= :items_min")
        params["items_min"] = f_items_min
    
    if f_items_max is not None:
        conditions.append("COALESCE(ui.items_count, 0) <= :items_max")
        params["items_max"] = f_items_max
    
    if f_currency:
        conditions.append("ui.currency = :currency")
        params["currency"] = f_currency.upper()
    
    if payment_status:
        conditions.append("ui.payment_status = :payment_status")
        params["payment_status"] = payment_status
    
    if receiving_status:
        if receiving_status == "not_started":
            conditions.append("ui.receiving_session_id IS NULL")
        else:
            conditions.append("rs.status = :receiving_status")
            params["receiving_status"] = receiving_status
    
    if is_overdue is True:
        conditions.append("ui.due_date < CURRENT_DATE AND ui.payment_status != 'paid'")
    elif is_overdue is False:
        conditions.append("(ui.due_date >= CURRENT_DATE OR ui.due_date IS NULL OR ui.payment_status = 'paid')")
    
    where_clause = " AND ".join(conditions) if conditions else "1=1"
    
    # Sorting
    sort_columns = {
        "invoice_number": "ui.invoice_number",
        "invoice_date": "ui.invoice_date",
        "due_date": "ui.due_date",
        "total_amount": "ui.total_amount",
        "supplier_name": "s.name",
        "created_at": "ui.created_at",
        "original_filename": "ui.original_filename",
    }
    sort_col = sort_columns.get(sort_by, "ui.created_at")
    sort_dir = "DESC" if sort_order.lower() == "desc" else "ASC"
    
    # Count query
    count_sql = text(f"""
        SELECT COUNT(*) 
        FROM uploaded_invoices ui
        JOIN suppliers s ON ui.supplier_id = s.id
        LEFT JOIN receiving_sessions rs ON ui.receiving_session_id = rs.id
        WHERE {where_clause}
    """)
    
    count_result = await db.execute(count_sql, params)
    total = count_result.scalar() or 0
    
    # Data query
    offset = (page - 1) * page_size
    params["limit"] = page_size
    params["offset"] = offset
    
    data_sql = text(f"""
        SELECT 
            ui.id,
            ui.supplier_id,
            s.code as supplier_code,
            s.name as supplier_name,
            ui.original_filename,
            ui.stored_filename,
            ui.file_path,
            ui.file_size_bytes,
            ui.file_type,
            ui.invoice_number,
            ui.invoice_date,
            ui.due_date,
            ui.currency,
            ui.total_amount,
            ui.total_without_vat,
            ui.vat_amount,
            ui.total_with_vat,
            ui.vat_rate,
            ui.vat_included,
            ui.items_count,
            ui.payment_status,
            ui.paid_at,
            ui.paid_amount,
            ui.is_parsed,
            ui.parse_error,
            ui.receiving_session_id,
            ui.notes,
            ui.created_at,
            ui.updated_at,
            CASE 
                WHEN ui.due_date IS NOT NULL AND ui.due_date < CURRENT_DATE AND ui.payment_status != 'paid'
                THEN true ELSE false 
            END as is_overdue,
            CASE 
                WHEN ui.due_date IS NOT NULL THEN ui.due_date - CURRENT_DATE 
                ELSE NULL 
            END as days_until_due,
            CASE 
                WHEN ui.receiving_session_id IS NOT NULL THEN rs.status
                ELSE 'not_started'
            END as receiving_status
        FROM uploaded_invoices ui
        JOIN suppliers s ON ui.supplier_id = s.id
        LEFT JOIN receiving_sessions rs ON ui.receiving_session_id = rs.id
        WHERE {where_clause}
        ORDER BY {sort_col} {sort_dir} NULLS LAST
        LIMIT :limit OFFSET :offset
    """)
    
    result = await db.execute(data_sql, params)
    rows = result.fetchall()
    
    items = []
    for row in rows:
        items.append({
            "id": row.id,
            "supplier_id": row.supplier_id,
            "supplier_code": row.supplier_code,
            "supplier_name": row.supplier_name,
            "original_filename": row.original_filename,
            "stored_filename": row.stored_filename,
            "file_path": row.file_path,
            "file_size_bytes": row.file_size_bytes,
            "file_type": row.file_type,
            "invoice_number": row.invoice_number,
            "invoice_date": row.invoice_date.isoformat() if row.invoice_date else None,
            "due_date": row.due_date.isoformat() if row.due_date else None,
            "currency": row.currency or "EUR",
            "total_amount": float(row.total_amount) if row.total_amount else None,
            "total_without_vat": float(row.total_without_vat) if row.total_without_vat else None,
            "vat_amount": float(row.vat_amount) if row.vat_amount else None,
            "total_with_vat": float(row.total_with_vat) if row.total_with_vat else None,
            "vat_rate": float(row.vat_rate) if row.vat_rate else 23.0,
            "vat_included": row.vat_included,
            "items_count": row.items_count or 0,
            "payment_status": row.payment_status or "unpaid",
            "paid_at": row.paid_at.isoformat() if row.paid_at else None,
            "paid_amount": float(row.paid_amount) if row.paid_amount else None,
            "is_parsed": row.is_parsed,
            "parse_error": row.parse_error,
            "receiving_session_id": row.receiving_session_id,
            "receiving_status": row.receiving_status,
            "notes": row.notes,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "is_overdue": row.is_overdue,
            "days_until_due": row.days_until_due,
        })
    
    # Summary stats
    summary_params = {k: v for k, v in params.items() if k not in ('limit', 'offset')}
    summary_sql = text(f"""
        SELECT 
            COUNT(*) as filtered_count,
            COALESCE(SUM(ui.total_amount), 0) as filtered_total,
            COUNT(*) FILTER (WHERE ui.payment_status = 'unpaid') as unpaid_count,
            COALESCE(SUM(ui.total_amount) FILTER (WHERE ui.payment_status = 'unpaid'), 0) as unpaid_total
        FROM uploaded_invoices ui
        JOIN suppliers s ON ui.supplier_id = s.id
        LEFT JOIN receiving_sessions rs ON ui.receiving_session_id = rs.id
        WHERE {where_clause}
    """)
    
    summary_result = await db.execute(summary_sql, summary_params)
    summary_row = summary_result.fetchone()
    
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size if total > 0 else 1,
        "summary": {
            "filtered_count": summary_row.filtered_count if summary_row else 0,
            "filtered_total": float(summary_row.filtered_total) if summary_row else 0,
            "unpaid_count": summary_row.unpaid_count if summary_row else 0,
            "unpaid_total": float(summary_row.unpaid_total) if summary_row else 0,
        }
    }


@router.get("/stats")
async def get_invoice_stats(db: AsyncSession = Depends(get_session)):
    """Get overall invoice statistics."""
    sql = text("""
        SELECT 
            COUNT(*) as total_invoices,
            COALESCE(SUM(total_amount), 0) as total_amount,
            COUNT(*) FILTER (WHERE payment_status = 'unpaid') as unpaid_count,
            COALESCE(SUM(total_amount) FILTER (WHERE payment_status = 'unpaid'), 0) as unpaid_amount,
            COUNT(*) FILTER (WHERE due_date < CURRENT_DATE AND payment_status != 'paid') as overdue_count,
            COALESCE(SUM(total_amount) FILTER (WHERE due_date < CURRENT_DATE AND payment_status != 'paid'), 0) as overdue_amount
        FROM uploaded_invoices
    """)
    
    result = await db.execute(sql)
    row = result.fetchone()
    
    return {
        "total_invoices": row.total_invoices if row else 0,
        "total_amount": float(row.total_amount) if row else 0,
        "unpaid_count": row.unpaid_count if row else 0,
        "unpaid_amount": float(row.unpaid_amount) if row else 0,
        "overdue_count": row.overdue_count if row else 0,
        "overdue_amount": float(row.overdue_amount) if row else 0,
    }


@router.get("/{invoice_id}")
async def get_invoice_detail(invoice_id: int, db: AsyncSession = Depends(get_session)):
    """Get invoice detail with line items."""
    invoice_sql = text("""
        SELECT 
            ui.*,
            s.code as supplier_code,
            s.name as supplier_name,
            CASE 
                WHEN ui.due_date IS NOT NULL AND ui.due_date < CURRENT_DATE AND ui.payment_status != 'paid'
                THEN true ELSE false 
            END as is_overdue,
            CASE 
                WHEN ui.due_date IS NOT NULL THEN ui.due_date - CURRENT_DATE 
                ELSE NULL 
            END as days_until_due
        FROM uploaded_invoices ui
        JOIN suppliers s ON ui.supplier_id = s.id
        WHERE ui.id = :id
    """)
    
    result = await db.execute(invoice_sql, {"id": invoice_id})
    row = result.fetchone()
    
    if not row:
        raise HTTPException(404, "Invoice not found")
    
    # Get line items
    lines_sql = text("""
        SELECT 
            il.*,
            p.sku as product_sku,
            p.name as matched_product_name,
            sp.name as supplier_product_name,
            sp.images as supplier_product_images
        FROM uploaded_invoice_lines il
        LEFT JOIN products p ON il.matched_product_id = p.id
        LEFT JOIN supplier_products sp ON il.matched_supplier_product_id = sp.id
        WHERE il.invoice_id = :invoice_id
        ORDER BY il.line_number
    """)
    
    lines_result = await db.execute(lines_sql, {"invoice_id": invoice_id})
    lines = []
    for line in lines_result.fetchall():
        lines.append({
            "id": line.id,
            "line_number": line.line_number,
            "ean": line.ean,
            "supplier_sku": line.supplier_sku,
            "product_name": line.product_name,
            "quantity": float(line.quantity) if line.quantity else 1,
            "unit": line.unit or "ks",
            "unit_price": float(line.unit_price) if line.unit_price else None,
            "discount_percent": float(line.discount_percent) if line.discount_percent else None,
            "total_price": float(line.total_price) if line.total_price else None,
            "vat_rate": float(line.vat_rate) if line.vat_rate else None,
            "unit_price_with_vat": float(line.unit_price_with_vat) if line.unit_price_with_vat else None,
            "total_price_with_vat": float(line.total_price_with_vat) if line.total_price_with_vat else None,
            "matched_product_id": line.matched_product_id,
            "matched_supplier_product_id": line.matched_supplier_product_id,
            "is_new_product": line.is_new_product,
            "product_sku": line.product_sku,
            "matched_product_name": line.matched_product_name,
            "supplier_product_name": line.supplier_product_name,
            "supplier_product_images": line.supplier_product_images,
        })
    
    return {
        "id": row.id,
        "supplier_id": row.supplier_id,
        "supplier_code": row.supplier_code,
        "supplier_name": row.supplier_name,
        "original_filename": row.original_filename,
        "stored_filename": row.stored_filename,
        "file_path": row.file_path,
        "file_size_bytes": row.file_size_bytes,
        "file_type": row.file_type,
        "invoice_number": row.invoice_number,
        "invoice_date": row.invoice_date.isoformat() if row.invoice_date else None,
        "due_date": row.due_date.isoformat() if row.due_date else None,
        "currency": row.currency or "EUR",
        "total_amount": float(row.total_amount) if row.total_amount else None,
        "total_without_vat": float(row.total_without_vat) if row.total_without_vat else None,
        "vat_amount": float(row.vat_amount) if row.vat_amount else None,
        "total_with_vat": float(row.total_with_vat) if row.total_with_vat else None,
        "vat_rate": float(row.vat_rate) if row.vat_rate else 23.0,
        "vat_included": row.vat_included,
        "items_count": row.items_count or len(lines),
        "payment_status": row.payment_status,
        "paid_at": row.paid_at.isoformat() if row.paid_at else None,
        "paid_amount": float(row.paid_amount) if row.paid_amount else None,
        "is_parsed": row.is_parsed,
        "parse_error": row.parse_error,
        "receiving_session_id": row.receiving_session_id,
        "notes": row.notes,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "is_overdue": row.is_overdue,
        "days_until_due": row.days_until_due,
        "lines": lines,
    }


@router.get("/{invoice_id}/download")
async def download_invoice(invoice_id: int, db: AsyncSession = Depends(get_session)):
    """Download original invoice file."""
    sql = text("SELECT file_path, original_filename FROM uploaded_invoices WHERE id = :id")
    result = await db.execute(sql, {"id": invoice_id})
    row = result.fetchone()
    
    if not row:
        raise HTTPException(404, "Invoice not found")
    
    file_path = Path(row.file_path)
    if not file_path.exists():
        raise HTTPException(404, "File not found on disk")
    
    return FileResponse(
        path=str(file_path),
        filename=row.original_filename,
        media_type="application/octet-stream"
    )


@router.patch("/{invoice_id}")
async def update_invoice(invoice_id: int, request: InvoiceUpdateRequest, db: AsyncSession = Depends(get_session)):
    """Update invoice metadata (manual corrections)."""
    check_sql = text("SELECT id FROM uploaded_invoices WHERE id = :id")
    result = await db.execute(check_sql, {"id": invoice_id})
    if not result.fetchone():
        raise HTTPException(404, "Invoice not found")
    
    updates = []
    params = {"id": invoice_id}
    
    if request.invoice_number is not None:
        updates.append("invoice_number = :invoice_number")
        params["invoice_number"] = request.invoice_number
    
    if request.invoice_date is not None:
        updates.append("invoice_date = :invoice_date")
        params["invoice_date"] = request.invoice_date
    
    if request.due_date is not None:
        updates.append("due_date = :due_date")
        params["due_date"] = request.due_date
    
    if request.currency is not None:
        updates.append("currency = :currency")
        params["currency"] = request.currency
    
    if request.total_amount is not None:
        updates.append("total_amount = :total_amount")
        params["total_amount"] = request.total_amount
    
    if request.vat_rate is not None:
        updates.append("vat_rate = :vat_rate")
        params["vat_rate"] = request.vat_rate
    
    if request.vat_included is not None:
        updates.append("vat_included = :vat_included")
        params["vat_included"] = request.vat_included
    
    if request.items_count is not None:
        updates.append("items_count = :items_count")
        params["items_count"] = request.items_count
    
    if request.notes is not None:
        updates.append("notes = :notes")
        params["notes"] = request.notes
    
    if not updates:
        raise HTTPException(400, "No fields to update")
    
    updates.append("updated_at = NOW()")
    
    update_sql = text(f"UPDATE uploaded_invoices SET {', '.join(updates)} WHERE id = :id")
    await db.execute(update_sql, params)
    await db.commit()
    
    return {"id": invoice_id, "message": "Invoice updated"}


@router.patch("/{invoice_id}/payment")
async def update_payment_status(invoice_id: int, request: PaymentUpdateRequest, db: AsyncSession = Depends(get_session)):
    """Update invoice payment status."""
    check_sql = text("SELECT id, total_amount FROM uploaded_invoices WHERE id = :id")
    result = await db.execute(check_sql, {"id": invoice_id})
    row = result.fetchone()
    
    if not row:
        raise HTTPException(404, "Invoice not found")
    
    params = {"id": invoice_id, "payment_status": request.payment_status.value}
    update_parts = ["payment_status = :payment_status", "updated_at = NOW()"]
    
    if request.paid_amount is not None:
        update_parts.append("paid_amount = :paid_amount")
        params["paid_amount"] = request.paid_amount
    
    if request.payment_status == PaymentStatus.paid:
        update_parts.append("paid_at = COALESCE(:paid_at, NOW())")
        params["paid_at"] = request.paid_at
        if request.paid_amount is None and row.total_amount:
            update_parts.append("paid_amount = :auto_amount")
            params["auto_amount"] = row.total_amount
    
    update_sql = text(f"UPDATE uploaded_invoices SET {', '.join(update_parts)} WHERE id = :id")
    await db.execute(update_sql, params)
    await db.commit()
    
    return {"id": invoice_id, "payment_status": request.payment_status.value}


@router.delete("/{invoice_id}")
async def delete_invoice(invoice_id: int, delete_file: bool = Query(False), db: AsyncSession = Depends(get_session)):
    """Delete invoice record (optionally with file)."""
    sql = text("SELECT file_path FROM uploaded_invoices WHERE id = :id")
    result = await db.execute(sql, {"id": invoice_id})
    row = result.fetchone()
    
    if not row:
        raise HTTPException(404, "Invoice not found")
    
    # Delete lines first
    await db.execute(text("DELETE FROM uploaded_invoice_lines WHERE invoice_id = :id"), {"id": invoice_id})
    
    # Delete invoice
    delete_sql = text("DELETE FROM uploaded_invoices WHERE id = :id")
    await db.execute(delete_sql, {"id": invoice_id})
    await db.commit()
    
    # Delete file if requested
    if delete_file and row.file_path:
        file_path = Path(row.file_path)
        if file_path.exists():
            file_path.unlink()
    
    return {"id": invoice_id, "deleted": True, "file_deleted": delete_file}
