# inventory_hub/routers/invoices_unified.py
"""
Unified Invoice Management Router - PostgreSQL-backed.

Provides:
- Unified listing of invoices from ALL suppliers
- Invoice detail with line items
- Payment status management
- Product matching preview
- Basic reporting/statistics
"""
from __future__ import annotations
from fastapi import APIRouter, HTTPException, Depends, Query, Body
from typing import Any, Dict, Optional, List
from datetime import datetime, date, timedelta
from decimal import Decimal
from enum import Enum
import json

from sqlalchemy import select, func, case, and_, or_, desc, asc, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, joinedload
from pydantic import BaseModel, Field

from inventory_hub.database import get_session
from inventory_hub.settings import settings
from inventory_hub.db_models import (
    Supplier, Warehouse, Product, ProductIdentifier, IdentifierType,
    ReceivingStatus
)
from inventory_hub.db_models_ext import (
    ReceivingSession, ReceivingLine, SupplierProduct
)

router = APIRouter(prefix="/invoices", tags=["Invoice Management"])


# ============================================================================
# Enums & Models
# ============================================================================

class PaymentStatus(str, Enum):
    unpaid = "unpaid"
    partial = "partial"
    paid = "paid"


class InvoiceSortField(str, Enum):
    invoice_date = "invoice_date"
    due_date = "due_date"
    total = "total"
    supplier = "supplier"
    created_at = "created_at"
    invoice_number = "invoice_number"


class InvoiceListFilters(BaseModel):
    supplier_id: Optional[int] = None
    supplier_code: Optional[str] = None
    payment_status: Optional[PaymentStatus] = None
    receiving_status: Optional[str] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    is_overdue: Optional[bool] = None
    search: Optional[str] = None  # invoice number search


class InvoiceListResponse(BaseModel):
    items: List[Dict[str, Any]]
    total: int
    page: int
    page_size: int
    total_pages: int
    summary: Dict[str, Any]


class PaymentUpdateRequest(BaseModel):
    payment_status: PaymentStatus
    paid_amount: Optional[Decimal] = None
    paid_at: Optional[datetime] = None


class InvoiceStatsResponse(BaseModel):
    total_invoices: int
    total_amount: Decimal
    unpaid_count: int
    unpaid_amount: Decimal
    overdue_count: int
    overdue_amount: Decimal
    by_supplier: List[Dict[str, Any]]
    by_month: List[Dict[str, Any]]


# ============================================================================
# Helper Functions
# ============================================================================

def _payment_status_to_enum(status: str) -> str:
    """Convert payment status string to enum value."""
    mapping = {
        "unpaid": "unpaid",
        "partial": "partial", 
        "paid": "paid",
    }
    return mapping.get(status, "unpaid")


def _serialize_invoice(session: ReceivingSession, supplier: Supplier, warehouse: Warehouse) -> Dict[str, Any]:
    """Serialize a receiving session as an invoice."""
    # Calculate overdue status
    is_overdue = False
    days_until_due = None
    
    if session.due_date:
        days_until_due = (session.due_date - date.today()).days
        is_overdue = days_until_due < 0 and getattr(session, 'payment_status', 'unpaid') != 'paid'
    
    return {
        "id": session.id,
        "invoice_number": session.invoice_number,
        "invoice_date": session.invoice_date.isoformat() if session.invoice_date else None,
        "due_date": session.due_date.isoformat() if hasattr(session, 'due_date') and session.due_date else None,
        "currency": session.invoice_currency,
        "total_amount": float(session.total_amount) if session.total_amount else None,
        "total_without_vat": float(session.total_without_vat) if hasattr(session, 'total_without_vat') and session.total_without_vat else None,
        "vat_amount": float(session.computed_vat) if hasattr(session, 'computed_vat') and session.computed_vat else None,
        "total_with_vat": float(session.total_with_vat) if hasattr(session, 'total_with_vat') and session.total_with_vat else None,
        "vat_rate": float(session.vat_rate) if hasattr(session, 'vat_rate') and session.vat_rate else 23.0,
        "vat_included": session.vat_included if hasattr(session, 'vat_included') else True,
        "payment_status": getattr(session, 'payment_status', 'unpaid'),
        "paid_at": session.paid_at.isoformat() if hasattr(session, 'paid_at') and session.paid_at else None,
        "paid_amount": float(session.paid_amount) if hasattr(session, 'paid_amount') and session.paid_amount else None,
        "receiving_status": session.status.value if session.status else "new",
        "items_count": session.total_lines or 0,
        "supplier": {
            "id": supplier.id,
            "code": supplier.code,
            "name": supplier.name,
        },
        "warehouse": {
            "id": warehouse.id,
            "code": warehouse.code,
            "name": warehouse.name,
        },
        "invoice_file_path": session.invoice_file_path,
        "is_overdue": is_overdue,
        "days_until_due": days_until_due,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "updated_at": session.updated_at.isoformat() if session.updated_at else None,
    }


