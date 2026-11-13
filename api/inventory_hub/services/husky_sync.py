"""Husky catalog + stock XML + optional B2B in the existing supplier worker."""
import asyncio
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from sqlalchemy import select

from inventory_hub.adapters.husky_b2b import fetch_quotes, HuskyError
from inventory_hub.adapters.husky_catalog import parse_catalog, read_stock
from inventory_hub.db_models import Supplier
from inventory_hub.services import catalog
from inventory_hub.services.supplier_availability_source import AvailabilityError, Observation, ParsedAvailability
from inventory_hub.supplier_availability_models import SupplierAvailabilitySettings


PRODUCTS_URL = "https://www.huskysk.sk/feed/partners.xml"
STOCK_URL = "https://www.huskysk.sk/data/partners/Katalog_export.xml"


class Snapshot(ParsedAvailability):
    def __init__(self, records, catalog_records, warnings=(), b2b_count=None, stock_count=None):
        issues = [{"row": i, "sku": row.sku, "stock": row.raw.get("stock"), "external_stock": None,
                   "code": "supplier_availability_unknown_quantity", "kind": "unknown"}
                  for i, row in enumerate(records, 1) if row.available is None]
        super().__init__(records, issues, total=len(records))
        self.b2b_count = b2b_count
        self.stock_count = stock_count
        self.catalog_records = catalog_records
        self.warnings = list(warnings)

    def report(self):
        result = super().report()
        result["accepted"] -= result["unknown"]
        return result


