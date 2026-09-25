"""Supplier catalog snapshots. This module never changes local/shop stock."""
from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
import unicodedata
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import requests
from sqlalchemy import cast, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH, insert
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub import config_io
from inventory_hub.adapters.paul_lange_catalog import parse_catalog
from inventory_hub.adapters.northfinder_catalog import parse_catalog as parse_northfinder
from inventory_hub.catalog_types import CatalogPage, CatalogProduct, CatalogRow, CatalogSelection
from inventory_hub.db_models import FeedRunStatus, Product, Shop, Supplier, SupplierFeed, SupplierFeedItemRaw, SupplierFeedRun, SupplierProduct
from inventory_hub.db_models_ext import ShopProduct, ShopProductContent
from inventory_hub.services.catalog_identity import cached_identities, parent_shop_code
from inventory_hub.services.catalog_sort import variant_sort_key


class CatalogError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


PARSERS = {"paul-lange": parse_catalog, "northfinder": parse_northfinder}


def folded(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c)).casefold().strip()


def supplier_config(supplier: str) -> dict:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,50}", supplier) or not config_io.supplier_path(supplier).is_file():
        raise CatalogError("supplier_not_found", "Supplier configuration was not found", 404)
    return config_io.load_supplier(supplier, write_back_on_load=False)


def source_config(supplier: str, cfg: dict, feed_key: str, *, require_parser: bool = True) -> tuple[dict, str | None]:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,50}", feed_key):
        raise CatalogError("invalid_feed_key", "Invalid listing feed key")
    source = (cfg.get("feeds", {}).get("sources", {}) or {}).get(feed_key)
    if not isinstance(source, dict) or feed_key == "stock" or source.get("type") == "stock":
        raise CatalogError("listing_feed_not_configured", "Select a configured listing feed")
    parser_name = source.get("catalog_parser") or (cfg.get("adapter_settings", {}).get("catalog") or {}).get("parser")
    if not parser_name and supplier in PARSERS:
        parser_name = supplier
    if require_parser and parser_name not in PARSERS:
        raise CatalogError("catalog_parser_unavailable", "A catalog parser is not available for this supplier", 422)
    return source, parser_name


