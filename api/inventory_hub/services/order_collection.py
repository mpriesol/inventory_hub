"""Opt-in order metadata collection. Never calls the order stock mutation service."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from math import ceil
from uuid import uuid4

from sqlalchemy import func, or_, select

from inventory_hub.config_io import load_shop
from inventory_hub.db_models import Shop, Warehouse
from inventory_hub.db_models_ext import ShopOrder
from inventory_hub.order_collection_models import OrderCollectionSettings, OrderCollectionRun, OrderInboxEntry
from inventory_hub.order_stock_models import OrderStockPolicy
from inventory_hub.services.order_collection_source import connection_fingerprint
from inventory_hub.services import stock_settings


LEGACY_RUN_DEFAULTS = {"poll_interval_seconds": 300, "retry_base_seconds": 300, "retry_max_seconds": 3600}


class CollectionError(Exception):
    def __init__(self, code, status=409, retry_after=None):
        self.code, self.status = code, status
        self.retry_after = retry_after
        super().__init__(code)


def now():
    return datetime.now(timezone.utc)


def target_fingerprint(shop_code):
    """Bind discovery to its target, without storing or exposing credentials."""
    try:
        config = load_shop(shop_code) or {}
        base, login = config.get("upgates_api_base_url"), config.get("upgates_login")
        if not isinstance(base, str) or not isinstance(login, str) or not login or not config.get("upgates_api_key"):
            return None
        return connection_fingerprint(base, login)
    except (OSError, ValueError, TypeError):
        return None


async def _shop(db, code, *, lock=False):
    statement = select(Shop).where(Shop.code == code, Shop.is_active.is_(True), Shop.platform == "upgates")
    if lock:
        statement = statement.with_for_update()
    row = await db.scalar(statement.execution_options(populate_existing=True))
    if row is None:
        raise CollectionError("order_collection_shop_not_found", 404)
    return row


async def _settings(db, shop_id, *, lock=False):
    statement = select(OrderCollectionSettings).where(OrderCollectionSettings.shop_id == shop_id)
    if lock:
        statement = statement.with_for_update()
    return await db.scalar(statement.execution_options(populate_existing=True))


async def _policy(db, shop_id, *, lock=False):
    statement = select(OrderStockPolicy).where(OrderStockPolicy.shop_id == shop_id)
    if lock:
        statement = statement.with_for_update(read=True)
    return await db.scalar(statement.execution_options(populate_existing=True))


async def _policy_ready(db, policy):
    warehouse = await db.get(Warehouse, policy.warehouse_id) if policy else None
    return warehouse if warehouse and warehouse.is_active else None


async def _configuration(db, shop_id, *, lock=False):
    try:
        return await stock_settings.effective(db, shop_id, lock=lock)
    except stock_settings.SettingsError as error:
        code = ("order_collection_not_configured" if error.code in (
            "stock_settings_policy_required", "stock_settings_warehouse_not_found") else error.code)
        raise CollectionError(code, 409 if code == "order_collection_not_configured" else error.status) from None


async def status(db, shop_code):
    shop = await _shop(db, shop_code)
    policy = await _policy(db, shop.id)
    warehouse = await db.get(Warehouse, policy.warehouse_id) if policy else None
    configuration, configuration_error = None, None
    try:
        configuration = await _configuration(db, shop.id)
    except CollectionError as error:
        configuration_error = error.code
    row = await _settings(db, shop.id)
    fingerprint = target_fingerprint(shop.code)
    fields = ("enabled", "revision", "cursor_at", "last_reconciled_at", "next_poll_at", "last_started_at",
              "last_completed_at", "last_error", "entries_seen", "manual_requested_at")
    return {"shop": {"code": shop.code, "name": shop.name},
            "policy": {"starts_at": policy.starts_at, "warehouse_code": warehouse.code} if warehouse else None,
            "collector": {**{key: getattr(row, key) for key in fields},
                          "manual_pending": row.manual_requested_at is not None} if row else None,
            "configuration_hash": configuration["configuration_hash"] if configuration else None,
            "configuration": configuration["values"] if configuration else None,
            "effective": configuration,
            "configuration_error": configuration_error,
            "connection_configured": fingerprint is not None,
            "connection_matches": row is None or fingerprint == row.target_fingerprint,
            "external_write_enabled": False}


async def configure(db, payload):
    shop = await _shop(db, payload.shop_code, lock=True)
    policy = await _policy(db, shop.id, lock=True)
    if payload.enabled:
        await _configuration(db, shop.id, lock=True)
    row = await _settings(db, shop.id, lock=True)
    if payload.expected_revision != (row.revision if row else None):
        raise CollectionError("order_collection_settings_changed")
    if row is None and not payload.enabled:
        return await status(db, shop.code)
    if payload.enabled:
        if not await _policy_ready(db, policy):
            raise CollectionError("order_collection_not_configured")
        fingerprint = target_fingerprint(shop.code)
        if fingerprint is None:
            raise CollectionError("order_collection_connection_missing")
        if row and row.target_fingerprint != fingerprint:
            raise CollectionError("order_collection_target_changed")
        if row is None:
            row = OrderCollectionSettings(shop_id=shop.id, enabled=True, revision=1, target_fingerprint=fingerprint,
                cursor_at=policy.starts_at, reconcile_cursor_at=policy.starts_at, next_poll_at=now())
            db.add(row)
        else:
            row.revision += 1
            row.enabled = True
            row.next_poll_at = max(now(), row.retry_after_at or now())
            if not (row.last_error == "order_collection_rate_limited" and row.retry_after_at and row.retry_after_at > now()):
                row.last_error = None
    else:
        row.enabled = False
        row.revision += 1
    await db.flush()
    response = await status(db, shop.code)
    await db.commit()
    return response


async def refresh(db, payload):
    shop = await _shop(db, payload.shop_code, lock=True)
    policy = await _policy(db, shop.id, lock=True)
    configuration = await _configuration(db, shop.id, lock=True)
    row = await _settings(db, shop.id, lock=True)
    if payload.expected_revision != (row.revision if row else None):
        raise CollectionError("order_collection_settings_changed")
    if not await _policy_ready(db, policy):
        raise CollectionError("order_collection_not_configured")
    fingerprint = target_fingerprint(shop.code)
    if fingerprint is None:
        raise CollectionError("order_collection_connection_missing")
    if row and fingerprint != row.target_fingerprint:
        raise CollectionError("order_collection_target_changed")
    at = now()
    deadline = configuration.get("retry_after_at")
    if deadline and deadline > at:
        raise CollectionError("order_collection_retry_later", 429)
    if row and row.manual_requested_at is not None:
        response = await status(db, shop.code)
        await db.commit()
        return response  # Repeated requests coalesce until a worker starts it.
    if row and ((row.retry_after_at and at < row.retry_after_at) or
                (row.last_started_at and at < row.last_started_at + timedelta(seconds=60))):
        raise CollectionError("order_collection_retry_later", 429)
    if row is None:
        row = OrderCollectionSettings(shop_id=shop.id, enabled=False, revision=1, target_fingerprint=fingerprint,
            cursor_at=policy.starts_at, reconcile_cursor_at=policy.starts_at, next_poll_at=at)
        db.add(row)
    row.manual_requested_at = at
    row.next_poll_at = at
    await db.flush()
    response = await status(db, shop.code)
    await db.commit()
    return response


async def inbox(db, shop_code, limit=50, offset=0):
    shop = await _shop(db, shop_code)
    total = await db.scalar(select(func.count()).select_from(OrderInboxEntry).where(OrderInboxEntry.shop_id == shop.id))
    rows = (await db.execute(select(OrderInboxEntry, ShopOrder).outerjoin(ShopOrder,
        (ShopOrder.shop_id == OrderInboxEntry.shop_id) & (ShopOrder.stock_source_uuid == OrderInboxEntry.source_uuid)
    ).where(OrderInboxEntry.shop_id == shop.id).order_by(OrderInboxEntry.updated_at.desc(), OrderInboxEntry.id.desc())
    .offset(offset).limit(limit))).all()
    fields = ("id", "order_number", "source_uuid", "created_at", "updated_at", "deleted", "origin", "status_id",
              "observed_at", "last_seen_at", "review_reason")
    return {"entries": [{**{key: getattr(entry, key) for key in fields},
        "stock_state": order.stock_state if order else None,
        "stock_issued_at": order.stock_issued_at if order else None,
        "stock_updated_at": (order.stock_snapshot or {}).get("updated_at") if order else None} for entry, order in rows],
        "total": total, "limit": limit, "offset": offset}


async def runs(db, shop_code):
    shop = await _shop(db, shop_code)
    rows = (await db.scalars(select(OrderCollectionRun).where(OrderCollectionRun.shop_id == shop.id)
                           .order_by(OrderCollectionRun.started_at.desc(), OrderCollectionRun.id).limit(20))).all()
    fields = ("id", "mode", "status", "started_at", "completed_at", "from_at", "until_at", "pages", "observed_count", "error",
              "manual", "configuration_hash", "configuration_snapshot")
    return {"runs": [{**{key: getattr(row, key) for key in fields}, "trigger": "manual" if row.manual else "automatic"} for row in rows]}


async def start_run(db, shop_id):
    # Same shop -> settings order as configure; new child rows acquire a shop FK lock.
    shop = await db.scalar(select(Shop).where(Shop.id == shop_id).with_for_update(read=True)
                           .execution_options(populate_existing=True))
    policy = await _policy(db, shop_id, lock=True)
    configuration, configuration_error = None, None
    try:
        configuration = await _configuration(db, shop_id, lock=True)
    except CollectionError as error:
        configuration_error = error.code
    row = await _settings(db, shop_id, lock=True)
    if not row or (not row.enabled and row.manual_requested_at is None) or row.next_poll_at > now():
        return None
    if row.retry_after_at and row.retry_after_at > now():
        return None
    deadline = configuration.get("retry_after_at") if configuration else None
    if deadline and deadline > now():
        row.next_poll_at = max(row.next_poll_at, deadline)
        return None
    warehouse = await _policy_ready(db, policy)
    error = configuration_error
    if not shop or not shop.is_active or shop.platform != "upgates" or not warehouse or not warehouse.is_active:
        error = "order_collection_not_configured"
    elif target_fingerprint(shop.code) != row.target_fingerprint:
        error = "order_collection_target_changed"
    if error:
        row.enabled, row.last_error = False, error
        row.revision += 1
        values = configuration["values"] if configuration else LEGACY_RUN_DEFAULTS
        row.next_poll_at = now() + timedelta(seconds=values["retry_max_seconds"])
        return None
    at = now()
    values = configuration["values"]
    manual = row.manual_requested_at is not None
    reconcile_due = row.last_reconciled_at is None or at - row.last_reconciled_at >= timedelta(hours=values["reconcile_interval_hours"])
    # Delta scans take every other turn while a multi-window reconciliation is in progress.
    mode = "reconcile" if reconcile_due and row.last_mode != "reconcile" else "delta"
    if mode == "reconcile":
        row.reconcile_until_at = row.reconcile_until_at or at
        start = max(policy.starts_at, row.reconcile_cursor_at)
        until = min(start + timedelta(days=values["reconcile_window_days"]), row.reconcile_until_at)
        created_from, changed_from = start, policy.starts_at
    else:
        start, until = max(policy.starts_at, row.cursor_at - timedelta(minutes=values["overlap_minutes"])), at
        created_from, changed_from = policy.starts_at, start
    run = OrderCollectionRun(id=str(uuid4()), shop_id=shop_id, settings_revision=row.revision, mode=mode,
        status="running", started_at=at, from_at=start, until_at=until, pages=0, observed_count=0, manual=manual,
        configuration_hash=configuration["configuration_hash"], configuration_snapshot=dict(values))
    db.add(run)
    row.last_started_at, row.last_mode = at, mode
    row.manual_requested_at = None
    row.next_poll_at = at + timedelta(seconds=values["poll_interval_seconds"])
    await db.flush()
    return {"id": run.id, "shop_id": shop_id, "shop_code": shop.code, "target_fingerprint": row.target_fingerprint, "created_from": created_from,
            "changed_from": changed_from, "created_to": until, "manual": manual,
            "configuration_hash": configuration["configuration_hash"], "configuration_snapshot": dict(values)}


async def _running(db, run_id):
    run = await db.get(OrderCollectionRun, run_id)
    shop = await db.scalar(select(Shop).where(Shop.id == run.shop_id).with_for_update(read=True)
                           .execution_options(populate_existing=True))
    policy = await _policy(db, run.shop_id, lock=True)
    configuration = await _configuration(db, run.shop_id, lock=True)
    row = await _settings(db, run.shop_id, lock=True)
    if run.status != "running" or (not row.enabled and not run.manual) or row.revision != run.settings_revision:
        raise CollectionError("order_collection_interrupted")
    if configuration["configuration_hash"] != run.configuration_hash:
        raise CollectionError("order_collection_settings_changed")
    deadline = configuration.get("retry_after_at")
    if deadline and deadline > now():
        raise CollectionError("order_collection_retry_later", 429, ceil((deadline - now()).total_seconds()))
    if not shop or not shop.is_active or shop.platform != "upgates":
        raise CollectionError("order_collection_not_configured")
    if not await _policy_ready(db, policy):
        raise CollectionError("order_collection_not_configured")
    if target_fingerprint(shop.code) != row.target_fingerprint:
        raise CollectionError("order_collection_target_changed")
    return row, run


async def check_run(db, run_id):
    await _running(db, run_id)


async def save_page(db, run_id, entries):
    row, run = await _running(db, run_id)
    at = now()
    if entries:
        existing = (await db.scalars(select(OrderInboxEntry).where(OrderInboxEntry.shop_id == run.shop_id,
            or_(OrderInboxEntry.source_uuid.in_([entry["uuid"] for entry in entries]),
                OrderInboxEntry.order_number.in_([entry["order_number"] for entry in entries]))))).all()
        by_uuid = {entry.source_uuid: entry for entry in existing}
        by_number = {entry.order_number: entry for entry in existing}
        for data in entries:
            identifier, number = data["uuid"], data["order_number"]
            old, numbered = by_uuid.get(identifier), by_number.get(number)
            if (old and old.order_number != number) or (numbered and numbered.source_uuid != identifier):
                for conflict in (old, numbered):
                    if conflict:
                        conflict.review_reason = "order_collection_identity_conflict"
                        conflict.last_seen_at = at
                continue
            digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            created, updated = datetime.fromisoformat(data["created_at"]), datetime.fromisoformat(data["updated_at"])
            if old is None:
                old = OrderInboxEntry(shop_id=run.shop_id, source_uuid=identifier, order_number=number,
                    created_at=created, updated_at=updated, deleted=data["deleted"], origin=data["origin"],
                    status_id=data["status_id"], observation_hash=digest, observed_at=at, last_seen_at=at,
                    review_reason="order_collection_deleted" if data["deleted"] else None)
                db.add(old)
                by_uuid[identifier], by_number[number] = old, old
            else:
                old.last_seen_at = at
                if created != old.created_at:
                    old.review_reason = "order_collection_identity_conflict"
                elif updated < old.updated_at:
                    continue
                elif updated == old.updated_at and digest != old.observation_hash:
                    # Equal timestamps do not prove which observation is newer.
                    old.review_reason = "order_collection_source_conflict"
                elif updated > old.updated_at:
                    old.updated_at, old.deleted, old.origin = updated, data["deleted"], data["origin"]
                    old.status_id, old.observation_hash, old.observed_at = data["status_id"], digest, at
                    if old.review_reason not in ("order_collection_identity_conflict", "order_collection_source_conflict"):
                        old.review_reason = "order_collection_deleted" if data["deleted"] else None
    run.pages += 1
    run.observed_count += len(entries)
    row.entries_seen = run.observed_count
    await db.flush()


async def complete_run(db, run_id):
    row, run = await _running(db, run_id)
    at = now()
    if run.mode == "delta":
        row.cursor_at = max(row.cursor_at, run.started_at)
    else:
        row.reconcile_cursor_at = run.until_at
        if run.until_at >= row.reconcile_until_at:
            policy = await db.get(OrderStockPolicy, run.shop_id)
            row.reconcile_cursor_at, row.reconcile_until_at = policy.starts_at, None
            row.last_reconciled_at = run.started_at
    row.last_completed_at, row.last_error, row.failure_count, row.retry_after_at = at, None, 0, None
    row.next_poll_at = max(at, row.last_started_at + timedelta(seconds=60)) if row.manual_requested_at else at + timedelta(seconds=run.configuration_snapshot["poll_interval_seconds"])
    run.status, run.completed_at = "completed", at


async def fail_run(db, run_id, code, retry_after=None):
    run = await db.get(OrderCollectionRun, run_id)
    if not run or run.status != "running":
        return
    await db.scalar(select(Shop).where(Shop.id == run.shop_id).with_for_update(read=True))
    row = await _settings(db, run.shop_id, lock=True)
    at = now()
    run.status, run.completed_at, run.error = "failed", at, code
    if row.revision != run.settings_revision:
        return  # A newer operator decision remains authoritative.
    preserve_rate_limit = (code == "order_collection_retry_later" and
        row.last_error == "order_collection_rate_limited" and row.retry_after_at and row.retry_after_at > at)
    if not preserve_rate_limit:
        row.last_error = code
    row.failure_count += 1
    values = {**LEGACY_RUN_DEFAULTS, **(run.configuration_snapshot or {})}
    delay = min(values["retry_max_seconds"], values["retry_base_seconds"] * 2 ** min(row.failure_count - 1, 20))
    if type(retry_after) is int and retry_after > 0:
        delay = max(delay, min(retry_after, 604800))
    row.retry_after_at = max(at + timedelta(seconds=delay), row.retry_after_at or at)
    row.next_poll_at = row.retry_after_at
    if code in ("order_collection_upgates_access", "order_collection_target_changed", "order_collection_not_configured"):
        row.enabled = False
        row.revision += 1
