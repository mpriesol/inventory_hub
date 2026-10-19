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
from inventory_hub.services.supplier_availability_source import AvailabilityError, ParsedAvailability, configuration
from inventory_hub.supplier_availability_models import SupplierAvailabilityObservation, SupplierAvailabilitySettings


def now():
    return datetime.now(timezone.utc)


def _feed_keys(cfg):
    feeds = cfg.get("feeds") or {}
    sources = feeds.get("sources") if isinstance(feeds, dict) else None
    if not isinstance(sources, dict):
        return []
    result = []
    for key, source in sources.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,50}", key) or not isinstance(source, dict):
            continue
        remote = source.get("remote") or {}
        location = source.get("local_path") if source.get("mode", "remote") == "local" else remote.get("url") if isinstance(remote, dict) else None
        if isinstance(location, str) and location.strip():
            result.append(key)
    return result


def _safe_error(value):
    # Persisted workflow codes are safe; never expose legacy exception/URL text.
    return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,99}", value) else (
        "supplier_availability_source_unavailable" if value else None)


def public(supplier, cfg, settings=None):
    manual_only = supplier == "sloger"
    keys = ["b2b"] if manual_only else _feed_keys(cfg)
    result = {"supplier": supplier, "name": cfg.get("name") or supplier,
        "feed_keys": keys, "feed_key": settings.feed_key if settings else ("b2b" if manual_only else "stock" if "stock" in keys else "products" if "products" in keys else next(iter(keys), "")),
        "revision": settings.revision if settings else 0, "enabled": bool(settings and settings.enabled and not manual_only),
        "manual_only": manual_only, "source_kind": "b2b" if manual_only else "feed",
        "refresh_scope": "hub_products" if manual_only else "supplier_catalog", "refresh_progress": None, "latest_run": None,
        "interval_seconds": settings.interval_seconds if settings else 3600,
        "freshness_seconds": settings.freshness_seconds if settings else 21600,
        "min_coverage_percent": settings.min_coverage_percent if settings else 100,
        "availability": normalize_supplier_availability((cfg.get("adapter_settings") or {}).get("availability")),
        "publication": (cfg.get("adapter_settings") or {}).get("availability_publication"),
        "last_item_count": settings.last_item_count if settings else 0,
        "running": bool(settings and settings.running_run_id)}
    for field in ("last_started_at", "last_success_at", "next_run_at", "last_error", "manual_requested_at"):
        result[field] = getattr(settings, field) if settings else None
    result["last_error"] = _safe_error(result["last_error"])
    result["status"] = ("unconfigured" if settings is None else "not_loaded" if not settings.last_success_at
        else "stale" if now() >= settings.last_success_at + timedelta(seconds=settings.freshness_seconds) else "fresh")
    result["configuration_error"] = None
    if settings and settings.feed_key not in keys:
        result.update(status="configuration_error", configuration_error="supplier_availability_source_missing")
    return result


async def overview(db):
    rows = {code: value for code, value in (await db.execute(select(Supplier.code, SupplierAvailabilitySettings)
            .outerjoin(SupplierAvailabilitySettings, SupplierAvailabilitySettings.supplier_id == Supplier.id))).all()}
    directory = config_io.DATA_ROOT / "suppliers"
    codes = sorted({code for code in rows if re.fullmatch(r"[a-zA-Z0-9_-]{1,50}", code)} |
                   {path.parent.name for path in directory.glob("*/config.json")
                    if re.fullmatch(r"[a-zA-Z0-9_-]{1,50}", path.parent.name)})
    result = []
    for code in codes:
        try:
            cfg = catalog.supplier_config(code)
            value = public(code, cfg, rows.get(code))
        except (catalog.CatalogError, OSError, TypeError, ValueError):
            value = public(code, {}, rows.get(code))
            value.update(status="configuration_error", configuration_error="supplier_availability_source_missing")
        if code == "sloger" and rows.get(code):
            from inventory_hub.services.supplier_availability_b2b import latest_progress
            value["refresh_progress"] = await latest_progress(db, rows[code].supplier_id)
        elif rows.get(code):
            value["latest_run"] = await latest_run(db, rows[code].supplier_id, rows[code].feed_key)
        if value.get("publication"):
            from inventory_hub.product_editor_models import ProductEditorPublication
            counts = (await db.execute(select(ProductEditorPublication.state, func.count())
                .where(ProductEditorPublication.document["workflow"].astext == "supplier_availability",
                       ProductEditorPublication.document["supplier"].astext == code)
                .group_by(ProductEditorPublication.state))).all()
            value["publication"] = {**value["publication"], "counts":dict(counts)}
        result.append(value)
    return {"suppliers": result}


