# inventory_hub/routers/receiving_db.py
"""
Receiving Router - PostgreSQL-backed implementation for v12 FINAL.

Replaces JSON-based receiving.py with proper database operations.
"""
from __future__ import annotations
from fastapi import APIRouter, HTTPException, Body, Depends, Query
from typing import Any, Dict, Optional, List
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
import csv, io, re
import hashlib
import json
from uuid import UUID
import logging

from sqlalchemy import select, func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from inventory_hub.database import get_session
from inventory_hub.settings import settings
from inventory_hub.db_models import (
    Product, ProductIdentifier, IdentifierType, Supplier, Warehouse,
    ReceivingStatus, MovementType, SupplierProduct
)
from inventory_hub.db_models_ext import (
    ReceivingSession, ReceivingLine, ScanEvent, ScanSessionType, ScanStatus,
    StockMovement, StockBalance
)
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK, RemoteIdentity, load_identity_index, verified_barcodes
from inventory_hub.receiving_scan_models import ReceivingScanRequest
from inventory_hub.services.stock_balances import lock_stock_balances
from inventory_hub.services import fifo
from inventory_hub.services.stock_publication_gate import StockPublicationHoldError
from inventory_hub.config_io import load_supplier as load_supplier_config
from inventory_hub.config_io import claim_supplier_prefix
from inventory_hub.supplier_prefix import SupplierPrefixError, canonical_supplier_sku, get_supplier_prefix
from inventory_hub.routers.receiving import _update_invoice_status

router = APIRouter(tags=["Receiving"])
logger = logging.getLogger(__name__)

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
        return get_supplier_prefix(load_supplier_config(supplier_code, write_back_on_load=False))
    except SupplierPrefixError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": str(error)}) from None


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

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    invoice_id: str
    warehouse_code: Optional[str] = None


class ScanRequest(BaseModel):
    code: str = Field(min_length=1, max_length=100, pattern=r"\S")
    request_id: Optional[UUID] = None
    line_id: Optional[int] = Field(default=None, gt=0)
    qty: Decimal = Field(default=Decimal("1"), gt=0, max_digits=12, decimal_places=3)
    scanned_by: str = Field(default="scanner", min_length=1, max_length=100)


class SetQtyRequest(BaseModel):
    line_index: int
    received_qty: Decimal = Field(ge=0, max_digits=12, decimal_places=3)
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
    product = line.__dict__.get("product")  # Never trigger async lazy loading during serialization.
    product_code = product.sku if product is not None else (f"{prefix}{line.supplier_sku}" if line.supplier_sku else None)
    return {
        "id": line.id,
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
    db: AsyncSession, supplier_code: str, session_id: int, *, lock: bool = False
) -> ReceivingSession:
    stmt = (
        select(ReceivingSession)
        .options(selectinload(ReceivingSession.lines).selectinload(ReceivingLine.product))
        .options(selectinload(ReceivingSession.supplier))
        .where(ReceivingSession.id == session_id)
    )
    if lock:
        # All receiving mutations take this lock before reading line quantities.
        # Refresh cached ORM objects as another transaction may have committed
        # while this request was waiting for the lock.
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()
    if not session or session.supplier.code != supplier_code:
        raise HTTPException(404, detail="Session not found")
    return session


ACTIVE_STATUSES = (ReceivingStatus.new, ReceivingStatus.in_progress, ReceivingStatus.paused)


def _receiving_identity(supplier: Supplier, line: ReceivingLine, prefix: str) -> RemoteIdentity:
    sku = (line.supplier_sku or "").strip()
    return RemoteIdentity(0, f"{prefix}{sku}" if sku else "", barcodes=(line.ean or "",),
        supplier_id=supplier.id, supplier_sku=sku, expected_product_id=line.product_id)