def _serialize_line(line: ReceivingLine, product: Optional[Product] = None, supplier_product: Optional[SupplierProduct] = None) -> Dict[str, Any]:
    """Serialize an invoice line with product info."""
    return {
        "id": line.id,
        "line_number": line.line_number,
        "ean": line.ean,
        "supplier_sku": line.supplier_sku,
        "description": line.description,
        "ordered_qty": float(line.ordered_qty) if line.ordered_qty else 0,
        "received_qty": float(line.received_qty) if line.received_qty else 0,
        "unit_price": float(line.unit_price) if line.unit_price else None,
        "total_price": float(line.total_price) if line.total_price else None,
        "unit_price_with_vat": float(line.unit_price_with_vat) if hasattr(line, 'unit_price_with_vat') and line.unit_price_with_vat else None,
        "total_price_with_vat": float(line.total_price_with_vat) if hasattr(line, 'total_price_with_vat') and line.total_price_with_vat else None,
        "vat_rate": float(line.vat_rate) if hasattr(line, 'vat_rate') and line.vat_rate else None,
        "status": line.status,
        "is_new_product": getattr(line, 'is_new_product', False),
        "product_image_url": getattr(line, 'product_image_url', None),
        "product": {
            "id": product.id,
            "sku": product.sku,
            "name": product.name,
            "brand": product.brand,
        } if product else None,
        "supplier_product": {
            "id": supplier_product.id,
            "name": supplier_product.name,
            "images": supplier_product.images,
            "purchase_price": float(supplier_product.purchase_price) if supplier_product.purchase_price else None,
        } if supplier_product else None,
        "match_method": line.match_method,
    }


# ============================================================================
# Endpoints
# ============================================================================

