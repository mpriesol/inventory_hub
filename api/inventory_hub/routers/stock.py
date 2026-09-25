# inventory_hub/routers/stock.py
"""
Stock overview endpoints — real data from stock_balances + products.

Read-only projections of locally recorded balances, valuation and orders.
Physical writers live in receiving and the dedicated stock services.
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends
from sqlalchemy import Numeric, and_, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.database import get_session
from inventory_hub.db_models import Product, Shop, Warehouse
from inventory_hub.db_models_ext import StockBalance, ShopOrder
from inventory_hub.fifo_models import FifoLayer, FifoState
from inventory_hub.services.product_editor import effective_names
from inventory_hub.product_editor_models import ProductEditorOverride
from inventory_hub.services.stock_tracking import confirmed_stock, current_layer

router = APIRouter(prefix="/stock", tags=["stock"])


async def _image_urls_by_product(db: AsyncSession) -> Dict[int, str]:
    """
    product_id -> main image URL, resolved in TWO set-based queries
    (no per-row JSON parsing in Python):
    1. main image per shop and external_code straight from the JSONB vault,
    2. product_id -> shop and external/parent code mapping from shop_products.
    """
    from sqlalchemy import text
    img_rows = await db.execute(text("""
        SELECT shop_id, external_code,
               COALESCE(
                 (SELECT img->>'url'
                    FROM jsonb_array_elements(data->'images') img
                   WHERE (img->>'main_yn')::boolean IS TRUE
                   LIMIT 1),
                 data->'images'->0->>'url'
               ) AS url
          FROM shop_product_content
    """))
    url_by_code = {(r.shop_id, r.external_code): r.url for r in img_rows if r.url}

    from inventory_hub.db_models_ext import ShopProduct as _SP
    sp_rows = await db.execute(select(_SP.product_id, _SP.shop_id, _SP.external_code, _SP.parent_code))
    out: Dict[int, str] = {}
    for pid, shop_id, ext, parent in sp_rows.all():
        code = (shop_id, parent or ext)
        if pid not in out and code in url_by_code:
            out[pid] = url_by_code[code]
    return out


def valuation_balances():
    """One row per balance; partial FIFO costs never become complete stock value."""
    layers = select(
        FifoLayer.product_id, FifoLayer.warehouse_id,
        func.sum(case((FifoLayer.cost_status == "known", func.round(FifoLayer.quantity_remaining * FifoLayer.unit_cost, 4)), else_=0)).label("known_value"),
        func.sum(case((FifoLayer.cost_status == "provisional", func.round(FifoLayer.quantity_remaining * FifoLayer.unit_cost, 4)), else_=0)).label("provisional_value"),
        func.sum(case((FifoLayer.cost_status == "provisional", FifoLayer.quantity_remaining), else_=0)).label("provisional_quantity"),
        func.sum(case((FifoLayer.cost_status == "unknown", FifoLayer.quantity_remaining), else_=0)).label("unknown_quantity"),
    ).where(current_layer()).group_by(FifoLayer.product_id, FifoLayer.warehouse_id).subquery()
    active = FifoState.product_id.is_not(None)
    unknown = case((active, func.coalesce(layers.c.unknown_quantity, 0)),
                   (StockBalance.total_value.is_(None), StockBalance.qty_on_hand), else_=0)
    provisional = func.coalesce(layers.c.provisional_quantity, 0)
    return select(
        StockBalance.product_id, StockBalance.warehouse_id, StockBalance.qty_on_hand,
        StockBalance.qty_reserved, StockBalance.qty_quarantined,
        func.coalesce(cast(ProductEditorOverride.data["warehouses"][Warehouse.code]["min_quantity"].astext, Numeric(12, 3)),
                      StockBalance.min_quantity).label("min_quantity"),
        StockBalance.total_value,
        case((active, func.coalesce(layers.c.known_value, 0)), else_=func.coalesce(StockBalance.total_value, 0)).label("known_value"),
        func.coalesce(layers.c.provisional_value, 0).label("provisional_value"),
        unknown.label("unknown_quantity"), provisional.label("provisional_quantity"),
        case((and_(unknown == 0, provisional == 0, StockBalance.total_value.is_not(None)), 0), else_=1).label("incomplete"),
    ).where(confirmed_stock()).join(Warehouse, Warehouse.id == StockBalance.warehouse_id)\
     .outerjoin(ProductEditorOverride, ProductEditorOverride.product_id == StockBalance.product_id)\
     .outerjoin(FifoState, and_(FifoState.product_id == StockBalance.product_id,
                               FifoState.warehouse_id == StockBalance.warehouse_id))\
     .outerjoin(layers, and_(layers.c.product_id == StockBalance.product_id,
                            layers.c.warehouse_id == StockBalance.warehouse_id)).subquery()


def valuation_fields(balance):
    return [func.sum(balance.c.qty_on_hand).label("on_hand"),
            func.sum(balance.c.qty_reserved).label("reserved"),
            func.sum(balance.c.qty_quarantined).label("quarantined"),
            func.sum(balance.c.total_value).label("total_value"),
            func.sum(balance.c.known_value).label("known_value"),
            func.sum(balance.c.provisional_value).label("provisional_value"),
            func.sum(balance.c.unknown_quantity).label("unknown_quantity"),
            func.sum(balance.c.provisional_quantity).label("provisional_quantity"),
            func.sum(balance.c.incomplete).label("incomplete")]


def valuation_output(row):
    on_hand, reserved, quarantined = (float(row.on_hand or 0), float(row.reserved or 0), float(row.quarantined or 0))
    complete = not row.incomplete
    value = float(row.total_value or 0) if complete else None
    return {"on_hand": on_hand, "reserved": reserved, "quarantined": quarantined,
            "available": on_hand - reserved - quarantined,
            "avg_cost": value / on_hand if complete and on_hand else (0 if complete else None),
            "total_value": value, "value_complete": complete,
            "known_value": float(row.known_value or 0), "provisional_value": float(row.provisional_value or 0),
            "unknown_quantity": float(row.unknown_quantity or 0), "provisional_quantity": float(row.provisional_quantity or 0)}


@router.get("/items")
async def stock_items(db: AsyncSession = Depends(get_session)) -> List[Dict[str, Any]]:
    balances = valuation_balances()
    result = await db.execute(select(Product.id, Product.sku, Product.name, Product.brand,
        *valuation_fields(balances), func.max(balances.c.min_quantity).label("min_quantity"))
        .join(balances, balances.c.product_id == Product.id)
        .group_by(Product.id, Product.sku, Product.name, Product.brand).order_by(Product.sku))
    images = await _image_urls_by_product(db)
    names = await effective_names(db)
    return [{"sku": row.sku, "image_url": images.get(row.id), "name": names.get(row.id, {}).get("name", row.name), "brand": names.get(row.id, {}).get("brand", row.brand) or "",
             **valuation_output(row),
             "low_stock": float(row.on_hand or 0) - float(row.reserved or 0) - float(row.quarantined or 0) <= float(row.min_quantity or 0)}
            for row in result.all()]


@router.get("/summary")
async def stock_summary(db: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    balances = valuation_balances()
    row = (await db.execute(select(*valuation_fields(balances),
        func.count(func.distinct(balances.c.product_id)).label("products_with_stock"),
        func.sum(case((balances.c.qty_on_hand - balances.c.qty_reserved - balances.c.qty_quarantined <= balances.c.min_quantity, 1), else_=0)).label("low_stock")))).one()
    values = valuation_output(row)
    products_total = (await db.execute(select(func.count(Product.id)))).scalar() or 0
    open_orders = int(await db.scalar(select(func.count(ShopOrder.id)).where(
        ShopOrder.stock_state.in_(("pending", "reserved")))) or 0)
    return {"products_total": int(products_total), "products_with_stock": int(row.products_with_stock or 0),
            "confirmed_products": int(row.products_with_stock or 0),
            "unconfirmed_products": int(products_total) - int(row.products_with_stock or 0),
            "inventory_value": values["total_value"], "known_inventory_value": values["known_value"],
            "provisional_inventory_value": values["provisional_value"], "value_complete": values["value_complete"],
            "unknown_quantity": values["unknown_quantity"], "provisional_quantity": values["provisional_quantity"],
            "quarantined_total": values["quarantined"], "reserved_total": values["reserved"], "low_stock_count": int(row.low_stock or 0),
            "on_hand_total": values["on_hand"], "available_total": values["available"], "open_managed_orders": open_orders}


# ============================================================================
# Product detail (for /products/{sku} page)
# ============================================================================

from inventory_hub.db_models import ProductGroup, ProductIdentifier
from inventory_hub.db_models_ext import (
    ProductVariantAttribute, ShopProduct, ShopProductContent, StockBalance as _SB,
)
from fastapi import HTTPException


def _image_from_content(data: dict, variant_code: str | None) -> str | None:
    """Main image URL from the vault payload; prefer the variant's own image."""
    def pick(images):
        if not isinstance(images, list):
            return None
        for img in images:
            if isinstance(img, dict) and img.get("main_yn") and img.get("url"):
                return str(img["url"])
        for img in images:
            if isinstance(img, dict) and img.get("url"):
                return str(img["url"])
        return None

    if variant_code:
        for v in data.get("variants") or []:
            if isinstance(v, dict) and str(v.get("code")) == variant_code:
                url = pick(v.get("images"))
                if url:
                    return url
                break
    return pick(data.get("images"))


