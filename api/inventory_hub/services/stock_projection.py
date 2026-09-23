"""Read-only drafts of central own stock for mapped shop leaves.

These observations are not an Upgates payload or permission to publish stock.
No source parent is expanded, supplier quantity is added, or balance created.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select

from inventory_hub.db_models import Product, Shop, Warehouse
from inventory_hub.db_models_ext import ShopProduct, StockBalance, StockMovement
from inventory_hub.order_stock_models import OrderStockPolicy
from inventory_hub.services.product_identity import mapping_code


class StockProjectionError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


def _validate_skus(skus):
    valid = isinstance(skus, list) and 1 <= len(skus) <= 100
    if valid:
        try:
            valid = all(isinstance(sku, str) and 1 <= len(sku) <= 100 and sku == sku.strip()
                        and not any(ord(char) < 32 or ord(char) == 127 for char in sku)
                        and bool(sku.encode("utf-8")) for sku in skus) and len(skus) == len(set(skus))
        except UnicodeError:
            valid = False
    if not valid:
        raise StockProjectionError("stock_projection_invalid_skus", 422)


def _quantity(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _number_text(number):
    # Valid projected stock consists of whole pieces; never convert to float.
    return format(number.quantize(Decimal("1")), "f")


def _row(sku, product, mappings, code_owners, sku_owners, stock):
    row = {"sku": sku, "product_id": product.id if product else None, "target": None,
           "quantity_known": False, "qty_on_hand": None, "qty_reserved": None,
           "qty_available": None, "qty_quarantined": None, "errors": []}

    def reject(reason):
        row["errors"].append("stock_projection_" + reason)

    if product is None:
        reject("product_missing")
        return row
    if not product.is_active:
        reject("product_inactive")
    if len(sku_owners[sku.casefold()]) != 1:
        reject("product_ambiguous")
    if not mappings or (len(mappings) == 1 and not mappings[0].is_listed):
        reject("mapping_missing")
    elif len(mappings) != 1:
        reject("mapping_ambiguous")
    else:
        mapping = mappings[0]
        code = mapping_code(mapping)
        raw_code = mapping.variant_code if mapping.is_variant else mapping.external_code
        parent = mapping.parent_code if mapping.is_variant else mapping.external_code
        if not code or raw_code != sku or code != sku:
            reject("mapping_alias")
        elif len(code_owners[code.casefold()]) != 1:
            reject("mapping_ambiguous")
        elif (not isinstance(parent, str) or not parent or parent != parent.strip()
              or len(parent) > 100 or any(ord(char) < 32 or ord(char) == 127 for char in parent)):
            reject("mapping_invalid")
        else:
            row["target"] = {"parent_code": parent, "variant_code": code if mapping.is_variant else None, "code": code}
    if stock is None:
        reject("balance_missing")
        return row
    balance, has_movement = stock
    if not has_movement:
        reject("balance_unverified")
    on_hand, reserved = _quantity(balance.qty_on_hand), _quantity(balance.qty_reserved)
    quarantined = _quantity(balance.qty_quarantined)
    if (on_hand is None or reserved is None or quarantined is None or quarantined < 0
            or not Decimal("0") <= reserved <= on_hand - quarantined):
        reject("quantity_invalid")
    elif any(value != value.to_integral_value() for value in (on_hand, reserved, quarantined)):
        reject("unit_unsupported")
    if not row["errors"]:
        row.update(quantity_known=True, qty_on_hand=_number_text(on_hand), qty_reserved=_number_text(reserved),
                   qty_quarantined=_number_text(quarantined),
                   qty_available=_number_text(on_hand - reserved - quarantined))
    return row


async def preview(db, shop_code: str, skus: list[str]) -> dict:
    """Observe at most 100 exact SKUs under the shop's confirmed order policy.

The existing policy selects one active warehouse. Legacy default/sum-all
availability settings do not broaden it. Missing or unverified stock is NULL,
including a zero balance without a physical ledger record.
"""
    _validate_skus(skus)
    # Even a caller with staged ORM changes cannot cause this read to flush.
    with db.no_autoflush:
        shop = (await db.execute(select(Shop.id, Shop.code).where(Shop.code == shop_code, Shop.is_active.is_(True),
                                                                Shop.platform == "upgates"))).one_or_none()
        if shop is None:
            raise StockProjectionError("stock_projection_shop_not_found", 404)
        policy = (await db.execute(select(OrderStockPolicy.warehouse_id).where(
            OrderStockPolicy.shop_id == shop.id))).one_or_none()
        if policy is None:
            raise StockProjectionError("stock_projection_not_configured")
        warehouse = (await db.execute(select(Warehouse.id, Warehouse.code, Warehouse.name).where(
            Warehouse.id == policy.warehouse_id, Warehouse.is_active.is_(True)))).one_or_none()
        if warehouse is None:
            raise StockProjectionError("stock_projection_warehouse_unavailable")
        # Match the shared resolver's case-collision guard while still requiring
        # an exact SKU for the selected product. An unmapped case-only duplicate
        # must not make a stock draft certify an identity orders would reject.
        products = {product.sku: product for product in (await db.execute(
            select(Product.id, Product.sku, Product.is_active).where(
                func.lower(Product.sku).in_(sorted({sku.lower() for sku in skus}))))).all()}
        sku_owners = defaultdict(list)
        for product in products.values():
            sku_owners[product.sku.casefold()].append(product.id)
        # One query for the whole shop detects duplicate/case-conflicting leaf
        # codes even when the other owner is outside the requested SKU subset.
        mappings = (await db.execute(select(ShopProduct.id, ShopProduct.product_id, ShopProduct.is_variant,
            ShopProduct.variant_code, ShopProduct.external_code, ShopProduct.parent_code, ShopProduct.is_listed)
            .where(ShopProduct.shop_id == shop.id))).all()
        by_product, code_owners = defaultdict(list), defaultdict(list)
        for mapping in mappings:
            by_product[mapping.product_id].append(mapping)
            code = mapping_code(mapping)
            if code:
                code_owners[code.casefold()].append(mapping.id)
        product_ids = [products[sku].id for sku in skus if sku in products]
        has_movement = select(StockMovement.id).where(
            StockMovement.product_id == StockBalance.product_id,
            StockMovement.warehouse_id == StockBalance.warehouse_id,
        ).exists()
        # Quantity and physical evidence use the same SQL snapshot, so a
        # concurrent first receipt cannot validate an earlier zero observation.
        balances = {balance.product_id: (balance, balance.has_movement) for balance in
                    (await db.execute(select(StockBalance.product_id, StockBalance.qty_on_hand, StockBalance.qty_reserved,
                                             StockBalance.qty_quarantined,
                                             has_movement.label("has_movement")).where(
                        StockBalance.product_id.in_(product_ids), StockBalance.warehouse_id == warehouse.id,
                    ))).all()}
        rows = [_row(sku, products.get(sku), by_product[products[sku].id] if sku in products else [],
                     code_owners, sku_owners, balances.get(products[sku].id) if sku in products else None) for sku in skus]
    return {"shop_code": shop.code, "warehouse": {"id": warehouse.id, "code": warehouse.code, "name": warehouse.name},
            "captured_at": datetime.now(timezone.utc).isoformat(), "external_write_enabled": False, "rows": rows}
