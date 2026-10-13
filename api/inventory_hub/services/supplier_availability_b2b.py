"""Manual, resumable Sloger source reads using the existing availability audit.

Each cursor advance and its supplier facts commit together. Only an uncommitted
read-only chunk may repeat after interruption; failed products are never retried
automatically. Supplier observations cannot create physical stock or FIFO costs.
"""
from datetime import timedelta
from decimal import Decimal
import hashlib

from sqlalchemy import exists, func, select, text
from sqlalchemy.dialects.postgresql import insert

from inventory_hub.db_models import FeedRunStatus, Product, Supplier, SupplierFeed, SupplierFeedItemRaw, SupplierFeedRun, SupplierProduct
from inventory_hub.db_models_ext import ProductSupplySource
from inventory_hub.services import catalog
from inventory_hub.services.sloger_data import MAX_BATCH_ITEMS
from inventory_hub.services.supplier_availability_source import AvailabilityError, configuration
from inventory_hub.supplier_availability_models import SupplierAvailabilityObservation, SupplierAvailabilitySettings

MAX_FAILURE_DETAILS = 50


async def scope_skus(db, supplier_id):
    """Only active products already held in the Hub, including zero own stock."""
    linked = select(ProductSupplySource.supplier_product_id).join(Product, Product.id == ProductSupplySource.product_id).where(
        Product.is_active.is_(True), ProductSupplySource.is_active.is_(True), ProductSupplySource.orderable.is_(True))
    fallback = select(Product.source_supplier_product_id).where(Product.is_active.is_(True),
        Product.source_supplier_product_id.is_not(None), ~exists(select(ProductSupplySource.id).where(
            ProductSupplySource.product_id == Product.id)))
    return list((await db.scalars(select(SupplierProduct.supplier_sku).where(
        SupplierProduct.supplier_id == supplier_id, SupplierProduct.is_active.is_(True),
        SupplierProduct.is_discontinued.is_(False), SupplierProduct.id.in_(linked.union(fallback)))
        .order_by(SupplierProduct.supplier_sku))).all())


def progress(run):
    details = run.error_details or {}
    return {"total": len(details.get("skus", [])), "processed": details.get("cursor", 0),
            "succeeded": details.get("succeeded", 0), "failed": details.get("failed", 0),
            "failures": details.get("failures", [])[:MAX_FAILURE_DETAILS], "status": run.status.value}


async def latest_progress(db, supplier_id):
    run = await db.scalar(select(SupplierFeedRun).join(SupplierFeed, SupplierFeed.id == SupplierFeedRun.feed_id).where(
        SupplierFeed.supplier_id == supplier_id,
        SupplierFeedRun.error_details["workflow"].astext == "supplier_availability",
        SupplierFeedRun.error_details["scope"].astext == "hub_products")
        .order_by(SupplierFeedRun.id.desc()).limit(1))
    return progress(run) if run else None


async def resume(db, supplier_id, run_id):
    from inventory_hub.services.supplier_availability import now
    supplier = await db.get(Supplier, supplier_id)
    if not supplier or supplier.code != "sloger":
        return None
    locked = await db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": catalog._lock_key("sloger")})
    if not locked:
        raise AvailabilityError("sloger_b2b_busy")
    row = await db.get(SupplierAvailabilitySettings, supplier_id, with_for_update=True)
    run = await db.get(SupplierFeedRun, run_id)
    if not row or row.running_run_id != run_id or not run or run.status != FeedRunStatus.running:
        return None
    details = run.error_details or {}
    if details.get("scope") != "hub_products":
        return None
    cfg, source, parser, fingerprint = configuration("sloger", row.feed_key)
    if row.revision != details.get("revision") or fingerprint != details.get("configuration_hash"):
        raise AvailabilityError("supplier_availability_changed")
    cursor, skus = details["cursor"], details["skus"]
    return {"supplier": "sloger", "supplier_id": supplier_id, "feed_key": "b2b", "feed_id": run.feed_id,
            "run_id": run_id, "revision": row.revision, "configuration_hash": fingerprint,
            "config": cfg, "source": source, "parser": parser, "observed_at": now(),
            "cursor": cursor, "skus": skus[cursor:cursor + MAX_BATCH_ITEMS]}


