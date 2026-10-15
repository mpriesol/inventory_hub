"""Close a stock review without inventing a return or changing the issued ledger.

The immutable OrderStockPreview is a separate acknowledgement of an exact fresh
source and processing context. It does not alter the original order or movements.
Physical returns continue through fifo_returns with their own explicit receipt.
"""
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from inventory_hub.db_models import Product, Shop, Warehouse
from inventory_hub.db_models_ext import MovementType, ShopOrder, ShopOrderItem, StockMovement
from inventory_hub.fifo_models import FifoAllocation
from inventory_hub.order_collection_models import OrderInboxEntry
from inventory_hub.order_processing_models import OrderProcessingJob
from inventory_hub.order_stock_models import OrderStockPreview
from inventory_hub.services import order_stock as stock, order_stock_ledger as ledger, stock_settings
from inventory_hub.services.order_stock_source import load_source, resolve_lines


ELIGIBLE_ERRORS = frozenset({"order_stock_issued_locked", "order_stock_return_requires_review",
                             "order_stock_status_requires_review"})


class ExceptionError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def candidate(job):
    return job.status == "review" and job.error in ELIGIBLE_ERRORS


def _public(row):
    data = row.preview_data
    return {key: data[key] for key in ("id", "order_number", "shop_code", "warehouse", "expires_at",
            "source_status_name", "issued_at", "lines")} | {
        "preview_hash": row.preview_hash, "status": row.status, "result": row.result}


async def _preview_row(db, identifier, *, lock=False):
    query = select(OrderStockPreview).where(OrderStockPreview.id == str(identifier))
    row = await db.scalar((query.with_for_update() if lock else query).execution_options(populate_existing=True))
    if row is None or row.preview_data.get("trigger") != "exception_resolution":
        raise ExceptionError("order_exception_preview_not_found", 404)
    return row


async def get_preview(db, identifier):
    return _public(await _preview_row(db, identifier))


def _validate_source(job, entry, policy, config, fetched):
    from inventory_hub.services import order_processing as processing
    if processing._inbox_error(entry, job):
        raise ExceptionError("order_exception_context_changed")
    source = fetched["order"]
    created, updated = (datetime.fromisoformat(source[key]) for key in ("created_at", "updated_at"))
    if (source["uuid"] != job.source_uuid or source["order_number"] != job.order_number
            or created != entry.created_at or created < policy.starts_at
            or created < config["automation_starts_at"] or created > stock.now()
            or updated < entry.updated_at or (updated == entry.updated_at and
                (source["status_id"] != entry.status_id or source["origin"] != entry.origin))
            or job.observation_hash != entry.observation_hash or fetched["status_hash"] != policy.status_hash):
        raise ExceptionError("order_exception_source_changed")
    status = next((item for item in fetched["statuses"] if item["id"] == source["status_id"]), None)
    # Only an explicit review/cancellation of already-issued, unchanged lines.
    if status is None or not (stock.is_return_status(status)
            or policy.status_actions.get(str(status["id"]), "review") in ("review", "cancel")):
        raise ExceptionError("order_exception_not_available")
    return status


async def _issued_order(db, job, source, warehouse_id, *, lock=False):
    query = select(ShopOrder).where(ShopOrder.shop_id == job.shop_id, ShopOrder.external_id == job.order_number)
    order = await db.scalar((query.with_for_update() if lock else query).execution_options(populate_existing=True))
    if (order is None or order.stock_state != "issued" or order.stock_warehouse_id != warehouse_id
            or order.stock_source_uuid != job.source_uuid or not order.stock_snapshot
            or not order.stock_issue_result or not order.stock_issued_at
            or stock._content(source) != stock._content(order.stock_snapshot)):
        raise ExceptionError("order_exception_not_available")
    return order