def _download(supplier: str, source: dict, feed_key: str, *, max_elapsed_seconds: int | None = None) -> Path:
    directory = config_io.supplier_path(supplier).parent / "feeds" / "xml"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"catalog_{feed_key}_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{uuid4().hex[:8]}.xml"
    temporary = destination.with_suffix(".part")
    limit = 100 * 1024 * 1024
    started_monotonic = time.monotonic()
    try:
        if source.get("mode", "remote") == "local":
            local = Path(source.get("local_path") or "")
            if not local.is_absolute():
                local = config_io.supplier_path(supplier).parent / local
            if not local.is_file():
                raise CatalogError("feed_file_missing", "Configured local feed file does not exist", 404)
            if local.stat().st_size > limit:
                raise CatalogError("feed_too_large", "Feed exceeds the 100 MB download limit", 413)
            temporary.write_bytes(local.read_bytes())
        else:
            remote = source.get("remote") or {}
            url = remote.get("url") or ""
            if not url.startswith(("https://", "http://")):
                raise CatalogError("feed_url_missing", "The listing feed URL is not configured")
            method = str(remote.get("method") or "GET").upper()
            if method not in ("GET", "POST"):
                raise CatalogError("feed_method_invalid", "Feed download supports GET or POST")
            with requests.Session() as session:
                session.headers.update({"User-Agent": "InventoryHub/catalog", **(remote.get("headers") or {})})
                params = dict(remote.get("params") or {})
                auth = remote.get("auth") or {}
                mode = auth.get("mode") or auth.get("type") or "none"
                if mode == "basic":
                    session.auth = (auth.get("basic_user") or auth.get("username") or "", auth.get("basic_pass") or auth.get("password") or "")
                elif mode in ("token", "bearer", "header"):
                    token = auth.get("token") or auth.get("value") or ""
                    session.headers[auth.get("header_name") or auth.get("header") or "Authorization"] = ("Bearer " + token) if mode == "bearer" else token
                elif mode == "cookie":
                    session.headers["Cookie"] = auth.get("cookie") or ""
                elif mode == "query":
                    params[auth.get("name") or "token"] = auth.get("value") or auth.get("token") or ""
                elif mode != "none":
                    raise CatalogError("feed_auth_invalid", "Unsupported feed authentication mode")
                with session.request(method, url, params=params, data=remote.get("body"),
                                     timeout=(15, 120), stream=True,
                                     verify=remote.get("verify_ssl", True)) as response:
                    if response.status_code >= 400:
                        code = {401: "feed_auth_failed", 403: "feed_auth_failed", 404: "feed_not_found", 429: "feed_rate_limited"}.get(response.status_code, "feed_http_error")
                        raise CatalogError(code, f"Feed download returned HTTP {response.status_code}", 502)
                    size = 0
                    with temporary.open("wb") as output:
                        for chunk in response.iter_content(65536):
                            if max_elapsed_seconds is not None and time.monotonic() - started_monotonic > max_elapsed_seconds:
                                raise CatalogError("feed_timeout", "The supplier exceeded the feed download deadline", 504)
                            size += len(chunk)
                            if size > limit:
                                raise CatalogError("feed_too_large", "Feed exceeds the 100 MB download limit", 413)
                            output.write(chunk)
        if not temporary.stat().st_size:
            raise CatalogError("feed_empty", "The supplier returned an empty feed", 502)
        temporary.replace(destination)
        return destination
    except requests.exceptions.SSLError:
        raise CatalogError("feed_tls_failed", "The supplier TLS certificate could not be verified", 502) from None
    except requests.exceptions.Timeout:
        raise CatalogError("feed_timeout", "The supplier did not deliver the feed within the timeout", 504) from None
    except requests.exceptions.ConnectionError:
        raise CatalogError("feed_connection_failed", "The connection to the supplier failed or was interrupted", 502) from None
    except requests.RequestException:
        # Requests exceptions can contain credentials embedded in URLs.
        raise CatalogError("feed_download_failed", "Could not download the configured supplier feed", 502) from None
    finally:
        temporary.unlink(missing_ok=True)


def download_catalog_source(supplier: str, feed_key: str = "products") -> dict:
    """Save the configured listing source, even before its parser is available."""
    cfg = supplier_config(supplier)
    source, _ = source_config(supplier, cfg, feed_key, require_parser=False)
    path = _download(supplier, source, feed_key)
    return {"supplier": supplier, "feed_key": feed_key, "status": "downloaded",
            "filename": path.name, "relpath": path.relative_to(config_io.DATA_ROOT).as_posix(),
            "size_bytes": path.stat().st_size, "downloaded_at": datetime.now(timezone.utc)}


async def _feed(db: AsyncSession, supplier: str, cfg: dict, feed_key: str, create: bool = False) -> SupplierFeed | None:
    sup = await db.scalar(select(Supplier).where(Supplier.code == supplier))
    if sup is None and create:
        sup = Supplier(code=supplier, name=cfg.get("name") or supplier,
                       adapter=cfg.get("adapter"), config_path=str(config_io.supplier_path(supplier)))
        db.add(sup)
        await db.flush()
    if sup is None:
        return None
    feed = await db.scalar(select(SupplierFeed).where(SupplierFeed.supplier_id == sup.id, SupplierFeed.code == feed_key))
    if feed is None and create:
        feed = SupplierFeed(supplier_id=sup.id, code=feed_key, name=feed_key,
                            feed_type="products", source_format="xml", mapping_config={})
        db.add(feed)
        await db.flush()
    return feed


def _lock_key(supplier: str) -> int:
    return int.from_bytes(hashlib.sha256(("supplier-catalog:" + supplier).encode()).digest()[:8], "big", signed=True)