@router.get("/product/{sku}")
async def product_detail(sku: str, db: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    product = (await db.execute(select(Product).where(Product.sku == sku))).scalar_one_or_none()
    if not product:
        raise HTTPException(404, detail=f"Product not found: {sku}")

    names = (await effective_names(db, [product.id])).get(product.id, {})
    group = None
    if product.group_id:
        group = await db.get(ProductGroup, product.group_id)

    attrs = (await db.execute(
        select(ProductVariantAttribute)
        .where(ProductVariantAttribute.product_id == product.id)
        .order_by(ProductVariantAttribute.display_order)
    )).scalars().all()

    idents = (await db.execute(
        select(ProductIdentifier).where(ProductIdentifier.product_id == product.id)
    )).scalars().all()

    balances = valuation_balances()
    stock_row = (await db.execute(select(*valuation_fields(balances),
        func.count(balances.c.product_id).label("confirmed_warehouses"))
        .where(balances.c.product_id == product.id))).one()
    valuation = valuation_output(stock_row)
    if not stock_row.confirmed_warehouses:
        valuation = {key: None for key in valuation}
        valuation["value_complete"] = False
    valuation["known"] = bool(stock_row.confirmed_warehouses)

    shop_rows = (await db.execute(
        select(ShopProduct, Shop.code)
        .join(Shop, Shop.id == ShopProduct.shop_id)
        .where(ShopProduct.product_id == product.id)
    )).all()

    # Each mapping must read its own shop's parent payload.
    image_url = None
    for sp, _shop_code in shop_rows:
        parent = sp.parent_code or sp.external_code
        if not parent:
            continue
        content = (await db.execute(
            select(ShopProductContent).where(ShopProductContent.shop_id == sp.shop_id,
                                            ShopProductContent.external_code == parent).limit(1)
        )).scalar_one_or_none()
        if content and isinstance(content.data, dict):
            image_url = _image_from_content(content.data, sp.variant_code)
            if image_url:
                break

    return {
        "sku": product.sku,
        "name": names.get("name", product.name),
        "brand": names.get("brand", product.brand),
        "category": product.category,
        "weight_g": product.weight_g,
        "created_from_source": product.created_from_source,
        "created_at": product.created_at.isoformat() if product.created_at else None,
        "updated_at": product.updated_at.isoformat() if product.updated_at else None,
        "validation_required": product.validation_required,
        "group": {"code": group.code, "name": group.name} if group else None,
        "image_url": image_url,
        "attributes": [{"name": a.attribute_name, "value": a.attribute_value} for a in attrs],
        "identifiers": [
            {"type": i.identifier_type.value if hasattr(i.identifier_type, "value") else str(i.identifier_type),
             "value": i.value, "is_primary": i.is_primary}
            for i in idents
        ],
        "stock": valuation,
        "shops": [
            {"shop": shop_code, "external_code": sp.external_code, "variant_code": sp.variant_code,
             "parent_code": sp.parent_code,
             "shop_availability": sp.shop_availability,
             "shop_stock": float(sp.shop_stock) if sp.shop_stock is not None else None,
             "last_pull_at": sp.last_pull_at.isoformat() if sp.last_pull_at else None}
            for sp, shop_code in shop_rows
        ],
    }