async def latest_run(db, supplier_id, feed_key):
    run = await db.scalar(select(SupplierFeedRun).join(SupplierFeed, SupplierFeed.id == SupplierFeedRun.feed_id)
        .where(SupplierFeed.supplier_id == supplier_id, SupplierFeed.code == feed_key,
               SupplierFeedRun.error_details["workflow"].astext == "supplier_availability")
        .order_by(SupplierFeedRun.id.desc()).limit(1))
    if run is None:
        return None
    report = (run.error_details or {}).get("availability_report")
    return {"id": run.id, "feed_key": feed_key, "status": ("partial" if run.status == FeedRunStatus.completed
            and report and (report["rejected"] or report["unknown"] or report.get("warnings")) else run.status.value),
        "started_at": run.started_at, "finished_at": run.finished_at,
        "error": _safe_error(run.error_message), "report": report}


async def run_failures(db, supplier, run_id, offset=0, limit=50):
    run = await db.scalar(select(SupplierFeedRun).join(SupplierFeed, SupplierFeed.id == SupplierFeedRun.feed_id)
        .join(Supplier, Supplier.id == SupplierFeed.supplier_id)
        .where(Supplier.code == supplier, SupplierFeedRun.id == run_id,
               SupplierFeedRun.error_details["workflow"].astext == "supplier_availability"))
    if run is None:
        raise AvailabilityError("supplier_availability_run_not_found", 404)
    condition = (SupplierFeedItemRaw.run_id == run.id, SupplierFeedItemRaw.processing_error.is_not(None),
                 SupplierFeedItemRaw.raw_data["workflow"].astext == "supplier_availability")
    total = await db.scalar(select(func.count()).select_from(SupplierFeedItemRaw).where(*condition))
    rows = (await db.scalars(select(SupplierFeedItemRaw).where(*condition)
        .order_by(SupplierFeedItemRaw.id).offset(offset).limit(limit))).all()
    return {"run_id": run.id, "total": total, "offset": offset, "limit": limit,
            "items": [{key: row.raw_data.get(key) for key in ("row", "sku", "stock", "external_stock", "code", "kind")}
                      for row in rows]}


async def configure(db, supplier, payload):
    if supplier == "sloger" and payload.enabled:
        raise AvailabilityError("supplier_availability_manual_only", 422)
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
    if not row or row.running_run_id or row.next_run_at > now() or (
            (not row.enabled or supplier.code == "sloger") and row.manual_requested_at is None):
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
    details = {"workflow": "supplier_availability", "trigger": "manual" if row.manual_requested_at else "scheduled", "revision": row.revision,
               "configuration_hash": fingerprint}
    if supplier.code == "sloger":
        from inventory_hub.services.supplier_availability_b2b import scope_skus
        details.update(scope="hub_products", skus=await scope_skus(db, supplier_id), cursor=0,
                       succeeded=0, failed=0, failures=[])
    run = SupplierFeedRun(feed_id=feed.id, run_number=number, status=FeedRunStatus.running,
        started_at=started, error_details=details)
    db.add(run)
    await db.flush()
    row.running_run_id, row.last_started_at, row.manual_requested_at = run.id, started, None
    row.next_run_at = started + timedelta(seconds=row.interval_seconds)
    plan = {"supplier": supplier.code, "supplier_id": supplier.id, "feed_key": row.feed_key,
        "feed_id": feed.id, "run_id": run.id, "revision": row.revision,
        "configuration_hash": fingerprint, "config": cfg, "source": source, "parser": parser,
        "observed_at": started}
    if supplier.code == "sloger":
        from inventory_hub.services.sloger_data import MAX_BATCH_ITEMS
        plan["skus"] = details["skus"][:MAX_BATCH_ITEMS]
        plan["cursor"] = 0
    return plan