async def refresh_catalog(db: AsyncSession, supplier: str, feed_key: str = "products") -> dict:
    cfg = supplier_config(supplier)
    source, parser_name = source_config(supplier, cfg, feed_key)
    locked = await db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _lock_key(supplier)})
    if not locked:
        raise CatalogError("catalog_refresh_running", "This supplier is already being refreshed", 409)
    started = datetime.now(timezone.utc)
    try:
        feed = await _feed(db, supplier, cfg, feed_key, create=True)
        run_number = (await db.scalar(select(func.max(SupplierFeedRun.run_number)).where(SupplierFeedRun.feed_id == feed.id)) or 0) + 1
        run = SupplierFeedRun(feed_id=feed.id, run_number=run_number, started_at=started, status=FeedRunStatus.running)
        db.add(run)
        await db.flush()
        path = await asyncio.to_thread(_download, supplier, source, feed_key)
        records = await asyncio.to_thread(PARSERS[parser_name], path, supplier, cfg, feed_key)
        # Order identity/source locks consistently with receiving and local
        # repair. Acquire only after the supplier download/parse has finished.
        from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": IDENTITY_WRITE_LOCK})
        old = {row.supplier_sku: row for row in (await db.execute(
            select(SupplierProduct.id, SupplierProduct.supplier_sku, SupplierProduct.source_feed_id,
                   SupplierProduct.attributes["catalog"]["source_hash"].astext.label("source_hash"))
            .where(SupplierProduct.supplier_id == feed.supplier_id)
        )).all()}
        now = datetime.now(timezone.utc)
        new_count = updated_count = 0
        for start in range(0, len(records), 200):
            chunk = records[start:start + 200]
            values = []
            for product, raw in chunk:
                previous = old.get(product.code)
                if previous and previous.source_feed_id not in (None, feed.id):
                    raise CatalogError("catalog_source_conflict", f"Code {product.code} already belongs to another feed", 409)
                new_count += previous is None
                updated_count += bool(previous and previous.source_hash != product.source_hash)
                product.run_id, product.fetched_at = run.id, now
                catalog = product.model_dump(mode="json", exclude={"id", "listed"})
                attrs = {"catalog": catalog, "search_name": folded(product.name),
                         "search_code": folded(product.code), "search_shop_code": folded(product.shop_code)}
                values.append({
                    "supplier_id": feed.supplier_id, "supplier_sku": product.code,
                    "ean": product.eans[0] if product.eans and not product.import_blockers else None,
                    "manufacturer_sku": product.manufacturer_code, "name": product.name,
                    "brand": product.brand, "category": product.category if len(product.category or "") <= 255 else None,
                    "description": product.description, "images": product.images, "attributes": attrs,
                    "supplier_group_code": product.group_code,
                    "purchase_price": product.prices.purchase_net, "purchase_currency": product.prices.currency,
                    "recommended_price": product.prices.retail_gross,
                    "supplier_availability_text": product.availability, "stock_qty": product.supplier_stock,
                    "is_active": True, "is_discontinued": False, "source_feed_id": feed.id,
                    "last_seen_run_id": run.id, "last_seen_at": now, "updated_at": now,
                })
            statement = insert(SupplierProduct).values(values)
            changes = {key: getattr(statement.excluded, key) for key in values[0] if key not in ("supplier_id", "supplier_sku")}
            # Preserve unrelated attributes created by another Hub workflow.
            changes["attributes"] = SupplierProduct.attributes.op("||")(statement.excluded.attributes)
            id_rows = (await db.execute(statement.on_conflict_do_update(
                constraint="uq_supplier_products_sku", set_=changes
            ).returning(SupplierProduct.id, SupplierProduct.supplier_sku))).all()
            id_map = {row.supplier_sku: row.id for row in id_rows}
            await db.execute(insert(SupplierFeedItemRaw), [{
                "feed_id": feed.id, "run_id": run.id, "item_hash": product.source_hash,
                "raw_data": raw, "processed": True, "processed_at": now,
                "supplier_product_id": id_map[product.code], "fetched_at": now,
            } for product, raw in chunk])
        await db.execute(update(SupplierProduct).where(
            SupplierProduct.source_feed_id == feed.id,
            SupplierProduct.last_seen_run_id != run.id,
        ).values(is_active=False))
        run.status, run.finished_at = FeedRunStatus.completed, now
        run.items_fetched, run.items_new, run.items_updated = len(records), new_count, updated_count
        run.items_unchanged = len(records) - new_count - updated_count
        run.source_size_bytes = path.stat().st_size
        feed.last_run_id, feed.last_run_at = run.id, now
        feed.last_run_status, feed.last_run_items_count = FeedRunStatus.completed, len(records)
        feed.mapping_config = {**(feed.mapping_config or {}), "catalog_parser": parser_name,
                               "last_successful_run_id": run.id,
                               "xml_path": path.relative_to(config_io.DATA_ROOT).as_posix()}
        await db.flush()
        from inventory_hub.services.supplier_links import reconcile_source_codes
        link_reports = await reconcile_source_codes(db, supplier, [product.code for product, _raw in records], cfg)
        return {"supplier": supplier, "feed_key": feed_key, "run_id": run.id,
                "status": "completed", "items": len(records), "new": new_count,
                "updated": updated_count, "fetched_at": now,
                "groups": len({p.group_code for p, _ in records if p.group_code}),
                "warnings": sum(bool(p.warnings) for p, _ in records),
                "supplier_links": {"linked": sum(report["linked"] for report in link_reports),
                    "conflicts": sum(len(report["conflicts"]) for report in link_reports)}}
    except Exception as error:
        await db.rollback()
        # Keep the old catalog, but commit a separate failed-run audit record.
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _lock_key(supplier)})
        feed = await _feed(db, supplier, cfg, feed_key, create=True)
        number = (await db.scalar(select(func.max(SupplierFeedRun.run_number)).where(SupplierFeedRun.feed_id == feed.id)) or 0) + 1
        code = error.code if isinstance(error, CatalogError) else "catalog_parse_failed"
        db.add(SupplierFeedRun(feed_id=feed.id, run_number=number, started_at=started,
                              finished_at=datetime.now(timezone.utc), status=FeedRunStatus.failed,
                              error_message=code))
        feed.last_run_status = FeedRunStatus.failed
        await db.commit()
        if isinstance(error, CatalogError):
            raise
        raise CatalogError(code, "The feed could not be indexed; the last successful catalog is unchanged", 422) from None