async def _issued_lines(db, order, *, lock=False):
    items = (await db.scalars(select(ShopOrderItem).where(ShopOrderItem.order_id == order.id,
        ShopOrderItem.stock_managed.is_(True)).order_by(ShopOrderItem.id))).all()
    issued, _, errors = ledger._source_lines(order.stock_snapshot)
    expected = {line["line_key"]: line for line in issued}
    current = {item.external_item_id: item for item in items if item.external_item_id in expected}
    if errors or set(current) != set(expected) or any(item.status not in ("removed", "cancelled")
            for item in items if item.external_item_id not in expected):
        raise ExceptionError("order_exception_ledger_changed")
    result = []
    for item in items:
        if item.external_item_id not in expected:
            continue  # Immutable issue excludes earlier removed/cancelled lines.
        line = expected[item.external_item_id]
        if item.product_id != line["product_id"] or item.quantity != line["quantity"]:
            raise ExceptionError("order_exception_ledger_changed")
        movement = await db.scalar(select(StockMovement).where(
            StockMovement.idempotency_key == f"order:{order.id}:issue:{item.id}"))
        if (movement is None or item.status != "fulfilled" or movement.movement_type != MovementType.SALE_OUT
                or movement.product_id != item.product_id or movement.warehouse_id != order.stock_warehouse_id
                or movement.quantity != -item.quantity or movement.reference_type != "shop_order"
                or movement.reference_id != str(order.id) or movement.reference_source != str(order.shop_id)):
            raise ExceptionError("order_exception_ledger_changed")
        query = select(FifoAllocation).where(FifoAllocation.issue_movement_id == movement.id).order_by(FifoAllocation.id)
        allocations = (await db.scalars((query.with_for_update(read=True) if lock else query)
            .execution_options(populate_existing=True))).all()
        returned = sum((allocation.returned_quantity for allocation in allocations), Decimal(0))
        if allocations and (sum((allocation.quantity for allocation in allocations), Decimal(0)) != item.quantity
                or any(a.returned_quantity < 0 or a.returned_quantity > a.quantity for a in allocations)):
            raise ExceptionError("order_exception_ledger_changed")
        sku = await db.scalar(select(Product.sku).where(Product.id == item.product_id))
        result.append({"sku": sku, "product_id": item.product_id, "issue_movement_id": movement.id,
            "issued_qty": format(item.quantity, "f"), "returned_qty": format(returned, "f") if allocations else None,
            "returnable_qty": format(item.quantity - returned, "f") if allocations else None,
            "physical_return_supported": bool(allocations)})
    original = order.stock_issue_result
    if not result and not (not issued and original.get("movements_created") == 0
            and original.get("movement_ids") == [] and original.get("lines") == [] and original.get("effects") == []):
        raise ExceptionError("order_exception_ledger_changed")
    if result and (original.get("movements_created") != len(result)
            or sorted(original.get("movement_ids", [])) != sorted(line["issue_movement_id"] for line in result)):
        raise ExceptionError("order_exception_ledger_changed")
    return result


def _binding(job, entry, policy, config, order):
    return {"job_id": job.id, "inbox_id": entry.id, "generation": job.generation,
        "observation_hash": entry.observation_hash, "source_uuid": job.source_uuid,
        "order_id": order.id, "order_revision": order.stock_revision, "policy_revision": policy.revision,
        "configuration_hash": config["configuration_hash"], "target_fingerprint": config["target_fingerprint"]}


async def _fetch(db, job_id):
    job = await db.get(OrderProcessingJob, job_id)
    if job is None:
        raise ExceptionError("order_exception_not_available", 404)
    shop = await db.get(Shop, job.shop_id)
    config = await stock_settings.effective(db, job.shop_id)
    if not shop or not config.get("processing_ready"):
        raise ExceptionError("order_exception_not_available")
    fetched = await load_source(db, shop, job.order_number, expected_target_fingerprint=config["target_fingerprint"])
    return job.shop_id, fetched


async def _locked(db, job_id, shop_id, fetched):
    from inventory_hub.services import order_processing as processing
    shop, policy, config = await processing._context(db, shop_id, identity=True)
    job = await processing._job(db, job_id)
    if job is None or job.shop_id != shop.id or not candidate(job):
        raise ExceptionError("order_exception_not_available")
    entry = await db.scalar(select(OrderInboxEntry).where(OrderInboxEntry.id == job.inbox_id)
        .with_for_update(read=True).execution_options(populate_existing=True))
    status = _validate_source(job, entry, policy, config, fetched)
    fetched["order"]["lines"] = await resolve_lines(db, shop, fetched["order"])
    order = await _issued_order(db, job, fetched["order"], policy.warehouse_id, lock=True)
    return shop, policy, config, job, entry, order, status


async def preview(db, job_id, payload):
    identifier = str(payload.request_id)
    request_hash = stock._hash({"job_id": job_id, "request_id": identifier, "trigger": "exception_resolution"})
    existing = await db.get(OrderStockPreview, identifier)
    if existing:
        if existing.request_hash != request_hash or existing.preview_data.get("trigger") != "exception_resolution":
            raise ExceptionError("order_exception_request_changed")
        return _public(existing)
    shop_id, fetched = await _fetch(db, job_id)
    shop, policy, config, job, entry, order, status = await _locked(db, job_id, shop_id, fetched)
    # Identity lock serializes concurrent requests, including repeated UUIDs.
    existing = await db.get(OrderStockPreview, identifier, populate_existing=True)
    if existing:
        if existing.request_hash != request_hash:
            raise ExceptionError("order_exception_request_changed")
        return _public(existing)
    at = stock.now()
    warehouse = await db.get(Warehouse, policy.warehouse_id)
    snapshot = {"id": identifier, "trigger": "exception_resolution", "action": "resolve_exception",
        "created_at": at.isoformat(), "expires_at": (at + timedelta(minutes=stock.EXPIRY_MINUTES)).isoformat(),
        "order_number": job.order_number, "shop_code": shop.code,
        "warehouse": {"id": warehouse.id, "code": warehouse.code, "name": warehouse.name},
        "source_status_name": status["name"], "source": fetched["order"],
        "source_hash": fetched["source_hash"], "status_hash": fetched["status_hash"],
        "issued_at": order.stock_issued_at.isoformat(), "lines": await _issued_lines(db, order, lock=True),
        "binding": _binding(job, entry, policy, config, order), "plan": {"ready": True, "errors": []}}
    row = OrderStockPreview(id=identifier, shop_id=shop.id, order_id=order.id, request_hash=request_hash,
        preview_hash=stock._hash(snapshot), preview_data=snapshot, status="prepared", created_at=at,
        expires_at=at + timedelta(minutes=stock.EXPIRY_MINUTES))
    db.add(row)
    await db.flush()
    response = _public(row)
    await db.commit()
    return response


