# inventory_hub/routers/stock.py
"""
Stock overview endpoints — real data from stock_balances + products.

Read-only for now. Stock balances are populated by receiving finalize
(stock_movements) once that is implemented; until then these endpoints
honestly return an empty stock instead of mock data.
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.database import get_session
from inventory_hub.db_models import Product, Shop
from inventory_hub.db_models_ext import StockBalance

router = APIRouter(prefix="/stock", tags=["stock"])


@router.get("/items")
async def stock_items(db: AsyncSession = Depends(get_session)) -> List[Dict[str, Any]]:
    """
    Stock items aggregated per product across warehouses.
    Empty list until stock movements start populating stock_balances.
    """
    stmt = (
        select(
            Product.sku,
            Product.name,
            Product.brand,
            func.sum(StockBalance.qty_on_hand).label("on_hand"),
            func.sum(StockBalance.qty_reserved).label("reserved"),
            func.sum(StockBalance.total_value).label("total_value"),
            func.max(StockBalance.min_quantity).label("min_quantity"),
            func.max(StockBalance.avg_cost).label("avg_cost"),
        )
        .join(StockBalance, StockBalance.product_id == Product.id)
        .group_by(Product.id, Product.sku, Product.name, Product.brand)
        .order_by(Product.sku)
    )
    result = await db.execute(stmt)

    items: List[Dict[str, Any]] = []
    for row in result.all():
        on_hand = float(row.on_hand or 0)
        reserved = float(row.reserved or 0)
        min_qty = float(row.min_quantity or 0)
        items.append({
            "sku": row.sku,
            "name": row.name,
            "brand": row.brand or "",
            "on_hand": on_hand,
            "reserved": reserved,
            "available": on_hand - reserved,
            "avg_cost": float(row.avg_cost or 0),
            "total_value": float(row.total_value or 0),
            "low_stock": on_hand <= min_qty,
        })
    return items


@router.get("/summary")
async def stock_summary(db: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    """Real aggregate numbers for dashboard / stock page header."""
    bal = await db.execute(
        select(
            func.count(func.distinct(StockBalance.product_id)),
            func.coalesce(func.sum(StockBalance.total_value), 0),
            func.coalesce(func.sum(StockBalance.qty_reserved), 0),
            func.coalesce(
                func.sum(case((StockBalance.qty_on_hand <= StockBalance.min_quantity, 1), else_=0)),
                0,
            ),
        )
    )
    products_with_stock, total_value, reserved_total, low_stock = bal.one()

    products_total = (await db.execute(select(func.count(Product.id)))).scalar() or 0

    return {
        "products_total": int(products_total),
        "products_with_stock": int(products_with_stock or 0),
        "inventory_value": float(total_value or 0),
        "reserved_total": float(reserved_total or 0),
        "low_stock_count": int(low_stock or 0),
    }


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

    balances = (await db.execute(
        select(_SB).where(_SB.product_id == product.id)
    )).scalars().all()

    shop_rows = (await db.execute(
        select(ShopProduct, Shop.code)
        .join(Shop, Shop.id == ShopProduct.shop_id)
        .where(ShopProduct.product_id == product.id)
    )).all()

    # Image: find vault content by parent external code (any shop)
    image_url = None
    for sp, _shop_code in shop_rows:
        parent = sp.parent_code or sp.external_code
        if not parent:
            continue
        content = (await db.execute(
            select(ShopProductContent).where(ShopProductContent.external_code == parent).limit(1)
        )).scalar_one_or_none()
        if content and isinstance(content.data, dict):
            image_url = _image_from_content(content.data, sp.variant_code)
            if image_url:
                break

    on_hand = sum(float(b.qty_on_hand or 0) for b in balances)
    reserved = sum(float(b.qty_reserved or 0) for b in balances)

    return {
        "sku": product.sku,
        "name": product.name,
        "brand": product.brand,
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
        "stock": {
            "on_hand": on_hand,
            "reserved": reserved,
            "available": on_hand - reserved,
            "avg_cost": float(balances[0].avg_cost) if balances and balances[0].avg_cost is not None else None,
        },
        "shops": [
            {"shop": shop_code, "external_code": sp.external_code, "variant_code": sp.variant_code,
             "shop_availability": sp.shop_availability,
             "shop_stock": float(sp.shop_stock) if sp.shop_stock is not None else None,
             "last_pull_at": sp.last_pull_at.isoformat() if sp.last_pull_at else None}
            for sp, shop_code in shop_rows
        ],
    }