def public_product(row: SupplierProduct, *, detail: bool = False, listed: bool = False) -> CatalogProduct:
    data = dict((row.attributes or {}).get("catalog") or {})
    data.update(id=row.id, listed=listed)
    if not detail:
        for key in ("description", "manufacturer_description", "safety_information", "parameters", "static_parameters"):
            data.pop(key, None)
    return CatalogProduct.model_validate(data)


async def listed_codes(db: AsyncSession, shop_code: str | None) -> set[str]:
    if not shop_code:
        return set()
    rows = (await db.execute(select(Product.sku, ShopProduct.external_code, ShopProduct.variant_code)
                            .join(ShopProduct, ShopProduct.product_id == Product.id)
                            .join(Shop, Shop.id == ShopProduct.shop_id)
                            .where(Shop.code == shop_code, ShopProduct.is_listed.is_(True)))).all()
    return {code.casefold() for row in rows for code in row if code}


def _http_url(value) -> str | None:
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme in ("https", "http") and parsed.hostname and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return None


async def attach_shop_links(db: AsyncSession, shop_code: str | None, products: list[CatalogProduct]) -> None:
    """Resolve each parent's local content once; never call the shop API for links."""
    codes = {code for p in products if p.listed for code in
             [p.shop_code, *(m.code for m in p.shop_matches), *(m.parent_code for m in p.shop_matches)] if code}
    if not shop_code or not codes:
        return
    rows = (await db.execute(select(
        Product.sku, ShopProduct.external_code, ShopProduct.variant_code,
        func.coalesce(func.nullif(ShopProduct.parent_code, ""), ShopProduct.external_code).label("content_code"),
    ).select_from(Product).join(ShopProduct, ShopProduct.product_id == Product.id)
        .join(Shop, Shop.id == ShopProduct.shop_id)
        .where(Shop.code == shop_code, ShopProduct.is_listed.is_(True)))).all()
    folded_codes = {code.casefold() for code in codes}
    rows = [row for row in rows if any(code and code.casefold() in folded_codes
                                     for code in (row.sku, row.external_code, row.variant_code))]
    if not rows:
        return
    # A legacy parent can contain thousands of variants. Extracting its large JSON
    # through the variant join repeats the same work for every matching variant.
    content = ShopProductContent.data
    parents = (await db.execute(select(
        ShopProductContent.external_code,
        func.jsonb_path_query_first(content, cast('$.descriptions[*] ? (@.language == "sk").url', JSONPATH), type_=JSONB).label("sk_url"),
        content["descriptions"][0]["url"].astext.label("default_url"),
        content["admin_url"].astext.label("admin_url"), content["active_yn"].as_boolean().label("active"),
    ).select_from(ShopProductContent).join(Shop, Shop.id == ShopProductContent.shop_id)
        .where(Shop.code == shop_code, ShopProductContent.external_code.in_({row.content_code for row in rows})))).all()
    by_parent = {row.external_code: row for row in parents}
    links = {code.casefold(): by_parent[row.content_code] for row in rows if row.content_code in by_parent
             for code in (row.sku, row.external_code, row.variant_code) if code}
    for product in products:
        row = next((links[code.casefold()] for code in [product.shop_code, *(m.code for m in product.shop_matches),
                    *(m.parent_code for m in product.shop_matches)] if code.casefold() in links), None)
        if product.listed and row is not None:
            product.shop_url = _http_url(row.sk_url) or _http_url(row.default_url)
            product.shop_admin_url = _http_url(row.admin_url)
            product.shop_active = row.active if isinstance(row.active, bool) else None


