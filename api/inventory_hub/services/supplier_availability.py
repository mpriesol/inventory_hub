"""Durable supplier scheduling and freshness-aware availability projection.

Only feed observations are written. Physical stock and catalog content are untouched.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import re

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from inventory_hub import config_io
from inventory_hub.config_normalize import normalize_supplier_availability
from inventory_hub.db_models import FeedRunStatus, Product, Supplier, SupplierFeed, SupplierFeedItemRaw, SupplierFeedRun, SupplierProduct
from inventory_hub.db_models_ext import ProductSupplySource
from inventory_hub.services import catalog
from inventory_hub.services.supplier_availability_source import AvailabilityError, configuration
from inventory_hub.supplier_availability_models import SupplierAvailabilityObservation, SupplierAvailabilitySettings


def now():
    return datetime.now(timezone.utc)


def _feed_keys(cfg):
    return [key for key, source in (cfg.get("feeds", {}).get("sources") or {}).items()
            if re.fullmatch(r"[a-zA-Z0-9_-]{1,50}", key) and isinstance(source, dict)
            and (source.get("local_path") if source.get("mode", "remote") == "local" else (source.get("remote") or {}).get("url"))]


def public(supplier, cfg, settings=None):
    keys = _feed_keys(cfg)
    result = {"supplier": supplier, "name": cfg.get("name") or supplier,
        "feed_keys": keys, "feed_key": settings.feed_key if settings else ("stock" if "stock" in keys else "products"),
        "revision": settings.revision if settings else 0, "enabled": settings.enabled if settings else False,
        "interval_seconds": settings.interval_seconds if settings else 3600,
        "freshness_seconds": settings.freshness_seconds if settings else 21600,
        "min_coverage_percent": settings.min_coverage_percent if settings else 100,
        "availability": normalize_supplier_availability((cfg.get("adapter_settings") or {}).get("availability")),
        "last_item_count": settings.last_item_count if settings else 0,
        "running": bool(settings and settings.running_run_id)}
    for field in ("last_started_at", "last_success_at", "next_run_at", "last_error", "manual_requested_at"):
        result[field] = getattr(settings, field) if settings else None
    return result


async def overview(db):
    rows = {code: value for code, value in (await db.execute(select(Supplier.code, SupplierAvailabilitySettings)
            .join(SupplierAvailabilitySettings, SupplierAvailabilitySettings.supplier_id == Supplier.id))).all()}
    directory = config_io.DATA_ROOT / "suppliers"
    codes = sorted(path.parent.name for path in directory.glob("*/config.json")
                   if re.fullmatch(r"[a-zA-Z0-9_-]{1,50}", path.parent.name))
    result = []
    for code in codes:
        cfg = config_io.load_supplier(code, write_back_on_load=False)
        result.append(public(code, cfg, rows.get(code)))
    return {"suppliers": result}


async def configure(db, supplier, payload):
    cfg, _source, _parser, _fingerprint = configuration(supplier, payload.feed_key)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": catalog._lock_key(supplier)})
    feed = await catalog._feed(db, supplier, cfg, payload.feed_key, create=True)
    if payload.feed_key == "stock" or (_source.get("type") == "stock"):
        feed.feed_type = "stock"
    row = await db.get(SupplierAvailabilitySettings, feed.supplier_id, with_for_update=True)
    if (row.revision if row else 0) != payload.expected_revision:
        raise AvailabilityError("supplier_availability_changed")
    if row is None:
        row = SupplierAvailabilitySettings(supplier_id=feed.supplier_id, feed_key=payload.feed_key,
            revision=0, last_item_count=0, next_run_at=now())
        db.add(row)
    if row.feed_key != payload.feed_key:
        row.last_item_count, row.last_success_at = 0, None
    for key in ("enabled", "feed_key", "interval_seconds", "freshness_seconds", "min_coverage_percent"):
        setattr(row, key, getattr(payload, key))
    row.revision += 1
    row.next_run_at = now()
    row.last_error = None
    # SupplierFeed is the registry; only this workflow's settings activate automation.
    feed.fetch_interval_min = max(1, payload.interval_seconds // 60)
    await db.flush()
    return public(supplier, cfg, row)


async def request_run(db, supplier, payload):
    supplier_row = await db.scalar(select(Supplier).where(Supplier.code == supplier))
    row = await db.get(SupplierAvailabilitySettings, supplier_row.id, with_for_update=True) if supplier_row else None
    if row is None:
        raise AvailabilityError("supplier_availability_not_configured")
    if row.revision != payload.expected_revision:
        raise AvailabilityError("supplier_availability_changed")
    configuration(supplier, row.feed_key)
    # Coalesce clicks into one additional pass; never parallel requests for one supplier.
    row.manual_requested_at = row.manual_requested_at or now()
    row.next_run_at = now()
    await db.flush()
    return {"supplier": supplier, "queued": True}


async def start_run(db, supplier_id):
    supplier = await db.get(Supplier, supplier_id)
    if not supplier:
        return None
    locked = await db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": catalog._lock_key(supplier.code)})
    if not locked:
        return None
    row = await db.get(SupplierAvailabilitySettings, supplier_id, with_for_update=True)
    if not row or row.running_run_id or row.next_run_at > now() or (not row.enabled and row.manual_requested_at is None):
        return None
    try:
        cfg, source, parser, fingerprint = configuration(supplier.code, row.feed_key)
    except AvailabilityError as error:
        row.last_error, row.manual_requested_at = error.code, None
        row.next_run_at = now() + timedelta(seconds=row.interval_seconds)
        return None
    feed = await catalog._feed(db, supplier.code, cfg, row.feed_key, create=True)
    number = (await db.scalar(select(func.max(SupplierFeedRun.run_number)).where(SupplierFeedRun.feed_id == feed.id)) or 0) + 1
    started = now()
    run = SupplierFeedRun(feed_id=feed.id, run_number=number, status=FeedRunStatus.running,
        started_at=started, error_details={"workflow": "supplier_availability", "revision": row.revision,
            "configuration_hash": fingerprint})
    db.add(run)
    await db.flush()
    row.running_run_id, row.last_started_at, row.manual_requested_at = run.id, started, None
    row.next_run_at = started + timedelta(seconds=row.interval_seconds)
    return {"supplier": supplier.code, "supplier_id": supplier.id, "feed_key": row.feed_key,
        "feed_id": feed.id, "run_id": run.id, "revision": row.revision,
        "configuration_hash": fingerprint, "config": cfg, "source": source, "parser": parser,
        "observed_at": started}


def coverage_ok(item_count, baseline, percent):
    return item_count > 0 and item_count * 100 >= baseline * percent


async def accept_run(db, plan, records, source_bytes=0):
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": catalog._lock_key(plan["supplier"])})
    row = await db.get(SupplierAvailabilitySettings, plan["supplier_id"], with_for_update=True)
    if not row or row.running_run_id != plan["run_id"] or row.revision != plan["revision"]:
        raise AvailabilityError("supplier_availability_changed")
    if configuration(plan["supplier"], row.feed_key)[3] != plan["configuration_hash"]:
        raise AvailabilityError("supplier_availability_changed")
    if not coverage_ok(len(records), row.last_item_count, row.min_coverage_percent):
        raise AvailabilityError("supplier_availability_incomplete_feed", 422)
    run = await db.get(SupplierFeedRun, plan["run_id"])
    if not run or run.status != FeedRunStatus.running:
        raise AvailabilityError("supplier_availability_changed")
    observed = plan["observed_at"]
    expires = observed + timedelta(seconds=row.freshness_seconds)
    from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": IDENTITY_WRITE_LOCK})
    products = dict((await db.execute(select(SupplierProduct.supplier_sku, SupplierProduct.id)
                      .where(SupplierProduct.supplier_id == plan["supplier_id"]))).all())
    for start in range(0, len(records), 500):
        chunk = records[start:start + 500]
        values = [{"supplier_id": plan["supplier_id"], "supplier_sku": item.sku,
            "feed_id": plan["feed_id"], "run_id": plan["run_id"], "available": item.available,
            "quantity": item.quantity, "quantity_kind": item.quantity_kind,
            "raw": item.raw, "observed_at": observed, "expires_at": expires} for item in chunk]
        statement = insert(SupplierAvailabilityObservation).values(values)
        await db.execute(statement.on_conflict_do_update(
            index_elements=[SupplierAvailabilityObservation.supplier_id, SupplierAvailabilityObservation.supplier_sku],
            set_={key: getattr(statement.excluded, key) for key in values[0] if key not in ("supplier_id", "supplier_sku")}))
        # Archive only stock facts and supplier identity, never downloaded URL credentials.
        await db.execute(insert(SupplierFeedItemRaw).values([{"feed_id": plan["feed_id"], "run_id": plan["run_id"],
            "item_hash": hashlib.sha256(item.sku.encode()).hexdigest(),
            "raw_data": {"workflow": "supplier_availability", "supplier_sku": item.sku, **item.raw},
            "processed": True, "processed_at": now(), "fetched_at": observed,
            "supplier_product_id": products.get(item.sku)} for item in chunk]))
    completed = now()
    run.status, run.finished_at, run.items_fetched = FeedRunStatus.completed, completed, len(records)
    run.items_updated, run.source_size_bytes = len(records), source_bytes
    row.last_success_at, row.last_error, row.last_item_count = observed, None, len(records)
    row.running_run_id = None
    if row.manual_requested_at is None:
        row.next_run_at = completed + timedelta(seconds=row.interval_seconds)
    await db.flush()
    from inventory_hub.services.supplier_links import reconcile_source_codes
    reports = await reconcile_source_codes(db, plan["supplier"], [item.sku for item in records], plan["config"])
    run.error_details = {**(run.error_details or {}), "supplier_links": {
        "linked": sum(report["linked"] for report in reports),
        "conflicts": sum(len(report["conflicts"]) for report in reports)}}


async def fail_run(db, supplier_id, run_id, code):
    row = await db.get(SupplierAvailabilitySettings, supplier_id, with_for_update=True)
    run = await db.get(SupplierFeedRun, run_id)
    if run and run.status == FeedRunStatus.running:
        run.status, run.finished_at, run.error_message = FeedRunStatus.failed, now(), code
    if row and row.running_run_id == run_id:
        row.running_run_id, row.last_error = None, code
        if row.manual_requested_at is None:
            row.next_run_at = now() + timedelta(seconds=row.interval_seconds)
    await db.flush()


def project_observation(observation, label, supplier, at, freshness_seconds=None):
    expires = observation.expires_at if observation else None
    if observation and freshness_seconds is not None:
        # Shortening TTL takes effect immediately; extending it never revives expired facts.
        expires = min(expires, observation.observed_at + timedelta(seconds=freshness_seconds))
    fresh = bool(observation and observation.observed_at <= at < expires)
    available = observation.available if fresh else None
    return {"available": available, "fresh": fresh, "label": label if available is True else "overíme",
        "source": supplier, "quantity": str(observation.quantity) if fresh and observation.quantity is not None else None,
        "quantity_kind": observation.quantity_kind if fresh else "unknown",
        "observed_at": observation.observed_at.isoformat() if observation else None,
        "expires_at": expires.isoformat() if expires else None, "orderable": True,
        "status": "fresh" if fresh else "stale" if observation else "missing_observation",
        "reason": ("supplier_quantity_unknown" if fresh and available is None else None) if fresh
            else "supplier_observation_expired" if observation else "supplier_observation_missing"}


async def project(db, product_ids, at=None):
    """Resolve active supply links by priority; expiry is evaluated on every read.

