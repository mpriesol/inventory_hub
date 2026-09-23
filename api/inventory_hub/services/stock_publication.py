"""Explicit, one-attempt stock publication under a durable warehouse hold.

All remote I/O happens after commit. A sending record is a durable fence, not
permission to retry: interrupted or ambiguous attempts require operator repair.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4

from sqlalchemy import func, select, text

from inventory_hub.db_models import Shop, Warehouse
from inventory_hub.order_stock_models import OrderStockPolicy
from inventory_hub.order_collection_models import OrderCollectionSettings
from inventory_hub.stock_settings_models import StockShopSettings
from inventory_hub.stock_publication_models import (
    StockPublicationPolicy as Policy, StockPublicationHold as Hold,
    StockPublicationBatch as Batch, StockPublicationItem as Item,
)
from inventory_hub.settings import settings
from inventory_hub.services import stock_projection, stock_publication_source as source
from inventory_hub.services.order_collection import target_fingerprint
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK
from inventory_hub.services.stock_settings import effective, SettingsError


class PublicationError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def now():
    return datetime.now(timezone.utc)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


def _local_hash(projection):
    return digest({key: projection[key] for key in ("shop_code", "warehouse", "rows")})


def _enabled():
    return bool(settings.STOCK_PUBLICATION_WRITE_ENABLED)


async def _one(db, model, condition, lock=None):
    statement = select(model).where(condition).execution_options(populate_existing=True)
    if lock:
        statement = statement.with_for_update(read=lock == "share")
    return await db.scalar(statement)


async def _network_ready(db, shop_code, fingerprint):
    """Shared quota applies to maintenance reads as well as collection/processing."""
    if target_fingerprint(shop_code) != fingerprint:
        raise PublicationError("stock_publication_target_changed")
    shop_id = await db.scalar(select(Shop.id).where(Shop.code == shop_code))
    row = await _one(db, StockShopSettings, StockShopSettings.shop_id == shop_id)
    collector = await _one(db, OrderCollectionSettings, OrderCollectionSettings.shop_id == shop_id)
    delay = row.processing_retry_after_at if row else None
    if (collector and collector.target_fingerprint == fingerprint
            and collector.last_error == "order_collection_rate_limited" and collector.retry_after_at):
        delay = max(delay, collector.retry_after_at) if delay else collector.retry_after_at
    if delay and delay > now():
        await db.commit()
        raise PublicationError("stock_publication_retry_later", 429)
    await db.commit()


async def _remember_rate_limit(db, shop_code, fingerprint, error):
    if not isinstance(error, source.SourceError) or error.status != 429:
        return
    await db.rollback()
    shop = await _one(db, Shop, Shop.code == shop_code, "update")
    if shop is None or target_fingerprint(shop_code) != fingerprint:
        await db.commit()
        return
    policy = await _one(db, OrderStockPolicy, OrderStockPolicy.shop_id == shop.id, "share")
    if policy:
        await _one(db, Warehouse, Warehouse.id == policy.warehouse_id, "share")
    row = await _one(db, StockShopSettings, StockShopSettings.shop_id == shop.id, "update")
    if row is None:
        row = StockShopSettings(shop_id=shop.id, revision=1, overrides={}, mode="manual", updated_at=now())
        db.add(row)
    delay = getattr(error, "retry_after", None)
    delay = min(604800, max(1, delay)) if type(delay) is int else 300
    until = now() + timedelta(seconds=delay)
    if row.processing_retry_after_at is None or row.processing_retry_after_at < until:
        row.processing_retry_after_at = until
    await db.flush()
    await db.commit()


async def _scope(db, shop_code, *, lock=False, shop_write=False):
    # Identity writers and stock writers take this prefix before Warehouse.
    # Never take product/balance/order locks after draining the warehouse.
    if lock:
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": IDENTITY_WRITE_LOCK})
    shop = await _one(db, Shop, Shop.code == shop_code, "update" if shop_write else "share" if lock else None)
    if shop is None or not shop.is_active or shop.platform != "upgates":
        raise PublicationError("stock_publication_shop_not_found", 404)
    order_policy = await _one(db, OrderStockPolicy, OrderStockPolicy.shop_id == shop.id, "share" if lock else None)
    if order_policy is None:
        raise PublicationError("stock_publication_order_policy_required")
    warehouse = await _one(db, Warehouse, Warehouse.id == order_policy.warehouse_id, "update" if lock else None)
    if warehouse is None or not warehouse.is_active:
        raise PublicationError("stock_publication_warehouse_unavailable")
    try:
        config = await effective(db, shop.id, lock=lock)
    except SettingsError as error:
        raise PublicationError(error.code, error.status) from None
    policy = await _one(db, Policy, Policy.shop_id == shop.id, "share" if lock else None)
    hold = await _one(db, Hold, (Hold.warehouse_id == warehouse.id) & Hold.active.is_(True), "update" if lock else None)
    return {"shop": shop, "warehouse": warehouse, "order_policy": order_policy, "policy": policy,
            "config": config, "hold": hold, "target": target_fingerprint(shop_code)}


def _warehouse(scope):
    row = scope["warehouse"]
    return {"id": row.id, "code": row.code, "name": row.name}


def _authority(scope):
    return {"hold_id": scope["hold"].id if scope["hold"] else None,
            "policy_revision": scope["policy"].revision if scope["policy"] else 0,
            "order_policy_revision": scope["order_policy"].revision,
            "configuration_hash": scope["config"]["configuration_hash"],
            "target_fingerprint": scope["target"], "warehouse": _warehouse(scope)}


def _assert_target(scope):
    if not scope["target"]:
        raise PublicationError("stock_publication_target_unavailable")
    if scope["policy"] and scope["policy"].target_fingerprint != scope["target"]:
        raise PublicationError("stock_publication_target_changed")


def _assert_hold(scope, hold_id=None):
    if scope["hold"] is None or (hold_id and scope["hold"].id != hold_id):
        raise PublicationError("stock_publication_hold_required")


async def _quarantine(db, scope, excluding=None):
    statement = select(Batch.id).join(Hold, Hold.id == Batch.hold_id).where(
        Hold.warehouse_id == scope["warehouse"].id, Batch.status == "uncertain")
    if excluding:
        statement = statement.where(Batch.id != excluding)
    if await db.scalar(statement.limit(1)):
        raise PublicationError("stock_publication_warehouse_uncertain")


async def _hold_dto(db, hold):
    if hold is None:
        return None
    shop_code = await db.scalar(select(Shop.code).where(Shop.id == hold.shop_id))
    return {"id": hold.id, "warehouse_id": hold.warehouse_id, "shop_code": shop_code,
            "active": hold.active, "assertions": hold.assertions, "created_at": hold.created_at,
            "closed_at": hold.closed_at, "close_result": hold.close_result}


async def options(db, shop_code):
    scope = await _scope(db, shop_code)
    policy = scope["policy"]
    return {"shop": {"code": scope["shop"].code, "name": scope["shop"].name},
            "warehouse": _warehouse(scope),
            "policy": {"revision": policy.revision if policy else 0, "enabled": policy.enabled if policy else False,
                       "target_fingerprint": policy.target_fingerprint if policy else None},
            "hold": await _hold_dto(db, scope["hold"]), "server_write_enabled": _enabled(),
            "external_write_enabled": _enabled() and bool(policy and policy.enabled),
            "values": {key: scope["config"]["values"][key] for key in
                       ("publication_batch_size", "publication_preview_minutes")}}


async def configure(db, payload):
    scope = await _scope(db, payload.shop_code, lock=True, shop_write=True)
    policy = scope["policy"]
    if payload.expected_revision != (policy.revision if policy else 0):
        raise PublicationError("stock_publication_policy_changed")
    if payload.enabled and not scope["target"]:
        raise PublicationError("stock_publication_target_unavailable")
    if policy is None:
        policy = Policy(shop_id=scope["shop"].id, revision=1)
        db.add(policy)
    else:
        policy.revision += 1
    policy.enabled, policy.target_fingerprint, policy.updated_at = payload.enabled, scope["target"], now()
    await db.flush()
    result = await options(db, payload.shop_code)
    await db.commit()
    return result


async def open_hold(db, payload):
    scope = await _scope(db, payload.shop_code, lock=True)
    hold = scope["hold"]
    if hold is None:
        hold = Hold(id=str(uuid4()), shop_id=scope["shop"].id, warehouse_id=scope["warehouse"].id,
                    active=True, created_at=now(), assertions={"external_writers_paused": True,
                    "orders_reconciled": True, "confirmed": True,
                    "scope": "all warehouse channels, checkout, POS and product administration"})
        db.add(hold)
        await db.flush()
    result = await _hold_dto(db, hold)
    await db.commit()
    return result


async def release_hold(db, identifier, payload):
    # Release must remain possible if a shop's policy/configuration changed.
    original = await _one(db, Hold, Hold.id == str(identifier))
    if original is None:
        raise PublicationError("stock_publication_hold_not_found", 404)
    await _one(db, Warehouse, Warehouse.id == original.warehouse_id, "update")
    hold = await _one(db, Hold, Hold.id == original.id, "update")
    rows = (await db.scalars(select(Batch).where(Batch.hold_id == hold.id).order_by(Batch.id)
                            .with_for_update().execution_options(populate_existing=True))).all()
    if any(row.status in ("queued", "running", "uncertain") for row in rows):
        raise PublicationError("stock_publication_hold_busy")
    if hold.active:
        for batch in rows:
            if batch.status == "prepared":
                await _stop(db, batch, "cancelled", "stock_publication_hold_released")
        hold.active, hold.closed_at = False, now()
        hold.close_result = {"confirmed": True, "maintenance_completed": True}
        await db.flush()
    result = await _hold_dto(db, hold)
    await db.commit()
    return result


async def _items(db, identifier):
    return list((await db.scalars(select(Item).where(Item.batch_id == identifier).order_by(Item.position)
                                 .execution_options(populate_existing=True))).all())


async def _dto(db, batch, *, detailed=True):
    result = {key: getattr(batch, key) for key in ("id", "hold_id", "status", "preview_hash", "created_at",
              "expires_at", "queued_at", "started_at", "completed_at", "error", "result")}
    result["shop_code"] = await db.scalar(select(Shop.code).where(Shop.id == batch.shop_id))
    if detailed:
        result["preview_data"] = batch.preview_data
        result["items"] = [{key: getattr(item, key) for key in ("id", "position", "sku", "target", "quantity",
            "before_quantity", "after_quantity", "status", "attempt_id", "attempt_started_at", "attempt_completed_at",
            "verified_at", "error", "observation", "acknowledgement", "resolution")} for item in await _items(db, batch.id)]
    return result


async def get_batch(db, identifier):
    batch = await _one(db, Batch, Batch.id == str(identifier))
    if batch is None:
        raise PublicationError("stock_publication_batch_not_found", 404)
    return await _dto(db, batch)


async def batches(db, shop_code, limit=50, offset=0):
    shop = await _one(db, Shop, Shop.code == shop_code)
    if shop is None:
        raise PublicationError("stock_publication_shop_not_found", 404)
    rows = (await db.scalars(select(Batch).where(Batch.shop_id == shop.id).order_by(Batch.created_at.desc(), Batch.id)
                            .limit(limit).offset(offset))).all()
    return {"batches": [await _dto(db, row, detailed=False) for row in rows],
            "total": await db.scalar(select(func.count()).select_from(Batch).where(Batch.shop_id == shop.id)),
            "limit": limit, "offset": offset}


async def _projection(db, shop_code, skus):
    try:
        return await stock_projection.preview(db, shop_code, skus)
    except stock_projection.StockProjectionError as error:
        raise PublicationError(error.code, error.status) from None


async def preview(db, payload):
    identifier = str(payload.request_id)
    request_hash = digest({"shop_code": payload.shop_code, "skus": payload.skus})
    scope = await _scope(db, payload.shop_code, lock=True)
    existing = await _one(db, Batch, Batch.id == identifier)
    if existing:
        if existing.request_hash != request_hash:
            raise PublicationError("stock_publication_request_conflict")
        result = await _dto(db, existing)
        await db.commit()
        return result
    _assert_target(scope)
    _assert_hold(scope)
    await _quarantine(db, scope)
    if len(payload.skus) > scope["config"]["values"]["publication_batch_size"]:
        raise PublicationError("stock_publication_batch_too_large", 422)
    projection = await _projection(db, payload.shop_code, payload.skus)
    authority = _authority(scope)
    created = now()
    expires = created + timedelta(minutes=scope["config"]["values"]["publication_preview_minutes"])
    rows = deepcopy(projection["rows"])
    await db.commit()  # Remote reads never keep a transaction/warehouse lock open.
    rate_limited = False
    for row in rows:
        row["remote"] = None
        if row["errors"]:
            continue
        if rate_limited:
            row["errors"].append("stock_publication_read_not_attempted")
            continue
        try:
            await _network_ready(db, payload.shop_code, authority["target_fingerprint"])
            row["remote"] = await source.read_stock(payload.shop_code, row["target"], authority["target_fingerprint"])
        except (source.SourceError, PublicationError) as error:
            row["errors"].append(error.code)
            await _remember_rate_limit(db, payload.shop_code, authority["target_fingerprint"], error)
            rate_limited = (error.status in (401, 403, 429) or error.code == "stock_publication_upgates_access")
        except asyncio.CancelledError:
            raise
        except Exception:
            row["errors"].append("stock_publication_source_unavailable")
    scope = await _scope(db, payload.shop_code, lock=True)
    existing = await _one(db, Batch, Batch.id == identifier)
    if existing:
        if existing.request_hash != request_hash:
            raise PublicationError("stock_publication_request_conflict")
        result = await _dto(db, existing)
        await db.commit()
        return result
    if _authority(scope) != authority:
        raise PublicationError("stock_publication_configuration_changed")
    _assert_hold(scope)
    await _quarantine(db, scope)
    if _local_hash(await _projection(db, payload.shop_code, payload.skus)) != _local_hash(projection):
        raise PublicationError("stock_publication_local_changed")
    data = {"shop_code": payload.shop_code, **authority, "local_hash": _local_hash(projection),
            "rows": rows, "created_at": created.isoformat(), "expires_at": expires.isoformat()}
    valid = all(not row["errors"] and row["remote"] is not None for row in rows)
    batch = Batch(id=identifier, shop_id=scope["shop"].id, hold_id=scope["hold"].id, request_hash=request_hash,
        preview_hash=digest(data), preview_data=data, status="prepared" if valid else "blocked", created_at=created,
        expires_at=expires, error=None if valid else "stock_publication_preview_blocked")
    db.add(batch)
    await db.flush()
    for position, row in enumerate(rows):
        if not row["errors"] and row["remote"] is not None:
            db.add(Item(batch_id=identifier, position=position, sku=row["sku"], target=row["remote"]["identity"],
                        quantity=row["qty_available"], before_quantity=row["remote"]["quantity"], status="prepared"))
    await db.flush()
    result = await _dto(db, batch)
    await db.commit()
    return result


async def _batch_scope(db, identifier):
    shop_code = await db.scalar(select(Shop.code).join(Batch, Batch.shop_id == Shop.id).where(Batch.id == str(identifier)))
    if shop_code is None:
        raise PublicationError("stock_publication_batch_not_found", 404)
    scope = await _scope(db, shop_code, lock=True)
    batch = await _one(db, Batch, Batch.id == str(identifier), "update")
    return scope, batch


async def _ready(db, scope, batch):
    if not _enabled():
        raise PublicationError("stock_publication_write_disabled")
    if not scope["policy"] or not scope["policy"].enabled:
        raise PublicationError("stock_publication_policy_disabled")
    _assert_target(scope)
    _assert_hold(scope, batch.hold_id)
    if _authority(scope) != {key: batch.preview_data[key] for key in _authority(scope)}:
        raise PublicationError("stock_publication_configuration_changed")
    await _quarantine(db, scope, excluding=batch.id)
    if scope["config"].get("retry_after_at") and scope["config"]["retry_after_at"] > now():
        raise PublicationError("stock_publication_retry_later", 429)
    projection = await _projection(db, scope["shop"].code, [row["sku"] for row in batch.preview_data["rows"]])
    if _local_hash(projection) != batch.preview_data["local_hash"]:
        raise PublicationError("stock_publication_local_changed")


async def submit(db, identifier, payload):
    scope, batch = await _batch_scope(db, identifier)
    if batch.preview_hash != payload.preview_hash:
        raise PublicationError("stock_publication_preview_changed")
    if batch.status not in ("queued", "running", "completed"):
        if batch.status != "prepared":
            raise PublicationError("stock_publication_batch_not_prepared")
        if batch.expires_at <= now():
            raise PublicationError("stock_publication_preview_expired")
        await _ready(db, scope, batch)
        items = await _items(db, batch.id)
        if len(items) != len(batch.preview_data["rows"]) or any(item.status != "prepared" for item in items):
            raise PublicationError("stock_publication_preview_blocked")
        batch.status, batch.queued_at = "queued", now()
        await db.flush()
    result = await _dto(db, batch)
    await db.commit()
    return result


async def _stop(db, batch, status, error):
    batch.status, batch.error, batch.completed_at = status, error, now()
    for item in await _items(db, batch.id):
        if item.status == "prepared":
            item.status, item.error = "cancelled", error
    await db.flush()
    batch.result = {"status": status, "verified": sum(item.status == "verified" for item in await _items(db, batch.id)),
                    "error": error}
    await db.flush()


async def _maintenance_batch(db, identifier):
    """Recovery/cancellation must work even after shop credentials or policy change."""
    original = await _one(db, Batch, Batch.id == str(identifier))
    if original is None:
        raise PublicationError("stock_publication_batch_not_found", 404)
    hold = await _one(db, Hold, Hold.id == original.hold_id)
    await _one(db, Warehouse, Warehouse.id == hold.warehouse_id, "update")
    hold = await _one(db, Hold, Hold.id == hold.id, "update")
    batch = await _one(db, Batch, Batch.id == original.id, "update")
    return hold, batch


async def cancel(db, identifier, payload):
    _, batch = await _maintenance_batch(db, identifier)
    if batch.status not in ("prepared", "queued", "running", "blocked", "cancelled"):
        raise PublicationError("stock_publication_batch_busy")
    if batch.status != "cancelled":
        if any(item.attempt_id or item.status in ("sending", "uncertain") for item in await _items(db, batch.id)):
            raise PublicationError("stock_publication_batch_busy")
        await _stop(db, batch, "cancelled", None)
    result = await _dto(db, batch)
    await db.commit()
    return result


async def _observe(db, identifier, *, resolving):
    hold, batch = await _maintenance_batch(db, identifier)
    if batch.status in ("queued", "running"):
        raise PublicationError("stock_publication_batch_busy")
    if resolving and batch.status != "uncertain":
        raise PublicationError("stock_publication_resolution_not_required")
    shop_code = await db.scalar(select(Shop.code).where(Shop.id == batch.shop_id))
    fingerprint = batch.preview_data["target_fingerprint"]
    if target_fingerprint(shop_code) != fingerprint:
        raise PublicationError("stock_publication_target_changed")
    batch_status = batch.status
    targets = [{"id": item.id, "target": deepcopy(item.target),
                "fence": (item.attempt_id, item.status, deepcopy(item.observation), deepcopy(item.resolution))}
               for item in await _items(db, batch.id)]
    await db.commit()
    observations = {}
    for item in targets:
        try:
            await _network_ready(db, shop_code, fingerprint)
            observations[item["id"]] = {"observed_at": now().isoformat(),
                **await source.read_stock(shop_code, item["target"], fingerprint)}
        except (source.SourceError, PublicationError) as error:
            await _remember_rate_limit(db, shop_code, fingerprint, error)
            observations[item["id"]] = {"observed_at": now().isoformat(), "error": error.code}
            if (error.status in (401, 403, 429) or error.code == "stock_publication_upgates_access"):
                break
        except asyncio.CancelledError:
            raise
        except Exception:
            observations[item["id"]] = {"observed_at": now().isoformat(), "error": "stock_publication_source_unavailable"}
    hold, batch = await _maintenance_batch(db, identifier)
    if batch.status in ("queued", "running"):
        raise PublicationError("stock_publication_batch_busy")
    # No configuration change can redirect a recovery read to another target.
    if target_fingerprint(shop_code) != fingerprint:
        raise PublicationError("stock_publication_target_changed")
    current_items = await _items(db, batch.id)
    fences = {item["id"]: item["fence"] for item in targets}
    if batch.status != batch_status or any(fences.get(item.id) !=
            (item.attempt_id, item.status, item.observation, item.resolution) for item in current_items):
        raise PublicationError("stock_publication_observation_changed")
    for item in current_items:
        observation = observations.get(item.id)
        if observation is None:
            continue
        item.observation = observation
        item.after_quantity = observation.get("quantity")
        if resolving and item.status == "uncertain":
            item.resolution = {"confirmed": True, "external_requests_finished": True, "at": now().isoformat(),
                               "observation": observation}
            if not observation.get("error") and observation.get("identity") == item.target:
                matches = observation["quantity"] == item.quantity
                item.status, item.error = ("verified", None) if matches else ("failed", "stock_publication_remote_mismatch")
                if matches:
                    item.verified_at = now()
    await db.flush()
    if resolving and batch.status == "uncertain":
        items = await _items(db, batch.id)
        if not any(item.status == "uncertain" for item in items):
            if all(item.status == "verified" for item in items):
                await _stop(db, batch, "completed", None)
            else:
                await _stop(db, batch, "blocked", "stock_publication_resolution_incomplete")
    result = await _dto(db, batch)
    await db.commit()
    return result


async def verify(db, identifier, payload):
    return await _observe(db, identifier, resolving=False)


async def resolve(db, identifier, payload):
    return await _observe(db, identifier, resolving=True)


async def _failure(db, identifier, item_id, error, *, uncertain=False, attempt_id=None):
    await db.rollback()
    _, batch = await _maintenance_batch(db, identifier)
    if batch.status != "running":
        await db.commit()
        return
    if item_id is not None:
        item = await _one(db, Item, (Item.id == item_id) & (Item.batch_id == batch.id), "update")
        if attempt_id is not None and item.attempt_id != attempt_id:
            raise PublicationError("stock_publication_attempt_changed")
        item.status, item.error = ("uncertain" if uncertain else "failed"), error
        item.attempt_completed_at = now() if item.attempt_id else None
        await db.flush()
    await _stop(db, batch, "uncertain" if uncertain else "blocked", error)
    await db.commit()


async def recover(db):
    """Only the global worker-lock owner may recover an interrupted run."""
    identifiers = list((await db.scalars(select(Batch.id).where(Batch.status == "running").order_by(Batch.id))).all())
    await db.commit()
    for identifier in identifiers:
        _, batch = await _maintenance_batch(db, identifier)
        if batch.status != "running":
            await db.commit()
            continue
        uncertain = False
        for item in await _items(db, batch.id):
            if item.status == "sending":
                item.status, item.error = "uncertain", "stock_publication_attempt_interrupted"
                uncertain = True
        await db.flush()
        await _stop(db, batch, "uncertain" if uncertain else "blocked", "stock_publication_attempt_interrupted")
        await db.commit()
    return len(identifiers)


async def process_batch(db, identifier):
    """Single worker owner; each leaf gets at most one durable send attempt."""
    try:
        scope, batch = await _batch_scope(db, identifier)
        if batch.status != "queued":
            result = await _dto(db, batch)
            await db.commit()
            return result
        batch.status, batch.started_at = "running", now()
        await db.flush()
        await _ready(db, scope, batch)
        if batch.expires_at <= now():
            raise PublicationError("stock_publication_preview_expired")
        items = [{"id": item.id, "target": deepcopy(item.target), "quantity": item.quantity,
                  "before_quantity": item.before_quantity} for item in await _items(db, batch.id)]
        shop_code, fingerprint = scope["shop"].code, batch.preview_data["target_fingerprint"]
        await db.commit()
    except PublicationError as error:
        # Persist a known, unsent block even if current configuration is unusable.
        await db.rollback()
        _, batch = await _maintenance_batch(db, identifier)
        if batch.status == "queued":
            await _stop(db, batch, "blocked", error.code)
        await db.commit()
        return await get_batch(db, identifier)
    for frozen in items:
        attempt_id = None
        acknowledgement = None
        try:
            # Re-read the exact frozen leaf before creating a send intent.
            await _network_ready(db, shop_code, fingerprint)
            observation = await source.read_stock(shop_code, frozen["target"], fingerprint)
            if observation["identity"] != frozen["target"]:
                raise PublicationError("stock_publication_identity_changed")
            if observation["quantity"] not in (frozen["quantity"], frozen["before_quantity"]):
                raise PublicationError("stock_publication_remote_changed")
            scope, batch = await _batch_scope(db, identifier)
            await _ready(db, scope, batch)
            item = await _one(db, Item, (Item.id == frozen["id"]) & (Item.batch_id == batch.id), "update")
            if batch.status != "running" or item.status != "prepared" or item.attempt_id:
                raise PublicationError("stock_publication_attempt_changed")
            item.observation, item.after_quantity = observation, observation["quantity"]
            if observation["quantity"] == item.quantity:
                item.status, item.verified_at = "verified", now()
                await db.flush()
                await db.commit()
                continue
            attempt_id = str(uuid4())
            item.status, item.attempt_id, item.attempt_started_at = "sending", attempt_id, now()
            await db.flush()
            await db.commit()  # Durable fence precedes the only PUT.
            acknowledgement = await source.write_stock_once(shop_code, frozen["target"], frozen["quantity"], fingerprint)
            if acknowledgement != {"acknowledged": True}:
                raise PublicationError("stock_publication_acknowledgement_invalid")
            await _network_ready(db, shop_code, fingerprint)
            readback = await source.read_stock(shop_code, frozen["target"], fingerprint)
            if readback["identity"] != frozen["target"] or readback["quantity"] != frozen["quantity"]:
                raise PublicationError("stock_publication_readback_mismatch")
            scope, batch = await _batch_scope(db, identifier)
            await _ready(db, scope, batch)
            item = await _one(db, Item, Item.id == frozen["id"], "update")
            if batch.status != "running" or item.status != "sending" or item.attempt_id != attempt_id:
                raise PublicationError("stock_publication_attempt_changed")
            item.status, item.error, item.observation = "verified", None, readback
            item.acknowledgement, item.after_quantity = acknowledgement, readback["quantity"]
            item.verified_at = item.attempt_completed_at = now()
            await db.flush()
            await db.commit()
        except asyncio.CancelledError:
            await db.rollback()
            raise  # Recovery quarantines a persisted sending intent.
        except (PublicationError, source.SourceError) as error:
            uncertain = attempt_id is not None and (not isinstance(error, source.SourceError) or error.uncertain)
            # Any failed GET after an acknowledged PUT is ambiguous, even if
            # the read transport knows that its own GET made no mutation.
            if attempt_id is not None and acknowledgement is not None:
                uncertain = True
            await _failure(db, identifier, frozen["id"], error.code, uncertain=uncertain, attempt_id=attempt_id)
            await _remember_rate_limit(db, shop_code, fingerprint, error)
            break
        except Exception:
            await _failure(db, identifier, frozen["id"], "stock_publication_source_unavailable",
                           uncertain=attempt_id is not None, attempt_id=attempt_id)
            break
    _, batch = await _maintenance_batch(db, identifier)
    if batch.status == "running":
        if all(item.status == "verified" for item in await _items(db, batch.id)):
            await _stop(db, batch, "completed", None)
        else:
            await _stop(db, batch, "blocked", "stock_publication_incomplete")
    result = await _dto(db, batch)
    await db.commit()
    return result
