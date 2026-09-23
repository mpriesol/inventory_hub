"""Explicitly authorized automatic LOCAL order stock operations.

Callers own commit/rollback. Fetches happen between start_job and finish_job,
without transaction locks. Completion, ledger and authorization audit commit
together. Discovery and receipt writers never acquire these job locks.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from uuid import uuid4

from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert

from inventory_hub.db_models import Shop, Warehouse
from inventory_hub.order_collection_models import OrderCollectionSettings, OrderInboxEntry
from inventory_hub.order_processing_models import OrderProcessingJob
from inventory_hub.order_stock_models import OrderStockPolicy, OrderStockPreview
from inventory_hub.stock_settings_models import StockShopSettings
from inventory_hub.services import order_stock as stock, order_stock_ledger as ledger, stock_settings
from inventory_hub.services.order_collection import target_fingerprint
from inventory_hub.services.order_stock_source import resolve_lines
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK
from inventory_hub.services.stock_publication_gate import require_stock_write_allowed, StockPublicationHoldError


class ProcessingError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def now():
    return datetime.now(timezone.utc)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()).hexdigest()


def observation(entry):
    # Collector metadata already has a stable hash. Compare that hash in SQL,
    # not observed/processed clocks: a collector can wait on the inbox lock
    # and commit an observation whose timestamp predates our completion.
    # Identity, deletion and sticky review flags are checked independently
    # under the inbox row lock before any stock effect.
    return entry.observation_hash


def _enabled(config, policy):
    if config["mode"] not in ("reserve", "fulfill"):
        raise ProcessingError("order_processing_manual")
    if config["processing_paused"]:
        raise ProcessingError("order_processing_paused")
    if config["authorized_policy_revision"] != policy.revision:
        raise ProcessingError("order_processing_policy_changed")
    if not config["automation_starts_at"] or not config["target_fingerprint"]:
        raise ProcessingError("order_processing_not_authorized")
    if config["warehouse_id"] != policy.warehouse_id:
        raise ProcessingError("order_processing_warehouse_changed")


def authorize(config, policy, source, fetched):
    _enabled(config, policy)
    created = datetime.fromisoformat(source["created_at"])
    if created < config["automation_starts_at"]:
        raise ProcessingError("order_processing_before_activation")
    action = stock._action(policy, source, fetched["status_hash"], fetched["statuses"])
    if action == "issue":
        if config["mode"] != "fulfill" or not config["issue_starts_at"]:
            raise ProcessingError("order_processing_issue_not_authorized")
        if created < config["issue_starts_at"]:
            raise ProcessingError("order_processing_before_issue_activation")
    return action


def unchanged_hold(order, source_hash, source, action, plan):
    """No repeat revision when stock allocation and observed source are unchanged."""
    return (action in ("reserve", "cancel") and plan["ready"]
            and order.stock_state == {"reserve": "reserved", "cancel": "cancelled"}[action]
            and order.stock_source_hash == source_hash and order.stock_snapshot is not None
            and stock._content(order.stock_snapshot) == stock._content(source)
            and all(Decimal(row["old_allocation"]) == Decimal(row["allocation"]) for row in plan["effects"]))


async def _context(db, shop_id, *, identity=False, shop_write=False):
    if identity:
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": IDENTITY_WRITE_LOCK})
    shop = await db.scalar(select(Shop).where(Shop.id == shop_id).with_for_update(read=not shop_write)
                           .execution_options(populate_existing=True))
    if shop is None or not shop.is_active or shop.platform != "upgates":
        raise ProcessingError("order_processing_shop_unavailable")
    policy = await db.scalar(select(OrderStockPolicy).where(OrderStockPolicy.shop_id == shop_id)
                             .with_for_update(read=True).execution_options(populate_existing=True))
    if policy is None:
        raise ProcessingError("order_processing_not_configured")
    config = await stock_settings.effective(db, shop_id, lock=True)
    _enabled(config, policy)
    try:
        await require_stock_write_allowed(db, policy.warehouse_id)
    except StockPublicationHoldError as error:
        raise ProcessingError(error.code, error.status) from None
    if target_fingerprint(shop.code) != config["target_fingerprint"]:
        raise ProcessingError("order_processing_target_changed")
    if config.get("retry_after_at") and config["retry_after_at"] > now():
        raise ProcessingError("order_processing_rate_limited", 429)
    collection_cooldown = await db.scalar(select(OrderCollectionSettings.retry_after_at).where(
        OrderCollectionSettings.shop_id == shop_id,
        OrderCollectionSettings.last_error == "order_collection_rate_limited",
        OrderCollectionSettings.target_fingerprint == config["target_fingerprint"]))
    if collection_cooldown and collection_cooldown > now():
        raise ProcessingError("order_processing_rate_limited", 429)
    return shop, policy, config


async def _job(db, identifier):
    return await db.scalar(select(OrderProcessingJob).where(OrderProcessingJob.id == identifier).with_for_update()
                           .execution_options(populate_existing=True))


def _requeue(job, entry):
    digest = observation(entry)
    if job.observation_hash != digest:
        job.generation += 1
        job.observation_hash = digest
        job.status, job.error, job.next_attempt_at = "pending", None, now()


async def enqueue(db, shop_id, force=False):
    """Discover work after collector commit; never called by a stock writer."""
    # Queue refresh may touch many jobs; the shop lock serializes it with other
    # refreshes/claims so their different subsets cannot invert job-row locks.
    _, _, config = await _context(db, shop_id, shop_write=True)
    query = select(OrderInboxEntry).outerjoin(OrderProcessingJob,
        (OrderProcessingJob.shop_id == OrderInboxEntry.shop_id) &
        (OrderProcessingJob.source_uuid == OrderInboxEntry.source_uuid)).where(
        OrderInboxEntry.shop_id == shop_id, OrderInboxEntry.created_at >= config["automation_starts_at"])
    if not force:
        query = query.where(or_(OrderProcessingJob.id.is_(None), OrderInboxEntry.observation_hash != OrderProcessingJob.observation_hash,
            (OrderInboxEntry.review_reason.is_not(None) & OrderInboxEntry.review_reason.is_distinct_from(OrderProcessingJob.error))))
    entries = (await db.scalars(query.order_by(OrderInboxEntry.observed_at, OrderInboxEntry.id)
                              .limit(config["values"]["processing_batch_size"] * 5))).all()
    for entry in entries:
        await db.execute(insert(OrderProcessingJob).values(shop_id=shop_id, inbox_id=entry.id,
            source_uuid=entry.source_uuid, order_number=entry.order_number, observation_hash=observation(entry),
            generation=1, status="pending", next_attempt_at=now(), attempts=0).on_conflict_do_nothing(
                constraint="uq_order_processing_jobs_source"))
        job = await db.scalar(select(OrderProcessingJob).where(OrderProcessingJob.shop_id == shop_id,
            OrderProcessingJob.source_uuid == entry.source_uuid).with_for_update().execution_options(populate_existing=True))
        _requeue(job, entry)
        if entry.review_reason and job.error != entry.review_reason:
            # A sticky conflict can change without changing the collector hash.
            # Consume that local observation now, otherwise these same rows can
            # occupy every bounded discovery batch and starve later orders.
            if job.status == "running":
                job.generation += 1
            _schedule(job, config, status="review", error=entry.review_reason)
        if force and job.status != "running":
            job.next_attempt_at = now()
    await db.flush()
    return len(entries)


def _inbox_error(entry, job):
    if entry is None or entry.source_uuid != job.source_uuid or entry.order_number != job.order_number or entry.shop_id != job.shop_id:
        return "order_processing_identity_conflict"
    return entry.review_reason or ("order_processing_deleted" if entry.deleted else None)


def _schedule(job, config, *, status, error=None, result=None):
    at = now()
    job.status, job.error = status, error
    job.next_attempt_at = at + (timedelta(minutes=config["values"]["processing_retry_minutes"]) if status == "retry"
                               else timedelta(hours=config["values"]["full_order_check_hours"]))
    job.last_checked_at = at
    if result is not None:
        job.result, job.last_completed_at = result, at


async def start_job(db, job_id):
    old = await db.get(OrderProcessingJob, job_id)
    if old is None:
        return None
    shop, policy, config = await _context(db, old.shop_id)
    job = await _job(db, job_id)
    if job.status == "running" or job.next_attempt_at > now():
        return None
    entry = await db.scalar(select(OrderInboxEntry).where(OrderInboxEntry.id == job.inbox_id)
                            .with_for_update(read=True).execution_options(populate_existing=True))
    error = _inbox_error(entry, job)
    if error:
        _schedule(job, config, status="review", error=error)
        return None
    _requeue(job, entry)
    if entry.created_at < config["automation_starts_at"]:
        _schedule(job, config, status="review", error="order_processing_before_activation")
        return None
    job.status, job.error, job.last_started_at = "running", None, now()
    job.attempts += 1
    job.configuration_hash = config["configuration_hash"]
    await db.flush()
    return {"id": job.id, "shop_id": shop.id, "shop_code": shop.code, "order_number": job.order_number,
            "source_uuid": job.source_uuid, "generation": job.generation, "attempt": job.attempts,
            "observation_hash": job.observation_hash, "configuration_hash": config["configuration_hash"],
            "target_fingerprint": config["target_fingerprint"], "values": config["values"]}


def _owns(job, plan):
    return job is not None and job.status == "running" and job.generation == plan["generation"] and job.attempts == plan["attempt"]


async def finish_job(db, plan, fetched):
    shop, policy, config = await _context(db, plan["shop_id"], identity=True)
    job = await _job(db, plan["id"])
    if not _owns(job, plan):
        return {"status": "superseded"}
    if config["configuration_hash"] != plan["configuration_hash"]:
        raise ProcessingError("order_processing_configuration_changed")
    entry = await db.scalar(select(OrderInboxEntry).where(OrderInboxEntry.id == job.inbox_id)
                            .with_for_update(read=True).execution_options(populate_existing=True))
    error = _inbox_error(entry, job)
    if error:
        _schedule(job, config, status="review", error=error)
        return {"status": "review", "error": error}
    if observation(entry) != plan["observation_hash"]:
        _requeue(job, entry)
        return {"status": "superseded"}
    source = fetched["order"]
    if (source["uuid"] != job.source_uuid or source["order_number"] != job.order_number
            or datetime.fromisoformat(source["created_at"]) != entry.created_at):
        raise ProcessingError("order_processing_identity_conflict")
    updated = datetime.fromisoformat(source["updated_at"])
    if updated < entry.updated_at:
        raise ProcessingError("order_processing_source_behind")
    if updated == entry.updated_at and (source["status_id"] != entry.status_id or source["origin"] != entry.origin):
        raise ProcessingError("order_processing_source_conflict")
    source["lines"] = await resolve_lines(db, shop, source)
    action = authorize(config, policy, source, fetched)
    order = await ledger.get_or_create_order(db, shop, source, config["warehouse_id"])
    if order.stock_state == "issued":
        if action != "issue" or stock._content(source) != stock._content(order.stock_snapshot):
            raise ProcessingError("order_stock_issued_locked")
        result = order.stock_issue_result
        job.source_hash = fetched["source_hash"]
        _schedule(job, config, status="completed", result=result)
        return result
    projected = await ledger.plan_order(db, order, source, action, config["warehouse_id"], lock=True)
    if not projected["ready"]:
        codes = {error["code"] for error in projected["errors"]}
        retry = codes == {"order_stock_insufficient_stock"}
        _schedule(job, config, status="retry" if retry else "review",
                  error="order_stock_insufficient_stock" if retry else "order_processing_plan_blocked",
                  result={"errors": projected["errors"]})
        job.source_hash = fetched["source_hash"]
        return {"status": job.status, "errors": projected["errors"]}
    if unchanged_hold(order, fetched["source_hash"], source, action, projected):
        result = {"order_id": order.id, "order_number": order.external_id, "action": action, "noop": True,
                  "stock_state": order.stock_state, "revision": order.stock_revision, "movements_created": 0,
                  "lines": projected["lines"], "effects": projected["effects"]}
    else:
        result = await ledger.apply_order(db, order, source, action, config["warehouse_id"], projected)
        order.stock_source_hash = fetched["source_hash"]
        at, identifier = now(), str(uuid4())
        authorization = {key: config[key] for key in ("configuration_hash", "mode", "warehouse_revision", "shop_revision",
            "authorized_policy_revision", "target_fingerprint", "automation_starts_at", "issue_starts_at")}
        authorization = {key: value.isoformat() if isinstance(value, datetime) else value for key, value in authorization.items()}
        warehouse_name = await db.scalar(select(Warehouse.name).where(Warehouse.id == config["warehouse_id"]))
        snapshot = {"id": identifier, "trigger": "automatic", "job_id": job.id, "generation": job.generation,
                    "created_at": at.isoformat(), "expires_at": at.isoformat(), "shop_code": shop.code,
                    "warehouse": {"id": config["warehouse_id"], "code": config["warehouse_code"], "name": warehouse_name},
                    "source": source, "source_hash": fetched["source_hash"], "status_hash": fetched["status_hash"],
                    "action": action, "order_revision": projected["order_revision"], "policy_revision": policy.revision,
                    "authorization": authorization, "plan": projected}
        db.add(OrderStockPreview(id=identifier, shop_id=shop.id, order_id=order.id,
            request_hash=_hash({"job_id": job.id, "generation": job.generation, "attempt": job.attempts}),
            preview_hash=_hash(snapshot), preview_data=snapshot, status="completed", created_at=at,
            expires_at=at, completed_at=at, result=result))
        await db.flush()
        job.audit_preview_id = identifier
    shortage = any(Decimal(row["shortage"]) > 0 for row in projected["lines"])
    _schedule(job, config, status="retry" if shortage else "completed",
              error="order_processing_backorder" if shortage else None, result=result)
    job.source_hash = fetched["source_hash"]
    await db.flush()
    return result


async def fail_job(db, plan, code, retry_after=None):
    cooldown_settings = None
    if code == "order_stock_rate_limited":
        # A runtime cooldown is independent of authority and survives pause,
        # manual mode, newer inbox generations and an operator wake request.
        await db.scalar(select(Shop.id).where(Shop.id == plan["shop_id"]).with_for_update(read=True))
        policy = await db.scalar(select(OrderStockPolicy).where(OrderStockPolicy.shop_id == plan["shop_id"])
                                 .with_for_update(read=True))
        if policy is not None:
            await db.scalar(select(Warehouse.id).where(Warehouse.id == policy.warehouse_id).with_for_update(read=True))
        cooldown_settings = await db.scalar(select(StockShopSettings).where(StockShopSettings.shop_id == plan["shop_id"])
                                            .with_for_update().execution_options(populate_existing=True))
        delay = retry_after if type(retry_after) is int and 0 < retry_after <= 604800 else 0
        until = now() + max(timedelta(seconds=delay), timedelta(minutes=plan["values"]["processing_retry_minutes"]))
        if cooldown_settings is not None:
            cooldown_settings.processing_retry_after_at = max(until, cooldown_settings.processing_retry_after_at or until)
    job = await _job(db, plan["id"])
    if not _owns(job, plan):
        return
    retry = code in {"order_processing_source_behind", "order_processing_interrupted", "order_processing_source_unavailable",
                     "order_stock_source_unavailable", "order_stock_rate_limited", "order_processing_configuration_changed",
                     "order_processing_paused", "stock_publication_warehouse_held"}
    _schedule(job, {"values": plan["values"]}, status="retry" if retry else "review", error=code)
    if cooldown_settings is not None:
        job.next_attempt_at = max(job.next_attempt_at, cooldown_settings.processing_retry_after_at)


async def recover(db):
    rows = (await db.scalars(select(OrderProcessingJob).where(OrderProcessingJob.status == "running")
                            .order_by(OrderProcessingJob.id).with_for_update())).all()
    for job in rows:
        job.status, job.error, job.next_attempt_at = "retry", "order_processing_interrupted", now()


async def jobs(db, shop_code, limit=50, offset=0):
    shop = await db.scalar(select(Shop).where(Shop.code == shop_code, Shop.is_active.is_(True)))
    if shop is None:
        raise ProcessingError("order_processing_shop_unavailable", 404)
    limit, offset = min(max(limit, 1), 100), max(offset, 0)
    total = await db.scalar(select(func.count()).select_from(OrderProcessingJob).where(OrderProcessingJob.shop_id == shop.id))
    rows = (await db.scalars(select(OrderProcessingJob).where(OrderProcessingJob.shop_id == shop.id)
        .order_by(OrderProcessingJob.updated_at.desc(), OrderProcessingJob.id.desc()).offset(offset).limit(limit))).all()
    keys = ("id", "source_uuid", "order_number", "generation", "status", "next_attempt_at", "last_started_at",
            "last_completed_at", "last_checked_at", "attempts", "error", "result", "audit_preview_id")
    return {"jobs": [{key: getattr(row, key) for key in keys} for row in rows], "total": total, "limit": limit, "offset": offset}


async def refresh(db, shop_code):
    shop = await db.scalar(select(Shop).where(Shop.code == shop_code))
    if shop is None:
        raise ProcessingError("order_processing_shop_unavailable", 404)
    await enqueue(db, shop.id, force=True)
    _, _, config = await _context(db, shop.id)
    rows = (await db.scalars(select(OrderProcessingJob).where(OrderProcessingJob.shop_id == shop.id,
        OrderProcessingJob.status != "running").order_by(OrderProcessingJob.id).with_for_update())).all()
    for job in rows:
        job.next_attempt_at = now()
    await db.flush()
    return {"scheduled": len(rows), "mode": config["mode"], "external_write_enabled": False}