async def _matches(db: AsyncSession, supplier: str, feed_key: str, *, q: str = "", code: str = "",
                   ean: str = "", manufacturer: str = "", sort: str = "name", shop: str | None = None,
                   listing: str = "all"):
    cfg = supplier_config(supplier)
    source_config(supplier, cfg, feed_key)
    feed = await _feed(db, supplier, cfg, feed_key)
    known = await listed_codes(db, shop)
    index = await asyncio.to_thread(cached_identities, shop)
    if feed is None:
        return None, [], known, [], index
    conditions = [SupplierProduct.source_feed_id == feed.id, SupplierProduct.is_active.is_(True),
                  SupplierProduct.attributes.has_key("catalog")]
    if q.strip():
        query = folded(q)
        conditions.append(or_(SupplierProduct.attributes["search_name"].astext.contains(query, autoescape=True),
                              SupplierProduct.attributes["search_code"].astext.contains(query, autoescape=True),
                              SupplierProduct.attributes["search_shop_code"].astext.contains(query, autoescape=True),
                              func.lower(SupplierProduct.manufacturer_sku).contains(q.strip().lower(), autoescape=True),
                              SupplierProduct.attributes["catalog"]["eans"].contains([q.strip()])))
    if code.strip():
        conditions.append(or_(SupplierProduct.attributes["search_code"].astext == folded(code),
                              SupplierProduct.attributes["search_shop_code"].astext == folded(code),
                              func.lower(SupplierProduct.manufacturer_sku) == code.strip().lower()))
    if ean.strip():
        conditions.append(SupplierProduct.attributes["catalog"]["eans"].contains([ean.strip()]))
    if manufacturer:
        conditions.append(SupplierProduct.brand == manufacturer)
    columns = [SupplierProduct.id, SupplierProduct.supplier_group_code]
    if listing in ("listed", "unlisted"):
        if not shop:
            raise CatalogError("shop_required", "Choose a shop for the listing filter")
        data = SupplierProduct.attributes["catalog"]
        columns.extend([data[k].astext.label(k) for k in ("code", "shop_code", "group_code", "variant_relationship")])
        columns.append(data["eans"].label("eans"))
    if listing == "warnings":
        conditions.append(func.jsonb_array_length(SupplierProduct.attributes["catalog"]["warnings"]) > 0)
    order = {"name": SupplierProduct.attributes["search_name"].astext,
             "code": SupplierProduct.supplier_sku, "manufacturer": SupplierProduct.brand}.get(sort)
    if order is None:
        raise CatalogError("invalid_sort", "Supported sorts: name, code, manufacturer")
    rows = (await db.execute(select(*columns)
                            .where(*conditions).order_by(order, SupplierProduct.id))).all()
    if listing in ("listed", "unlisted"):
        # Use the preview's Unicode casefold in both paths. PostgreSQL lower()
        # differs for identifiers such as STRAẞE; only small identity fields are read.
        codes = known | index.codes.keys()
        rows = [row for row in rows if (row.shop_code.casefold() in codes or
                any(ean in index.eans for ean in row.eans) or parent_shop_code(row).casefold() in index.codes) == (listing == "listed")]
    manufacturers = list((await db.scalars(select(SupplierProduct.brand).where(
        SupplierProduct.source_feed_id == feed.id, SupplierProduct.is_active.is_(True),
        SupplierProduct.brand.is_not(None)).distinct().order_by(SupplierProduct.brand))).all())
    return feed, rows, known, manufacturers, index


