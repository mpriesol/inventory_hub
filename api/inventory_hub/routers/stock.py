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
from inventory_hub.db_models import Product
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
