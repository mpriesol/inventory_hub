"""Frozen opening previews and an atomic initial-stock posting, without shop I/O."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import io
import json
import re

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.db_models import MovementType, Product, Warehouse
from inventory_hub.db_models_ext import StockBalance, StockMovement
from inventory_hub.opening_stock_models import OpeningStockBatch, OpeningStockLine
from inventory_hub.opening_stock_types import OpeningFinalizeRequest, OpeningPreviewRequest
from inventory_hub.services.stock_balances import lock_stock_balances
from inventory_hub.services import fifo
from inventory_hub.services import stock_tracking
from inventory_hub.stock_tracking_models import StockTracking
from inventory_hub.services.stock_publication_gate import StockPublicationHoldError


MAX_BYTES = 1_048_576
MAX_ROWS = 5000
MAX_QUANTITY = Decimal("999999999")
MAX_UNIT_COST = Decimal("99999999.9999")
MAX_VALUE = Decimal("999999999999.9999")
EXPIRY_MINUTES = 30
HEADERS = {"sku", "quantity", "unit_cost", "unit"}


class OpeningError(Exception):
    def __init__(self, code: str, status: int = 409, errors: list | None = None):
        self.code, self.status, self.errors = code, status, errors or []
        super().__init__(code)


def now() -> datetime:
    return datetime.now(timezone.utc)


def _error(row: int | None, code: str, field: str | None = None) -> dict:
    return {"row": row, "code": code, **({"field": field} if field else {})}


def _decimal(value: str, scale: int) -> Decimal | None:
    value = value.strip()
    if not re.fullmatch(r"[0-9]+(?:[.,][0-9]{1," + str(scale) + r"})?", value, flags=re.ASCII):
        return None
    # A single comma is a decimal separator. No grouping, exponent or coercion.
    return Decimal(value.replace(",", "."))


def parse_csv(csv_text: str) -> tuple[list[dict], list[dict], list[dict]]:
    """Validate the bounded file completely before any DB mutation."""
    try:
        if not isinstance(csv_text, str) or len(csv_text.encode("utf-8")) > MAX_BYTES:
            return [], [_error(None, "csv_too_large")], []
    except UnicodeError:
        return [], [_error(None, "csv_invalid")], []
    csv_text = csv_text.removeprefix("\ufeff")
    candidates = []
    for delimiter in (";", ",", "\t"):
        try:
            header = next(csv.reader(io.StringIO(csv_text, newline=""), delimiter=delimiter, strict=True))
            candidates.append((len(HEADERS.intersection(cell.strip() for cell in header)), delimiter, header))
        except (StopIteration, csv.Error):
            continue
    if not candidates or max(row[0] for row in candidates) == 0:
        return [], [_error(1, "csv_headers_invalid")], []
    _, delimiter, header = max(candidates, key=lambda row: row[0])
    header = [cell.strip() for cell in header]
    errors, warnings, rows = [], [], []
    if len(header) != len(set(header)):
        errors.append(_error(1, "csv_duplicate_headers"))
    if set(header) - HEADERS:
        errors.append(_error(1, "csv_unknown_columns"))
    if HEADERS - set(header):
        errors.append(_error(1, "csv_missing_columns"))
    if errors:
        return [], errors, []
    reader = csv.reader(io.StringIO(csv_text, newline=""), delimiter=delimiter, strict=True)
    next(reader)
    seen = set()
    try:
        for number, cells in enumerate(reader, 2):
            if number > MAX_ROWS + 1:
                return [], [_error(None, "csv_too_many_rows")], []
            if len(cells) != len(header):
                errors.append(_error(number, "csv_column_count"))
                continue
            raw = dict(zip(header, cells))
            sku = raw["sku"].strip()
            row_errors = []
            if not sku or len(sku) > 100 or any(ord(c) < 32 or ord(c) == 127 for c in sku):
                row_errors.append(_error(number, "invalid_sku", "sku"))
            elif sku in seen:
                row_errors.append(_error(number, "duplicate_sku", "sku"))
            seen.add(sku)
            quantity = _decimal(raw["quantity"], 3)
            if quantity is None or not 0 < quantity <= MAX_QUANTITY or quantity != quantity.to_integral_value():
                row_errors.append(_error(number, "invalid_quantity", "quantity"))
            cost = _decimal(raw["unit_cost"], 4)
            if not raw["unit_cost"].strip():
                row_errors.append(_error(number, "missing_unit_cost", "unit_cost"))
            elif cost is None or not 0 <= cost <= MAX_UNIT_COST:
                row_errors.append(_error(number, "invalid_unit_cost", "unit_cost"))
            if raw["unit"] != "ks":
                row_errors.append(_error(number, "unsupported_unit", "unit"))
            if not row_errors:
                value = quantity * cost
                if value > MAX_VALUE:
                    row_errors.append(_error(number, "line_value_too_large", "unit_cost"))
                else:
                    rows.append({"line_number": number, "sku": sku, "quantity": quantity,
                                 "unit_cost": cost, "value": value.quantize(Decimal("0.0001")), "unit": "ks"})
                    if cost == 0:
                        warnings.append(_error(number, "zero_unit_cost"))
            errors.extend(row_errors)
    except csv.Error:
        errors.append(_error(None, "csv_invalid"))
    if not rows and not errors:
        errors.append(_error(None, "csv_empty"))
    return rows, errors, warnings


def _hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _public(batch: OpeningStockBatch) -> dict:
    return {**batch.preview_data, "preview_hash": batch.preview_hash, "status": batch.status,
            "completed_at": batch.completed_at.isoformat() if batch.completed_at else None, "result": batch.result}


def _preview_response(batch: OpeningStockBatch) -> dict:
    return {"ready": True, "errors": [], "warnings": batch.preview_data["warnings"], "batch": _public(batch)}


def _invalid(errors: list, warnings: list | None = None) -> dict:
    return {"ready": False, "errors": errors, "warnings": warnings or [], "batch": None}


async def options(db: AsyncSession) -> dict:
    warehouses = (await db.scalars(select(Warehouse).where(Warehouse.is_active.is_(True)).order_by(Warehouse.code))).all()
    return {"warehouses": [{"id": w.id, "code": w.code, "name": w.name} for w in warehouses],
            "limits": {"max_bytes": MAX_BYTES, "max_rows": MAX_ROWS, "max_quantity": str(MAX_QUANTITY),
                       "max_unit_cost": str(MAX_UNIT_COST)},
            "unit": "ks", "currency": "EUR", "price_basis": "ex_vat", "expires_minutes": EXPIRY_MINUTES}


async def get_batch(db: AsyncSession, batch_id: str) -> dict:
    batch = await db.get(OpeningStockBatch, str(batch_id))
    if batch is None:
        raise OpeningError("opening_batch_not_found", 404)
    return _public(batch)


async def list_batches(db: AsyncSession, limit: int = 20) -> dict:
    if not 1 <= limit <= 100:
        raise OpeningError("opening_invalid_limit", 422)
    batches = (await db.scalars(select(OpeningStockBatch).order_by(OpeningStockBatch.created_at.desc(), OpeningStockBatch.id)
                               .limit(limit))).all()
    return {"batches": [{key: value for key, value in _public(batch).items() if key not in ("lines", "result")}
                        for batch in batches], "limit": limit}


async def _occupied(db: AsyncSession, warehouse_id: int, product_ids: set[int]) -> set[int]:
    occupied = set()
    ids = sorted(product_ids)
    for start in range(0, len(ids), 500):
        for model in (StockTracking,):
            occupied.update((await db.scalars(select(model.product_id).where(
                model.warehouse_id == warehouse_id, model.product_id.in_(ids[start:start + 500])).distinct())).all())
    return occupied


async def _sku_candidates(db: AsyncSession, skus: list[str]) -> tuple[dict[str, Product], set[str]]:
    products, candidates = {}, {}
    keys = sorted({sku.lower() for sku in skus})
    for start in range(0, len(keys), 500):
        found = await db.scalars(select(Product).where(func.lower(Product.sku).in_(keys[start:start + 500])))
        for product in found:
            products[product.sku] = product
            candidates.setdefault(product.sku.lower(), set()).add(product.id)
    return products, {key for key, ids in candidates.items() if len(ids) > 1}


async def preview(db: AsyncSession, payload: OpeningPreviewRequest) -> dict:
    rows, errors, warnings = parse_csv(payload.csv_text)
    if errors:
        return _invalid(errors, warnings)
    request_hash = _hash({"version": 1, **payload.model_dump(mode="json")})
    batch_id = str(payload.request_id)
    existing = await db.get(OpeningStockBatch, batch_id)
    if existing is not None:
        if existing.input_hash != request_hash:
            raise OpeningError("opening_request_changed")
        return _preview_response(existing)
    at = now()
    if payload.counted_at > at:
        errors.append(_error(None, "counted_at_future", "counted_at"))
    if errors:
        return _invalid(errors, warnings)
    warehouse = await db.scalar(select(Warehouse).where(Warehouse.code == payload.warehouse_code, Warehouse.is_active.is_(True)))
    if warehouse is None:
        return _invalid([_error(None, "warehouse_unavailable", "warehouse_code")], warnings)
    products, ambiguous = await _sku_candidates(db, [row["sku"] for row in rows])
    for row in rows:
        if row["sku"].lower() in ambiguous:
            errors.append(_error(row["line_number"], "ambiguous_sku", "sku"))
        elif row["sku"] not in products:
            errors.append(_error(row["line_number"], "unknown_sku", "sku"))
    occupied = await _occupied(db, warehouse.id, {p.id for p in products.values()})
    for row in rows:
        if row["sku"] in products and products[row["sku"]].id in occupied:
            errors.append(_error(row["line_number"], "existing_stock", "sku"))
    if errors:
        return _invalid(errors, warnings)
    lines = [{"line_number": row["line_number"], "product_id": products[row["sku"]].id,
              "sku": row["sku"], "name": products[row["sku"]].name, "quantity": format(row["quantity"], ".0f"),
              "unit_cost": format(row["unit_cost"], ".4f"), "value": format(row["value"], ".4f"), "unit": "ks",
              "warnings": ["zero_unit_cost"] if row["unit_cost"] == 0 else []} for row in rows]
    expires_at = at + timedelta(minutes=EXPIRY_MINUTES)
    snapshot = {"id": batch_id, "warehouse": {"id": warehouse.id, "code": warehouse.code, "name": warehouse.name},
                "source_reference": payload.source_reference, "operator_name": payload.operator_name,
                "counted_at": payload.counted_at.isoformat(), "created_at": at.isoformat(), "expires_at": expires_at.isoformat(),
                "unit": "ks", "currency": "EUR", "price_basis": "ex_vat", "lines": lines, "warnings": warnings,
                "summary": {"lines": len(lines), "quantity": format(sum(row["quantity"] for row in rows), ".0f"),
                            "total_value": format(sum(row["value"] for row in rows), ".4f")}}
    created_id = (await db.execute(insert(OpeningStockBatch).values(
        id=batch_id, warehouse_id=warehouse.id, warehouse_code=warehouse.code, source_reference=payload.source_reference,
        operator_name=payload.operator_name, counted_at=payload.counted_at, created_at=at, expires_at=expires_at,
        status="prepared", input_hash=request_hash, preview_hash=_hash(snapshot), preview_data=snapshot,
    ).on_conflict_do_nothing(index_elements=["id"]).returning(OpeningStockBatch.id))).scalar_one_or_none()
    if created_id is not None:
        db.add_all([OpeningStockLine(batch_id=batch_id, product_id=products[row["sku"]].id, **row) for row in rows])
        await db.flush()
    batch = await db.get(OpeningStockBatch, batch_id)
    if batch.input_hash != request_hash:
        raise OpeningError("opening_request_changed")
    result = _preview_response(batch)
    await db.commit()
    return result


async def finalize(db: AsyncSession, batch_id: str, payload: OpeningFinalizeRequest) -> dict:
    batch = await db.scalar(select(OpeningStockBatch).where(OpeningStockBatch.id == str(batch_id))
                            .with_for_update().execution_options(populate_existing=True))
    if batch is None:
        raise OpeningError("opening_batch_not_found", 404)
    if payload.preview_hash != batch.preview_hash:
        raise OpeningError("opening_preview_changed")
    if batch.status == "completed":
        return batch.result
    at = now()
    if at >= batch.expires_at:
        raise OpeningError("opening_preview_expired")
    warehouse = await db.scalar(select(Warehouse).where(Warehouse.id == batch.warehouse_id)
                                .with_for_update(read=True).execution_options(populate_existing=True))
    if warehouse is None or not warehouse.is_active or warehouse.code != batch.warehouse_code:
        raise OpeningError("opening_warehouse_changed")
    lines = (await db.scalars(select(OpeningStockLine).where(OpeningStockLine.batch_id == batch.id)
                             .order_by(OpeningStockLine.line_number))).all()
    if not lines:
        raise OpeningError("opening_preview_changed")
    ids = sorted({line.product_id for line in lines})
    products = {}
    for start in range(0, len(ids), 500):
        rows = await db.scalars(select(Product).where(Product.id.in_(ids[start:start + 500])).order_by(Product.id)
                               .with_for_update(read=True).execution_options(populate_existing=True))
        products.update((product.id, product) for product in rows)
    if any(line.product_id not in products or products[line.product_id].sku != line.sku for line in lines):
        raise OpeningError("opening_products_changed")
    _, ambiguous = await _sku_candidates(db, [line.sku for line in lines])
    if ambiguous:
        raise OpeningError("opening_products_changed")
    # Revalidate persisted numeric facts against the frozen confirmation before
    # claiming any stock row; drafts have no patch/update route.
    saved = batch.preview_data["lines"]
    actual = [{"line_number": line.line_number, "product_id": line.product_id, "sku": line.sku,
               "quantity": format(line.quantity, ".0f"), "unit_cost": format(line.unit_cost, ".4f"),
               "value": format(line.value, ".4f"), "unit": line.unit} for line in lines]
    expected = [{key: row[key] for key in actual[0]} for row in saved]
    if actual != expected or _hash(batch.preview_data) != batch.preview_hash:
        raise OpeningError("opening_preview_changed")
    try:
        balances, newly_created = await lock_stock_balances(db, set(ids), batch.warehouse_id)
    except StockPublicationHoldError as error:
        raise OpeningError(error.code, error.status) from None
    if await _occupied(db, batch.warehouse_id, set(ids)):
        raise OpeningError("opening_existing_stock")
    for product_id in ids:
        try:
            await stock_tracking.activate(db, balances[product_id], source_type="opening_stock", source_id=batch.id,
                                         operator_name=batch.operator_name)
        except fifo.FifoError as error:
            raise OpeningError(error.code, error.status) from None
    movements = []
    for line in lines:
        movement = StockMovement(
            idempotency_key=f"opening:{batch.id}:{line.id}", product_id=line.product_id, warehouse_id=batch.warehouse_id,
            movement_type=MovementType.INITIAL, quantity=line.quantity, unit_cost=line.unit_cost,
            unit_cost_original=line.unit_cost, unit_cost_currency="EUR", fx_rate_to_eur=Decimal("1"),
            reference_type="opening_stock", reference_id=batch.id, reference_source="operator_opening",
            balance_after=line.quantity, avg_cost_after=line.unit_cost, created_by="operator_opening", created_at=at,
        )
        db.add(movement)
        movements.append(movement)
    await db.flush()
    result_lines = []
    for line, movement in zip(lines, movements):
        balance = balances[line.product_id]
        balance.qty_on_hand, balance.avg_cost, balance.total_value = line.quantity, line.unit_cost, line.value
        balance.last_movement_at, balance.last_movement_id = at, movement.id
        try:
            await fifo.add_receipt(db, balance, movement, batch.counted_at, line.unit_cost, "known",
                {"kind": "opening_stock", "batch_id": batch.id, "source_reference": batch.source_reference,
                 "operator_name": batch.operator_name, "counted_at": batch.counted_at.isoformat()})
        except fifo.FifoError as error:
            raise OpeningError(error.code, error.status) from None
        line.movement_id = movement.id
        result_lines.append({"line_number": line.line_number, "sku": line.sku, "product_id": line.product_id,
                             "movement_id": movement.id, "quantity": format(line.quantity, ".0f"),
                             "unit_cost": format(line.unit_cost, ".4f"), "value": format(line.value, ".4f")})
    result = {"batch_id": batch.id, "completed_at": at.isoformat(), "movements_created": len(movements),
              "summary": batch.preview_data["summary"], "lines": result_lines}
    batch.status, batch.completed_at, batch.result = "completed", at, result
    await db.flush()
    await db.commit()
    return result
