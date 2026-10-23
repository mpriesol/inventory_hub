# inventory_hub/routers/invoices_unified.py
"""
Unified Invoice Management Router - uses uploaded_invoices table.

Upload → Save file + DB record → List in /invoices
receiving_sessions is created only when "Príjem" workflow starts.
"""
from __future__ import annotations
import asyncio
import os
import re
from pathlib import Path
from datetime import datetime, date
from decimal import Decimal
from enum import Enum
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Query, UploadFile, File, Form, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from inventory_hub.access import operator_access
from inventory_hub.file_access import stored_document_path, document_response
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from inventory_hub.database import get_session
from inventory_hub.db_models import Supplier
from inventory_hub.services import invoice_finance as finance

class InvoiceRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request):
            try:
                response = await original(request)
            except RequestValidationError:
                response = JSONResponse(status_code=422, content={"detail": {
                    "code": "invoice_invalid_request", "known_unapplied": True}})
            except HTTPException as error:
                error.headers = {**(error.headers or {}), "Cache-Control": "no-store"}
                raise
            response.headers["Cache-Control"] = "no-store"
            return response
        return handler


router = APIRouter(prefix="/invoices", tags=["Invoice Management"], route_class=InvoiceRoute,
                   dependencies=[Depends(operator_access)])

# ============================================================================
# Constants
# ============================================================================

from inventory_hub.settings import settings

INVOICE_STORAGE_BASE = Path(settings.INVENTORY_DATA_ROOT) / "suppliers"
ALLOWED_EXTENSIONS = {'.pdf', '.csv', '.tsv', '.xlsx', '.xls', '.doc', '.docx', '.xml', '.txt', '.jpg', '.jpeg', '.png'}

# ============================================================================
# Pydantic Models
# ============================================================================

class PaymentStatus(str, Enum):
    unpaid = "unpaid"
    partial = "partial"
    paid = "paid"


class InvoiceUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: StrictInt = Field(ge=0)
    invoice_number: Optional[str] = None
    invoice_date: Optional[date] = None
    due_date: Optional[date] = None
    currency: Optional[str] = None
    total_amount: Optional[Decimal] = None
    vat_rate: Optional[Decimal] = None
    vat_included: Optional[bool] = None
    items_count: Optional[int] = Field(default=None, ge=0)
    notes: Optional[str] = None

    @field_validator("total_amount", "vat_rate")
    @classmethod
    def exact_amount(cls, value):
        return None if value is None else finance.amount_value(value)

    @field_validator("vat_rate")
    @classmethod
    def tax_rate(cls, value):
        if value is not None and not 0 <= value <= 100:
            raise ValueError("VAT rate must be between zero and 100")
        return value

    @field_validator("currency")
    @classmethod
    def currency_code(cls, value):
        if value is not None:
            value = value.upper()
            if not re.fullmatch(r"[A-Z]{3}", value):
                raise ValueError("Use a three-letter currency code")
        return value


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
            COALESCE(COUNT(ui.id), 0) as invoice_count
        FROM suppliers s
        LEFT OUTER JOIN uploaded_invoices ui ON s.id = ui.supplier_id
        WHERE s.is_active = true
        GROUP BY s.id, s.code, s.name
        ORDER BY s.name
    """)
    
    result = await db.execute(sql)
    financial = await finance.financial_rows(db, conditions="ui.direction='purchase'")
    suppliers = [
        {
            "id": row.id,
            "code": row.code,
            "name": row.name,
            "invoice_count": row.invoice_count,
            **finance.summarize([item for item in financial if item['supplier_id'] == row.id]),
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
    """Archive the original and supported invoice lines without changing stock."""
    from hashlib import sha256
    from inventory_hub.services.invoice_archive import ingest_invoice, parse_uploaded_document, InvoiceArchiveError, MAX_FILE
    if not file.filename:
        raise HTTPException(400, "No filename provided")
    if Path(file.filename).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(422, detail={"code": "invoice_file_type_unsupported", "known_unapplied": True})
    supplier = await db.scalar(select(Supplier).where(Supplier.code == supplier_code))
    if supplier is None:
        raise HTTPException(404, "Supplier not found")
    try:
        parsed_invoice_date = date.fromisoformat(invoice_date) if invoice_date else None
        parsed_due_date = date.fromisoformat(due_date) if due_date else None
    except ValueError:
        raise HTTPException(422, detail={"code": "invoice_date_invalid", "known_unapplied": True}) from None
    content = await file.read(MAX_FILE + 1)
    if not content or len(content) > MAX_FILE:
        raise HTTPException(422, detail={"code": "invoice_file_invalid", "known_unapplied": True})
    metadata, lines = await asyncio.to_thread(parse_uploaded_document, supplier.code, file.filename, content)
    supplied = {"invoice_number": invoice_number, "invoice_date": parsed_invoice_date, "due_date": parsed_due_date}
    for key, value in supplied.items():
        if value is not None and metadata.get(key) is not None and value != metadata[key]:
            raise HTTPException(422, detail={"code": "invoice_source_metadata_conflict", "known_unapplied": True})
        if value is not None:
            metadata[key] = value
    metadata.setdefault("invoice_number", _extract_invoice_number(file.filename))
    try:
        result = await ingest_invoice(db, direction="purchase", source_kind="upload",
            source_key="sha256:" + sha256(content).hexdigest(), supplier_id=supplier.id,
            metadata=metadata, lines=lines, files=[{"filename": file.filename, "content": content}],
            source_data={"kind": "manual-upload"})
        if not result["replayed"]:
            await db.execute(text("UPDATE uploaded_invoices SET vat_included=:included WHERE id=:id"),
                             {"id": result["id"], "included": vat_included})
        await db.commit()
    except InvoiceArchiveError as error:
        await db.rollback()
        raise HTTPException(error.status, detail={"code": error.code, "known_unapplied": True}) from None
    return {**result, "supplier_code": supplier_code, "original_filename": file.filename,
            "invoice_number": metadata["invoice_number"], "message": "Invoice archived"}


@router.get("")
async def list_invoices(
    direction: Literal["purchase", "sale"] = Query("purchase"),
    # Filter row parameters
    f_invoice_number: Optional[str] = Query(None, description="Invoice number (partial match)"),
    f_supplier: Optional[str] = Query(None, description="Supplier name or code (partial match)"),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    due_from: Optional[date] = Query(None),
    due_to: Optional[date] = Query(None),
    f_amount_min: Optional[Decimal] = Query(None),
    f_amount_max: Optional[Decimal] = Query(None),
    f_items_min: Optional[int] = Query(None),
    f_items_max: Optional[int] = Query(None),
    f_currency: Optional[str] = Query(None),
    payment_status: Optional[Literal["unpaid", "partial", "paid", "unknown"]] = Query(None),
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
    conditions = ["ui.direction = :direction"]
    params = {"direction": direction}
    
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
        conditions.append("ui.total_amount >= :amount_min")
        params["amount_min"] = f_amount_min
    
    if f_amount_max is not None:
        conditions.append("ui.total_amount <= :amount_max")
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
    
    if receiving_status:
        if receiving_status == "not_started":
            conditions.append("ui.receiving_session_id IS NULL")
        else:
            conditions.append("rs.status = :receiving_status")
            params["receiving_status"] = receiving_status
    
    where_clause = " AND ".join(conditions) if conditions else "1=1"
    financial_rows = await finance.financial_rows(db, conditions=where_clause, params=params)
    finances = {row["id"]: finance.snapshot(row) for row in financial_rows}
    financial_rows = [row for row in financial_rows
        if (payment_status is None or finances[row["id"]]["status"] == payment_status)
        and (is_overdue is None or finances[row["id"]]["is_overdue"] == is_overdue)]
    if payment_status is not None or is_overdue is not None:
        conditions.append("ui.id = ANY(:financial_ids)")
        params["financial_ids"] = [row["id"] for row in financial_rows]
        where_clause = " AND ".join(conditions)
    
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
        LEFT JOIN suppliers s ON ui.supplier_id = s.id
        LEFT JOIN receiving_sessions rs ON ui.receiving_session_id = rs.id AND rs.supplier_id = ui.supplier_id
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
            ui.direction, ui.document_type, ui.source_kind, ui.payment_revision,
            sh.code as shop_code, sh.name as shop_name,
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
        LEFT JOIN suppliers s ON ui.supplier_id = s.id
        LEFT JOIN shops sh ON ui.shop_id = sh.id
        LEFT JOIN receiving_sessions rs ON ui.receiving_session_id = rs.id AND rs.supplier_id = ui.supplier_id
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
            "shop_code": row.shop_code,
            "shop_name": row.shop_name,
            "direction": row.direction,
            "document_type": row.document_type,
            "source_kind": row.source_kind,
            "payment_revision": row.payment_revision,
            "finance": finances[row.id],
            "original_filename": row.original_filename,
            "stored_filename": row.stored_filename,
            "file_path": row.file_path,
            "file_size_bytes": row.file_size_bytes,
            "file_type": row.file_type,
            "invoice_number": row.invoice_number,
            "invoice_date": row.invoice_date.isoformat() if row.invoice_date else None,
            "due_date": row.due_date.isoformat() if row.due_date else None,
            "currency": row.currency,
            "total_amount": float(row.total_amount) if row.total_amount is not None else None,
            "total_without_vat": float(row.total_without_vat) if row.total_without_vat is not None else None,
            "vat_amount": float(row.vat_amount) if row.vat_amount is not None else None,
            "total_with_vat": float(row.total_with_vat) if row.total_with_vat is not None else None,
            "vat_rate": float(row.vat_rate) if row.vat_rate is not None else None,
            "vat_included": row.vat_included,
            "items_count": row.items_count or 0,
            "payment_status": finances[row.id]["status"],
            "paid_at": row.paid_at.isoformat() if row.paid_at else None,
            "paid_amount": float(finances[row.id]["paid_amount"]) if finances[row.id]["paid_amount"] is not None else None,
            "is_parsed": row.is_parsed,
            "parse_error": row.parse_error,
            "receiving_session_id": row.receiving_session_id,
            "receiving_status": row.receiving_status,
            "notes": row.notes,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "is_overdue": finances[row.id]["is_overdue"],
            "days_until_due": row.days_until_due,
        })
    
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size if total > 0 else 1,
        "direction": direction,
        "summary": finance.summarize(financial_rows),
    }


@router.get("/stats")
async def get_invoice_stats(direction: Literal["purchase", "sale"] = Query("purchase"), db: AsyncSession = Depends(get_session)):
    """Get overall invoice statistics."""
    rows = await finance.financial_rows(db, conditions="ui.direction=:direction", params={"direction": direction})
    return {"direction": direction, **finance.summarize(rows)}


@router.get("/{invoice_id}")
async def get_invoice_detail(invoice_id: int, db: AsyncSession = Depends(get_session)):
    """Get invoice detail with line items."""
    invoice_sql = text("""
        SELECT 
            ui.*,
            s.code as supplier_code,
            s.name as supplier_name,
            sh.code as shop_code,
            sh.name as shop_name,
            COALESCE(rs.status::text, 'not_started') as receiving_status,
            rs.invoice_number as receiving_invoice_number,
            CASE 
                WHEN ui.due_date IS NOT NULL AND ui.due_date < CURRENT_DATE AND ui.payment_status != 'paid'
                THEN true ELSE false 
            END as is_overdue,
            CASE 
                WHEN ui.due_date IS NOT NULL THEN ui.due_date - CURRENT_DATE 
                ELSE NULL 
            END as days_until_due
        FROM uploaded_invoices ui
        LEFT JOIN suppliers s ON ui.supplier_id = s.id
        LEFT JOIN shops sh ON ui.shop_id = sh.id
        LEFT JOIN receiving_sessions rs ON rs.id = ui.receiving_session_id AND rs.supplier_id = ui.supplier_id
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
            "quantity": float(line.quantity) if line.quantity is not None else None,
            "unit": line.unit,
            "unit_price": float(line.unit_price) if line.unit_price is not None else None,
            "discount_percent": float(line.discount_percent) if line.discount_percent is not None else None,
            "total_price": float(line.total_price) if line.total_price is not None else None,
            "vat_rate": float(line.vat_rate) if line.vat_rate is not None else None,
            "unit_price_with_vat": float(line.unit_price_with_vat) if line.unit_price_with_vat is not None else None,
            "total_price_with_vat": float(line.total_price_with_vat) if line.total_price_with_vat is not None else None,
            "matched_product_id": line.matched_product_id,
            "matched_supplier_product_id": line.matched_supplier_product_id,
            "is_new_product": line.is_new_product,
            "product_sku": line.product_sku,
            "matched_product_name": line.matched_product_name,
            "supplier_product_name": line.supplier_product_name,
            "supplier_product_images": line.supplier_product_images,
        })
    
    financial = await finance.detail(db, row._mapping)
    return {
        "id": row.id,
        "supplier_id": row.supplier_id,
        "supplier_code": row.supplier_code,
        "supplier_name": row.supplier_name,
        "shop_code": row.shop_code,
        "shop_name": row.shop_name,
        "direction": row.direction,
        "source_kind": row.source_kind,
        "document_type": row.document_type,
        "payment_revision": row.payment_revision,
        "finance": financial,
        "original_filename": row.original_filename,
        "stored_filename": row.stored_filename,
        "file_path": row.file_path,
        "file_size_bytes": row.file_size_bytes,
        "file_type": row.file_type,
        "invoice_number": row.invoice_number,
        "invoice_date": row.invoice_date.isoformat() if row.invoice_date else None,
        "due_date": row.due_date.isoformat() if row.due_date else None,
        "currency": row.currency,
        "total_amount": float(row.total_amount) if row.total_amount is not None else None,
        "total_without_vat": float(row.total_without_vat) if row.total_without_vat is not None else None,
        "vat_amount": float(row.vat_amount) if row.vat_amount is not None else None,
        "total_with_vat": float(row.total_with_vat) if row.total_with_vat is not None else None,
        "vat_rate": float(row.vat_rate) if row.vat_rate is not None else None,
        "vat_included": row.vat_included,
        "items_count": row.items_count if row.items_count is not None else len(lines),
        "payment_status": financial["status"],
        "paid_at": row.paid_at.isoformat() if row.paid_at else None,
        "paid_amount": float(financial["paid_amount"]) if financial["paid_amount"] is not None else None,
        "is_parsed": row.is_parsed,
        "parse_error": row.parse_error,
        "receiving_session_id": row.receiving_session_id,
        "receiving_status": row.receiving_status,
        "receiving_invoice_number": row.receiving_invoice_number,
        "notes": row.notes,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "is_overdue": financial["is_overdue"],
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
    
    file_path = stored_document_path(settings.INVENTORY_DATA_ROOT, row.file_path)
    return document_response(file_path, filename=row.original_filename)



async def _finance_call(db, operation, *, commit=False):
    try:
        response = await operation
        if commit:
            await db.commit()
        return response
    except finance.FinanceError as error:
        await db.rollback()
        raise HTTPException(error.status, detail={"code": error.code,
                            "known_unapplied": error.code != "invoice_payment_request_conflict"},
                            headers={"Cache-Control": "no-store"}) from None


def _actor(request):
    user = getattr(request.state, "hub_user", None)
    return f"user:{user['id']}" if user and user.get("id") is not None else "operator-token"


@router.patch("/{invoice_id}")
async def update_invoice(invoice_id: int, request: InvoiceUpdateRequest, db: AsyncSession = Depends(get_session)):
    """Correct archive metadata under the same revision as manual settlements."""
    changes = request.model_dump(exclude_unset=True, exclude={"expected_revision"})
    return await _finance_call(db, finance.update_metadata(db, invoice_id, changes, request.expected_revision), commit=True)


@router.patch("/{invoice_id}/payment")
async def update_payment_status(invoice_id: int, request: PaymentUpdateRequest, db: AsyncSession = Depends(get_session)):
    """A status flag cannot replace a recorded settlement or its reversal."""
    raise HTTPException(409, detail={"code": "invoice_payment_ledger_required", "known_unapplied": True})


@router.post("/{invoice_id}/payments")
async def record_invoice_payment(invoice_id: int, payload: finance.PaymentRequest, request: Request,
                                 db: AsyncSession = Depends(get_session)):
    return await _finance_call(db, finance.apply_payment(db, invoice_id, payload, actor=_actor(request)), commit=True)


@router.post("/{invoice_id}/payments/{payment_id}/reverse")
async def reverse_invoice_payment(invoice_id: int, payment_id: int, payload: finance.ReversalRequest,
                                  request: Request, db: AsyncSession = Depends(get_session)):
    return await _finance_call(db, finance.apply_payment(db, invoice_id, payload,
        actor=_actor(request), reverses_payment_id=payment_id), commit=True)


@router.get("/{invoice_id}/payment-operations/{request_id}")
async def invoice_payment_result(invoice_id: int, request_id: UUID, db: AsyncSession = Depends(get_session)):
    return await _finance_call(db, finance.operation_result(db, invoice_id, request_id))


@router.delete("/{invoice_id}")
async def delete_invoice(invoice_id: int, delete_file: bool = Query(False), db: AsyncSession = Depends(get_session)):
    """Delete invoice record (optionally with file)."""
    sql = text("SELECT file_path FROM uploaded_invoices WHERE id = :id FOR UPDATE")
    result = await db.execute(sql, {"id": invoice_id})
    row = result.fetchone()
    
    if not row:
        raise HTTPException(404, "Invoice not found")
    if await db.scalar(text("SELECT id FROM invoice_payments WHERE invoice_id=:id LIMIT 1"), {"id": invoice_id}):
        raise HTTPException(409, detail={"code": "invoice_has_payment_history", "known_unapplied": True})
    if await db.scalar(text("SELECT id FROM invoice_files WHERE invoice_id=:id LIMIT 1"), {"id": invoice_id}):
        raise HTTPException(409, detail={"code": "invoice_has_archive_history", "known_unapplied": True})
    file_path = stored_document_path(settings.INVENTORY_DATA_ROOT, row.file_path) if delete_file and row.file_path else None
    
    # Delete lines first
    await db.execute(text("DELETE FROM uploaded_invoice_lines WHERE invoice_id = :id"), {"id": invoice_id})
    
    # Delete invoice
    delete_sql = text("DELETE FROM uploaded_invoices WHERE id = :id")
    await db.execute(delete_sql, {"id": invoice_id})
    await db.commit()
    
    # Delete file if requested
    if file_path is not None:
        file_path.unlink(missing_ok=True)
    
    return {"id": invoice_id, "deleted": True, "file_deleted": delete_file}