def _identity_error(line: ReceivingLine, resolution) -> HTTPException:
    return HTTPException(409, detail={
        "code": "receiving_identity_conflict",
        "message": f"Riadok {line.line_number}: kód, EAN alebo uložené priradenie označujú odlišné produkty. Skontroluj identifikáciu produktu.",
        "line_number": line.line_number, "reasons": resolution.reasons,
        "candidate_product_ids": resolution.candidate_product_ids,
    })


async def _resolve_line(
    db: AsyncSession, supplier: Supplier, line: ReceivingLine, prefix: str,
) -> tuple[Optional[Product], Optional[str]]:
    identity = _receiving_identity(supplier, line, prefix)
    index = await load_identity_index(db, 0, [identity], local=True)
    resolution = index.resolve(identity)
    if resolution.status == "conflict":
        raise _identity_error(line, resolution)
    return index.products.get(resolution.product_id), resolution.matched_by


async def _ensure_product_for_line(
    db: AsyncSession,
    supplier: Supplier,
    line: ReceivingLine,
    prefix: str,
    invoice_no: str,
    identifier_service: ProductIdentifierService,
) -> tuple[Optional[Product], bool, Optional[str]]:
    """Revalidate every evidence source before linking or creating a receipt leaf.

    The caller holds IDENTITY_WRITE_LOCK, shared with catalog imports and shop
    pulls. An existing product_id is evidence to verify, never a bypass.
    """
    ean = (line.ean or "").strip()
    sku_raw = (line.supplier_sku or "").strip()
    if sku_raw:
        try:
            claim_supplier_prefix(supplier.code, prefix)
        except SupplierPrefixError as error:
            raise HTTPException(error.status, detail={"code": error.code, "message": str(error)}) from None
    product, matched_by = await _resolve_line(db, supplier, line, prefix)
    created = False
    if product is None:
        if not sku_raw:
            return None, False, "missing supplier SKU for new product"
        product_sku = canonical_supplier_sku(prefix, sku_raw)
        # Revalidate the final canonical code before creating a new product.
        identity = RemoteIdentity(0, product_sku, barcodes=(ean,))
        index = await load_identity_index(db, 0, [identity], local=True)
        resolution = index.resolve(identity)
        if resolution.status == "conflict":
            raise _identity_error(line, resolution)
        product = index.products.get(resolution.product_id)
        if product is None:
            product = Product(
                sku=product_sku, supplier_id=supplier.id,
                name=(line.description or product_sku)[:500],
                created_from_source=f"invoice:{invoice_no}", validation_required=True,
                validation_reason="auto-created from receiving finalize",
            )
            db.add(product)
            await db.flush()
            created = True
    line.match_method = "auto_created" if created else matched_by or "shared_sku"

    # Split compound EANs, preserving leading zeros and per-type uniqueness.
    # We deliberately never turn an unverified short barcode into global identity.
    identifiers = list(dict.fromkeys(identifier_service.split_compound_ean(ean)))
    identifiers = [item for item in identifiers if item[0].strip("0")]
    identifiers.sort(key=lambda item: item[0] not in verified_barcodes((ean,)))
    if sku_raw:
        identifiers.append((sku_raw, IdentifierType.supplier_sku))
    primary_set = bool(await identifier_service.get_primary_barcode(product.id))
    for value, kind in identifiers:
        conditions = [ProductIdentifier.product_id == product.id,
                      ProductIdentifier.value == value, ProductIdentifier.identifier_type == kind]
        if kind == IdentifierType.supplier_sku:
            conditions.append(ProductIdentifier.supplier_id == supplier.id)
        existing = await db.scalar(select(ProductIdentifier.id).where(*conditions))
        if existing is not None:
            continue
        primary = not primary_set and kind in identifier_service.BARCODE_TYPES
        try:
            async with db.begin_nested():
                await identifier_service.add_identifier(product.id, value, kind,
                    supplier_id=supplier.id if kind == IdentifierType.supplier_sku else None,
                    is_primary=primary)
        except IntegrityError:
            raise HTTPException(409, detail={"code": "receiving_identity_conflict",
                "message": f"Riadok {line.line_number}: identifikátor medzitým prevzal iný produkt. Príjem nebol dokončený.",
                "line_number": line.line_number}) from None
        primary_set = primary_set or primary

    if sku_raw:
        existing_sp = await db.scalar(select(SupplierProduct).where(
            SupplierProduct.supplier_id == supplier.id, SupplierProduct.supplier_sku == sku_raw))
        if existing_sp is None:
            db.add(SupplierProduct(supplier_id=supplier.id, supplier_sku=sku_raw,
                ean=ean or None, name=(line.description or product.sku)[:500], purchase_price=line.unit_price))
            await db.flush()
        from inventory_hub.services.supplier_links import reconcile_supplier_links
        report = await reconcile_supplier_links(db, supplier.code, product_ids=[product.id])
        if report["conflicts"]:
            raise HTTPException(409, detail={"code": "receiving_identity_conflict",
                "message": f"Riadok {line.line_number}: dodávateľský kód označuje iný produkt. Skontroluj prepojenie.",
                "line_number": line.line_number, "conflicts": report["conflicts"]})
    return product, created, None