Regular shop synchronization reprojects all mapped leaves each pass, so TTL expiry
changes availability even when no newer feed has arrived. No data is turned into zero.
"""
    at = at or now()
    product_ids = list(dict.fromkeys(product_ids))
    result = {identifier: project_observation(None, "overíme", None, at) for identifier in product_ids}
    for value in result.values():
        value.update(status="missing_link", reason="supplier_link_missing")
    if not product_ids:
        return result
    links = (await db.execute(select(ProductSupplySource.product_id, ProductSupplySource.supplier_product_id,
                 ProductSupplySource.priority, ProductSupplySource.is_primary).where(
                     ProductSupplySource.product_id.in_(product_ids), ProductSupplySource.is_active.is_(True),
                     ProductSupplySource.orderable.is_(True)))).all()
    candidates = [(link.product_id, link.supplier_product_id, link.priority, link.is_primary) for link in links]
    # An explicitly disabled supply link must not be revived by the legacy fallback.
    linked = set((await db.scalars(select(ProductSupplySource.product_id).where(
        ProductSupplySource.product_id.in_(product_ids)))).all())
    candidates.extend((product_id, source, 100, False) for product_id, source in
        (await db.execute(select(Product.id, Product.source_supplier_product_id).where(
            Product.id.in_(product_ids), Product.source_supplier_product_id.is_not(None)))).all() if product_id not in linked)
    for product_id in linked | {candidate[0] for candidate in candidates}:
        result[product_id].update(status="unavailable_source", reason="supplier_source_disabled")
    ids = {row[1] for row in candidates}
    sources = {product.id: (product, supplier, observation, settings) for product, supplier, observation, settings in
        (await db.execute(select(SupplierProduct, Supplier, SupplierAvailabilityObservation, SupplierAvailabilitySettings)
            .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
            .outerjoin(SupplierAvailabilityObservation, (SupplierAvailabilityObservation.supplier_id == Supplier.id) &
                (SupplierAvailabilityObservation.supplier_sku == SupplierProduct.supplier_sku))
            .outerjoin(SupplierAvailabilitySettings, SupplierAvailabilitySettings.supplier_id == Supplier.id)
            .where(SupplierProduct.id.in_(ids), SupplierProduct.is_active.is_(True),
                   SupplierProduct.is_discontinued.is_(False), Supplier.is_active.is_(True)))).all()}
    feed_keys = dict((await db.execute(select(SupplierFeed.id, SupplierFeed.code).where(
        SupplierFeed.id.in_({o.feed_id for _, _, o, _ in sources.values() if o})))).all())
    labels = {}
    for product_id, source_id, _priority, _primary in sorted(candidates, key=lambda x: (x[0], not x[3], x[2], x[1])):
        if source_id not in sources or result[product_id]["available"] is True:
            continue
        product, supplier, observation, settings = sources[source_id]
        if result[product_id]["status"] == "unavailable_source":
            result[product_id].update(status="missing_observation", reason="supplier_observation_missing")
        if supplier.code not in labels:
            try:
                cfg = catalog.supplier_config(supplier.code)
                labels[supplier.code] = normalize_supplier_availability((cfg.get("adapter_settings") or {}).get("availability"))["orderable"]
            except (catalog.CatalogError, ValueError):
                labels[supplier.code] = None
        label = labels[supplier.code]
        if (not settings or not settings.last_success_at or not observation
                or feed_keys.get(observation.feed_id) != settings.feed_key or not label):
            continue
        candidate = project_observation(observation, label, supplier.code, at, settings.freshness_seconds)
        if result[product_id]["source"] is None or candidate["available"] is True:
            result[product_id] = candidate
    from inventory_hub.services.supplier_links import missing_link_diagnostics
    diagnostics = await missing_link_diagnostics(db, [identifier for identifier, value in result.items()
        if value["status"] == "missing_link"])
    for identifier, detail in diagnostics.items():
        result[identifier].update(detail)
    return result