async def catalog_page(db: AsyncSession, supplier: str, feed_key: str = "products", *,
                       page: int = 1, page_size: int = 50, grouped: bool = True, **filters) -> CatalogPage:
    feed, matches, known, manufacturers, index = await _matches(db, supplier, feed_key, **filters)
    groups: OrderedDict[str, list[int]] = OrderedDict()
    for row in matches:
        key = "group:" + row.supplier_group_code if grouped and row.supplier_group_code else "item:" + str(row.id)
        groups.setdefault(key, []).append(row.id)
    selected = list(groups.items())[(page - 1) * page_size:page * page_size]
    ids = [id for _, group_ids in selected for id in group_ids]
    data = {p.id: p for p in (await db.scalars(select(SupplierProduct).where(SupplierProduct.id.in_(ids)))).all()} if ids else {}
    items = []
    for key, group_ids in selected:
        products = [public_product(data[id], listed=(data[id].attributes["catalog"]["shop_code"].casefold() in known)) for id in group_ids]
        for product in products:
            product.shop_matches = index.matches(product)
            product.listed = product.listed or bool(product.shop_matches)
        is_group = key.startswith("group:")
        if is_group:
            products.sort(key=variant_sort_key)
        items.append(CatalogRow(key=key, product=products[0], is_group=is_group,
                                variants_count=len(products) if is_group else 0,
                                matching_ids=group_ids, variants=products if is_group else []))
    await attach_shop_links(db, filters.get("shop"), [p for item in items for p in (item.variants or [item.product])])
    return CatalogPage(supplier=supplier, feed_key=feed_key, run_id=feed.last_run_id if feed else None,
                       fetched_at=feed.last_run_at if feed else None, total=len(groups), total_items=len(matches),
                       page=page, page_size=page_size, pages=math.ceil(len(groups) / page_size),
                       manufacturers=manufacturers, items=items, shop_checked_at=index.checked_at)


async def catalog_selection(db: AsyncSession, supplier: str, feed_key: str = "products", **filters) -> CatalogSelection:
    feed, matches, _, _, _ = await _matches(db, supplier, feed_key, **filters)
    return CatalogSelection(supplier=supplier, feed_key=feed_key, run_id=feed.last_run_id if feed else None,
                            ids=[row.id for row in matches], total=len(matches))


async def selected_products(db: AsyncSession, supplier: str, feed_key: str, ids: list[int], run_id: int | None = None) -> list[CatalogProduct]:
    cfg = supplier_config(supplier)
    source_config(supplier, cfg, feed_key)
    feed = await _feed(db, supplier, cfg, feed_key)
    if feed is None or (run_id is not None and feed.last_run_id != run_id):
        raise CatalogError("catalog_changed", "The catalog changed; reload your selection", 409)
    rows = (await db.scalars(select(SupplierProduct).where(
        SupplierProduct.id.in_(ids), SupplierProduct.source_feed_id == feed.id,
        SupplierProduct.is_active.is_(True), SupplierProduct.attributes.has_key("catalog")
    ))).all()
    if len(rows) != len(set(ids)):
        raise CatalogError("catalog_items_missing", "Some selected products are no longer in this supplier feed", 409)
    by_id = {row.id: public_product(row, detail=True) for row in rows}
    return [by_id[id] for id in dict.fromkeys(ids)]


