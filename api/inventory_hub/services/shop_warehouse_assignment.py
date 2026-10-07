"""Local shop/warehouse links, independent of physical-stock activation."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from inventory_hub.db_models import Shop, ShopWarehouse, ShopWarehouseRole, Warehouse
from inventory_hub.order_stock_models import OrderStockPolicy
from inventory_hub.order_stock_types import ShopWarehouseAssignmentRequest


class AssignmentError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


async def _state(db, shop_id: int):
    rows = (await db.execute(select(ShopWarehouse, Warehouse)
        .join(Warehouse, Warehouse.id == ShopWarehouse.warehouse_id)
        .where(ShopWarehouse.shop_id == shop_id)
        .order_by(ShopWarehouse.id).execution_options(populate_existing=True))).all()
    selected = [(link, warehouse) for link, warehouse in rows
                if link.is_active and warehouse.is_active
                and link.role in (ShopWarehouseRole.fulfillment, ShopWarehouseRole.both)]
    fingerprint = [{"id": link.id, "warehouse_id": warehouse.id,
                    "warehouse_active": warehouse.is_active, "role": link.role.value,
                    "priority": link.priority, "active": link.is_active}
                   for link, warehouse in rows]
    digest = hashlib.sha256(json.dumps(fingerprint, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    assignment = None
    if len(selected) == 1:
        _, warehouse = selected[0]
        assignment = {"warehouse_id": warehouse.id, "warehouse_code": warehouse.code,
                      "warehouse_name": warehouse.name}
    return rows, {"warehouse_assignment": assignment, "assignment_hash": digest}


async def assignment_options(db, shop_id: int) -> dict:
    _, result = await _state(db, shop_id)
    return result


async def ensure_assignment_matches(db, shop_id: int, warehouse_id: int):
    rows, _ = await _state(db, shop_id)
    if any(link.is_active and link.role in (ShopWarehouseRole.fulfillment, ShopWarehouseRole.both)
           and link.warehouse_id != warehouse_id for link, _ in rows):
        raise AssignmentError("warehouse_assignment_conflict")


async def assign_warehouse(db, payload: ShopWarehouseAssignmentRequest) -> dict:
    # Same shop-row lock as order_stock.configure: assignment cannot race the
    # creation of a conflicting physical processing policy.
    shop = await db.scalar(select(Shop).where(Shop.code == payload.shop_code,
        Shop.is_active.is_(True), Shop.platform == "upgates").with_for_update()
        .execution_options(populate_existing=True))
    if shop is None:
        raise AssignmentError("order_stock_shop_not_found", 404)
    warehouse = await db.scalar(select(Warehouse).where(Warehouse.code == payload.warehouse_code,
        Warehouse.is_active.is_(True)).with_for_update(read=True)
        .execution_options(populate_existing=True))
    if warehouse is None:
        raise AssignmentError("order_stock_warehouse_unavailable")
    policy = await db.get(OrderStockPolicy, shop.id, populate_existing=True)
    if policy is not None and policy.warehouse_id != warehouse.id:
        raise AssignmentError("warehouse_assignment_policy_conflict")
    rows, current = await _state(db, shop.id)
    # Never silently remove another warehouse link or repoint an existing one.
    if any(link.is_active and link.role in (ShopWarehouseRole.fulfillment, ShopWarehouseRole.both)
           and link.warehouse_id != warehouse.id for link, _ in rows):
        raise AssignmentError("warehouse_assignment_conflict")
    existing = next((link for link, _ in rows if link.warehouse_id == warehouse.id), None)
    if existing is not None and existing.is_active and existing.role in (
            ShopWarehouseRole.fulfillment, ShopWarehouseRole.both):
        await db.commit()
        return current
    if payload.expected_assignment_hash != current["assignment_hash"]:
        raise AssignmentError("warehouse_assignment_changed")
    if existing is None:
        db.add(ShopWarehouse(shop_id=shop.id, warehouse_id=warehouse.id,
                            role=ShopWarehouseRole.both, priority=100, is_active=True))
    else:
        # Adding fulfillment retains an existing availability relationship.
        existing.role = ShopWarehouseRole.both
        existing.is_active = True
    await db.flush()
    result = await assignment_options(db, shop.id)
    await db.commit()
    return result