async def _write_observations(db, supplier_id, feed_id, run_id, observations, freshness_seconds):
    """Shared by import and refresh. Never store quotes, HTML or authentication."""
    if not observations:
        return
    values, audit = [], []
    for item in observations:
        kind = item.quantity_kind if item.quantity is not None else "unknown"
        raw = {"availability": item.availability, "quantity_relation": item.quantity_relation,
               "availability_tiers": [tier.model_dump(mode="json") for tier in item.availability_tiers]}
        values.append({"supplier_id": supplier_id, "supplier_sku": item.sku, "feed_id": feed_id, "run_id": run_id,
            "available": item.available, "quantity": Decimal(item.quantity) if item.quantity is not None else None,
            "quantity_kind": kind, "raw": raw, "observed_at": item.observed_at,
            "expires_at": item.observed_at + timedelta(seconds=freshness_seconds)})
        audit.append({"feed_id": feed_id, "run_id": run_id, "item_hash": hashlib.sha256(item.sku.encode()).hexdigest(),
            "raw_data": {"workflow": "supplier_availability", "supplier_sku": item.sku, **raw,
                         "available": item.available, "quantity": item.quantity, "quantity_kind": kind},
            "processed": True, "processed_at": item.observed_at, "fetched_at": item.observed_at})
    statement = insert(SupplierAvailabilityObservation).values(values)
    await db.execute(statement.on_conflict_do_update(
        index_elements=[SupplierAvailabilityObservation.supplier_id, SupplierAvailabilityObservation.supplier_sku],
        set_={key: getattr(statement.excluded, key) for key in values[0] if key not in ("supplier_id", "supplier_sku")},
        where=SupplierAvailabilityObservation.observed_at <= statement.excluded.observed_at))
    await db.execute(insert(SupplierFeedItemRaw).values(audit))


async def accept_batch(db, plan, result):
    from inventory_hub.services.supplier_availability import now
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": catalog._lock_key("sloger")})
    row = await db.get(SupplierAvailabilitySettings, plan["supplier_id"], with_for_update=True)
    run = await db.get(SupplierFeedRun, plan["run_id"])
    if (not row or row.running_run_id != plan["run_id"] or row.revision != plan["revision"]
            or not run or run.status != FeedRunStatus.running
            or configuration("sloger", row.feed_key)[3] != plan["configuration_hash"]):
        raise AvailabilityError("supplier_availability_changed")
    details = dict(run.error_details or {})
    if details.get("cursor") != plan["cursor"]:
        raise AvailabilityError("supplier_availability_changed")
    if [item.sku for item in result.items] != plan["skus"]:
        raise AvailabilityError("supplier_availability_invalid_identity", 422)
    observations = [item.observation for item in result.items if item.observation is not None]
    if any(item.observation and item.observation.sku != item.sku for item in result.items):
        raise AvailabilityError("supplier_availability_invalid_identity", 422)
    await _write_observations(db, row.supplier_id, run.feed_id, run.id, observations, row.freshness_seconds)
    failures = [{"sku": item.sku, "code": item.error or "sloger_b2b_unavailable"}
                for item in result.items if item.observation is None]
    details["cursor"] += len(result.items)
    details["succeeded"] += len(observations)
    details["failed"] += len(failures)
    details["failures"] = (details.get("failures", []) + failures)[:MAX_FAILURE_DETAILS]
    run.error_details = details
    run.items_fetched = details["cursor"]
    run.items_updated = details["succeeded"]
    run.items_error = details["failed"]
    if result.aborted or details["cursor"] == len(details["skus"]):
        run.status = FeedRunStatus.failed if result.aborted else FeedRunStatus.completed
        run.finished_at = now()
        run.error_message = ("supplier_availability_b2b_aborted" if result.aborted else
                             "supplier_availability_partial" if details["failed"] else None)
        row.running_run_id = None
        row.last_error = run.error_message
        row.last_item_count = details["succeeded"]
        if not details["failed"] and not result.aborted:
            row.last_success_at = run.started_at
        if row.manual_requested_at is None:
            row.next_run_at = now() + timedelta(seconds=row.interval_seconds)
    await db.flush()


async def record_import(db, observations, draft_id):
    from inventory_hub.services.supplier_availability import now
    if not observations:
        return {}
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": catalog._lock_key("sloger")})
    cfg, _source, _parser, fingerprint = configuration("sloger", "b2b")
    feed = await catalog._feed(db, "sloger", cfg, "b2b", create=True)
    feed.feed_type = "stock"
    row = await db.get(SupplierAvailabilitySettings, feed.supplier_id, with_for_update=True)
    if row is None:
        row = SupplierAvailabilitySettings(supplier_id=feed.supplier_id, revision=1, enabled=False,
            feed_key="b2b", interval_seconds=3600, freshness_seconds=21600, min_coverage_percent=100,
            next_run_at=now(), last_item_count=0)
        db.add(row)
    if row.feed_key != "b2b":
        raise AvailabilityError("supplier_availability_source_missing", 422)
    number = (await db.scalar(select(func.max(SupplierFeedRun.run_number)).where(SupplierFeedRun.feed_id == feed.id)) or 0) + 1
    run = SupplierFeedRun(feed_id=feed.id, run_number=number, status=FeedRunStatus.completed,
        started_at=min(item.observed_at for item in observations), finished_at=now(), items_fetched=len(observations),
        items_updated=len(observations), error_details={"workflow": "product_import_b2b", "draft_id": draft_id,
            "configuration_hash": fingerprint})
    db.add(run)
    await db.flush()
    await _write_observations(db, feed.supplier_id, feed.id, run.id, observations, row.freshness_seconds)
    return {item.sku: item.observed_at + timedelta(seconds=row.freshness_seconds) for item in observations}
