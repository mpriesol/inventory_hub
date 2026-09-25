"""Read-only purchase-cost evidence for product and issued-order publication.

Products use the next physically available FIFO layer, not the balance average.
Orders use current documented costs of their original issue allocations. Returns
do not change that original sale quantity or gross cost. No ledger is rebuilt.
Each database projection is read in one statement, so its evidence comes from
one PostgreSQL statement snapshot; the publisher must recheck before sending.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation

from sqlalchemy import and_, select, true
from sqlalchemy.orm import aliased

from inventory_hub.db_models import MovementType, Product, Warehouse
from inventory_hub.db_models_ext import Reservation, ShopOrder, ShopOrderItem, StockBalance, StockMovement
from inventory_hub.fifo_models import FifoAllocation, FifoLayer, FifoState
from inventory_hub.services import fifo
from inventory_hub.services.order_stock_ledger import _source_lines
from inventory_hub.services.order_stock_source import _quantity


class FifoCostProjectionError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


def _fail(reason: str, status: int = 409):
    raise FifoCostProjectionError("fifo_cost_" + reason, status)


def _signed(result: dict) -> dict:
    signature = fifo.digest(result)
    return {**result, "signature": signature, "source_hash": signature}


def project_product(product, warehouse, balance, state, layers, roots=None) -> dict:
    """Pure projection; scalar ORM objects or equivalent detached records work."""
    if product is None or not product.is_active:
        _fail("product_not_found", 404)
    if warehouse is None or not warehouse.is_active:
        _fail("warehouse_not_found", 404)
    if balance is None:
        _fail("balance_missing")
    if state is None:
        _fail("fifo_required")
    scope = (product.id, warehouse.id)
    if ((balance.product_id, balance.warehouse_id) != scope
            or (state.product_id, state.warehouse_id) != scope
            or any((row.product_id, row.warehouse_id) != scope for row in layers)):
        _fail("evidence_mismatch")
    quantity = sum((row.quantity_remaining for row in layers), Decimal("0"))
    quarantined = sum((row.quantity_remaining for row in layers if row.stock_status == "quarantine"), Decimal("0"))
    if quantity != balance.qty_on_hand or quarantined != balance.qty_quarantined:
        _fail("layer_balance_mismatch")
    candidates = sorted((row for row in layers if row.stock_status == "available" and row.quantity_remaining > 0),
                        key=lambda row: (row.physical_received_at, row.id))
    if not candidates:
        # Zero stock and quarantine-only stock retain the previous shop price.
        _fail("no_available_layer")
    layer = candidates[0]
    if layer.cost_status != "known" or layer.unit_cost is None:
        _fail("cost_incomplete")
    if not layer.unit_cost.is_finite() or not 0 <= layer.unit_cost <= fifo.MAX_UNIT_COST:
        _fail("cost_invalid")
    root = (roots or {}).get(layer.root_cost_layer_id)
    if roots is not None and (root is None or root.product_id != product.id
            or root.root_cost_layer_id != root.id or root.unit_cost != layer.unit_cost
            or root.cost_status != layer.cost_status):
        _fail("evidence_mismatch")
    return _signed({"kind": "product", "product_id": product.id, "warehouse_id": warehouse.id,
        "sku": product.sku, "currency": "EUR", "vat_included": False,
        "unit_cost": fifo.number(layer.unit_cost), "fifo_revision": state.revision,
        "layer_id": layer.id, "layer_cost_revision": layer.cost_revision,
        "root_layer_id": layer.root_cost_layer_id,
        "root_cost_revision": root.cost_revision if root is not None else layer.cost_revision,
        "physical_received_at": layer.physical_received_at.isoformat(),
        "layer_quantity_remaining": fifo.number(layer.quantity_remaining),
        "quantity_on_hand": fifo.number(balance.qty_on_hand),
        "quantity_quarantined": fifo.number(balance.qty_quarantined)})


async def product_cost(db, product_id: int, warehouse_id: int) -> dict:
    root = aliased(FifoLayer)
    query = (select(Product, Warehouse, StockBalance, FifoState, FifoLayer, root,
                    fifo.stock_tracking.confirmed_stock(Product.id, Warehouse.id))
        .select_from(Product).join(Warehouse, true())
        .outerjoin(StockBalance, and_(StockBalance.product_id == Product.id, StockBalance.warehouse_id == Warehouse.id))
        .outerjoin(FifoState, and_(FifoState.product_id == Product.id, FifoState.warehouse_id == Warehouse.id))
        .outerjoin(FifoLayer, and_(FifoLayer.product_id == Product.id, FifoLayer.warehouse_id == Warehouse.id,
                                  FifoLayer.quantity_remaining > 0, fifo.stock_tracking.current_layer()))
        .outerjoin(root, root.id == FifoLayer.root_cost_layer_id)
        .where(Product.id == product_id, Warehouse.id == warehouse_id)
        .order_by(FifoLayer.id).execution_options(populate_existing=True))
    with db.no_autoflush:
        rows = (await db.execute(query)).all()
    if not rows:
        _fail("product_or_warehouse_not_found", 404)
    if not rows[0][6]:
        _fail("stock_unconfirmed")
    product, warehouse, balance, state = rows[0][:4]
    return project_product(product, warehouse, balance, state,
        [row[4] for row in rows if row[4] is not None], {row[5].id: row[5] for row in rows if row[5] is not None})


def project_order(order, evidence: list[tuple]) -> dict:
    """Project joined (item, reservation, movement, allocation, layer, root) rows.