def combine(records, stock, quotes, cfg):
    """CODE joins the public stock; PRODUCTNO joins B2B. Never join by title."""
    if not any(product.code in stock for product, _ in records):
        raise AvailabilityError("husky_stock_incomplete", 422)
    options = (cfg.get("adapter_settings") or {}).get("husky") or {}
    basis = options.get("purchase_price_basis", "unknown")
    if basis not in ("unknown", "net", "gross"):
        raise AvailabilityError("husky_price_basis_invalid", 422)
    counts = Counter(product.manufacturer_code for product, _ in records if product.manufacturer_code)
    observations = []
    labels = (cfg.get("adapter_settings") or {}).get("availability") or {}
    warnings = ["husky_b2b_disabled"] if quotes is None else []
    if any(product.code not in stock for product, _ in records):
        warnings.append("husky_public_stock_unmatched")
    for product, raw in records:
        public_stock = stock.get(product.code)
        raw["public_stock"] = public_stock
        if public_stock is None:
            product.warnings.append("husky_public_stock_unmatched")
        product.warnings = [w for w in product.warnings if w not in ("supplier_stock_unverified", "husky_public_stock_only")]
        if quotes is None:
            # This export describes public retail availability, not B2B stock.
            # Keep it as source evidence only; neither True nor False proves a
            # wholesale offer, a quantity, or wholesale unavailability.
            available, quantity, kind = None, None, "unknown"
            stock_raw, source = None, "husky_public_stock"
            product.warnings.append("husky_public_stock_only")
        else:
            quote = quotes.get(product.manufacturer_code) if counts[product.manufacturer_code] == 1 else None
            if quote is None:
                available, quantity, kind, stock_raw = None, None, "unknown", None
                product.warnings.append("husky_b2b_unmatched")
            else:
                available, quantity, kind, stock_raw = quote.available, quote.quantity, quote.kind, quote.stock
                raw["b2b"] = {"reg_code": quote.code, "stock": quote.stock, "price": str(quote.purchase_price) if quote.purchase_price is not None else None,
                              "base_price": str(quote.base_price) if quote.base_price is not None else None,
                              "price_basis": basis, "currency": "EUR"}
                if basis == "unknown":
                    product.warnings.append("husky_price_basis_unconfirmed")
                elif quote.purchase_price is not None:
                    setattr(product.prices, "purchase_" + basis, quote.purchase_price)
                    if product.prices.vat_percent is not None:
                        factor = 1 + product.prices.vat_percent / 100
                        other = quote.purchase_price / factor if basis == "gross" else quote.purchase_price * factor
                        setattr(product.prices, "purchase_" + ("net" if basis == "gross" else "gross"), other.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            source = "husky_b2b"
        label = (labels.get("orderable") or "do 2-4 dní") if available is True and quantity is not None and quantity >= 2 else (labels.get("unknown") or "overíme")
        product.supplier_available = available
        product.supplier_stock = quantity if kind == "exact" else None
        product.supplier_stock_min = quantity if kind == "minimum" else None
        product.supplier_stock_raw, product.availability = stock_raw, label
        observations.append(Observation(product.code, available, quantity, kind,
            {"source": source, "stock": stock_raw, "public_stock": public_stock, "availability": label}))
        product.source_hash = hashlib.sha256(json.dumps({"source": raw, "prices": product.prices.model_dump(mode="json")},
            ensure_ascii=True, sort_keys=True).encode()).hexdigest()
    from inventory_hub.services.catalog_variants import normalize_singleton_groups
    normalize_singleton_groups([product for product, _ in records])
    if quotes is not None:
        if not any("b2b" in raw for _, raw in records):
            raise AvailabilityError("husky_b2b_no_matches", 422)
        if basis == "unknown":
            warnings.append("husky_price_basis_unconfirmed")
        if any("husky_b2b_unmatched" in p.warnings for p, _ in records):
            warnings.append("husky_b2b_unmatched")
    return Snapshot(observations, records, warnings, len(quotes) if quotes is not None else None, len(stock))


def fetch(plan):
    cfg = plan["config"]
    sources = cfg.get("feeds", {}).get("sources") or {}
    if not catalog._source_configured(sources.get("stock") or {}):
        raise AvailabilityError("husky_stock_source_missing", 422)
    started = time.monotonic()
    try:
        with TemporaryDirectory(prefix="hub-husky-") as directory:
            paths = {}
            for key in ("products", "stock"):
                remaining = 480 - (time.monotonic() - started)
                if remaining <= 0:
                    raise AvailabilityError("husky_b2b_timeout")
                paths[key] = catalog._download("husky", sources[key], key, directory=Path(directory), max_elapsed_seconds=remaining)
            records = parse_catalog(paths["products"], "husky", cfg)
            stock = read_stock(paths["stock"])
            options = (cfg.get("adapter_settings") or {}).get("husky") or {}
            quotes = fetch_quotes(options.get("username"), options.get("password"), deadline=started + 480) if options.get("b2b_enabled") else None
            return combine(records, stock, quotes, cfg), sum(path.stat().st_size for path in paths.values())
    except HuskyError as error:
        raise AvailabilityError(error.code, 502) from None
    except (ValueError, KeyError):
        raise AvailabilityError("husky_catalog_invalid", 422) from None


async def fetch_async(plan):
    task = asyncio.create_task(asyncio.to_thread(fetch, plan))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Keep the worker lease until the bounded HTTP session is closed.
        try:
            await task
        except Exception:
            pass
        raise


async def refresh(db, cfg):
    """Manual catalog button queues the same serialized workflow, never a second scraper."""
    from inventory_hub.services import supplier_availability as service
    from inventory_hub.supplier_availability_types import SupplierAvailabilityInput, SupplierAvailabilityRunInput
    supplier = await db.scalar(select(Supplier).where(Supplier.code == "husky"))
    settings = await db.get(SupplierAvailabilitySettings, supplier.id) if supplier else None
    if settings is None:
        value = await service.configure(db, "husky", SupplierAvailabilityInput(expected_revision=0, feed_key="products"))
        revision = value["revision"]
    else:
        if settings.feed_key != "products":
            raise catalog.CatalogError("husky_select_products", "Select the Husky products source in supplier synchronization", 409)
        revision = settings.revision
    await service.request_run(db, "husky", SupplierAvailabilityRunInput(expected_revision=revision))
    return {"supplier": "husky", "feed_key": "products", "status": "queued", "items": 0}


async def store(db, plan, snapshot, row, run, observed, expires):
    from inventory_hub.db_models import SupplierFeed, SupplierFeedRun, FeedRunStatus
    from inventory_hub.services import feed_mapping
    # Do not silently bypass a user-defined mapping in this combined workflow.
    if await feed_mapping.mapping_row(db, "husky", "products"):
        raise AvailabilityError("husky_custom_mapping_unsupported", 422)
    if snapshot.total * 100 < row.last_item_count * row.min_coverage_percent:
        raise AvailabilityError("supplier_availability_incomplete_feed", 422)
    feed = await db.get(SupplierFeed, plan["feed_id"])
    previous = await db.get(SupplierFeedRun, feed.last_run_id) if feed.last_run_id else None
    previous_count = (previous.error_details or {}).get("husky_b2b_count") if previous else None
    if previous_count and snapshot.b2b_count is not None and snapshot.b2b_count * 100 < previous_count * row.min_coverage_percent:
        raise AvailabilityError("husky_b2b_incomplete", 422)
    previous_stock_count = (previous.error_details or {}).get("husky_stock_count") if previous else None
    if previous_stock_count and snapshot.stock_count is not None and snapshot.stock_count * 100 < previous_stock_count * row.min_coverage_percent:
        raise AvailabilityError("husky_stock_incomplete", 422)
    run.error_details = {**(run.error_details or {}), "husky_b2b_count": snapshot.b2b_count, "husky_stock_count": snapshot.stock_count}
    for product, _ in snapshot.catalog_records:
        product.supplier_observation_expires_at = expires
    new, updated = await catalog.store_catalog_records(db, feed, run, snapshot.catalog_records, observed)
    run.items_new, run.items_updated, run.items_unchanged = new, updated, len(snapshot.catalog_records) - new - updated
    feed.last_run_id, feed.last_run_at = run.id, observed
    feed.last_run_status, feed.last_run_items_count = FeedRunStatus.completed, snapshot.total
    feed.mapping_config = {**(feed.mapping_config or {}), "catalog_parser": "husky", "last_successful_run_id": run.id}