@router.get("", response_model=InvoiceListResponse)
async def list_invoices(
    # Filters
    supplier_id: Optional[int] = Query(None),
    supplier_code: Optional[str] = Query(None),
    payment_status: Optional[str] = Query(None),
    receiving_status: Optional[str] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    is_overdue: Optional[bool] = Query(None),
    search: Optional[str] = Query(None),
    # Sorting
    sort_by: InvoiceSortField = Query(InvoiceSortField.invoice_date),
    sort_order: str = Query("desc"),
    # Pagination
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_session),
):
    """
    List all invoices with filtering, sorting, and pagination.
    
    Returns unified invoice list from all suppliers.
    """
    # Base query
    stmt = (
        select(ReceivingSession)
        .options(
            joinedload(ReceivingSession.supplier),
            joinedload(ReceivingSession.warehouse)
        )
    )
    count_stmt = select(func.count(ReceivingSession.id))
    
    # Apply filters
    conditions = []
    
    if supplier_id:
        conditions.append(ReceivingSession.supplier_id == supplier_id)
    
    if supplier_code:
        subq = select(Supplier.id).where(Supplier.code == supplier_code)
        conditions.append(ReceivingSession.supplier_id.in_(subq))
    
    if payment_status:
        # Handle payment_status as text column (may be enum or string)
        conditions.append(text(f"payment_status = '{payment_status}'"))
    
    if receiving_status:
        try:
            status_enum = ReceivingStatus(receiving_status)
            conditions.append(ReceivingSession.status == status_enum)
        except ValueError:
            pass
    
    if date_from:
        conditions.append(ReceivingSession.invoice_date >= date_from)
    
    if date_to:
        conditions.append(ReceivingSession.invoice_date <= date_to)
    
    if is_overdue is not None:
        today = date.today()
        if is_overdue:
            conditions.append(and_(
                text("due_date IS NOT NULL"),
                text(f"due_date < '{today}'"),
                text("payment_status != 'paid'")
            ))
        else:
            conditions.append(or_(
                text("due_date IS NULL"),
                text(f"due_date >= '{today}'"),
                text("payment_status = 'paid'")
            ))
    
    if search:
        search_pattern = f"%{search}%"
        conditions.append(ReceivingSession.invoice_number.ilike(search_pattern))
    
    if conditions:
        stmt = stmt.where(and_(*conditions))
        count_stmt = count_stmt.where(and_(*conditions))
    
    # Get total count
    result = await db.execute(count_stmt)
    total = result.scalar() or 0
    
    # Apply sorting
    sort_column = {
        InvoiceSortField.invoice_date: ReceivingSession.invoice_date,
        InvoiceSortField.due_date: text("due_date"),
        InvoiceSortField.total: ReceivingSession.total_amount,
        InvoiceSortField.created_at: ReceivingSession.created_at,
        InvoiceSortField.invoice_number: ReceivingSession.invoice_number,
        InvoiceSortField.supplier: ReceivingSession.supplier_id,
    }.get(sort_by, ReceivingSession.invoice_date)
    
    if sort_order.lower() == "asc":
        stmt = stmt.order_by(asc(sort_column).nulls_last())
    else:
        stmt = stmt.order_by(desc(sort_column).nulls_last())
    
    # Apply pagination
    offset = (page - 1) * page_size
    stmt = stmt.offset(offset).limit(page_size)
    
    # Execute
    result = await db.execute(stmt)
    sessions = result.unique().scalars().all()
    
    # Serialize
    items = []
    for session in sessions:
        items.append(_serialize_invoice(session, session.supplier, session.warehouse))
    
    # Calculate summary for current filter
    summary_stmt = select(
        func.count(ReceivingSession.id).label("count"),
        func.coalesce(func.sum(ReceivingSession.total_amount), 0).label("total"),
    )
    if conditions:
        summary_stmt = summary_stmt.where(and_(*conditions))
    
    result = await db.execute(summary_stmt)
    summary_row = result.first()
    
    # Unpaid summary
    unpaid_conditions = conditions.copy() if conditions else []
    unpaid_conditions.append(text("payment_status = 'unpaid'"))
    unpaid_stmt = select(
        func.count(ReceivingSession.id).label("count"),
        func.coalesce(func.sum(ReceivingSession.total_amount), 0).label("total"),
    ).where(and_(*unpaid_conditions))
    
    result = await db.execute(unpaid_stmt)
    unpaid_row = result.first()
    
    return InvoiceListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size if total > 0 else 1,
        summary={
            "filtered_count": summary_row.count if summary_row else 0,
            "filtered_total": float(summary_row.total) if summary_row else 0,
            "unpaid_count": unpaid_row.count if unpaid_row else 0,
            "unpaid_total": float(unpaid_row.total) if unpaid_row else 0,
        }
    )