async def _write_stock_for_line(
    db: AsyncSession,
    session: ReceivingSession,
    line: ReceivingLine,
    product: Product,
    supplier_code: str,
    balance: StockBalance,
) -> StockMovement:
    """
    Append immutable RECEIVING_IN stock movement and update stock balance
    (weighted-average cost) in the same transaction. Idempotent via
    unique idempotency_key per (session, line).
    """
    qty = line.received_qty
    unit_cost = line.unit_price

    old_qty = balance.qty_on_hand or Decimal("0")
    old_avg = balance.avg_cost
    state = await fifo.lock_state(db, balance)
    if state is None and old_avg is None:
        raise HTTPException(409, detail={"code": "fifo_cutover_required", "message": "fifo_cutover_required"})
    new_qty = old_qty + qty
    if new_qty > fifo.MAX_QUANTITY:
        raise HTTPException(409, detail={"code": "fifo_value_out_of_range", "message": "fifo_value_out_of_range"})
    if unit_cost is not None and new_qty > 0 and old_avg is not None:
        new_avg = ((old_qty * old_avg) + (qty * unit_cost)) / new_qty
    else:
        new_avg = old_avg

    try:
        projected = await fifo.receipt_valuation(db, balance, qty, unit_cost)
    except fifo.FifoError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.code}) from None
    if projected is not None:
        new_avg = Decimal(projected["avg_cost"]) if projected["avg_cost"] is not None else None
    now = datetime.now(timezone.utc)
    movement = StockMovement(
        idempotency_key=f"receiving:{session.id}:{line.id}",
        product_id=product.id,
        warehouse_id=session.warehouse_id,
        movement_type=MovementType.RECEIVING_IN,
        quantity=qty,
        unit_cost=unit_cost,
        reference_type="receiving_session",
        reference_id=str(session.id),
        reference_source=f"{supplier_code}:{session.invoice_number}",
        balance_after=new_qty,
        avg_cost_after=new_avg,
        created_by="receiving",
    )
    db.add(movement)
    await db.flush()

    balance.qty_on_hand = new_qty
    balance.avg_cost = new_avg
    balance.total_value = new_qty * new_avg if new_avg is not None else None
    if unit_cost is not None:
        balance.last_purchase_price = unit_cost
        balance.last_purchase_at = now
    balance.last_movement_at = now
    balance.last_movement_id = movement.id
    try:
        await fifo.add_receipt(db, balance, movement, now, unit_cost, "known",
            {"kind": "receiving", "session_id": session.id, "line_id": line.id,
             "supplier_code": supplier_code, "invoice_number": session.invoice_number,
             "unit_cost_at_receipt": str(unit_cost)})
    except fifo.FifoError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.code}) from None
    return movement