async def resume_run(db, supplier_id, run_id):
    """Resume only an explicitly requested B2B run; ordinary feeds are not retried."""
    from inventory_hub.services.supplier_availability_b2b import resume
    return await resume(db, supplier_id, run_id)


async def record_sloger_import(db, observations, draft_id):
    """Record source facts during import without activating a supplier crawler."""
    from inventory_hub.services.supplier_availability_b2b import record_import
    return await record_import(db, observations, draft_id)


def coverage_ok(item_count, baseline, percent):
    return item_count > 0 and item_count * 100 >= baseline * percent


async def accept_run(db, plan, records, source_bytes=0):
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": catalog._lock_key(plan["supplier"])})
    row = await db.get(SupplierAvailabilitySettings, plan["supplier_id"], with_for_update=True)
    if not row or row.running_run_id != plan["run_id"] or row.revision != plan["revision"]:
        raise AvailabilityError("supplier_availability_changed")
    if configuration(plan["supplier"], row.feed_key)[3] != plan["configuration_hash"]:
        raise AvailabilityError("supplier_availability_changed")
    records = records if isinstance(records, ParsedAvailability) else ParsedAvailability(records)
    covered = coverage_ok(records.total, row.last_item_count, row.min_coverage_percent)
    report = {**records.report(), "warnings": [] if covered else ["supplier_availability_incomplete_feed"]}
    run = await db.get(SupplierFeedRun, plan["run_id"])
    if not run or run.status != FeedRunStatus.running:
        raise AvailabilityError("supplier_availability_changed")
    run.error_details = {**(run.error_details or {}), "availability_report": report}
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
            "item_hash": hashlib.sha256(("sku:" + item.sku).encode()).hexdigest(),
            "raw_data": {"workflow": "supplier_availability", "supplier_sku": item.sku, **item.raw},
            "processed": True, "processed_at": now(), "fetched_at": observed,
            "supplier_product_id": products.get(item.sku)} for item in chunk]))
    for start in range(0, len(records.issues), 500):
        # Terminal audit rows, not pending catalog work. No failed row renews an observation.
        await db.execute(insert(SupplierFeedItemRaw).values([{"feed_id": plan["feed_id"], "run_id": plan["run_id"],
            "item_hash": hashlib.sha256(f"issue:{item['row']}".encode()).hexdigest(),
            "raw_data": {"workflow": "supplier_availability", **item}, "processed": True,
            "processing_error": item["code"], "processed_at": now(), "fetched_at": observed}
            for item in records.issues[start:start + 500]]))
    completed = now()
    run.status, run.finished_at, run.items_fetched = FeedRunStatus.completed if records else FeedRunStatus.failed, completed, records.total
    run.items_updated, run.source_size_bytes = len(records), source_bytes
    run.items_error = len(records.issues)
    run.error_message = None if records else "supplier_availability_no_usable_rows"
    row.last_error = run.error_message
    if records:
        row.last_success_at = observed
        if covered:
            row.last_item_count = records.total
    row.running_run_id = None
    if row.manual_requested_at is None:
        row.next_run_at = completed + timedelta(seconds=row.interval_seconds)
    await db.flush()
    from inventory_hub.services.supplier_links import reconcile_source_codes
    reports = await reconcile_source_codes(db, plan["supplier"], [item.sku for item in records], plan["config"]) if records else []
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
        "availability_state": "available" if available is True else "unavailable" if available is False else "unknown",
        "source": supplier, "quantity": str(observation.quantity) if fresh and observation.quantity is not None else None,
        "quantity_kind": observation.quantity_kind if fresh else "unknown",
        "observed_at": observation.observed_at.isoformat() if observation else None,
        "expires_at": expires.isoformat() if expires else None, "orderable": True,
        "status": "fresh" if fresh else "stale" if observation else "missing_observation",
        "reason": ("supplier_quantity_unknown" if fresh and available is None else None) if fresh
            else "supplier_observation_expired" if observation else "supplier_observation_missing"}