@router.get("/stats", response_model=InvoiceStatsResponse)
async def get_invoice_stats(
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    db: AsyncSession = Depends(get_session),
):
    """
    Get invoice statistics and summaries.
    """
    conditions = []
    if date_from:
        conditions.append(ReceivingSession.invoice_date >= date_from)
    if date_to:
        conditions.append(ReceivingSession.invoice_date <= date_to)
    
    base_where = and_(*conditions) if conditions else True
    
    # Total stats
    total_stmt = select(
        func.count(ReceivingSession.id).label("count"),
        func.coalesce(func.sum(ReceivingSession.total_amount), 0).label("total"),
    ).where(base_where)
    
    result = await db.execute(total_stmt)
    total_row = result.first()
    
    # Unpaid stats
    unpaid_stmt = select(
        func.count(ReceivingSession.id).label("count"),
        func.coalesce(func.sum(ReceivingSession.total_amount), 0).label("total"),
    ).where(and_(base_where, text("payment_status = 'unpaid'")))
    
    result = await db.execute(unpaid_stmt)
    unpaid_row = result.first()
    
    # Overdue stats
    today = date.today()
    overdue_stmt = select(
        func.count(ReceivingSession.id).label("count"),
        func.coalesce(func.sum(ReceivingSession.total_amount), 0).label("total"),
    ).where(and_(
        base_where,
        text(f"due_date < '{today}'"),
        text("payment_status != 'paid'"),
        text("due_date IS NOT NULL")
    ))
    
    result = await db.execute(overdue_stmt)
    overdue_row = result.first()
    
    # By supplier
    by_supplier_stmt = (
        select(
            Supplier.code,
            Supplier.name,
            func.count(ReceivingSession.id).label("count"),
            func.coalesce(func.sum(ReceivingSession.total_amount), 0).label("total"),
        )
        .join(Supplier, ReceivingSession.supplier_id == Supplier.id)
        .where(base_where)
        .group_by(Supplier.id)
        .order_by(desc("total"))
    )
    
    result = await db.execute(by_supplier_stmt)
    by_supplier = [
        {"code": row.code, "name": row.name, "count": row.count, "total": float(row.total)}
        for row in result.all()
    ]
    
    # By month (last 12 months)
    by_month_stmt = (
        select(
            func.date_trunc('month', ReceivingSession.invoice_date).label("month"),
            func.count(ReceivingSession.id).label("count"),
            func.coalesce(func.sum(ReceivingSession.total_amount), 0).label("total"),
        )
        .where(and_(
            base_where,
            ReceivingSession.invoice_date.isnot(None)
        ))
        .group_by("month")
        .order_by(desc("month"))
        .limit(12)
    )
    
    result = await db.execute(by_month_stmt)
    by_month = [
        {"month": row.month.isoformat() if row.month else None, "count": row.count, "total": float(row.total)}
        for row in result.all()
    ]
    
    return InvoiceStatsResponse(
        total_invoices=total_row.count if total_row else 0,
        total_amount=Decimal(str(total_row.total)) if total_row else Decimal("0"),
        unpaid_count=unpaid_row.count if unpaid_row else 0,
        unpaid_amount=Decimal(str(unpaid_row.total)) if unpaid_row else Decimal("0"),
        overdue_count=overdue_row.count if overdue_row else 0,
        overdue_amount=Decimal(str(overdue_row.total)) if overdue_row else Decimal("0"),
        by_supplier=by_supplier,
        by_month=by_month,
    )


