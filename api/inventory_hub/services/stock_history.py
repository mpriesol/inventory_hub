"""Read-only, paginated view of the immutable physical movement ledger."""
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import String, and_, cast, func, or_, select

from inventory_hub.db_models import MovementType, Product, Shop, Supplier, Warehouse
from inventory_hub.db_models_ext import ReceivingSession, ShopOrder, StockMovement
from inventory_hub.fifo_models import FifoReceipt
from inventory_hub.opening_stock_models import OpeningStockBatch
from inventory_hub.product_editor_models import ProductEditorOverride
from inventory_hub.services.stock_tracking import current_movement


def decimal_text(value):
    return None if value is None else format(value, "f")


def history_query(*, q="", sku=None, warehouse_code=None, movement_type=None, date_from=None, date_to=None, snapshot_id=None, tracking_scope="all"):
    movement = StockMovement
    name = func.coalesce(ProductEditorOverride.data["common"]["name"].astext, Product.name)
    document = func.coalesce(ReceivingSession.invoice_number,
        FifoReceipt.preview_data["source_reference"].astext, OpeningStockBatch.source_reference)
    reference = func.coalesce(ShopOrder.external_code, ShopOrder.external_id, document, movement.reference_source)
    statement = select(movement, Product.sku, name.label("product_name"), Warehouse.code.label("warehouse_code"),
        Warehouse.name.label("warehouse_name"), reference.label("reference_label"), document.label("document"),
        Shop.code.label("shop_code"), Supplier.code.label("supplier_code"), current_movement().label("current_inventory"))\
        .join(Product, Product.id == movement.product_id).join(Warehouse, Warehouse.id == movement.warehouse_id)\
        .outerjoin(ProductEditorOverride, ProductEditorOverride.product_id == Product.id)\
        .outerjoin(ReceivingSession, and_(movement.reference_type == "receiving_session",
            movement.reference_id == cast(ReceivingSession.id, String)))\
        .outerjoin(Supplier, Supplier.id == ReceivingSession.supplier_id)\
        .outerjoin(ShopOrder, and_(movement.reference_type == "shop_order", movement.reference_id == cast(ShopOrder.id, String)))\
        .outerjoin(Shop, Shop.id == ShopOrder.shop_id)\
        .outerjoin(FifoReceipt, and_(movement.reference_type == "fifo_receipt", movement.reference_id == FifoReceipt.id))\
        .outerjoin(OpeningStockBatch, and_(movement.reference_type == "opening_stock", movement.reference_id == OpeningStockBatch.id))
    if tracking_scope != "all":
        statement = statement.where(current_movement() if tracking_scope == "current" else ~current_movement())
    if snapshot_id is not None:
        statement = statement.where(movement.id <= snapshot_id)
    if sku:
        statement = statement.where(Product.sku == sku)
    if warehouse_code:
        statement = statement.where(Warehouse.code == warehouse_code)
    if movement_type:
        statement = statement.where(movement.movement_type == MovementType(movement_type))
    if date_from:
        statement = statement.where(movement.created_at >= datetime.combine(date_from, time.min, timezone.utc))
    if date_to:
        statement = statement.where(movement.created_at < datetime.combine(date_to + timedelta(days=1), time.min, timezone.utc))
    if q:
        pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        statement = statement.where(or_(*(column.ilike(pattern, escape="\\") for column in
            (Product.sku, name, reference, movement.reference_id, movement.notes))))
    return statement


async def list_movements(db, *, page=1, page_size=50, snapshot_id=None, **filters):
    if snapshot_id is None:
        snapshot_id = int(await db.scalar(select(func.max(StockMovement.id))) or 0)
    statement = history_query(snapshot_id=snapshot_id, **filters)
    total = int(await db.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = (await db.execute(statement.order_by(StockMovement.created_at.desc(), StockMovement.id.desc())
        .offset((page - 1) * page_size).limit(page_size))).all()
    items = []
    for row in rows:
        movement = row[0]
        items.append({"id": movement.id, "product_id": movement.product_id, "sku": row.sku,
            "tracking_scope": "current" if row.current_inventory else "historical",
            "product_name": row.product_name, "warehouse_code": row.warehouse_code, "warehouse_name": row.warehouse_name,
            "movement_type": movement.movement_type.value, "quantity": decimal_text(movement.quantity),
            "balance_before": decimal_text(movement.balance_after - movement.quantity),
            "balance_after": decimal_text(movement.balance_after), "unit_cost": decimal_text(movement.unit_cost),
            "total_cost": decimal_text(movement.total_cost), "cost_basis": "recorded_at_movement",
            "reason": movement.notes, "reference_type": movement.reference_type, "reference_id": movement.reference_id,
            "reference_source": movement.reference_source, "reference_label": row.reference_label,
            "document": row.document, "shop_code": row.shop_code, "supplier_code": row.supplier_code,
            "created_by": movement.created_by, "created_at": movement.created_at.isoformat()})
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "snapshot_id": snapshot_id, "source": "hub", "upgates_calls": 0}


async def options(db):
    warehouses = (await db.execute(select(Warehouse.code, Warehouse.name).order_by(Warehouse.name, Warehouse.code))).all()
    return {"warehouses": [{"code": row.code, "name": row.name} for row in warehouses],
            "movement_types": [item.value for item in MovementType], "date_timezone": "UTC"}