async def catalog_detail(db: AsyncSession, supplier: str, product_id: int, include_variants: bool = True, shop: str | None = None) -> dict:
    supplier_config(supplier)
    row = await db.scalar(select(SupplierProduct).join(Supplier).where(
        SupplierProduct.id == product_id, Supplier.code == supplier, SupplierProduct.attributes.has_key("catalog")))
    if row is None:
        raise CatalogError("catalog_product_missing", "Product was not found in this supplier catalog", 404)
    known = await listed_codes(db, shop)
    product = public_product(row, detail=True, listed=row.attributes["catalog"]["shop_code"].casefold() in known)
    variants = []
    if include_variants and row.supplier_group_code:
        siblings = (await db.scalars(select(SupplierProduct).where(
            SupplierProduct.source_feed_id == row.source_feed_id,
            SupplierProduct.supplier_group_code == row.supplier_group_code,
            SupplierProduct.is_active.is_(True)).order_by(SupplierProduct.supplier_sku))).all()
        variants = [public_product(p, detail=True, listed=p.attributes["catalog"]["shop_code"].casefold() in known) for p in siblings]
        variants.sort(key=variant_sort_key)
    index = await asyncio.to_thread(cached_identities, shop)
    for item in [product, *variants]:
        item.shop_matches = index.matches(item)
        item.listed = item.listed or bool(item.shop_matches)
    await attach_shop_links(db, shop, [product, *variants])
    raw = await db.scalar(select(SupplierFeedItemRaw.raw_data).where(
        SupplierFeedItemRaw.supplier_product_id == product_id, SupplierFeedItemRaw.run_id == row.last_seen_run_id))
    return {"product": product, "variants": variants, "source_fields": (raw or {}).get("fields", {}),
            "source_xml": (raw or {}).get("xml", ""), "active": row.is_active}


async def catalog_status(db: AsyncSession, supplier: str, feed_key: str = "products") -> dict:
    cfg = supplier_config(supplier)
    sources = []
    for key, source in (cfg.get("feeds", {}).get("sources", {}) or {}).items():
        if key == "stock" or not isinstance(source, dict) or source.get("type") == "stock":
            continue
        try:
            _, parser = source_config(supplier, cfg, key)
        except CatalogError:
            parser = None
        sources.append({"key": key, "name": source.get("name") or key, "supported": parser is not None,
                        "configured": bool(source.get("local_path") if source.get("mode") == "local" else (source.get("remote") or {}).get("url"))})
    feed = await _feed(db, supplier, cfg, feed_key)
    shops = []
    for shop in (await db.scalars(select(Shop).where(Shop.is_active.is_(True)).order_by(Shop.name))).all():
        shop_cfg = config_io.load_shop(shop.code)
        ready = all(shop_cfg.get(key) for key in ("upgates_api_base_url", "upgates_login", "upgates_api_key"))
        shops.append({"code": shop.code, "name": shop.name, "ready": bool(ready)})
    adapter = cfg.get("adapter_settings") or {}
    return {"supplier": supplier, "name": cfg.get("name") or supplier, "sources": sources,
            "feed_key": feed_key, "run_id": feed.last_run_id if feed else None,
            "last_successful_at": feed.last_run_at if feed else None,
            "status": feed.last_run_status.value if feed and feed.last_run_status else "not_downloaded",
            "items": feed.last_run_items_count if feed else 0, "shops": shops,
            "defaults": {"category_code": adapter.get("default_category", "K00090"),
                         "currency": (adapter.get("catalog") or {}).get("currency", "EUR"),
                         "vat_percent": str(adapter.get("vat", 23))}}
