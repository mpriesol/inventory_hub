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

from inventory_hub.adapters.husky_b2b import fetch_quotes, HuskyError, MAX_COLLECTION_SECONDS
from inventory_hub.adapters.husky_catalog import parse_catalog, read_stock
from inventory_hub.db_models import Supplier
from inventory_hub.services import catalog
from inventory_hub.services.supplier_availability_source import AvailabilityError, Observation, ParsedAvailability
from inventory_hub.supplier_availability_models import SupplierAvailabilitySettings
from inventory_hub import operation_log


PRODUCTS_URL = "https://www.huskysk.sk/feed/partners.xml"
STOCK_URL = "https://www.huskysk.sk/data/partners/Katalog_export.xml"


class Snapshot(ParsedAvailability):
    def __init__(self, records, catalog_records, warnings=(), b2b_count=None, stock_count=None, issues=(), total=None):
        issues = list(issues) + [{"row": row.raw.get("source_row", i), "sku": row.sku, "stock": row.raw.get("stock"), "external_stock": None,
                   "code": "supplier_availability_unknown_quantity", "kind": "unknown"}
                  for i, row in enumerate(records, 1) if row.available is None]
        super().__init__(records, issues, total=len(records) if total is None else total)
        self.b2b_count = b2b_count
        self.stock_count = stock_count
        self.catalog_records = catalog_records
        self.warnings = list(warnings)
        self.coverage = []

    def report(self):
        result = super().report()
        result["accepted"] -= result["unknown"]
        return result

    def diagnostic(self):
        return {"catalog_rows": self.total, "stock_rows": self.stock_count, "b2b_rows": self.b2b_count,
                "matched_rows": sum("b2b" in raw for _, raw in self.catalog_records),
                "unknown": sum(row.available is None for row in self)}

    def check_coverage(self, count, baseline, percent, target):
        if count * 100 < baseline * percent:
            self.coverage.append({"target": target, "rows": count, "previous_rows": baseline,
                "required_rows": (baseline * percent + 99) // 100, "min_coverage_percent": percent})
            if "supplier_availability_incomplete_feed" not in self.warnings:
                self.warnings.append("supplier_availability_incomplete_feed")
            return baseline
        return count


def combine(records, stock, quotes, cfg):
    """CODE joins the public stock; PRODUCTNO joins B2B. Never join by title."""
    options = (cfg.get("adapter_settings") or {}).get("husky") or {}
    basis = options.get("purchase_price_basis", "unknown")
    if basis not in ("unknown", "net", "gross"):
        raise AvailabilityError("husky_price_basis_invalid", 422)
    counts = getattr(records, "manufacturer_counts", Counter(product.manufacturer_code for product, _ in records if product.manufacturer_code))
    observations, accepted_catalog = [], []
    issues = list(getattr(records, "issues", []))
    total = getattr(records, "total", len(records))
    labels = (cfg.get("adapter_settings") or {}).get("availability") or {}
    warnings = ["husky_b2b_disabled"] if quotes is None else []
    if any(product.code not in stock for product, _ in records):
        warnings.append("husky_public_stock_unmatched")
    for index, (product, raw) in enumerate(records, 1):
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
                issues.append({"row": raw.get("source_row", index), "sku": product.code, "stock": None, "external_stock": None,
                               "code": "husky_b2b_unmatched", "kind": "rejected"})
                continue
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
            {"source": source, "stock": stock_raw, "public_stock": public_stock, "availability": label, "source_row": raw.get("source_row", index)}))
        accepted_catalog.append((product, raw))
        product.source_hash = hashlib.sha256(json.dumps({"source": raw, "prices": product.prices.model_dump(mode="json")},
            ensure_ascii=True, sort_keys=True).encode()).hexdigest()
    from inventory_hub.services.catalog_variants import normalize_singleton_groups
    normalize_singleton_groups([product for product, _ in records])
    if quotes is not None:
        if not any("b2b" in raw for _, raw in records):
            raise AvailabilityError("husky_b2b_no_matches", 422, diagnostic={
                "phase": "join_sources", "reason": "no_matching_codes", "target": "husky_catalog",
                "catalog_rows": len(records), "stock_rows": len(stock), "b2b_rows": len(quotes), "matched_rows": 0})
        if basis == "unknown":
            warnings.append("husky_price_basis_unconfirmed")
        if any("husky_b2b_unmatched" in p.warnings for p, _ in records):
            warnings.append("husky_b2b_unmatched")
    if getattr(stock, "issues", []):
        warnings.append("husky_stock_rows_rejected")
    return Snapshot(observations, accepted_catalog, warnings, len(quotes) if quotes is not None else None, len(stock), issues, total)


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
                remaining = MAX_COLLECTION_SECONDS - (time.monotonic() - started)
                if remaining <= 0:
                    raise AvailabilityError("husky_b2b_timeout")
                download_started = time.monotonic()
                try:
                    paths[key] = catalog._download("husky", sources[key], key, directory=Path(directory), max_elapsed_seconds=remaining)
                except Exception:
                    operation_log.emit("feed_download_failed", component="husky", supplier="husky", level="error",
                        direction="outbound", target="husky_" + key + "_feed", phase="feed_download",
                        duration_ms=round((time.monotonic() - download_started) * 1000))
                    raise
                operation_log.emit("feed_download_completed", component="husky", supplier="husky",
                    direction="outbound", target="husky_" + key + "_feed", phase="feed_download",
                    duration_ms=round((time.monotonic() - download_started) * 1000))
            records = parse_catalog(paths["products"], "husky", cfg, tolerant=True)
            stock = read_stock(paths["stock"], tolerant=True)
            options = (cfg.get("adapter_settings") or {}).get("husky") or {}
            quotes = fetch_quotes(options.get("username"), options.get("password"), deadline=started + MAX_COLLECTION_SECONDS) if options.get("b2b_enabled") else None
            snapshot = combine(records, stock, quotes, cfg)
            operation_log.emit("snapshot_parsed", component="husky", supplier="husky", phase="join_sources", **snapshot.diagnostic())
            return snapshot, sum(path.stat().st_size for path in paths.values())
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
    snapshot.check_coverage(snapshot.total, row.last_item_count, row.min_coverage_percent, "husky_products_feed")
    feed = await db.get(SupplierFeed, plan["feed_id"])
    previous = await db.get(SupplierFeedRun, feed.last_run_id) if feed.last_run_id else None
    prior = previous.error_details or {} if previous else {}
    previous_count = prior.get("husky_b2b_baseline", prior.get("husky_b2b_count")) or 0
    previous_stock_count = prior.get("husky_stock_baseline", prior.get("husky_stock_count")) or 0
    b2b_baseline = snapshot.check_coverage(snapshot.b2b_count, previous_count, row.min_coverage_percent,
        "husky_catalog") if snapshot.b2b_count is not None else previous_count
    stock_baseline = snapshot.check_coverage(snapshot.stock_count, previous_stock_count, row.min_coverage_percent,
        "husky_stock_feed") if snapshot.stock_count is not None else previous_stock_count
    run.error_details = {**(run.error_details or {}), "husky_b2b_count": snapshot.b2b_count,
        "husky_stock_count": snapshot.stock_count, "husky_b2b_baseline": b2b_baseline, "husky_stock_baseline": stock_baseline}
    for product, _ in snapshot.catalog_records:
        product.supplier_observation_expires_at = expires
    # An availability batch cannot prove that omitted/unmatched products were retired.
    new, updated = await catalog.store_catalog_records(db, feed, run, snapshot.catalog_records, observed,
                                                      deactivate_missing=False)
    run.items_new, run.items_updated, run.items_unchanged = new, updated, len(snapshot.catalog_records) - new - updated
    feed.last_run_id, feed.last_run_at = run.id, observed
    feed.last_run_status, feed.last_run_items_count = FeedRunStatus.completed, snapshot.total
    feed.mapping_config = {**(feed.mapping_config or {}), "catalog_parser": "husky", "last_successful_run_id": run.id}