async def _lock_stock_balances(
    db: AsyncSession, warehouse_id: int, product_ids: set[int]
) -> Dict[int, StockBalance]:
    """Keep the receiving interface while sharing locks with other stock writers."""
    try:
        balances, _created_product_ids = await lock_stock_balances(db, product_ids, warehouse_id)
    except StockPublicationHoldError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.code}) from None
    return balances


async def _finalize_result(
    db: AsyncSession, session: ReceivingSession, movements_created: int,
    products_created: Optional[int], skipped_lines: List[Dict[str, Any]],
) -> Dict[str, Any]:
    lines = _lines_sorted(session)
    stats = {
        "total_lines": len(lines),
        "received_complete": sum(1 for ln in lines if ln.status == "matched"),
        "received_partial": sum(1 for ln in lines if ln.status == "partial"),
        "received_overage": sum(1 for ln in lines if ln.status == "overage"),
        "not_received": sum(1 for ln in lines if ln.status == "pending"),
        "total_scans": await _count_scans(db, session.id),
        "unexpected_scans": await _count_unexpected(db, session.id),
    }
    return {
        "success": True,
        "invoice_number": session.invoice_number,
        "invoice_no": session.invoice_number,
        "session_id": session.id,
        "completed_at": session.finished_at.isoformat() if session.finished_at else None,
        "stats": stats,
        "total_ordered": float(sum((ln.ordered_qty for ln in lines), Decimal("0"))),
        "total_received": float(sum((ln.received_qty for ln in lines), Decimal("0"))),
        "received_items_count": sum(1 for ln in lines if ln.received_qty > 0),
        "stock_movements_created": movements_created > 0,
        "movements_created": movements_created,
        # Older sessions did not persist this count; do not invent it on replay.
        "products_created": products_created,
        "skipped_lines": skipped_lines,
        "message": f"Príjem faktúry {session.invoice_number} dokončený",
    }


