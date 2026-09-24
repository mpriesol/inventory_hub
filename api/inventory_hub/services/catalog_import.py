"""Frozen, create-only supplier imports. Never send inventory fields to Upgates."""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
from collections import Counter, OrderedDict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub import config_io
from inventory_hub.adapters.pl_feed_convert import DEFAULT_COEFFS
from inventory_hub.catalog_types import CatalogProduct, ImportItem, ImportPriceLine, ShopImportOptions, ShopImportPreview, ShopImportPreviewRequest
from inventory_hub.database import get_session_context
from inventory_hub.db_models import Product, ProductGroup, Shop, Supplier
from inventory_hub.db_models_ext import ProductSupplySource, ShopProduct, ShopProductContent
from inventory_hub.services.catalog import CatalogError, selected_products, supplier_config
from inventory_hub.services.catalog_html import clean_description
from inventory_hub.services.catalog_identity import cached_identities, connection_fingerprint, parent_shop_code, read_cache
from inventory_hub.services.catalog_sort import variant_sort_key
from inventory_hub.services.catalog_merchandising import category_chain, availability_policy, apply_availability
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK, RemoteIdentity, load_identity_index
from inventory_hub.services.upgates import UpgatesClient, UpgatesError


def now() -> datetime:
    return datetime.now(timezone.utc)


def shop_config(shop: str) -> dict:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,50}", shop) or not config_io.shop_path(shop).is_file():
        raise CatalogError("shop_not_found", "Shop configuration was not found", 404)
    cfg = config_io.load_shop(shop)
    if not all(cfg.get(k) for k in ("upgates_api_base_url", "upgates_login", "upgates_api_key")):
        raise CatalogError("shop_not_ready", "Configure the target shop's Upgates connection first", 422)
    return cfg


def _target(cfg: dict) -> str:
    # Detect accidental target changes without storing credentials in the job.
    return hashlib.sha256(cfg["upgates_api_base_url"].rstrip("/").encode()).hexdigest()


def assert_supplier_availability(supplier: str, frozen: dict | None) -> None:
    if frozen is None or frozen != availability_policy(supplier, supplier_config(supplier)):
        raise CatalogError("supplier_availability_changed", "Supplier availability settings changed; create a new preview", 409)