def _source_projection(product, supplier, observation, settings, observed_feed_key, label, config_error, at):
    """Separate identity, source setup and exact-SKU evidence without guessing stock."""
    result = project_observation(None, "overíme", supplier.code, at)
    refresh = "running" if settings and settings.running_run_id else "queued" if settings and settings.manual_requested_at else "idle"
    result.update(supplier=supplier.code, supplier_name=supplier.name, supplier_sku=product.supplier_sku,
        feed_key=settings.feed_key if settings else None, observation_feed_key=observed_feed_key,
        last_success_at=settings.last_success_at.isoformat() if settings and settings.last_success_at else None,
        last_error=_safe_error(settings.last_error) if settings else None, refresh_state=refresh,
        observed_at=observation.observed_at.isoformat() if observation else None,
        expires_at=observation.expires_at.isoformat() if observation else None)
    def unavailable(status, reason, action):
        result.update(status=status, reason=reason, action="wait" if action == "refresh" and refresh != "idle" else action)
        if status == "unavailable_source":
            result["source"] = None
        return result
    if not product.is_active or product.is_discontinued or not supplier.is_active:
        return unavailable("unavailable_source", "supplier_source_disabled", "check_source")
    if config_error:
        return unavailable("configuration_error", config_error, "configure")
    if not settings:
        return unavailable("unconfigured", "supplier_availability_not_configured", "configure")
    if observation and observed_feed_key != settings.feed_key:
        return unavailable("feed_mismatch", "supplier_observation_feed_mismatch", "check_source")
    if not settings.last_success_at and supplier.code != "sloger":
        return unavailable("not_loaded", "supplier_availability_not_loaded", "refresh")
    if not observation:
        return unavailable("missing_observation", "supplier_observation_missing", "refresh")
    result.update(project_observation(observation, label, supplier.code, at, settings.freshness_seconds))
    result["action"] = "wait" if refresh != "idle" else "refresh"
    return result


def _source_label(supplier_code, settings):
    try:
        cfg = catalog.supplier_config(supplier_code)
        label = normalize_supplier_availability((cfg.get("adapter_settings") or {}).get("availability"))["orderable"]
        if settings and supplier_code != "sloger" and settings.feed_key not in _feed_keys(cfg):
            return None, "supplier_availability_source_missing"
        return label, None
    except (catalog.CatalogError, OSError, TypeError, ValueError):
        return None, "supplier_availability_source_missing"


async def source_observation(db, supplier_code, sku, at=None):
    """Read one exact catalog SKU and its independent availability evidence."""
    row = (await db.execute(select(SupplierProduct, Supplier, SupplierAvailabilityObservation, SupplierAvailabilitySettings)
        .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
        .outerjoin(SupplierAvailabilityObservation, (SupplierAvailabilityObservation.supplier_id == Supplier.id) &
            (SupplierAvailabilityObservation.supplier_sku == SupplierProduct.supplier_sku))
        .outerjoin(SupplierAvailabilitySettings, SupplierAvailabilitySettings.supplier_id == Supplier.id)
        .where(Supplier.code == supplier_code, SupplierProduct.supplier_sku == sku))).one_or_none()
    if row is None:
        raise AvailabilityError("supplier_availability_source_missing", 404)
    product, supplier, observation, settings = row
    feed_key = await db.scalar(select(SupplierFeed.code).where(SupplierFeed.id == observation.feed_id)) if observation else None
    label, error = _source_label(supplier.code, settings)
    return _source_projection(product, supplier, observation, settings, feed_key, label, error, at or now())