def _sync_finalized_invoice(supplier_code: str, result: Dict[str, Any]) -> None:
    """Best-effort UI index refresh, only after the ledger transaction commits."""
    try:
        updated = _update_invoice_status(supplier_code, result["invoice_number"], "processed", {
            "processed_at": result["completed_at"],
            "receiving_session_id": str(result["session_id"]),
            "receiving_stats": {
                key: result["stats"][key] for key in (
                    "total_lines", "received_complete", "received_partial", "not_received"
                )
            },
            "current_session_id": None,
            "paused_at": None,
            "pause_stats": None,
        })
        if not updated:
            logger.warning("Receiving session %s committed, but invoice index entry was not found; retry finalize to refresh it", result["session_id"])
    except Exception:
        # A retried finalize repairs the filesystem projection from the saved
        # successful result without receiving the goods a second time.
        logger.warning("Receiving session %s committed, but invoice index refresh failed; retry finalize to refresh it", result["session_id"], exc_info=True)


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

    lines_out: List[Dict[str, Any]] = []
    for line_number, row in enumerate(rows, start=1):
        line = ReceivingLine(
            session_id=session.id, line_number=line_number,
            supplier_sku=row["supplier_sku"], ean=row["ean"], description=row["description"],
            ordered_qty=row["ordered_qty"], received_qty=Decimal("0"),
            unit_price=row["unit_price"], status="pending",
        )
        product, match_method = await _resolve_line(db, supplier, line, prefix)
        line.product_id = product.id if product else None
        line.product = product
        line.match_method = match_method
        db.add(line)
        await db.flush()
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
    """One physical scan per request UUID; never deduplicate by EAN or time."""
    session = await _get_session_for_supplier(db, supplier_code, session_id, lock=True)
    code = request.code.strip()
    qty = request.qty
    fingerprint = hashlib.sha256(json.dumps({
        "session_id": session_id, "code": code, "qty": format(qty.quantize(Decimal(".001")), "f"),
        "scanned_by": request.scanned_by, "line_id": request.line_id,
    }, sort_keys=True).encode()).hexdigest()
    if request.request_id is not None:
        # Session locking serializes quantities; the UUID lock also catches
        # accidental reuse between two different receiving sessions.
        lock_key = int.from_bytes(hashlib.sha256(request.request_id.bytes).digest()[:8], "big", signed=True)
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
        previous = await db.get(ReceivingScanRequest, request.request_id)
        if previous is not None:
            if previous.session_id != session_id or previous.request_hash != fingerprint:
                raise HTTPException(409, detail={"code": "scan_request_conflict",
                    "message": "Identifikátor skenu už bol použitý s inými údajmi. Nový fyzický sken musí mať nové ID."})
            return {**previous.response, "replayed": True}

    if session.status == ReceivingStatus.paused:
        raise HTTPException(400, detail="Session is paused — resume it first")
    if session.status not in (ReceivingStatus.new, ReceivingStatus.in_progress):
        raise HTTPException(400, detail="Session already finalized or cancelled")

    prefix = _product_code_prefix(supplier_code)
    identifier_service = ProductIdentifierService(db)
    # A scan can name a listed invoice barcode, a supplier code or a canonical
    # prefixed SKU. Multiple invoice lines need an explicit selection because
    # their prices may differ even when the physical product is identical.
    lines = _lines_sorted(session)
    candidates = [line for line in lines if
        code in {value for value, _kind in identifier_service.split_compound_ean(line.ean or "")}
        or (line.supplier_sku and code in (line.supplier_sku.strip(), f"{prefix}{line.supplier_sku.strip()}"))]
    # Also allow an alternative registered EAN for an already assigned product.
    scan_identity = RemoteIdentity(0, code, barcodes=(code,),
        supplier_id=session.supplier_id, supplier_sku=code)
    index = await load_identity_index(db, 0, [scan_identity], local=True)
    scan_resolution = index.resolve(scan_identity)
    if scan_resolution.status == "conflict":
        raise HTTPException(409, detail={"code": "receiving_identity_conflict",
            "message": "Naskenovaný kód označuje viac produktov. Skontroluj identifikátory.",
            "reasons": scan_resolution.reasons, "candidate_product_ids": scan_resolution.candidate_product_ids})
    if scan_resolution.product_id is not None:
        for line in lines:
            if line.product_id == scan_resolution.product_id and line not in candidates:
                candidates.append(line)
    if request.line_id is not None:
        matched_line = next((line for line in candidates if line.id == request.line_id), None)
        if matched_line is None:
            raise HTTPException(409, detail={"code": "scan_line_mismatch",
                "message": "Vybraný riadok nepatrí k tomuto kódu a príjmu."})
    elif len(candidates) > 1:
        raise HTTPException(409, detail={"code": "scan_line_ambiguous",
            "message": "Kód zodpovedá viacerým riadkom. Vyber konkrétny riadok alebo uprav jeho množstvo ručne.",
            "line_numbers": [line.line_number for line in candidates], "line_ids": [line.id for line in candidates]})
    else:
        matched_line = candidates[0] if candidates else None
    product = index.products.get(scan_resolution.product_id)
    if matched_line is not None:
        line_product, matched_by = await _resolve_line(db, session.supplier, matched_line, prefix)
        if product is not None and line_product is not None and product.id != line_product.id:
            raise HTTPException(409, detail={"code": "receiving_identity_conflict",
                "message": "Naskenovaný kód a faktúrový riadok označujú odlišné produkty."})
        product = line_product or product
        if product is not None:
            matched_line.product_id = product.id
            matched_line.product = product
            matched_line.match_method = matched_by or scan_resolution.matched_by
        if matched_line.received_qty + qty > fifo.MAX_QUANTITY:
            raise HTTPException(409, detail={"code": "scan_quantity_out_of_range", "message": "Prijaté množstvo je príliš veľké."})
        matched_line.received_qty += qty
        matched_line.status = _status_for(matched_line.received_qty, matched_line.ordered_qty)

    if session.status == ReceivingStatus.new:
        session.status = ReceivingStatus.in_progress
        session.started_at = datetime.now(timezone.utc)
    scan_event = ScanEvent(
        session_type=ScanSessionType.receiving, receiving_session_id=session.id,
        receiving_line_id=matched_line.id if matched_line else None, scanned_code=code,
        scanned_code_type=identifier_service.classify_barcode(code),
        product_id=product.id if product else None,
        match_method=(matched_line.match_method or "invoice_code") if matched_line else None,
        quantity=qty, status=ScanStatus.active, scanned_by=request.scanned_by,
    )
    db.add(scan_event)
    unexpected = await _count_unexpected(db, session.id)
    result = {
        "status": matched_line.status if matched_line else "unexpected",
        "line": _line_dict(matched_line, prefix) if matched_line else None,
        "summary": _summary_counts(session, unexpected),
        "request_id": str(request.request_id) if request.request_id is not None else None,
        "replayed": False,
    }
    if request.request_id is not None:
        db.add(ReceivingScanRequest(request_id=request.request_id, session_id=session.id,
            scan_event_id=scan_event.id, request_hash=fingerprint, response=result))
        await db.flush()
    return result


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
    session = await _get_session_for_supplier(db, supplier_code, session_id, lock=True)
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

    # This is a manual quantity event, not a scan of the complete invoice EAN
    # source. Keep that source on the line and use one real bounded identifier
    # here; never truncate a barcode into a different invented identifier.
    product = line.__dict__.get("product")
    event_codes = (*verified_barcodes((line.ean or "",)), line.supplier_sku or "",
                   product.sku if product is not None else "")
    event_code = next((value for value in event_codes if value and len(value) <= 100), "")
    db.add(ScanEvent(
        session_type=ScanSessionType.receiving,
        receiving_session_id=session.id,
        receiving_line_id=line.id,
        scanned_code=event_code,
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
    session = await _get_session_for_supplier(db, supplier_code, session_id, lock=True)
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
    session = await _get_session_for_supplier(db, supplier_code, session_id, lock=True)
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
    """Finalize once; retries return the original committed result.

    Session mutations serialize on the session row. Balance creation and
    weighted-average updates use ordered row locks across receiving sessions.
    """
    session = await _get_session_for_supplier(db, supplier_code, session_id, lock=True)

    if session.status == ReceivingStatus.completed:
        result = (session.session_data or {}).get("finalize_result")
        if not result:
            # Backward compatibility for sessions finalized before result
            # snapshots existed. Never append movements on this path.
            movements = (await db.execute(select(StockMovement).where(
                StockMovement.reference_type == "receiving_session",
                StockMovement.reference_id == str(session.id),
            ))).scalars().all()
            keys = {movement.idempotency_key for movement in movements}
            skipped = [
                {"line_number": line.line_number, "reason": "Historical completed line has no receiving movement"}
                for line in session.lines
                if line.received_qty > 0 and f"receiving:{session.id}:{line.id}" not in keys
            ]
            result = await _finalize_result(db, session, len(movements), None, skipped)
            session.session_data = {**(session.session_data or {}), "finalize_result": result}
        await db.commit()
        _sync_finalized_invoice(supplier_code, result)
        return result

    lines = _lines_sorted(session)
    pending = sum(1 for ln in lines if ln.status == "pending")
    if pending and not request.force:
        raise HTTPException(
            400,
            detail=f"{pending} lines not received. Use force=true to finalize anyway.",
        )

    # Validate the entire receipt before resolving products or writing stock.
    # force only allows quantities not received; it never overrides identity
    # or cost validation. Unknown acquisition cost needs explicit support in
    # the future valuation model, not a guessed zero or old average.
    for line in lines:
        if not line.received_qty.is_finite() or line.received_qty < 0:
            raise HTTPException(409, detail=f"Riadok {line.line_number}: oprav neplatné prijaté množstvo.")
        if line.received_qty == 0:
            continue
        if line.unit_price is None or not line.unit_price.is_finite() or line.unit_price < 0:
            raise HTTPException(409, detail=(
                f"Riadok {line.line_number}: chýba platná nákupná cena. "
                "Správca musí opraviť cenu v uloženom riadku nedokončeného príjmu; "
                "samotná výmena CSV ho neaktualizuje. Príjem bez známej nákupnej ceny zatiaľ nie je podporovaný."
            ))
        if not line.product_id and not (line.ean or "").strip() and not (line.supplier_sku or "").strip():
            raise HTTPException(409, detail=(
                f"Riadok {line.line_number}: produkt nemá priradenie, EAN ani kód dodávateľa. "
                "Oprav identifikáciu produktu pred dokončením príjmu."
            ))

    supplier = await _get_supplier(db, supplier_code)
    prefix = _product_code_prefix(supplier_code)
    identifier_service = ProductIdentifierService(db)
    products_created = 0
    resolved = []

    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": IDENTITY_WRITE_LOCK})
    for line in lines:
        if line.received_qty <= 0:
            continue
        product, created, reason = await _ensure_product_for_line(
            db, supplier, line, prefix, session.invoice_number, identifier_service
        )
        if product is None:
            raise HTTPException(409, detail=(
                f"Riadok {line.line_number}: produkt nemožno priradiť ({reason}). "
                "Oprav identifikáciu produktu pred dokončením príjmu."
            ))
        products_created += int(created)
        line.product_id = product.id
        resolved.append((line, product))

    balances = await _lock_stock_balances(db, session.warehouse_id, {product.id for _, product in resolved})
    for line, product in resolved:
        await _write_stock_for_line(db, session, line, product, supplier_code, balances[product.id])

    session.status = ReceivingStatus.completed
    session.finished_at = datetime.now(timezone.utc)
    result = await _finalize_result(db, session, len(resolved), products_created, [])
    session.session_data = {**(session.session_data or {}), "finalize_result": result}

    # The index is outside the DB transaction. Commit first, so a failed
    # ledger write never marks the invoice processed in the filesystem.
    await db.commit()
    _sync_finalized_invoice(supplier_code, result)
    return result


@router.post("/suppliers/{supplier_code}/receiving/sessions/{session_id}/pause")
async def pause_session(
    supplier_code: str,
    session_id: int,
    db: AsyncSession = Depends(get_session),
):
    """Pause receiving session."""
    session = await _get_session_for_supplier(db, supplier_code, session_id, lock=True)

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
    session = await _get_session_for_supplier(db, supplier_code, session_id, lock=True)

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
      with current_session_id. Blocked if the session already wrote stock
      movements (re-finalizing would double the stock).
    - If no DB session exists (invoice processed via legacy flow): just flip
      the invoice index back to 'new' so a fresh session can be created.
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
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    result = await db.execute(stmt)
    session = result.scalar_one_or_none()

    if session:
        movements_cnt = (await db.execute(
            select(func.count()).where(
                StockMovement.reference_type == "receiving_session",
                StockMovement.reference_id == str(session.id),
            )
        )).scalar() or 0
        if movements_cnt:
            raise HTTPException(
                409,
                detail=(
                    f"Faktúru {invoice_no} nemožno znovu otvoriť — príjem už zapísal "
                    f"{movements_cnt} skladových pohybov (session {session.id}). "
                    "Opakované dokončenie by zdvojilo sklad. Ak treba korekciu, "
                    "použi skladovú úpravu (adjustment), nie reopen."
                ),
            )

        session.status = ReceivingStatus.paused
        session.session_data = {
            key: value for key, value in (session.session_data or {}).items()
            if key != "finalize_result"
        }
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