@router.get("/{invoice_id}")
async def get_invoice_detail(
    invoice_id: int,
    db: AsyncSession = Depends(get_session),
):
    """
    Get detailed invoice with all line items.
    """
    # Get session with relations
    stmt = (
        select(ReceivingSession)
        .options(
            joinedload(ReceivingSession.supplier),
            joinedload(ReceivingSession.warehouse),
            selectinload(ReceivingSession.lines).selectinload(ReceivingLine.product),
        )
        .where(ReceivingSession.id == invoice_id)
    )
    
    result = await db.execute(stmt)
    session = result.unique().scalar_one_or_none()
    
    if not session:
        raise HTTPException(404, detail="Invoice not found")
    
    # Serialize invoice
    invoice = _serialize_invoice(session, session.supplier, session.warehouse)
    
    # Serialize lines with product info
    lines = []
    for line in sorted(session.lines, key=lambda x: x.line_number):
        # Try to get supplier product if matched
        supplier_product = None
        if hasattr(line, 'matched_supplier_product_id') and line.matched_supplier_product_id:
            sp_stmt = select(SupplierProduct).where(SupplierProduct.id == line.matched_supplier_product_id)
            sp_result = await db.execute(sp_stmt)
            supplier_product = sp_result.scalar_one_or_none()
        
        lines.append(_serialize_line(line, line.product, supplier_product))
    
    # Line statistics
    stats = {
        "total_lines": len(lines),
        "matched_products": sum(1 for l in lines if l.get("product")),
        "new_products": sum(1 for l in lines if l.get("is_new_product")),
        "pending_lines": sum(1 for l in lines if l.get("status") == "pending"),
        "matched_lines": sum(1 for l in lines if l.get("status") == "matched"),
        "partial_lines": sum(1 for l in lines if l.get("status") == "partial"),
    }
    
    return {
        "invoice": invoice,
        "lines": lines,
        "stats": stats,
    }


@router.patch("/{invoice_id}/payment")
async def update_payment_status(
    invoice_id: int,
    request: PaymentUpdateRequest,
    db: AsyncSession = Depends(get_session),
):
    """
    Update invoice payment status.
    """
    stmt = select(ReceivingSession).where(ReceivingSession.id == invoice_id)
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()
    
    if not session:
        raise HTTPException(404, detail="Invoice not found")
    
    # Update payment status (handle as text attribute)
    setattr(session, 'payment_status', request.payment_status.value)
    
    if request.paid_amount is not None:
        session.paid_amount = request.paid_amount
    
    if request.payment_status == PaymentStatus.paid:
        session.paid_at = request.paid_at or datetime.utcnow()
        if request.paid_amount is None and session.total_amount:
            session.paid_amount = session.total_amount
    
    await db.commit()
    
    return {
        "id": session.id,
        "invoice_number": session.invoice_number,
        "payment_status": request.payment_status.value,
        "paid_at": session.paid_at.isoformat() if session.paid_at else None,
        "paid_amount": float(session.paid_amount) if session.paid_amount else None,
    }


@router.get("/{invoice_id}/lines/{line_id}/match-candidates")
async def get_line_match_candidates(
    invoice_id: int,
    line_id: int,
    db: AsyncSession = Depends(get_session),
):
    """
    Get potential product matches for an invoice line.
    
    Searches by EAN, supplier SKU, and name similarity.
    """
    # Get line
    stmt = (
        select(ReceivingLine)
        .join(ReceivingSession)
        .options(joinedload(ReceivingLine.session).joinedload(ReceivingSession.supplier))
        .where(ReceivingLine.id == line_id, ReceivingSession.id == invoice_id)
    )
    result = await db.execute(stmt)
    line = result.unique().scalar_one_or_none()
    
    if not line:
        raise HTTPException(404, detail="Line not found")
    
    candidates = []
    supplier_id = line.session.supplier_id
    
    # Search by EAN
    if line.ean:
        ean_stmt = (
            select(Product)
            .join(ProductIdentifier)
            .where(
                ProductIdentifier.value == line.ean,
                ProductIdentifier.identifier_type.in_([IdentifierType.ean, IdentifierType.unverified_barcode])
            )
            .limit(5)
        )
        result = await db.execute(ean_stmt)
        for product in result.scalars().all():
            candidates.append({
                "product_id": product.id,
                "sku": product.sku,
                "name": product.name,
                "match_type": "ean",
                "match_value": line.ean,
                "confidence": 1.0,
            })
    
    # Search by supplier SKU
    if line.supplier_sku:
        sku_stmt = (
            select(Product)
            .join(ProductIdentifier)
            .where(
                ProductIdentifier.value == line.supplier_sku,
                ProductIdentifier.identifier_type == IdentifierType.supplier_sku,
                ProductIdentifier.supplier_id == supplier_id
            )
            .limit(5)
        )
        result = await db.execute(sku_stmt)
        for product in result.scalars().all():
            if not any(c["product_id"] == product.id for c in candidates):
                candidates.append({
                    "product_id": product.id,
                    "sku": product.sku,
                    "name": product.name,
                    "match_type": "supplier_sku",
                    "match_value": line.supplier_sku,
                    "confidence": 0.95,
                })
    
    # Search in supplier_products (from feed)
    if line.ean or line.supplier_sku:
        sp_conditions = []
        if line.ean:
            sp_conditions.append(SupplierProduct.ean.ilike(f"%{line.ean}%"))
        if line.supplier_sku:
            sp_conditions.append(SupplierProduct.supplier_sku == line.supplier_sku)
        
        sp_stmt = (
            select(SupplierProduct)
            .where(
                SupplierProduct.supplier_id == supplier_id,
                or_(*sp_conditions)
            )
            .limit(10)
        )
        result = await db.execute(sp_stmt)
        for sp in result.scalars().all():
            candidates.append({
                "supplier_product_id": sp.id,
                "supplier_sku": sp.supplier_sku,
                "name": sp.name,
                "images": sp.images,
                "purchase_price": float(sp.purchase_price) if sp.purchase_price else None,
                "match_type": "supplier_feed",
                "match_value": sp.ean or sp.supplier_sku,
                "confidence": 0.8,
            })
    
    return {
        "line": {
            "id": line.id,
            "ean": line.ean,
            "supplier_sku": line.supplier_sku,
            "description": line.description,
        },
        "candidates": candidates,
    }