def _replayed(row, payload):
    if row.preview_hash != payload.preview_hash or stock._hash(row.preview_data) != row.preview_hash:
        raise ExceptionError("order_exception_preview_changed")
    if row.status == "completed":
        if row.result.get("confirmation_hash") != stock._hash(payload.model_dump(mode="json")):
            raise ExceptionError("order_exception_confirmation_changed")
        return row.result
    if stock.now() >= row.expires_at:
        raise ExceptionError("order_exception_preview_expired")
    return None


async def resolve(db, identifier, payload, actor):
    from inventory_hub.services import order_processing as processing
    row = await _preview_row(db, identifier)
    replay = _replayed(row, payload)
    if replay:
        return replay
    snapshot = row.preview_data
    job_id = snapshot["binding"]["job_id"]
    shop_id, fetched = await _fetch(db, job_id)
    # Take context before the preview row to match worker/manual ledger lock order.
    shop, policy, config = await processing._context(db, shop_id, identity=True)
    job = await processing._job(db, job_id)
    row = await _preview_row(db, identifier, lock=True)
    replay = _replayed(row, payload)
    if replay:
        return replay
    if not candidate(job):
        raise ExceptionError("order_exception_context_changed")
    entry = await db.scalar(select(OrderInboxEntry).where(OrderInboxEntry.id == job.inbox_id)
        .with_for_update(read=True).execution_options(populate_existing=True))
    _validate_source(job, entry, policy, config, fetched)
    fetched["order"]["lines"] = await resolve_lines(db, shop, fetched["order"])
    order = await _issued_order(db, job, fetched["order"], policy.warehouse_id, lock=True)
    if _binding(job, entry, policy, config, order) != snapshot["binding"]:
        raise ExceptionError("order_exception_context_changed")
    if fetched["source_hash"] != snapshot["source_hash"] or fetched["status_hash"] != snapshot["status_hash"]:
        raise ExceptionError("order_exception_source_changed")
    if await _issued_lines(db, order, lock=True) != snapshot["lines"]:
        raise ExceptionError("order_exception_ledger_changed")
    from inventory_hub.services import stock_native
    await stock_native.record(db, order, fetched["order"], fetched["statuses"], fetched["source_hash"])
    result = {"action": "resolve_exception", "preview_id": row.id, "order_id": order.id,
        "order_number": job.order_number, "stock_state": "issued", "stock_accounting": "accounted",
        "reason": payload.reason, "actor": actor, "movements_created": 0,
        "confirmation_hash": stock._hash(payload.model_dump(mode="json"))}
    row.status, row.completed_at, row.result = "completed", stock.now(), result
    job.source_hash, job.configuration_hash, job.audit_preview_id = fetched["source_hash"], config["configuration_hash"], row.id
    processing._schedule(job, config, status="completed", result=result)
    await db.flush()
    await db.commit()
    return result


async def current_resolution(db, job, entry, config, warehouse_id, *, fetched=None, order=None):
    """Proof shared by polling and publication; a stale acknowledgement is never a bypass."""
    if not job.audit_preview_id or not job.result or job.result.get("action") != "resolve_exception":
        return None
    row = await db.get(OrderStockPreview, job.audit_preview_id)
    if (row is None or row.status != "completed" or row.preview_data.get("trigger") != "exception_resolution"
            or stock._hash(row.preview_data) != row.preview_hash or row.result != job.result):
        return None
    snapshot, binding = row.preview_data, row.preview_data["binding"]
    from inventory_hub.services import order_processing as processing
    if processing._inbox_error(entry, job):
        return None
    if (row.shop_id != job.shop_id or row.order_id != binding["order_id"]
            or binding["job_id"] != job.id or binding["inbox_id"] != entry.id
            or binding["generation"] != job.generation or binding["observation_hash"] != entry.observation_hash
            or job.observation_hash != entry.observation_hash or binding["source_uuid"] != job.source_uuid
            or snapshot["order_number"] != job.order_number or snapshot["warehouse"]["id"] != warehouse_id
            or binding["policy_revision"] != config["authorized_policy_revision"]
            or binding["configuration_hash"] != config["configuration_hash"]
            or binding["target_fingerprint"] != config["target_fingerprint"]
            or job.configuration_hash != config["configuration_hash"] or job.source_hash != snapshot["source_hash"]):
        return None
    if fetched and (fetched["source_hash"] != snapshot["source_hash"] or fetched["status_hash"] != snapshot["status_hash"]
            or stock._content(fetched["order"]) != stock._content(snapshot["source"])):
        return None
    try:
        order = order or await _issued_order(db, job, snapshot["source"], warehouse_id)
        if order.id != binding["order_id"] or order.stock_revision != binding["order_revision"]:
            return None
        await _issued_lines(db, order)
    except ExceptionError:
        return None
    return row.result
