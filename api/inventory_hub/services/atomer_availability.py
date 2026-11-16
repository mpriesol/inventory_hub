"""Daily and receipt-scoped availability CSVs, never a physical/shop writer."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import csv
import hashlib
import io
import json

from sqlalchemy import select, text, func
from inventory_hub import config_io
from inventory_hub.atomer_availability_models import (
    AtomerAvailabilitySettings as Settings,
    AtomerAvailabilityRun as Run,
    AtomerAvailabilityAdminSource as AdminSource,
)
from inventory_hub.atomer_availability_types import ExportInput
from inventory_hub.db_models import Product, Warehouse
from inventory_hub.db_models_ext import StockBalance, ShopProduct
from inventory_hub.services import catalog, supplier_availability
from inventory_hub.services.product_identity import RemoteIdentity, load_identity_index
from inventory_hub.services.stock_tracking import confirmed_stock
from inventory_hub.services.atomer_availability_source import (
    ExportError,
    parse_buyers,
    apply_admin,
    assemble,
    summarize,
)

SETTINGS_LOCK = 691432255
SOURCE_KEY = "atomer_availability"
MAX_SOURCE = 100 * 1024 * 1024


def now():
    return datetime.now(timezone.utc)


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode()
    ).hexdigest()


def source_config():
    return config_io.load_shop("xtrek", write_back_on_load=False).get(SOURCE_KEY) or {}


async def warehouse(db, code=None, identifier=None):
    query = select(Warehouse).where(Warehouse.is_active.is_(True))
    query = (
        query.where(Warehouse.id == identifier)
        if identifier
        else (
            query.where(Warehouse.code == code)
            if code
            else query.where(Warehouse.is_default.is_(True))
        )
    )
    row = await db.scalar(query)
    if row is None:
        raise ExportError("atomer_availability_warehouse_missing", 422)
    return row


def dto(run):
    doc = run.document or {}
    available = (
        run.download_available
        if hasattr(run, "download_available")
        else run.csv_content is not None
    )
    return dict(
        id=run.id,
        status=run.status,
        revision=run.revision,
        trigger=run.trigger,
        receiving_batch_id=run.receiving_batch_id,
        created_at=run.created_at,
        source_fetched_at=run.source_fetched_at,
        source_sha256=run.source_sha256,
        completed_at=run.completed_at,
        error=run.error,
        summary=doc.get("summary") or {},
        coverage=doc.get("coverage") or {},
        warnings=doc.get("warnings") or [],
        missing_products=doc.get("missing_products") or [],
        download_available=available,
        download_url=(
            f"/api/atomer-availability/runs/{run.id}/download" if available else None
        ),
    )


def metadata_query():
    """Do not transfer full catalogs or CSV bytes for status/history screens."""
    return select(
        Run.id,
        Run.status,
        Run.revision,
        Run.trigger,
        Run.receiving_batch_id,
        Run.created_at,
        Run.source_fetched_at,
        Run.source_sha256,
        Run.completed_at,
        Run.error,
        Run.csv_content.is_not(None).label("download_available"),
        func.jsonb_build_object(
            "summary",
            Run.document["summary"],
            "coverage",
            Run.document["coverage"],
            "warnings",
            Run.document["warnings"],
            "missing_products",
            Run.document["missing_products"],
        ).label("document"),
    )


async def options(db):
    setting = await db.get(Settings, 1)
    cfg = source_config()
    wh = await db.get(Warehouse, setting.warehouse_id) if setting else None
    latest = (
        await db.execute(metadata_query().order_by(Run.created_at.desc()).limit(1))
    ).first()
    values = dict(
        revision=setting.revision if setting else 0,
        enabled=bool(setting and setting.enabled),
        warehouse_code=wh.code if wh else None,
        source_configured=bool(cfg.get("export_url")),
        admin_source_configured=bool(cfg.get("admin_export_url")),
        admin_complete=bool(setting and setting.admin_complete),
        source_format="buyers",
        interval_seconds=86400,
        latest_run=dto(latest) if latest else None,
        warehouses=[
            dict(code=w.code, name=w.name)
            for w in await db.scalars(
                select(Warehouse)
                .where(Warehouse.is_active.is_(True))
                .order_by(Warehouse.name)
            )
        ],
    )
    for name in ("last_started_at", "last_completed_at", "last_error", "next_run_at"):
        values[name] = getattr(setting, name, None)
    uploaded = (
        await db.execute(
            select(AdminSource.imported_at, AdminSource.complete).where(
                AdminSource.id == 1
            )
        )
    ).first()
    values["admin_upload"] = (
        dict(
            imported_at=uploaded.imported_at,
            complete=uploaded.complete,
            fresh=uploaded.imported_at + timedelta(days=1) > now(),
        )
        if uploaded
        else None
    )
    return values


async def configure(db, payload):
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SETTINGS_LOCK})
    row = await db.get(Settings, 1, with_for_update=True)
    request_hash = digest(payload.model_dump(mode="json"))
    if row and row.last_request_id == str(payload.request_id):
        if row.last_request_hash != request_hash:
            raise ExportError("atomer_availability_request_conflict")
        return await options(db)
    if (row.revision if row else 0) != payload.expected_revision:
        raise ExportError("atomer_availability_settings_changed")
    wh = await warehouse(db, payload.warehouse_code)
    cfg = source_config()
    if payload.feed_url:
        cfg["export_url"] = payload.feed_url
    if payload.admin_export_url is not None:
        cfg["admin_export_url"] = payload.admin_export_url
    if payload.enabled and not cfg.get("export_url"):
        raise ExportError("atomer_availability_source_missing", 422)
    # This namespace never changes the existing xTrek Upgates source fields.
    config_io.save_shop("xtrek", {SOURCE_KEY: cfg})
    if row is None:
        row = Settings(id=1, warehouse_id=wh.id, revision=0)
        db.add(row)
    row.warehouse_id, row.enabled, row.admin_complete = (
        wh.id,
        payload.enabled,
        payload.admin_complete,
    )
    row.revision += 1
    row.last_request_id, row.last_request_hash = str(payload.request_id), request_hash
    row.next_run_at = now()
    row.last_error = None
    await db.flush()
    return await options(db)


async def upload_admin(db, raw, complete):
    if not raw or len(raw) > MAX_SOURCE:
        raise ExportError("atomer_availability_source_size", 422)
    # Validate fully before replacing an existing usable snapshot.
    await asyncio.to_thread(apply_admin, {}, raw, complete)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SETTINGS_LOCK})
    row = await db.get(AdminSource, 1, with_for_update=True)
    checksum = hashlib.sha256(raw).hexdigest()
    if (
        row
        and row.source_sha256 == checksum
        and row.complete == complete
        and row.imported_at + timedelta(days=1) > now()
    ):
        return dict(
            source_sha256=row.source_sha256,
            imported_at=row.imported_at,
            complete=row.complete,
        )
    if row is None:
        row = AdminSource(id=1)
        db.add(row)
    row.content, row.complete, row.source_sha256, row.imported_at = (
        raw,
        complete,
        checksum,
        now(),
    )
    await db.flush()
    return dict(
        source_sha256=row.source_sha256,
        imported_at=row.imported_at,
        complete=row.complete,
    )


async def enqueue(db, payload, trigger="manual"):
    identifier = str(payload.request_id)
    # Serialize duplicate request creation and replay across API processes.
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SETTINGS_LOCK})
    request_hash = digest(payload.model_dump(mode="json"))
    old = await db.get(Run, identifier)
    if old:
        if old.request_hash != request_hash:
            raise ExportError("atomer_availability_request_conflict")
        return dto(old)
    if not source_config().get("export_url"):
        raise ExportError("atomer_availability_source_missing", 422)
    ids = payload.product_ids
    code = payload.warehouse_code
    if payload.receiving_batch_id:
        from inventory_hub.services.mixed_receiving import get_export_scope

        scope = await get_export_scope(db, str(payload.receiving_batch_id))
        if ids is not None or code is not None and code != scope["warehouse_code"]:
            raise ExportError("atomer_availability_receipt_scope_invalid", 422)
        ids, code = scope["product_ids"], scope["warehouse_code"]
        trigger = "receiving"
    setting = await db.get(Settings, 1)
    wh = await warehouse(
        db, code, setting.warehouse_id if setting and not code else None
    )
    run = Run(
        id=identifier,
        request_hash=request_hash,
        warehouse_id=wh.id,
        trigger=trigger,
        receiving_batch_id=(
            str(payload.receiving_batch_id) if payload.receiving_batch_id else None
        ),
        product_ids=ids,
        status="queued",
        revision=1,
        document={},
        created_at=now(),
    )
    db.add(run)
    await db.flush()
    return dto(run)


async def list_runs(db, limit=30):
    rows = list(
        (
            await db.execute(
                metadata_query().order_by(Run.created_at.desc()).limit(limit)
            )
        ).all()
    )
    successful = (
        metadata_query()
        .where(Run.status == "exported", Run.csv_content.is_not(None))
        .order_by(Run.completed_at.desc(), Run.id.desc())
        .limit(1)
    )
    latest = (await db.execute(successful)).first()
    latest_global = (
        await db.execute(
            successful.where(
                Run.product_ids.is_(None), Run.receiving_batch_id.is_(None)
            )
        )
    ).first()
    for retained in (latest, latest_global):
        if retained and retained.id not in {r.id for r in rows}:
            rows.append(retained)
    return {"items": [dto(r) for r in rows]}


async def get_run(db, identifier, offset=0, limit=100, view="all"):
    run = await db.get(Run, identifier)
    if run is None:
        raise ExportError("atomer_availability_run_missing", 404)
    all_rows = (run.document or {}).get("rows", [])
    rows = all_rows
    if view != "all":
        statuses = {"changes": {"changed"}, "issues": {"blocked", "unmatched"}}[view]
        parents = {
            row["parent_code"]
            for row in all_rows
            if row["status"] in statuses and row.get("parent_code")
        }
        rows = [
            row
            for row in all_rows
            if row["status"] in statuses
            or row["kind"] == "parent"
            and row["code"] in parents
        ]
    return {
        **dto(run),
        "view": view,
        "rows": rows[offset : offset + limit],
        "total": len(rows),
        "offset": offset,
        "availability_options": sorted(
            {
                r[k]
                for r in all_rows
                for k in ("current_availability", "proposed_availability")
                if r.get(k)
            }
        ),
    }


async def projections(db, source, warehouse_id):
    parents = {
        r["parent_code"]
        for r in source.values()
        if r["parent_code"] and r["parent_code"] != r["code"]
    }
    leaves = {k: r for k, r in source.items() if k not in parents}
    identities = [
        RemoteIdentity(shop_id=0, code=k, barcodes=(r["ean"],))
        for k, r in leaves.items()
    ]
    index = await load_identity_index(db, 0, identities, local=True)
    resolved = {identity.code: index.resolve(identity) for identity in identities}
    product_ids = sorted({r.product_id for r in resolved.values() if r.product_id})
    balances = {}
    shared = set()
    offers = {}
    for start in range(0, len(product_ids), 500):
        chunk = product_ids[start : start + 500]
        balances.update(
            (r.product_id, r)
            for r in (
                await db.execute(
                    select(
                        StockBalance.product_id,
                        StockBalance.qty_on_hand,
                        StockBalance.qty_reserved,
                        StockBalance.qty_quarantined,
                        confirmed_stock().label("confirmed"),
                    ).where(
                        StockBalance.product_id.in_(chunk),
                        StockBalance.warehouse_id == warehouse_id,
                    )
                )
            ).all()
        )
        shared.update(
            await db.scalars(
                select(ShopProduct.product_id).where(
                    ShopProduct.product_id.in_(chunk),
                    ShopProduct.shared_stock.is_(True),
                )
            )
        )
        offers.update(await supplier_availability.project(db, chunk))
    result = {}
    for code, resolution in resolved.items():
        pid = resolution.product_id
        value = dict(
            product_id=pid,
            sku=index.products[pid].sku if pid else None,
            quantity=None,
            offer=offers.get(pid, {}),
            reason=None,
        )
        if resolution.status == "conflict":
            value["reason"] = "identity_conflict"
        elif pid:
            product = index.products[pid]
            balance = balances.get(pid)
            if product.item_type != "stock" or product.stock_unit != "pcs":
                value["reason"] = "stock_unit_unsupported"
            elif pid in shared:
                value["reason"] = "shared_sale_mapping_required"
            elif balance and balance.confirmed:
                qty = (
                    balance.qty_on_hand - balance.qty_reserved - balance.qty_quarantined
                )
                if (
                    qty < 0
                    or qty != qty.to_integral_value()
                    or balance.qty_reserved < 0
                    or balance.qty_quarantined < 0
                ):
                    value["reason"] = "stock_invalid"
                else:
                    value["quantity"] = str(qty)
        result[code] = value
    return result


def projection_hash(values, ids=None):
    chosen = set(ids) if ids is not None else None
    return digest(
        {
            k: dict(
                product_id=v["product_id"],
                sku=v["sku"],
                quantity=v["quantity"],
                reason=v["reason"],
                offer={f: v["offer"].get(f) for f in ("fresh", "available", "label")},
            )
            for k, v in values.items()
            if chosen is None or v["product_id"] in chosen
        }
    )


def fetch_source(cfg):
    # Reuse the bounded downloader and sanitised errors, but store only in this
    # export's own private directory. No supplier registry is created.
    directory = config_io.shop_path("xtrek").parent / "exports" / "atomer-sources"
    result = {}
    for key in ("export_url", "admin_export_url"):
        if not cfg.get(key):
            continue
        path = catalog._download(
            "xtrek",
            {"mode": "remote", "remote": {"url": cfg[key]}},
            "atomer-" + key,
            max_elapsed_seconds=180,
            max_size_bytes=MAX_SOURCE,
            directory=directory,
        )
        try:
            result[key] = path.read_bytes()
        finally:
            path.unlink(missing_ok=True)
    if "export_url" not in result:
        raise ExportError("atomer_availability_source_missing", 422)
    return result


async def prepare(db, run, raws, source_at, config_hash):
    if digest(source_config()) != config_hash:
        raise ExportError("atomer_availability_source_changed")
    sha, source = await asyncio.to_thread(parse_buyers, raws["export_url"])
    settings = await db.get(Settings, 1)
    warnings = []
    admin_raw = raws.get("admin_export_url")
    admin_at = source_at if admin_raw else None
    admin_upload_sha = None
    complete = bool(settings and settings.admin_complete)
    if not admin_raw:
        uploaded = await db.get(AdminSource, 1)
        if uploaded and uploaded.imported_at + timedelta(days=1) > source_at:
            admin_raw, admin_at, complete = (
                uploaded.content,
                uploaded.imported_at,
                uploaded.complete,
            )
            admin_upload_sha = uploaded.source_sha256
        elif uploaded:
            warnings.append("admin_upload_stale")
    if admin_raw:
        source = await asyncio.to_thread(apply_admin, source, admin_raw, complete)
    missing = {
        r["parent_code"]
        for r in source.values()
        if r["parent_code"] and r["parent_code"] not in source
    }
    source_blocked_rows = sum(bool(row["reasons"]) for row in source.values())
    if source_blocked_rows:
        warnings.append("source_rows_blocked")
    if not admin_raw or not complete:
        warnings.append("hidden_catalog_coverage_unverified")
    if missing:
        warnings.append("parent_visibility_unverified")
    values = await projections(db, source, run.warehouse_id)
    rows = assemble(source, values, run.product_ids)
    if run.product_ids is not None:
        present = {r["product_id"] for r in rows if r.get("product_id")}
        if set(run.product_ids) - present:
            warnings.append("received_products_missing_from_source")
    missing_ids = sorted(
        set(run.product_ids or [])
        - {r["product_id"] for r in rows if r.get("product_id")}
    )
    missing_products = (
        [
            dict(id=p.id, sku=p.sku, name=p.name)
            for p in await db.scalars(
                select(Product).where(Product.id.in_(missing_ids))
            )
        ]
        if missing_ids
        else []
    )
    run.document = dict(
        source=list(source.values()),
        rows=rows,
        summary=summarize(rows),
        warnings=warnings,
        missing_products=missing_products,
        coverage=dict(
            kind=(
                "admin_complete"
                if admin_raw and complete
                else "admin_partial" if admin_raw else "buyers_only"
            ),
            hidden_rows=sum(r["current_visibility"] is False for r in source.values()),
            source_blocked_rows=source_blocked_rows,
            missing_parents=len(missing),
            admin_source_at=admin_at.isoformat() if admin_at else None,
        ),
        projection_hash=projection_hash(values, run.product_ids),
        admin_source_sha256=(
            hashlib.sha256(admin_raw).hexdigest() if admin_raw else None
        ),
        source_config_hash=config_hash,
        admin_upload_sha=admin_upload_sha,
        missing_product_ids=missing_ids,
    )
    run.source_fetched_at, run.source_sha256, run.status = source_at, sha, "ready"
    run.revision += 1
    await db.flush()


def edited_rows(rows, overrides):
    result = deepcopy(rows)
    by_code = {r["code"]: r for r in result}
    manual = {o.code: o for o in overrides}
    for override in overrides:
        row = by_code.get(override.code)
        if (
            row is None
            or row["status"] in ("blocked", "unmatched")
            or row["current_visibility"] is None
            or row["reasons"]
        ):
            raise ExportError("atomer_availability_override_blocked", 422)
        if override.visibility is not None:
            row["proposed_visibility"] = override.visibility
        if override.availability is not None:
            row["proposed_availability"] = override.availability
        row["manual_override"] = True
    # Respect explicit per-export parent decisions. Otherwise reveal the known
    # parent only when a visible stocked leaf actually needs it.
    for parent in (r for r in result if r["kind"] == "parent"):
        if (
            parent["code"] in manual
            or parent["current_visibility"] is None
            or parent["reasons"]
        ):
            continue
        children = [r for r in result if r["parent_code"] == parent["code"]]
        show = any(
            r["status"] not in ("blocked", "unmatched")
            and r["proposed_visibility"] is True
            and r.get("qty_available") is not None
            and Decimal(r["qty_available"]) > 0
            for r in children
        )
        parent["proposed_visibility"] = True if show else parent["current_visibility"]
    for row in result:
        if (
            row["status"] in ("blocked", "unmatched")
            or row["current_visibility"] is None
            or row["reasons"]
        ):
            continue
        row["status"] = (
            "changed"
            if (row["proposed_visibility"], row["proposed_availability"])
            != (row["current_visibility"], row["current_availability"])
            else "unchanged"
        )
    return result


def csv_bytes(rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(
        stream, delimiter=";", quoting=csv.QUOTE_ALL, lineterminator="\r\n"
    )
    writer.writerow(
        ["Katalógové číslo", "Katalógové číslo rodiča", "Viditeľnosť", "Dodacia doba"]
    )
    for row in rows:
        if row["status"] == "changed":
            writer.writerow(
                [
                    row["code"],
                    row.get("export_parent_code", row["parent_code"]),
                    int(row["proposed_visibility"]),
                    row["proposed_availability"],
                ]
            )
    return stream.getvalue().encode("utf-8-sig")


async def export(db, identifier, payload):
    run = await db.get(Run, identifier, with_for_update=True)
    if run is None:
        raise ExportError("atomer_availability_run_missing", 404)
    intent = digest(payload.model_dump(mode="json"))
    if run.status == "exported":
        if run.export_hash != intent:
            raise ExportError("atomer_availability_export_immutable")
        return dto(run)
    if run.status != "ready" or run.revision != payload.expected_revision:
        raise ExportError("atomer_availability_preview_changed")
    if run.source_fetched_at + timedelta(minutes=30) < now():
        raise ExportError("atomer_availability_preview_expired")
    if run.document["source_config_hash"] != digest(source_config()):
        raise ExportError("atomer_availability_source_changed")
    if run.document.get("admin_upload_sha"):
        uploaded = await db.get(AdminSource, 1)
        if (
            not uploaded
            or uploaded.source_sha256 != run.document["admin_upload_sha"]
            or uploaded.imported_at + timedelta(days=1) <= now()
        ):
            raise ExportError("atomer_availability_source_changed")
    original = {r["code"]: r for r in run.document["source"]}
    values = await projections(db, original, run.warehouse_id)
    if projection_hash(values, run.product_ids) != run.document["projection_hash"]:
        raise ExportError("atomer_availability_stock_changed")
    rows = edited_rows(run.document["rows"], payload.overrides)
    content = csv_bytes(rows)
    run.document = {
        **run.document,
        "rows": rows,
        "summary": summarize(rows),
        "overrides": [o.model_dump(exclude_none=True) for o in payload.overrides],
    }
    run.csv_content, run.csv_sha256, run.export_hash = (
        content,
        hashlib.sha256(content).hexdigest(),
        intent,
    )
    run.status, run.completed_at = "exported", now()
    run.revision += 1
    await db.flush()
    await prune(db)
    return dto(run)


async def prune(db):
    # A receipt-scoped file cannot replace the last full catalog export, nor
    # another receipt. Retain each scope until the same or a full scope succeeds.
    superseded = """
        old.status='exported' AND old.completed_at<:cutoff AND EXISTS (
            SELECT 1 FROM atomer_availability_runs newer
            WHERE newer.status='exported' AND newer.csv_content IS NOT NULL
                AND (newer.completed_at, newer.id) > (old.completed_at, old.id)
                AND (
                    (newer.product_ids IS NULL AND newer.receiving_batch_id IS NULL)
                    OR (newer.product_ids IS NOT DISTINCT FROM old.product_ids
                        AND newer.receiving_batch_id IS NOT DISTINCT FROM old.receiving_batch_id)
                )
        )
    """
    await db.execute(
        text(f"""
            UPDATE atomer_availability_runs old SET csv_content=NULL
            WHERE {superseded} AND old.csv_content IS NOT NULL
        """),
        {"cutoff": now() - timedelta(days=7)},
    )
    # Exact changed rows, decisions and hashes remain as immutable audit evidence.
    await db.execute(
        text(f"""
            UPDATE atomer_availability_runs old
            SET document = (old.document - 'source') || jsonb_build_object(
                'rows', jsonb_path_query_array(old.document->'rows', '$[*] ? (@.status == "changed")'),
                'archived', true,
                'warnings', COALESCE(old.document->'warnings', '[]'::jsonb)
                    || '["history_details_compacted"]'::jsonb)
            WHERE {superseded} AND old.document ? 'source'
                AND NOT COALESCE((old.document->>'archived')::boolean, false)
        """),
        {"cutoff": now() - timedelta(days=7)},
    )


async def download(db, identifier):
    run = await db.get(Run, identifier)
    if run is None or run.csv_content is None:
        raise ExportError("atomer_availability_download_missing", 404)
    if hashlib.sha256(run.csv_content).hexdigest() != run.csv_sha256:
        raise ExportError("atomer_availability_download_invalid")
    return (
        run.csv_content,
        f"atomer-dostupnost-{run.created_at:%Y%m%d-%H%M%S}-{run.id[:8]}.csv",
        run.csv_sha256,
    )
