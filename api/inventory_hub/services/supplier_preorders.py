"""Approved supplier commitments and projection of their confirmed deliveries."""
from collections import defaultdict
from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
from typing import Annotated
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from sqlalchemy import select, text

from inventory_hub.db_models import Product
from inventory_hub.product_editor_types import ProductCardCreateRequest
from inventory_hub.services import invoice_processing, product_editor
from inventory_hub.services.canonical_products import current_product_condition
from inventory_hub.services.item_units import validate_stock_quantity, ItemUnitError
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK
from inventory_hub.sku_policy_types import SkuIdentityInput


class PreorderError(Exception):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


class NewCard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=500)
    sku_identity: SkuIdentityInput
    eans: list[str] = Field(default_factory=list, max_length=20)


class PreorderLine(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    sku: str | None = Field(default=None, min_length=1, max_length=100)
    new_card: NewCard | None = None
    ordered_quantity: Decimal = Field(gt=0, le=Decimal("999999999.999"), decimal_places=3)
    expected_on: date

    @model_validator(mode="after")
    def choose_identity(self):
        if (self.sku is None) == (self.new_card is None):
            raise ValueError("Choose one stock identity")
        return self


class PreorderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: UUID
    supplier_id: Annotated[StrictInt, Field(gt=0)]
    reference: str = Field(min_length=1, max_length=100)
    ordered_on: date
    lines: list[PreorderLine] = Field(min_length=1, max_length=100)
    approved: bool


class InvoiceLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    invoice_id: Annotated[StrictInt, Field(gt=0)]


class ExpectedDate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    previous_expected_on: date
    expected_on: date


async def records(db, statement, params):
    return [dict(row) for row in (await db.execute(text(statement), params)).mappings().all()]


async def _order(db, identifier):
    found = await records(db, """SELECT o.id,o.supplier_id,s.name AS supplier_name,o.reference,o.ordered_on,o.approved_at
        FROM supplier_preorders o JOIN suppliers s ON s.id=o.supplier_id WHERE o.id=:id""", {"id": identifier})
    if not found:
        raise PreorderError("preorder_not_found", 404)
    return found[0]


async def _deliveries(db, invoices):
    events, seen = defaultdict(list), set()
    for invoice in invoices:
        receipts = await invoice_processing._receipts(db, invoice)
        ids = [receipt["id"] for receipt in receipts if not invoice_processing._receipt(receipt)["reversed"]]
        if not ids:
            continue
        movements = await records(db, """SELECT id,product_id,quantity,created_at
            FROM stock_movements WHERE reference_type='receiving_session'
            AND reference_id=ANY(CAST(:receipts AS text[])) AND movement_type::text='RECEIVING_IN'
            ORDER BY created_at,id""", {"receipts": [str(id_) for id_ in ids]})
        for movement in movements:
            if movement["id"] in seen:
                continue
            seen.add(movement["id"])
            events[movement["product_id"]].append({"invoice_id": invoice["id"],
                "invoice_number": invoice["invoice_number"] or invoice["original_filename"],
                "quantity": movement["quantity"], "received_at": movement["created_at"]})
    return events


async def detail(db, identifier, *, product_id=None):
    order = await _order(db, identifier)
    invoices = await records(db, """SELECT ui.id,ui.invoice_number,ui.original_filename,ui.invoice_date,ui.direction,
            ui.supplier_id,ui.receiving_session_id,ui.source_key,ui.source_data
        FROM supplier_preorder_invoices link JOIN uploaded_invoices ui ON ui.id=link.invoice_id
        WHERE link.preorder_id=:id ORDER BY ui.invoice_date NULLS LAST,ui.id""", {"id": identifier})
    lines = await records(db, """SELECT l.id,p.id AS product_id,p.sku,p.name,p.stock_unit,l.ordered_quantity,l.expected_on
        FROM supplier_preorder_lines l JOIN products p ON p.id=l.product_id
        WHERE l.preorder_id=:id AND (CAST(:product AS bigint) IS NULL OR l.product_id=:product) ORDER BY l.id""",
        {"id": identifier, "product": product_id})
    deliveries = await _deliveries(db, invoices)
    for line in lines:
        confirmed = deliveries[line["product_id"]]
        # Multiple stock movements can be posted on one receipt. Quantities are
        # already in the card's base stock unit.
        line["received_quantity"] = sum((entry["quantity"] for entry in confirmed), Decimal(0))
        line["remaining_quantity"] = max(Decimal(0), line["ordered_quantity"] - line["received_quantity"])
        line["deliveries"] = sorted(confirmed, key=lambda entry: entry["received_at"])
    return {**order, "lines": lines,
            "status": "open" if any(line["remaining_quantity"] for line in lines) else "completed",
            "invoices": [{key: invoice[key] for key in ("id", "invoice_number", "original_filename", "invoice_date")}
                         for invoice in invoices]}


async def list_orders(db, *, product_id=None, query="", page=1):
    found = await records(db, """SELECT o.id,COUNT(*) OVER() AS total
        FROM supplier_preorders o JOIN suppliers s ON s.id=o.supplier_id
        WHERE ((CAST(:product AS bigint) IS NULL) OR EXISTS (
            SELECT 1 FROM supplier_preorder_lines l WHERE l.preorder_id=o.id AND l.product_id=:product))
          AND (o.reference ILIKE :query OR s.name ILIKE :query)
        ORDER BY o.ordered_on DESC,o.id DESC OFFSET :offset LIMIT 20""",
        {"product": product_id, "query": f"%{query}%", "offset": (page-1)*20})
    return {"items": [await detail(db, row["id"], product_id=product_id) for row in found],
            "total": found[0]["total"] if found else 0, "page": page}


async def create(db, request, *, actor):
    if not request.approved:
        raise PreorderError("preorder_approval_required", 422)
    digest = sha256(json.dumps(request.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": int.from_bytes(request.request_id.bytes[:8], "big", signed=True)})
    prior = await db.scalar(text("SELECT request_hash FROM supplier_preorders WHERE id=:id"), {"id": request.request_id})
    if prior is not None:
        if prior != digest:
            raise PreorderError("preorder_request_conflict")
        return await detail(db, request.request_id)
    if not await db.scalar(text("SELECT id FROM suppliers WHERE id=:id AND is_active"), {"id": request.supplier_id}):
        raise PreorderError("preorder_supplier_required", 422)
    reserved = await db.scalar(text("""INSERT INTO supplier_preorders(id,supplier_id,reference,ordered_on,request_hash,approved_by)
        VALUES(:id,:supplier,:reference,:day,:hash,:actor) ON CONFLICT DO NOTHING RETURNING id"""), {
        "id": request.request_id, "supplier": request.supplier_id, "reference": request.reference,
        "day": request.ordered_on, "hash": digest, "actor": actor})
    if reserved is None:
        raise PreorderError("preorder_reference_exists")
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": IDENTITY_WRITE_LOCK})
    assigned = set()
    for index, line in enumerate(request.lines, 1):
        if line.new_card:
            definition = line.new_card
            created = await product_editor.create_card(db, ProductCardCreateRequest(
                request_id=uuid5(request.request_id, f"preorder-card:{index}"), name=definition.name,
                sku_identity=definition.sku_identity, eans=definition.eans, is_active=False),
                actor=actor, commit=False)
            product_id = created["results"][0]["product_id"]
            product = await db.get(Product, product_id)
        else:
            product = await db.scalar(select(Product).where(Product.sku == line.sku, current_product_condition()))
        if product is None or product.item_type != "stock":
            raise PreorderError("preorder_stock_card_required", 422)
        product_id = product.id
        if product_id in assigned:
            raise PreorderError("preorder_product_repeated", 422)
        assigned.add(product_id)
        try:
            quantity = validate_stock_quantity(line.ordered_quantity, product.stock_unit)
        except ItemUnitError:
            raise PreorderError("preorder_quantity_unit", 422) from None
        await db.execute(text("""INSERT INTO supplier_preorder_lines(preorder_id,product_id,ordered_quantity,expected_on)
            VALUES(:order,:product,:quantity,:expected)"""), {"order": request.request_id, "product": product_id,
            "quantity": quantity, "expected": line.expected_on})
    return await detail(db, request.request_id)


async def available_invoices(db, identifier, query):
    order = await _order(db, identifier)
    return await records(db, """SELECT ui.id,ui.invoice_number,ui.original_filename,ui.invoice_date
        FROM uploaded_invoices ui WHERE ui.supplier_id=:supplier AND ui.direction='purchase'
        AND ui.document_type='invoice' AND ui.archived_at IS NULL
        AND (ui.invoice_number ILIKE :query OR ui.original_filename ILIKE :query)
        AND NOT EXISTS(SELECT 1 FROM supplier_preorder_invoices l WHERE l.invoice_id=ui.id)
        ORDER BY ui.invoice_date DESC NULLS LAST,ui.id DESC LIMIT 40""",
        {"supplier": order["supplier_id"], "query": f"%{query}%"})


async def link_invoice(db, identifier, invoice_id, *, actor):
    order = await _order(db, identifier)
    linked = await db.scalar(text("SELECT preorder_id FROM supplier_preorder_invoices WHERE invoice_id=:id"), {"id": invoice_id})
    if linked == identifier:
        return await detail(db, identifier)
    if linked is not None:
        raise PreorderError("preorder_invoice_already_linked")
    eligible = await db.scalar(text("""SELECT id FROM uploaded_invoices
        WHERE id=:id AND supplier_id=:supplier AND direction='purchase' AND document_type='invoice'
        AND archived_at IS NULL FOR UPDATE"""), {"id": invoice_id, "supplier": order["supplier_id"]})
    if eligible is None:
        raise PreorderError("preorder_invalid_invoice", 422)
    await db.execute(text("""INSERT INTO supplier_preorder_invoices(invoice_id,preorder_id,linked_by)
        VALUES(:invoice,:order,:actor) ON CONFLICT(invoice_id) DO NOTHING"""),
        {"invoice": invoice_id, "order": identifier, "actor": actor})
    if await db.scalar(text("SELECT preorder_id FROM supplier_preorder_invoices WHERE invoice_id=:id"), {"id": invoice_id}) != identifier:
        raise PreorderError("preorder_invoice_already_linked")
    return await detail(db, identifier)


async def change_date(db, identifier, line_id, request):
    updated = await db.scalar(text("""UPDATE supplier_preorder_lines SET expected_on=:expected
        WHERE preorder_id=:order AND id=:line AND expected_on=:previous RETURNING id"""),
        {"order": identifier, "line": line_id, "expected": request.expected_on, "previous": request.previous_expected_on})
    if updated is None:
        raise PreorderError("preorder_date_changed")
    return await detail(db, identifier)