@router.post("/{invoice_id}/lines/{line_id}/link-product")
async def link_line_to_product(
    invoice_id: int,
    line_id: int,
    product_id: Optional[int] = Body(None, embed=True),
    supplier_product_id: Optional[int] = Body(None, embed=True),
    is_new_product: bool = Body(False, embed=True),
    db: AsyncSession = Depends(get_session),
):
    """
    Link an invoice line to a product or mark as new.
    """
    stmt = (
        select(ReceivingLine)
        .where(ReceivingLine.id == line_id, ReceivingLine.session_id == invoice_id)
    )
    result = await db.execute(stmt)
    line = result.scalar_one_or_none()
    
    if not line:
        raise HTTPException(404, detail="Line not found")
    
    if product_id:
        # Verify product exists
        p_stmt = select(Product).where(Product.id == product_id)
        p_result = await db.execute(p_stmt)
        if not p_result.scalar_one_or_none():
            raise HTTPException(404, detail="Product not found")
        
        line.product_id = product_id
        line.is_new_product = False
        line.match_method = "manual"
    
    if supplier_product_id:
        line.matched_supplier_product_id = supplier_product_id
    
    if is_new_product:
        line.is_new_product = True
        line.product_id = None
    
    await db.commit()
    
    return {
        "id": line.id,
        "product_id": line.product_id,
        "matched_supplier_product_id": getattr(line, 'matched_supplier_product_id', None),
        "is_new_product": getattr(line, 'is_new_product', False),
        "match_method": line.match_method,
    }


@router.get("/suppliers")
async def list_suppliers_with_invoices(
    db: AsyncSession = Depends(get_session),
):
    """
    List all suppliers that have invoices.
    """
    stmt = (
        select(
            Supplier.id,
            Supplier.code,
            Supplier.name,
            func.count(ReceivingSession.id).label("invoice_count"),
            func.coalesce(func.sum(ReceivingSession.total_amount), 0).label("total_amount"),
        )
        .join(ReceivingSession, Supplier.id == ReceivingSession.supplier_id)
        .group_by(Supplier.id)
        .order_by(Supplier.name)
    )
    
    result = await db.execute(stmt)
    suppliers = [
        {
            "id": row.id,
            "code": row.code,
            "name": row.name,
            "invoice_count": row.invoice_count,
            "total_amount": float(row.total_amount),
        }
        for row in result.all()
    ]
    
    return {"suppliers": suppliers}