async def project(db, product_ids, at=None):
    """Project active links, retaining exact cause and evidence for operator review.

    Existing publication fields keep their conservative behavior. Display state
    distinguishes a confirmed zero from unknown; neither becomes physical stock.
    """
    at = at or now()
    product_ids = list(dict.fromkeys(product_ids))
    result = {identifier: project_observation(None, "overíme", None, at) for identifier in product_ids}
    for value in result.values():
        value.update(status="missing_link", reason="supplier_link_missing", action="link", refresh_state="idle")
    if not product_ids:
        return result
    links = list(await db.scalars(select(ProductSupplySource).where(ProductSupplySource.product_id.in_(product_ids))))
    candidates = [(link.product_id, link.supplier_product_id, link.priority, link.is_primary,
                   link.is_active and link.orderable) for link in links]
    # Explicitly disabled links must never be revived through a legacy pointer.
    linked = {link.product_id for link in links}
    candidates.extend((product_id, source, 100, False, True) for product_id, source in
        (await db.execute(select(Product.id, Product.source_supplier_product_id).where(
            Product.id.in_(product_ids), Product.source_supplier_product_id.is_not(None)))).all() if product_id not in linked)
    ids = {row[1] for row in candidates}
    sources = {product.id: (product, supplier, observation, settings) for product, supplier, observation, settings in
        (await db.execute(select(SupplierProduct, Supplier, SupplierAvailabilityObservation, SupplierAvailabilitySettings)
            .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
            .outerjoin(SupplierAvailabilityObservation, (SupplierAvailabilityObservation.supplier_id == Supplier.id) &
                (SupplierAvailabilityObservation.supplier_sku == SupplierProduct.supplier_sku))
            .outerjoin(SupplierAvailabilitySettings, SupplierAvailabilitySettings.supplier_id == Supplier.id)
            .where(SupplierProduct.id.in_(ids)))).all()}
    feed_keys = dict((await db.execute(select(SupplierFeed.id, SupplierFeed.code).where(
        SupplierFeed.id.in_({o.feed_id for _, _, o, _ in sources.values() if o})))).all())
    labels, ranks = {}, {}
    for product_id, source_id, _priority, _primary, enabled in sorted(candidates, key=lambda x: (x[0], not x[3], x[2], x[1])):
        if source_id not in sources:
            if product_id not in ranks:
                result[product_id].update(status="unavailable_source", reason="supplier_source_disabled", action="link")
            continue
        product, supplier, observation, settings = sources[source_id]
        if supplier.code not in labels:
            labels[supplier.code] = _source_label(supplier.code, settings)
        label, error = labels[supplier.code]
        candidate = _source_projection(product, supplier, observation, settings, feed_keys.get(observation.feed_id) if observation else None,
                                       label, error, at)
        if not enabled:
            candidate.update(project_observation(None, "overíme", supplier.code, at))
            candidate.update(status="unavailable_source", reason="supplier_source_disabled", action="check_source", source=None)
        # Prefer usable evidence, then supplier priority. A lower-priority fresh
        # available source may satisfy demand, but an expired one never can.
        rank = 3 if candidate["available"] is True else 2 if candidate["fresh"] else 1 if candidate["status"] == "stale" else -1 if candidate["status"] == "unavailable_source" else 0
        if rank > ranks.get(product_id, -2):
            result[product_id], ranks[product_id] = candidate, rank
    from inventory_hub.services.supplier_links import missing_link_diagnostics
    diagnostics = await missing_link_diagnostics(db, [identifier for identifier, value in result.items()
        if value["status"] == "missing_link"])
    for identifier, detail in diagnostics.items():
        result[identifier].update(detail)
    return result