def _path(shop: str, preview_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", preview_id):
        raise CatalogError("preview_not_found", "Import preview was not found", 404)
    shop_config(shop)
    return config_io.shop_path(shop).parent / "catalog-imports" / (preview_id + ".json")


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix("." + uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _load(path: Path) -> dict:
    if not path.is_file():
        raise CatalogError("preview_not_found", "Import preview was not found", 404)
    return json.loads(path.read_text(encoding="utf-8"))


def _result_path(path: Path) -> Path:
    return path.with_suffix(".result.json")


def _compact_result(result: dict) -> dict:
    # Polling never transfers thousands of complete descriptions and API payloads.
    items = []
    for item in result["items"]:
        payload = item.get("payload") or {}
        summary = {"images": payload.get("images", [])[:1], "prices": payload.get("prices", [])}
        if payload.get("variants"):
            summary["variants"] = [{k: v[k] for k in ("code", "image", "parameters", "prices") if k in v} for v in payload["variants"]]
        items.append({**item, "payload": summary})
    return {**result, "items": items}


def _save_job(path: Path, document: dict) -> None:
    _write(path, document)
    _write(_result_path(path), _compact_result(document["result"]))
    events = path.with_suffix(".events.jsonl")
    if events.is_file():
        # Preserve the audit and start a clean journal after saving all its states.
        # This also isolates an incomplete trailing record after a process crash.
        events.replace(path.with_suffix(".events." + uuid4().hex + ".jsonl"))
        _sync_directory(path.parent)


def _item_checkpoint(path: Path, result: dict, item: dict) -> None:
    # Append only the changed state. Rewriting a full 6,000-item preview per item
    # would turn a normal batch into hundreds of GB of filesystem writes.
    event = {"updated_at": now().isoformat(), "code": item["code"],
             "status": item["status"], "errors": item["errors"], "warnings": item["warnings"]}
    with path.with_suffix(".events.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps(event, ensure_ascii=False) + "\n")
        output.flush()
        os.fsync(output.fileno())
    _sync_directory(path.parent)
    result["updated_at"] = event["updated_at"]


def _read_result(path: Path) -> dict | None:
    saved = _result_path(path)
    if not saved.is_file():
        return _load(path).get("result")
    result = _load(saved)
    checkpoint = result["updated_at"]
    items = {item["code"]: item for item in result["items"]}
    events = path.with_suffix(".events.jsonl")
    if events.is_file():
        with events.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.endswith("\n"):
                    # An incomplete final intent could not have been followed by a POST.
                    break
                event = json.loads(line)
                if event["updated_at"] > checkpoint and event["code"] in items:
                    items[event["code"]].update({k: event[k] for k in ("status", "errors", "warnings")})
                    result["updated_at"] = event["updated_at"]
    return result


def _load_job(path: Path) -> dict:
    document = _load(path)
    result = _read_result(path)
    if result is not None:
        payloads = {item["code"]: item["payload"] for item in document["preview"]["items"]}
        document["result"] = {**result, "items": [{**item, "payload": payloads[item["code"]]} for item in result["items"]]}
    return document


@contextmanager
def _shop_lock(shop: str):
    directory = config_io.shop_path(shop).parent / "catalog-imports"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "import.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CatalogError("shop_import_running", "An import is already running for this shop", 409) from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _get(client: UpgatesClient, path: str, params: dict | None = None) -> dict:
    try:
        result = client.get(path, params)
    except UpgatesError:
        raise CatalogError("upgates_read_failed", "Could not verify the target shop; check connection and API permissions", 502) from None
    if not isinstance(result, dict) or any(m.get("level") in ("error", "fatal_error") for m in result.get("messages", []) if isinstance(m, dict)):
        raise CatalogError("upgates_read_failed", "Upgates did not return a valid result", 502)
    return result


def _pages(client: UpgatesClient, path: str, key: str, params: dict | None = None) -> list[dict]:
    output = []
    page = 1
    while True:
        pagination = {"page": page, **({"current_page_items": 100} if path.startswith("products") else {})}
        data = _get(client, path, {**(params or {}), **pagination})
        if not isinstance(data.get(key), list):
            raise CatalogError("upgates_read_failed", f"Upgates did not return {key}", 502)
        output.extend(data[key])
        if page >= int(data.get("number_of_pages") or 1):
            return output
        if not data[key] or page >= 10000:
            raise CatalogError("upgates_read_failed", "Incomplete Upgates pagination", 502)
        page += 1


def import_options(shop: str, client: UpgatesClient | None = None) -> dict:
    shop_config(shop)
    client = client or UpgatesClient.from_shop(shop)
    config = _get(client, "config").get("config") or {}
    if not isinstance(config.get("prices_with_vat_yn"), bool):
        raise CatalogError("shop_vat_unknown", "The target shop's price VAT mode could not be verified", 422)
    languages = _get(client, "languages").get("languages")
    pricelists = _get(client, "pricelists").get("pricelists")
    if not isinstance(languages, list) or not isinstance(pricelists, list):
        raise CatalogError("upgates_read_failed", "Shop language or pricelist response is incomplete", 502)
    categories = _pages(client, "categories", "categories")
    from inventory_hub.services.catalog_merchandising import category_rows
    metas = _pages(client, "metas", "metas", {"key": "validation_required", "category": "products"})
    field = next((m for m in metas if m.get("key") == "validation_required" and m.get("category") == "products"), None)
    if field and (field.get("type") != "checkbox" or not field.get("common_languages_value_yn")):
        raise CatalogError("validation_field_incompatible", "validation_required must be a checkbox shared across languages", 422)
    return {"shop": shop, "prices_with_vat": config["prices_with_vat_yn"],
            "languages": [{"code": l.get("language_id"), "currency": l.get("currency_id"),
                           "default": bool(l.get("default_yn"))} for l in languages if l.get("active_yn")],
            "pricelists": [{"name": p["name"], "default": p.get("default_yn") in (True, 1, "1")} for p in pricelists],
            "categories": category_rows(categories), "category_tree_version": 2,
            "create_validation_field": field is None}


def remote_identities(client: UpgatesClient) -> tuple[set[str], set[str]]:
    codes, eans = set(), set()
    for product in _pages(client, "products/simple", "products"):
        for item in [product, *(product.get("variants") or [])]:
            if item.get("code"):
                codes.add(str(item["code"]).casefold())
            if item.get("ean"):
                eans.update(re.split(r"[;,|/\s]+", str(item["ean"]).strip()))
    return codes, eans


@contextmanager
def _shop_cache(shop: str, kind: str):
    cfg = shop_config(shop)
    fingerprint = connection_fingerprint(cfg)
    directory = config_io.shop_path(shop).parent / "catalog-cache"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (kind + ".json")
    with path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CatalogError("shop_check_running", "Another shop check is running; try again shortly", 409) from None
        try:
            saved = read_cache(path, fingerprint, now())
            yield path, saved, fingerprint
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def cached_import_options(shop: str, client: UpgatesClient | None = None, *, refresh: bool = False) -> dict:
    with _shop_cache(shop, "options") as (path, saved, fingerprint):
        cached = bool(not refresh and saved and isinstance(saved.get("data"), dict) and saved["data"].get("category_tree_version") == 2 and
                      now() - datetime.fromisoformat(saved["checked_at"]) < timedelta(minutes=15))
        if not cached:
            data = import_options(shop, client)
            saved = {"version": 1, "fingerprint": fingerprint, "checked_at": now().isoformat(), "data": data}
            _write(path, saved)
        checked = datetime.fromisoformat(saved["checked_at"])
        return {**saved["data"], "cache": {"checked_at": saved["checked_at"], "from_cache": cached,
                "expires_at": (checked + timedelta(minutes=15)).isoformat(), "max_age_seconds": 900}}


def checked_remote_identities(shop: str, client: UpgatesClient, *, refresh: bool = False) -> tuple[set[str], set[str], dict]:
    """Always contact Upgates; reuse a complete index and replace changed parents."""
    with _shop_cache(shop, "identities") as (path, saved, fingerprint):
        started = now()
        full_at = None
        if saved and isinstance(saved.get("products"), dict):
            try:
                full_at = datetime.fromisoformat(saved["full_checked_at"])
                if full_at.tzinfo is None or full_at > started:
                    full_at = None
            except (KeyError, ValueError, TypeError):
                pass
        full = refresh or full_at is None or started - full_at >= timedelta(hours=24)
        products = {} if full else dict(saved["products"])
        params = {} if full else {"last_update_time_from": (datetime.fromisoformat(saved["checked_at"]) - timedelta(minutes=5)).isoformat(timespec="seconds")}
        rows = _pages(client, "products/simple", "products", params)
        for product in rows:
            if not isinstance(product, dict) or (product.get("product_id") is None and not product.get("code")):
                raise CatalogError("upgates_read_failed", "The product identity response is incomplete", 502)
            # Product IDs survive code changes; replace the whole parent to remove old variant identities.
            key = str(product["product_id"]) if product.get("product_id") is not None else "code:" + str(product["code"])
            identities = []
            variants = product.get("variants") or []
            if not isinstance(variants, list):
                raise CatalogError("upgates_read_failed", "The variant identity response is incomplete", 502)
            for item in [product, *variants]:
                if not isinstance(item, dict):
                    raise CatalogError("upgates_read_failed", "A product or variant identity is invalid", 502)
                identities.append({"code": str(item.get("code") or "").casefold(),
                                   "display_code": str(item.get("code") or ""),
                                   "eans": re.split(r"[;,|/\s]+", str(item.get("ean") or "").strip())})
            products[key] = identities
        # Save only after every page succeeds; failures never become a successful check.
        state = {"version": 1, "fingerprint": fingerprint, "checked_at": started.isoformat(),
                 "full_checked_at": started.isoformat() if full else saved["full_checked_at"], "products": products}
        _write(path, state)
        codes = {item["code"] for entries in products.values() for item in entries if item["code"]}
        eans = {ean for entries in products.values() for item in entries for ean in item["eans"] if ean}
        return codes, eans, {"checked_at": state["checked_at"], "full_checked_at": state["full_checked_at"],
                             "mode": "full" if full else "changes"}


def _money(value: Decimal) -> float:
    # Round with Decimal; JSON has no Decimal type. The float is only the wire value.
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _prices(product: CatalogProduct, options: ShopImportOptions, cfg: dict, with_vat: bool, sale_gross: Decimal | None = None) -> list[dict]:
    source = product.prices
    if source.currency != options.currency:
        raise ValueError("currency_mismatch")
    if source.vat_percent is None:
        raise ValueError("missing_vat")
    for basis in ("retail", "purchase"):
        net, gross = getattr(source, basis + "_net"), getattr(source, basis + "_gross")
        if net is not None and gross is not None and abs(gross - net * (1 + source.vat_percent / 100)) > Decimal("0.02"):
            raise ValueError("invalid_price_vat")
    retail = source.retail_gross if with_vat else source.retail_net
    if sale_gross is None and (retail is None or retail <= 0):
        raise ValueError("missing_price")
    coefficients = {**DEFAULT_COEFFS, **{str(k).upper(): v for k, v in (cfg.get("adapter_settings", {}).get("price_coefficients") or {}).items()}}
    coefficient = Decimal(str(coefficients.get((product.brand or "").upper(), 1))) if options.pricing == "configured" else Decimal(1)
    if not coefficient.is_finite() or coefficient <= 0:
        raise ValueError("invalid_price_coefficient")
    if sale_gross is not None:
        if not sale_gross.is_finite() or sale_gross <= 0:
            raise ValueError("invalid_sale_price")
        sale = sale_gross if with_vat else sale_gross / (1 + source.vat_percent / 100)
    else:
        sale = retail * coefficient
    price = {"language": options.language, "pricelists": [{"name": options.pricelist, "price_original": _money(sale)}]}
    if retail is not None and retail > 0:
        price["price_common"] = _money(retail)
    purchase = source.purchase_gross if with_vat else source.purchase_net
    if purchase is not None:
        price["price_purchase"] = _money(purchase)
    return [price]


def _parameters(parameters, language: str) -> list[dict]:
    grouped = OrderedDict()
    for p in parameters:
        grouped.setdefault(p.name, []).append(p.value)
    return [{"descriptions": [{"language": language, "name": name}],
             "values": [{"descriptions": [{"language": language, "value": value}]} for value in dict.fromkeys(values)]}
            for name, values in grouped.items()]


def _base(product: CatalogProduct, options: ShopImportOptions, cfg: dict, with_vat: bool, sale_gross: Decimal | None = None) -> dict:
    result = {"code": product.shop_code, "code_supplier": product.code,
              "active_yn": False, "metas": [{"key": "validation_required", "value": "1"}],
              "prices": _prices(product, options, cfg, with_vat, sale_gross)}
    if product.eans:
        result["ean"] = product.eans[0]
    return result


def build_item(products: list[CatalogProduct], options: ShopImportOptions, cfg: dict, with_vat: bool,
               sale_price_overrides: dict[int, Decimal] | None = None) -> ImportItem:
    sale_price_overrides = sale_price_overrides or {}
    products = sorted(products, key=variant_sort_key)
    first = products[0]
    grouped = bool(first.group_code and first.variant_relationship == "explicit")
    # A Hub parent identifier is derived only from the supplier's explicit group ID.
    parent_code = parent_shop_code(first)
    item = ImportItem(code=parent_code, name=(first.group_name or first.name) if grouped else first.name,
                      product_ids=[p.id for p in products], variants_count=len(products) if grouped else 0,
                      status="ready", warnings=sorted({w for p in products for w in p.warnings}))
    try:
        blockers = sorted({error for p in products for error in p.import_blockers})
        if blockers:
            item.status, item.errors = "invalid", blockers
            return item
        if len(parent_code) > 100 or not parent_code or any(len(p.shop_code) > 100 for p in products):
            raise ValueError("invalid_shop_code")
        payload = _base(first, options, cfg, with_vat, sale_price_overrides.get(first.id))
        payload["code"] = parent_code
        payload["descriptions"] = [{"language": options.language, "active_yn": False, "title": item.name}]
        if options.include_description:
            payload["descriptions"][0]["long_description"] = clean_description("\n".join(filter(None, [first.description, first.safety_information])))
        payload["vats"] = {options.language: float(first.prices.vat_percent)}
        if first.brand:
            payload["manufacturer"] = first.brand
        if options.category_code:
            payload["categories"] = [{"code": options.category_code, "main_yn": True}]
        if options.include_images:
            images = list(dict.fromkeys(url for p in products for url in p.images))
            payload["images"] = [{"url": url, "main_yn": i == 0, "list_yn": i == 0, "position": i} for i, url in enumerate(images)]
        if options.include_parameters and not grouped:
            payload["parameters"] = _parameters(first.parameters, options.language)
        if grouped:
            if len(products) > 100:
                raise ValueError("too_many_variants")
            if parent_code in {p.shop_code for p in products}:
                raise ValueError("parent_code_conflict")
            signatures = [tuple(sorted((a.name, a.value) for a in p.variant_attributes)) for p in products]
            if any(not signature for signature in signatures) or len(set(signatures)) != len(signatures):
                raise ValueError("variant_attributes_ambiguous")
            if len({tuple(a.name for a in sorted(p.variant_attributes, key=lambda a: a.name)) for p in products}) != 1:
                raise ValueError("variant_attributes_ambiguous")
            if len({(p.brand, p.prices.vat_percent, p.prices.currency) for p in products}) != 1:
                raise ValueError("variant_group_inconsistent")
            if options.include_parameters:
                names = {a.name.casefold() for p in products for a in p.variant_attributes}
                common = set.intersection(*({(a.name, a.value) for a in p.parameters} for p in products))
                parameters = [a for a in first.parameters if (a.name, a.value) in common and a.name.casefold() not in names]
                if parameters:
                    payload["parameters"] = _parameters(parameters, options.language)
            payload.pop("ean", None)
            payload.pop("code_supplier", None)
            payload["variants"] = []
            for index, product in enumerate(products):
                variant = _base(product, options, cfg, with_vat, sale_price_overrides.get(product.id))
                variant.update(main_yn=index == 0, active_yn=True,
                               parameters=_parameters(product.variant_attributes, options.language))
                if options.include_images and product.images:
                    variant["image"] = {"url": product.images[0]}
                payload["variants"].append(variant)
        if any(len(p.eans) > 1 for p in products):
            item.warnings.append("primary_ean_exported")
        if any(p.id in sale_price_overrides for p in products):
            item.warnings.append("manual_sale_price")
        for product in products:
            prices = _prices(product, options, cfg, with_vat, sale_price_overrides.get(product.id))[0]
            if prices.get("price_purchase") is not None and prices["pricelists"][0]["price_original"] < prices["price_purchase"]:
                item.warnings.append("sale_below_purchase")
                break
        item.payload = payload
    except (ValueError, InvalidOperation) as error:
        item.status, item.errors = "invalid", [str(error) if isinstance(error, ValueError) else "invalid_price_coefficient"]
    return item


def _duplicate(item: dict, sources: dict[int, CatalogProduct], codes: set[str], eans: set[str]) -> bool:
    return (item["code"].casefold() in codes or
            any(sources[id].shop_code.casefold() in codes or bool(set(sources[id].eans) & eans) for id in item["product_ids"]))


async def create_preview(db: AsyncSession, shop: str, request: ShopImportPreviewRequest, *, enrichment: dict | None = None) -> ShopImportPreview:
    cfg = shop_config(shop)
    if not await db.scalar(select(Shop.id).where(Shop.code == shop, Shop.is_active.is_(True))):
        raise CatalogError("shop_not_found", "Choose an active Hub shop", 404)
    products = await selected_products(db, request.supplier, request.feed_key, request.product_ids, request.run_id)
    if set(request.sale_price_overrides) - {p.id for p in products}:
        raise CatalogError("price_override_not_selected", "Price overrides must refer to selected products", 422)
    supplier_cfg = supplier_config(request.supplier)
    client = UpgatesClient.from_shop(shop)
    remote = await asyncio.to_thread(cached_import_options, shop, client)
    options = request.options.model_copy()
    errors = []
    language = next((l for l in remote["languages"] if l["code"] == options.language), None)
    if not language or language["currency"] != options.currency:
        errors.append("target_language_currency_mismatch")
    if options.pricelist not in {p["name"] for p in remote["pricelists"]}:
        errors.append("pricelist_not_found")
    if options.category_code and options.category_code not in {c["code"] for c in remote["categories"]}:
        errors.append("category_not_found")
    groups = OrderedDict()
    for product in products:
        key = "g:" + product.group_code if product.group_code and product.variant_relationship == "explicit" else "p:" + str(product.id)
        groups.setdefault(key, []).append(product)
    items = [build_item(group, options, supplier_cfg, remote["prices_with_vat"], request.sale_price_overrides) for group in groups.values()]
    if enrichment:
        from inventory_hub.services.ai_content_validation import overlay
        if enrichment.get("content"):
            from inventory_hub.services.ai_content_upgates import content_fields
            enrichment = {**enrichment, "meta_common": await asyncio.to_thread(content_fields, shop, client)}
        if len(items) != 1:
            raise CatalogError("ai_family_mismatch", "A content revision applies to exactly one selected family", 422)
        items = [overlay(item, enrichment, options.language) if item.status == "ready" else item for item in items]
    policy = availability_policy(request.supplier, supplier_cfg)
    for item in items:
        if item.status == "ready":
            item.payload["categories"] = category_chain(remote["categories"], options.category_code)
            apply_availability(item.payload, [p for p in products if p.id in item.product_ids], policy)
    price_lines = []
    for product in products:
        try:
            sale = Decimal(str(_prices(product, options, supplier_cfg, True, request.sale_price_overrides.get(product.id))[0]["pricelists"][0]["price_original"]))
        except (ValueError, InvalidOperation):
            sale = None
        price_lines.append(ImportPriceLine(product_id=product.id, code=product.shop_code, name=product.name,
            image=product.images[0] if product.images else None, attributes=product.variant_attributes,
            retail_gross=product.prices.retail_gross, purchase_net=product.prices.purchase_net,
            sale_gross=sale, overridden=product.id in request.sale_price_overrides,
            blocked=bool(product.import_blockers), warnings=product.warnings))
    codes, eans, shop_check = await asyncio.to_thread(checked_remote_identities, shop, client, refresh=request.refresh_shop)
    index = await asyncio.to_thread(cached_identities, shop)
    sources = {p.id: p for p in products}
    lines = {line.product_id: line for line in price_lines}
    selection_eans = Counter(ean for p in products for ean in p.eans)
    selection_codes = Counter(p.shop_code.casefold() for p in products)
    parent_codes = Counter(item.code.casefold() for item in items)
    for item in items:
        item.parent_exists = bool(item.variants_count and item.code.casefold() in codes)
        item.existing_product_ids = [id for id in item.product_ids if item.parent_exists or
                                    sources[id].shop_code.casefold() in codes or bool(set(sources[id].eans) & eans)]
        for id in item.product_ids:
            lines[id].existing = id in item.existing_product_ids
            lines[id].shop_matches = index.matches(sources[id])
        if item.status == "invalid":
            continue
        if item.parent_exists or len(item.existing_product_ids) == len(item.product_ids):
            item.status = "exists"
            item.warnings.append("already_in_shop")
        elif item.existing_product_ids:
            item.status, item.errors = "invalid", ["some_variants_in_shop"]
        elif (parent_codes[item.code.casefold()] > 1 or
              (item.variants_count and item.code.casefold() in selection_codes) or
              any(selection_codes[sources[id].shop_code.casefold()] > 1 or any(selection_eans[e] > 1 for e in sources[id].eans) for id in item.product_ids)):
            item.status, item.errors = "invalid", ["selection_identity_conflict"]
    local, conflicts = await local_identities(db, products)
    for item in items:
        if any(id in conflicts for id in item.product_ids):
            item.status, item.errors = "invalid", ["local_identity_conflict"]
    created = now()
    preview = ShopImportPreview(preview_id=uuid4().hex, shop=shop, supplier=request.supplier,
                                created_at=created, expires_at=created + timedelta(hours=1),
                                options=options, prices_with_vat=remote["prices_with_vat"], items=items, errors=errors,
                                create_validation_field=remote["create_validation_field"], shop_check=shop_check,
                                price_lines=price_lines, sale_price_overrides=request.sale_price_overrides)
    document = {"preview": preview.model_dump(mode="json"), "sources": [p.model_dump(mode="json") for p in products],
                "target": _target(cfg), "prices_with_vat": remote["prices_with_vat"],
                "supplier_availability": policy, "result": None}
    if enrichment:
        document["content_approval"] = {k: enrichment[k] for k in ("job_id", "revision", "rules_version", "active_after_import")}
    _write(_path(shop, preview.preview_id), document)
    return preview


async def local_identities(db: AsyncSession, products: list[CatalogProduct]) -> tuple[dict[int, int], set[int]]:
    supplier_codes = {product.supplier for product in products}
    supplier_rows = (await db.execute(select(Supplier).where(Supplier.code.in_(supplier_codes)))).scalars()
    supplier_ids = {supplier.code: supplier.id for supplier in supplier_rows}
    identities = [RemoteIdentity(0, product.shop_code, barcodes=tuple(product.eans),
        supplier_id=supplier_ids.get(product.supplier), supplier_sku=product.code)
        for product in products]
    index = await load_identity_index(db, 0, identities, local=True)
    matches, conflicts = {}, set()
    for product, identity in zip(products, identities):
        resolution = index.resolve(identity)
        if resolution.status == "conflict":
            conflicts.add(product.id)
        elif resolution.product_id is not None:
            matches[product.id] = resolution.product_id
    # Two selected leaves must never acquire the same canonical product.
    duplicate_ids = {id for id, count in Counter(matches.values()).items() if count > 1}
    conflicts.update(id for id, product_id in matches.items() if product_id in duplicate_ids)
    return matches, conflicts


async def register_created(db: AsyncSession, shop: str, item: dict, sources: dict[int, CatalogProduct], remote: dict) -> None:
    # Only catalog identity/content is registered. No stock balance or movement is created.
    products = [sources[id] for id in item["product_ids"]]
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": IDENTITY_WRITE_LOCK})
    existing, conflicts = await local_identities(db, products)
    if conflicts:
        raise CatalogError("local_identity_conflict", "Local identifiers require manual reconciliation", 409)
    supplier_id = await db.scalar(select(Supplier.id).where(Supplier.code == products[0].supplier))
    shop_id = await db.scalar(select(Shop.id).where(Shop.code == shop))
    group_id = None
    if item["variants_count"]:
        group_id = await db.scalar(select(ProductGroup.id).where(ProductGroup.code == item["code"]))
        if group_id is None:
            group = ProductGroup(code=item["code"], name=item["name"], brand=products[0].brand,
                                 main_image_url=products[0].images[0] if products[0].images else None)
            db.add(group)
            await db.flush()
            group_id = group.id
    remote_variants = {v.get("code"): v for v in remote.get("variants", [])}
    for source in products:
        product_id = existing.get(source.id)
        if product_id is None:
            product = Product(sku=source.shop_code, supplier_id=supplier_id, name=source.name,
                              brand=source.brand, group_id=group_id, validation_required=True,
                              validation_reason="Supplier catalog import; review before publishing",
                              created_from_source="supplier_catalog", source_supplier_product_id=source.id,
                              supplier_feed_data=source.model_dump(mode="json"), supplier_feed_synced_at=source.fetched_at)
            db.add(product)
            await db.flush()
            product_id = product.id
            identifiers = ProductIdentifierService(db)
            for i, ean in enumerate(source.eans):
                await identifiers.add_identifier(product_id, ean, is_primary=i == 0)
        mapping = await db.scalar(select(ShopProduct).where(ShopProduct.shop_id == shop_id, ShopProduct.product_id == product_id))
        if mapping is not None and (mapping.variant_code or mapping.external_code) not in (None, source.shop_code):
            raise CatalogError("local_mapping_conflict", "The local product is already linked to another shop code", 409)
        if mapping is None:
            mapping = ShopProduct(shop_id=shop_id, product_id=product_id)
            db.add(mapping)
        variant = bool(item["variants_count"])
        external = remote_variants.get(source.shop_code, {}) if variant else remote
        mapping.external_id = str(external.get("variant_id") if variant else external.get("product_id"))
        mapping.external_code = source.shop_code
        mapping.variant_code = source.shop_code if variant else None
        mapping.parent_code = item["code"] if variant else None
        mapping.is_variant, mapping.is_listed = variant, True
        mapping.last_push_at, mapping.last_push_status = now(), "catalog_created"
        await db.execute(insert(ProductSupplySource).values(product_id=product_id, supplier_product_id=source.id)
                         .on_conflict_do_nothing(constraint="uq_supply_sources"))
    content = insert(ShopProductContent).values(shop_id=shop_id, external_code=item["code"], data=remote, pulled_at=now())
    await db.execute(content.on_conflict_do_update(index_elements=[ShopProductContent.shop_id, ShopProductContent.external_code],
                                                 set_={"data": remote, "pulled_at": now()}))
    await db.flush()


def _verify_created(client: UpgatesClient, item: dict) -> dict | None:
    rows = _get(client, "products", {"codes": item["code"], "current_page_items": 100}).get("products")
    if not isinstance(rows, list):
        raise CatalogError("upgates_read_failed", "Could not verify the imported product", 502)
    remote = next((p for p in rows if p.get("code") == item["code"]), None)
    if remote is None:
        return None
    def validation_value(value: dict):
        meta = next((m for m in value.get("metas", []) if m.get("key") == "validation_required"), None)
        return None if meta is None else str(meta.get("value", "")).lower() in ("1", "true")
    if remote.get("active_yn") is not item["payload"].get("active_yn", False) or validation_value(remote) != validation_value(item["payload"]):
        raise CatalogError("import_readback_mismatch", "Created product visibility or validation flag requires review", 409)
    if item["payload"].get("ean") and remote.get("ean") != item["payload"]["ean"]:
        raise CatalogError("import_readback_mismatch", "The created product's EAN differs from the selected item", 409)
    variants = {v.get("code"): v for v in remote.get("variants", [])}
    if any(v["code"] not in variants or validation_value(variants[v["code"]]) != validation_value(v) for v in item["payload"].get("variants", [])):
        raise CatalogError("import_readback_mismatch", "Some selected variants or their validation flags were not confirmed", 409)
    if any(v.get("ean") and variants[v["code"]].get("ean") != v["ean"] for v in item["payload"].get("variants", [])):
        raise CatalogError("import_readback_mismatch", "A created variant's EAN differs from the selection", 409)
    if any("short_description" in d for d in item["payload"].get("descriptions", [])):
        from inventory_hub.services.ai_content_upgates import verify_content
        verify_content(remote, item["payload"])
    return remote


def _confirmed_response(response: dict, item: dict) -> bool:
    if not isinstance(response, dict):
        return False
    row = next((p for p in response.get("products", []) if p.get("code") == item["code"]), {})
    if not row.get("inserted_yn"):
        return False
    variants = {v.get("code"): v for v in row.get("variants", [])}
    return all(variants.get(v["code"], {}).get("inserted_yn") for v in item["payload"].get("variants", []))


def _assert_payload(payload: dict, *, expected_active: bool = False) -> None:
    forbidden = {"stock", "stocks", "stock_increment", "stock_position", "variants_stock"}
    def visit(value):
        if isinstance(value, dict):
            if forbidden.intersection(value):
                raise CatalogError("unsafe_import_payload", "Catalog import cannot contain stock fields", 422)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(payload)
    if not isinstance(payload.get("active_yn"), bool) or (payload["active_yn"] and not expected_active):
        raise CatalogError("unsafe_import_payload", "Product visibility differs from the approved import policy", 422)


def queue_import(shop: str, preview_id: str, retry_failed: bool = False) -> tuple[dict, bool]:
    path = _path(shop, preview_id)
    current = _read_result(path)
    if current and current["status"] in ("queued", "running"):
        current = import_result(shop, preview_id)
        if current["status"] in ("queued", "running"):
            return current, False
    with _shop_lock(shop):
        document = _load_job(path)
        preview, result = document["preview"], document["result"]
        if result and result["status"] in ("queued", "running") and (now() - datetime.fromisoformat(result["updated_at"])).total_seconds() < 30:
            return result, False
        if result and result["status"] == "completed" and not retry_failed:
            return result, False
        if result and result["status"] == "failed" and not retry_failed:
            return result, False
        if preview["errors"]:
            raise CatalogError("preview_invalid", "Resolve the preview errors first", 422)
        if document["target"] != _target(shop_config(shop)):
            raise CatalogError("shop_target_changed", "The shop connection changed; create a new preview", 409)
        if not (result and any(item["status"] == "uncertain" for item in result["items"])):
            assert_supplier_availability(preview["supplier"], document.get("supplier_availability"))
        # Expired previews may only reconcile previously sent items; they never create more products.
        expired = now() > datetime.fromisoformat(preview["expires_at"])
        if expired and not (result and any(item["status"] == "uncertain" for item in result["items"])):
            raise CatalogError("preview_expired", "Create a fresh import preview", 409)
        if result is None:
            result = {"preview_id": preview_id, "shop": shop, "options": preview["options"],
                      "prices_with_vat": document["prices_with_vat"], "status": "queued", "items": preview["items"], "errors": [], "updated_at": now().isoformat()}
        else:
            result.update(status="queued", errors=[], updated_at=now().isoformat())
        document["result"] = result
        _save_job(path, document)
        return result, True


def import_result(shop: str, preview_id: str) -> dict:
    path = _path(shop, preview_id)
    result = _read_result(path)
    if result is None:
        raise CatalogError("import_not_started", "This preview has not been confirmed", 409)
    # A process restart releases the OS lock. Expose recovery instead of polling forever.
    if result["status"] in ("queued", "running") and (now() - datetime.fromisoformat(result["updated_at"])).total_seconds() > 30:
        try:
            with _shop_lock(shop):
                result = _read_result(path)
                if result["status"] in ("queued", "running"):
                    result.update(status="failed", errors=["import_interrupted"], updated_at=now().isoformat())
                    _write(_result_path(path), result)
        except CatalogError as error:
            if error.code != "shop_import_running":
                raise
    return result


async def execute_import(shop: str, preview_id: str) -> None:
    path = _path(shop, preview_id)
    try:
        with _shop_lock(shop):
            document = _load_job(path)
            result = document["result"]
            if result is None or result["status"] != "queued":
                return
            result.update(status="running", updated_at=now().isoformat())
            _save_job(path, document)
            try:
                await _execute_items(shop, path, document)
            except Exception as error:
                result.update(status="failed", errors=[error.code if isinstance(error, CatalogError) else "import_failed"])
            result["updated_at"] = now().isoformat()
            _save_job(path, document)
    except CatalogError as error:
        if error.code != "shop_import_running":
            raise
        # This queued job did not send anything. Explicit retry is available after the other job.
        document = _load_job(path)
        document["result"].update(status="failed", errors=[error.code], updated_at=now().isoformat())
        _save_job(path, document)


async def _execute_items(shop: str, path: Path, document: dict) -> None:
    preview, result = document["preview"], document["result"]
    if document["target"] != _target(shop_config(shop)):
        raise CatalogError("shop_target_changed", "The shop connection changed", 409)
    policy_current = True
    try:
        assert_supplier_availability(preview["supplier"], document.get("supplier_availability"))
    except CatalogError:
        # Old/changed previews may still reconcile an uncertain POST, but must
        # never create more products or auxiliary metadata with stale settings.
        if not any(item["status"] == "uncertain" for item in result["items"]):
            raise
        policy_current = False
    sources = {p["id"]: CatalogProduct.model_validate(p) for p in document["sources"]}
    client = UpgatesClient.from_shop(shop)
    remote_options = await asyncio.to_thread(cached_import_options, shop, client, refresh=True)
    options = preview["options"]
    if (document["prices_with_vat"] != remote_options["prices_with_vat"] or
        not any(l["code"] == options["language"] and l["currency"] == options["currency"] for l in remote_options["languages"]) or
        not any(p["name"] == options["pricelist"] for p in remote_options["pricelists"]) or
        (options["category_code"] and not any(c["code"] == options["category_code"] for c in remote_options["categories"]))):
        raise CatalogError("shop_options_changed", "Shop settings changed; create a new preview", 409)
    if policy_current and remote_options["create_validation_field"]:
        if not preview["create_validation_field"]:
            raise CatalogError("shop_options_changed", "The validation field changed; create a new preview", 409)
        try:
            await asyncio.to_thread(client.post, "metas", {"metas": [{"key": "validation_required", "category": "products",
                "type": "checkbox", "label": "Vyžaduje kontrolu", "active": True, "common_languages_value_yn": True}]})
        except UpgatesError:
            raise CatalogError("validation_field_create_failed", "Could not confirm the validation field; no product was sent", 502) from None
        verified = await asyncio.to_thread(cached_import_options, shop, client, refresh=True)
        if verified["create_validation_field"]:
            raise CatalogError("validation_field_create_failed", "The validation field was not created", 502)
    if policy_current and document.get("content_approval") and any("short_description" in d for item in preview["items"] for d in item["payload"].get("descriptions", [])):
        from inventory_hub.services.ai_content_upgates import content_fields
        await asyncio.to_thread(content_fields, shop, client, create=True)
    codes, eans, shop_check = await asyncio.to_thread(checked_remote_identities, shop, client)
    result["shop_check"] = shop_check
    _save_job(path, document)
    for item in result["items"]:
        if item["status"] not in ("ready", "failed", "uncertain"):
            continue
        uncertain = item["status"] == "uncertain"
        try:
            if uncertain:
                remote = await asyncio.to_thread(_verify_created, client, item)
                if remote is None:
                    # Absence after a timeout is not proof that a delayed request cannot finish.
                    item["errors"] = ["import_outcome_unknown"]
                    continue
            else:
                assert_supplier_availability(preview["supplier"], document.get("supplier_availability"))
                if now() > datetime.fromisoformat(preview["expires_at"]):
                    item.update(status="failed", errors=["preview_expired"])
                    continue
                async with get_session_context() as db:
                    fresh = await selected_products(db, preview["supplier"], sources[item["product_ids"][0]].feed_key, item["product_ids"])
                    ignored = {"run_id", "fetched_at", "listed"}
                    if any(p.model_dump(exclude=ignored) != sources[p.id].model_dump(exclude=ignored) for p in fresh):
                        item.update(status="failed", errors=["catalog_changed"])
                        continue
                    _, conflicts = await local_identities(db, fresh)
                    if conflicts:
                        item.update(status="failed", errors=["local_identity_conflict"])
                        continue
                if _duplicate(item, sources, codes, eans):
                    parent_exists = bool(item["variants_count"] and item["code"].casefold() in codes)
                    existing = [id for id in item["product_ids"] if parent_exists or sources[id].shop_code.casefold() in codes
                                or bool(set(sources[id].eans) & eans)]
                    partial = len(existing) < len(item["product_ids"])
                    item.update(status="invalid" if partial else "exists", existing_product_ids=existing, parent_exists=parent_exists,
                                errors=["some_variants_in_shop"] if partial else [],
                                warnings=list(dict.fromkeys([*item["warnings"], *([] if partial else ["already_in_shop"])])))
                    continue
                # Check codes immediately before each create as well as the complete EAN index.
                check_codes = [item["code"], *(sources[id].shop_code for id in item["product_ids"])]
                for key in ("codes", "variant_codes"):
                    found = await asyncio.to_thread(_pages, client, "products/simple", "products", {key: ";".join(dict.fromkeys(check_codes))})
                    if found:
                        item.update(status="exists", errors=[], warnings=[*item["warnings"], "already_in_shop"])
                        break
                if item["status"] == "exists":
                    continue
                _assert_payload(item["payload"], expected_active=bool(document.get("content_approval", {}).get("active_after_import", False)))
                assert_supplier_availability(preview["supplier"], document.get("supplier_availability"))
                item.update(status="uncertain", errors=["import_outcome_unknown"])
                result["updated_at"] = now().isoformat()
                _item_checkpoint(path, result, item)
                rejected = None
                try:
                    response = await asyncio.to_thread(client.post, "products", {"products": [item["payload"]]})
                except UpgatesError as error:
                    # Do not retry a POST that may have reached the shop.
                    response = {}
                    if error.status_code in (400, 401, 403, 422, 429):
                        rejected = "upgates_rejected_http_" + str(error.status_code)
                remote = await asyncio.to_thread(_verify_created, client, item)
                if remote is None:
                    row = next((p for p in response.get("products", []) if p.get("code") == item["code"]), {})
                    if rejected or (row.get("inserted_yn") is False and any(m.get("level") == "error" for m in row.get("messages", []))):
                        item.update(status="failed", errors=[rejected or "upgates_product_rejected"])
                    elif not _confirmed_response(response, item):
                        item["errors"] = ["import_outcome_unknown"]
                    continue
                if not _confirmed_response(response, item):
                    item["warnings"] = list(dict.fromkeys([*item["warnings"], "import_verified_by_readback"]))
            async with get_session_context() as db:
                await register_created(db, shop, item, sources, remote)
            item.update(status="created", errors=[])
            codes.update([item["code"].casefold(), *(sources[id].shop_code.casefold() for id in item["product_ids"])])
            eans.update(ean for id in item["product_ids"] for ean in sources[id].eans)
        except Exception as error:
            item["status"] = "uncertain" if item["status"] == "uncertain" or uncertain else "failed"
            item["errors"] = [error.code if isinstance(error, CatalogError) else "import_failed"]
            # Avoid repeated failed authentication/API calls for the rest of a large batch.
            if isinstance(error, CatalogError) and error.code == "upgates_read_failed":
                raise
        finally:
            result["updated_at"] = now().isoformat()
            _item_checkpoint(path, result, item)
    result["status"] = "completed"