Line identity is the original Upgates UUID, never SKU: two rows for the same
physical product can consume different layers and have different unit costs.
"""
    if order is None:
        _fail("order_not_found", 404)
    if order.stock_state != "issued" or order.stock_issued_at is None:
        _fail("order_not_issued")
    if order.currency not in (None, "EUR"):
        _fail("currency_unsupported")
    snapshot, issued = order.stock_snapshot, order.stock_issue_result
    if (not isinstance(snapshot, dict) or not isinstance(issued, dict)
            or snapshot.get("uuid") != order.stock_source_uuid
            or snapshot.get("order_number") != order.external_id
            or issued.get("order_id") != order.id or issued.get("action") != "issue"
            or issued.get("stock_state") != "issued" or issued.get("revision") != order.stock_revision):
        _fail("issue_evidence_missing")
    try:
        lines, _, errors = _source_lines(snapshot)
    except (TypeError, ValueError, KeyError, AttributeError):
        _fail("issue_evidence_missing")
    if errors or not lines:
        _fail("issue_evidence_missing")
    frozen = {line["line_key"]: line for line in lines}
    issued_lines = issued.get("lines")
    if not isinstance(issued_lines, list) or len(issued_lines) != len(lines):
        _fail("evidence_mismatch")
    for line in issued_lines:
        if not isinstance(line, dict):
            _fail("evidence_mismatch")
        original = frozen.get(line.get("line_key"))
        try:
            issued_quantity = Decimal(str(line.get("quantity")))
        except (InvalidOperation, TypeError, ValueError):
            _fail("evidence_mismatch")
        if (original is None or issued_quantity != original["quantity"]
                or any(line.get(key) != original[key] for key in ("product_id", "sku"))):
            _fail("evidence_mismatch")
    if len({line["line_key"] for line in issued_lines}) != len(lines):
        _fail("evidence_mismatch")
    grouped = defaultdict(list)
    for row in evidence:
        item = row[0]
        if item is not None and item.status == "fulfilled":
            grouped[item.external_item_id].append(row)
    if set(grouped) != set(frozen):
        _fail("evidence_mismatch")
    projections, all_movements, all_allocations = [], set(), set()
    for key, original in sorted(frozen.items()):
        rows = grouped[key]
        item, reservation, movement = rows[0][:3]
        if (item.order_id != order.id or not item.stock_managed or item.product_id != original["product_id"]
                or item.external_product_code != original["code"] or item.quantity != original["quantity"]
                or reservation is None or reservation.shop_order_item_id != item.id
                or reservation.product_id != item.product_id or reservation.warehouse_id != order.stock_warehouse_id
                or reservation.quantity != original["quantity"] or movement is None
                or reservation.sale_movement_id != movement.id or movement.id in all_movements
                or movement.product_id != item.product_id or movement.warehouse_id != order.stock_warehouse_id
                or movement.movement_type != MovementType.SALE_OUT or movement.quantity != -original["quantity"]
                or movement.reference_type != "shop_order" or movement.reference_id != str(order.id)
                or movement.reference_source != str(order.shop_id)
                or movement.idempotency_key != f"order:{order.id}:issue:{item.id}"):
            _fail("evidence_mismatch")
        if movement.unit_cost_currency != "EUR" or movement.fx_rate_to_eur != Decimal("1"):
            _fail("currency_unsupported")
        quantity = total = Decimal("0")
        allocations = []
        for joined_item, joined_reservation, joined_movement, allocation, layer, root in rows:
            if allocation is None:
                _fail("allocations_missing")
            if (joined_item.id != item.id or joined_reservation.id != reservation.id
                    or joined_movement.id != movement.id or allocation.id in all_allocations
                    or allocation.issue_movement_id != movement.id or layer is None
                    or allocation.layer_id != layer.id or layer.product_id != item.product_id
                    or layer.warehouse_id != order.stock_warehouse_id or root is None
                    or layer.root_cost_layer_id != root.id or root.root_cost_layer_id != root.id
                    or root.product_id != item.product_id or allocation.quantity <= 0
                    or allocation.quantity_before < allocation.quantity):
                _fail("evidence_mismatch")
            if (allocation.cost_status_current != "known" or allocation.total_cost_current is None
                    or allocation.unit_cost_current is None or layer.cost_status != "known" or root.cost_status != "known"):
                _fail("cost_incomplete")
            if (allocation.unit_cost_current != layer.unit_cost or layer.unit_cost != root.unit_cost
                    or not 0 <= allocation.unit_cost_current <= fifo.MAX_UNIT_COST
                    or allocation.total_cost_current != fifo.allocation_cost(
                        allocation.quantity_before, allocation.quantity, allocation.unit_cost_current)):
                _fail("evidence_mismatch")
            quantity += allocation.quantity
            total += allocation.total_cost_current
            all_allocations.add(allocation.id)
            # Original values remain visible in the proof after a documented
            # cost revision; no return quantities enter the sale-cost signature.
            allocations.append({"id": allocation.id, "sequence": allocation.sequence,
                "layer_id": layer.id, "root_layer_id": root.id,
                "root_cost_revision": root.cost_revision, "layer_cost_revision": layer.cost_revision,
                "quantity": fifo.number(allocation.quantity), "quantity_before": fifo.number(allocation.quantity_before),
                "unit_cost_at_issue": fifo.number(allocation.unit_cost_at_issue),
                "total_cost_at_issue": fifo.number(allocation.total_cost_at_issue),
                "cost_status_at_issue": allocation.cost_status_at_issue,
                "unit_cost_current": fifo.number(allocation.unit_cost_current),
                "total_cost_current": fifo.number(allocation.total_cost_current)})
        if quantity != original["quantity"]:
            _fail("allocation_quantity_mismatch")
        if total > fifo.MAX_VALUE:
            _fail("cost_out_of_range")
        unit_cost = fifo.money(total / quantity)
        if unit_cost > fifo.MAX_UNIT_COST:
            _fail("cost_out_of_range")
        all_movements.add(movement.id)
        projections.append({"line_key": key, "product_id": item.product_id, "sku": original["sku"],
            "code": original["code"], "quantity": fifo.number(quantity), "unit_cost": fifo.number(unit_cost),
            "total_cost": fifo.number(total), "movement_ids": [movement.id],
            "allocations": sorted(allocations, key=lambda row: (row["sequence"], row["id"]))})
    if set(issued.get("movement_ids", [])) != all_movements or issued.get("movements_created") != len(all_movements):
        _fail("evidence_mismatch")
    return _signed({"kind": "order", "order_id": order.id, "shop_id": order.shop_id,
        "warehouse_id": order.stock_warehouse_id, "order_number": order.external_id,
        "source_uuid": order.stock_source_uuid, "currency": "EUR", "vat_included": False,
        "target_currency_requires_verification": True, "stock_revision": order.stock_revision,
        "issued_at": order.stock_issued_at.isoformat(), "source_snapshot_hash": fifo.digest(snapshot),
        "issue_result_hash": fifo.digest(issued),
        "source_status_id": snapshot.get("status_id"), "source_resolved": snapshot.get("resolved"),
        "source_lines": sorted([{"line_key": row["line_key"], "code": row.get("code", ""),
            "ean": row.get("ean", ""), "quantity": _quantity(row.get("quantity")),
            "kind": row.get("kind", ""), "parent_uuid": row.get("parent_uuid", "")}
            for row in snapshot["lines"]], key=lambda row: row["line_key"]),
        "lines": projections,
        "total_cost": fifo.number(sum((Decimal(line["total_cost"]) for line in projections), Decimal("0")))})


async def order_cost(db, order_id: int) -> dict:
    root = aliased(FifoLayer)
    query = (select(ShopOrder, ShopOrderItem, Reservation, StockMovement, FifoAllocation, FifoLayer, root,
                    fifo.stock_tracking.current_movement())
        .select_from(ShopOrder)
        .outerjoin(ShopOrderItem, and_(ShopOrderItem.order_id == ShopOrder.id, ShopOrderItem.stock_managed.is_(True)))
        .outerjoin(Reservation, Reservation.shop_order_item_id == ShopOrderItem.id)
        .outerjoin(StockMovement, StockMovement.id == Reservation.sale_movement_id)
        .outerjoin(FifoAllocation, FifoAllocation.issue_movement_id == StockMovement.id)
        .outerjoin(FifoLayer, FifoLayer.id == FifoAllocation.layer_id)
        .outerjoin(root, root.id == FifoLayer.root_cost_layer_id)
        .where(ShopOrder.id == order_id).order_by(ShopOrderItem.id, FifoAllocation.sequence)
        .execution_options(populate_existing=True))
    with db.no_autoflush:
        rows = (await db.execute(query)).all()
    if not rows:
        _fail("order_not_found", 404)
    if any(row[3] is not None and not row[7] for row in rows):
        _fail("stock_unconfirmed")
    return project_order(rows[0][0], [tuple(row[1:7]) for row in rows])


product_cost_projection = product_cost


async def order_cost_projection(db, shop_id: int, order_number: str, warehouse_id: int) -> dict:
    with db.no_autoflush:
        order_id = await db.scalar(select(ShopOrder.id).where(ShopOrder.shop_id == shop_id,
            ShopOrder.external_id == order_number))
    if order_id is None:
        _fail("order_not_found", 404)
    result = await order_cost(db, order_id)
    if result["warehouse_id"] != warehouse_id or result["shop_id"] != shop_id or result["order_number"] != order_number:
        _fail("evidence_mismatch")
    return result
