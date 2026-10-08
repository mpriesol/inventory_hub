"""Database-owned feed mappings, source inspection and deterministic normalization."""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
from pathlib import Path
from uuid import uuid4

from lxml import etree
from sqlalchemy import select, text

from inventory_hub import config_io
from inventory_hub.catalog_types import CatalogProduct, CatalogPrices, CatalogParameter
from inventory_hub.db_models import Shop, Supplier, SupplierFeed, SupplierFeedItemRaw
from inventory_hub.feed_mapping_models import ProductFeedMapping, ProductFeedMappingRevision
from inventory_hub.feed_mapping_types import MappingDefinition, MappingPreview, MappingSave
from inventory_hub.services.feed_mapping_source import MAX_BYTES, detect_format, inspect_records, primitive, read_records, values_at, xml_fields
from inventory_hub.supplier_prefix import canonical_supplier_sku, get_supplier_prefix


TEXT_FIELDS = {"code", "name", "brand", "manufacturer_code", "description", "short_description", "seo_title",
               "seo_description", "seo_url", "manufacturer_description", "safety_information", "category", "category_code",
               "target_category_code", "url", "availability", "supplier_stock_raw", "supplier_stock_external_raw",
               "delivery_date", "group_code", "group_name"}
PRICE_FIELDS = {"prices.purchase_net", "prices.purchase_gross", "prices.retail_net", "prices.retail_gross",
                "prices.discount_net", "prices.discount_gross", "prices.vat_percent"}
NUMERIC_FIELDS = {"supplier_stock", "supplier_stock_min", "supplier_stock_external"}
LIST_FIELDS = {"images", "eans", "parameters", "variant_attributes"}
FIELDS = TEXT_FIELDS | PRICE_FIELDS | NUMERIC_FIELDS | LIST_FIELDS | {"prices.currency", "supplier_external_available"}
SHOP_FORBIDDEN = {"code", "group_code", "group_name", "eans", "supplier_stock", "supplier_stock_min", "supplier_stock_external",
                  "supplier_stock_raw", "supplier_stock_external_raw", "supplier_external_available", "availability"}


def field_catalog():
    return [{"key": key, "label": key, "type": "number" if key in PRICE_FIELDS | NUMERIC_FIELDS else "list" if key in LIST_FIELDS else "text"}
            for key in sorted(FIELDS)]


def _error(code, message, status=422):
    from inventory_hub.services.catalog import CatalogError
    return CatalogError(code, message, status)


def _listing_scope(cfg, feed_key):
    source = (cfg.get("feeds", {}).get("sources") or {}).get(feed_key) or {}
    if feed_key == "stock" or source.get("type") == "stock":
        raise _error("listing_feed_not_configured", "Product mappings require a listing feed, not a stock feed")


def validate_definition(definition: MappingDefinition, shop: str = ""):
    if len(json.dumps(definition.model_dump(by_alias=True), ensure_ascii=False).encode()) > 1024 * 1024:
        raise ValueError("Mapping exceeds the 1 MB limit")
    if not shop and definition.category_rules:
        raise ValueError("Choose the target shop before configuring category mappings")
    for binding in definition.bindings:
        key = binding.target
        if key not in FIELDS and not key.startswith(("parameter:", "meta:")):
            raise ValueError(f"Unsupported destination field: {key}")
        if key.startswith(("parameter:", "meta:")):
            name = key.split(":", 1)[1].strip()
            if not name or len(name) > 100 or any(ord(c) < 32 for c in name):
                raise ValueError("Parameter/custom-field names must contain 1–100 printable characters")
            if key.startswith("meta:") and (name.casefold() == "validation_required" or not re.fullmatch(r"[A-Za-z0-9_-]+", name)):
                raise ValueError("This custom field is reserved or its key is invalid")
        if shop and key in SHOP_FORBIDDEN:
            raise ValueError(f"Map shared supplier identity and observations in the base feed mapping: {key}")
        if not shop and key == "target_category_code":
            raise ValueError("A target category belongs to a shop mapping")
        for source in (binding.source, binding.param_name_path, binding.param_value_path):
            if source:
                for part in source.split("|"):
                    values_at({}, part)
        if key in ("parameters", "variant_attributes") and not (binding.param_name_path and binding.param_value_path):
            if binding.constant is None:
                raise ValueError("Repeated parameters need name and value paths")
        for transform in binding.transforms:
            if transform.op in ("multiply", "round", "truncate"):
                if transform.value is None:
                    raise ValueError("The selected transformation needs a value")
                number = _decimal(transform.value)
                if transform.op in ("round", "truncate") and (number != number.to_integral_value() or number < 0 or number > (8 if transform.op == "round" else 100000)):
                    raise ValueError("Invalid round/truncate limit")


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("p", "br", "li", "tr", "h1", "h2", "h3", "h4", "div"):
            self.parts.append(" ")
    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1
        elif tag in ("p", "li", "tr", "div"):
            self.parts.append(" ")
    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value):
    parser = _Text()
    parser.feed(str(value))
    return " ".join(html.unescape("".join(parser.parts)).split())


def _decimal(value):
    if isinstance(value, bool):
        raise ValueError("A boolean is not a numeric feed value")
    try:
        result = Decimal(str(value).strip().replace("\u00a0", "").replace(" ", "").replace(",", "."))
        if not result.is_finite() or abs(result) > Decimal("999999999999"):
            raise ValueError("Numeric feed value is not finite or is out of range")
        return result
    except InvalidOperation:
        raise ValueError("Numeric feed value is malformed") from None


def _one(values, target):
    if not values:
        return None
    if len(values) != 1 or isinstance(values[0], (dict, list)):
        raise ValueError(f"{target}: several or nested source values; choose Join or a more precise path")
    return values[0]


def _binding_values(raw: dict, binding):
    source = binding.source or ""
    literal = values_at(raw, source) if source else []
    values = [binding.constant] if binding.constant is not None else literal or [value for path in source.split("|") if path for value in values_at(raw, path)]
    values = [primitive(value) for value in values if value is not None and value != ""]
    if not values and binding.default is not None:
        values = binding.default if isinstance(binding.default, list) else [binding.default]
    if len(values) == 1 and isinstance(values[0], list):
        values = values[0]
    for transform in binding.transforms:
        op, arg = transform.op, transform.value or ""
        if op == "join":
            if any(isinstance(value, (dict, list)) for value in values):
                raise ValueError("Join needs scalar source values")
            values = [arg.join(str(value) for value in values)] if values else []
        elif op == "split":
            values = [part.strip() for value in values for part in (str(value).split(arg) if arg else re.split(r"[;,|\s]+", str(value))) if part.strip()]
        else:
            converted = []
            for value in values:
                if isinstance(value, (dict, list)):
                    raise ValueError("This transformation needs scalar source values")
                if op == "trim": value = str(value).strip()
                elif op == "strip_html": value = plain_text(value)
                elif op == "replace": value = str(value).replace(arg, transform.with_ or "")
                elif op == "prefix": value = arg + str(value)
                elif op == "suffix": value = str(value) + arg
                elif op == "map": value = transform.values.get(str(value), value)
                elif op == "decimal": value = str(_decimal(value))
                elif op == "multiply": value = str(_decimal(value) * _decimal(arg))
                elif op == "round": value = str(_decimal(value).quantize(Decimal(1).scaleb(-int(arg)), rounding=ROUND_HALF_UP))
                elif op == "truncate": value = str(value)[:int(arg)]
                converted.append(value)
            values = converted
    return values


def apply_definition(product: CatalogProduct | None, raw: dict, definition: MappingDefinition, supplier: str,
                     feed_key: str, cfg: dict, *, revision: int = 0, shop: str = "") -> CatalogProduct:
    """Overlay only configured fields. Native parsing remains authoritative elsewhere."""
    validate_definition(definition, shop)
    if product is None:
        configured_vat = (cfg.get("adapter_settings") or {}).get("vat", cfg.get("vat_rate"))
        vat = _decimal(configured_vat) if configured_vat is not None else None
        data = {"supplier": supplier, "feed_key": feed_key, "code": "", "shop_code": "", "name": "",
                "prices": {"currency": str(cfg.get("default_currency") or "EUR").upper(), "vat_percent": str(vat) if vat is not None else None},
                "parameters": [], "metadata": {}, "mapping_provenance": {}}
    else:
        data = product.model_dump(mode="json")
    data["raw_fields"] = raw
    provenance = data.setdefault("mapping_provenance", {})
    provenance["shop_revision" if shop else "base_revision"] = revision
    fields = provenance.setdefault("fields", {})
    changed = set()
    for binding in definition.bindings:
        target = binding.target
        values = _binding_values(raw, binding)
        # A missing source is not an instruction to erase a working native value.
        if not values:
            if target in ("code", "name") and not data.get(target):
                raise ValueError(f"Required source is missing: {target}")
            continue
        if target in ("parameters", "variant_attributes"):
            if binding.constant is not None:
                params = [CatalogParameter.model_validate(value).model_dump() for value in values]
            else:
                params = []
                for value in values:
                    names = values_at(value, binding.param_name_path or "")
                    name = _one([primitive(n) for n in names], target)
                    if name in (None, ""):
                        continue
                    for item in values_at(value, binding.param_value_path or ""):
                        item = primitive(item)
                        if isinstance(item, (dict, list)):
                            raise ValueError("A parameter value must be scalar")
                        if item not in (None, ""):
                            params.append({"name": str(name), "value": str(item)})
            data[target] = list({(p["name"], p["value"]): p for p in params}.values())
        elif target.startswith("parameter:"):
            name = target.split(":", 1)[1].strip()
            parameters = [item for item in data.get("parameters", []) if item["name"] != name]
            parameters.extend({"name": name, "value": str(_one([value], target))} for value in values)
            data["parameters"] = parameters
        elif target.startswith("meta:"):
            data.setdefault("metadata", {})[target.split(":", 1)[1]] = str(_one(values, target))
        elif target in ("eans", "images"):
            result = []
            for value in values:
                scalar = _one([value], target)
                # Codes stay textual; leading zeroes are never reconstructed from a number.
                if target == "eans" and not isinstance(scalar, str):
                    raise ValueError("EAN must be text in the source; numeric encoding can lose leading zeroes")
                result.append(str(scalar).strip())
            data[target] = list(dict.fromkeys(result))
        else:
            value = _one(values, target)
            if target in PRICE_FIELDS | NUMERIC_FIELDS:
                value = _decimal(value)
                if value < 0:
                    raise ValueError(f"Negative values are not supported for {target}")
                value = str(value)
            elif target == "supplier_external_available":
                boolean = str(value).strip().casefold()
                if boolean not in ("true", "false", "1", "0", "yes", "no", "ano", "áno", "nie"):
                    raise ValueError("Supplier availability needs an explicit boolean value")
                value = boolean in ("true", "1", "yes", "ano", "áno")
            else:
                if target == "code" and not isinstance(value, str):
                    raise ValueError("Supplier SKU must be textual to preserve leading zeroes")
                value = str(value)
            if target.startswith("prices."):
                data["prices"][target.split(".", 1)[1]] = value
            else:
                data[target] = value
        changed.add(target)
        fields[target] = {"scope": shop or "feed", "revision": revision, "source": binding.source,
                          "constant": binding.constant is not None,
                          "transforms": [t.model_dump(by_alias=True, exclude_none=True) for t in binding.transforms]}
    code, name = data.get("code", "").strip(), data.get("name", "").strip()
    if not code or len(code) > 100 or not name or len(name) > 500:
        raise ValueError("Product needs a supplier SKU (1–100) and name (1–500 characters)")
    data["code"], data["name"] = code, name
    data["shop_code"] = canonical_supplier_sku(get_supplier_prefix(cfg), code)
    if not re.fullmatch(r"[A-Z]{3}", data["prices"]["currency"]):
        raise ValueError("Currency must be a three-letter code")
    vat_value = data["prices"].get("vat_percent")
    vat = _decimal(vat_value) if vat_value is not None else None
    if vat is not None and not 0 <= vat <= 100:
        raise ValueError("VAT must be between 0 and 100")
    factor = 1 + vat / 100 if vat is not None else None
    if factor is None and "missing_vat" not in data.get("warnings", []):
        data.setdefault("warnings", []).append("missing_vat")
    for basis in ("retail", "purchase", "discount"):
        if factor is None:
            break
        net, gross = basis + "_net", basis + "_gross"
        net_changed, gross_changed = "prices." + net in changed, "prices." + gross in changed
        if data["prices"].get(net) is not None and (net_changed and not gross_changed or data["prices"].get(gross) is None):
            data["prices"][gross] = str((_decimal(data["prices"][net]) * factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        elif data["prices"].get(gross) is not None and (gross_changed and not net_changed or data["prices"].get(net) is None):
            data["prices"][net] = str((_decimal(data["prices"][gross]) / factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        if data["prices"].get(net) is not None and data["prices"].get(gross) is not None and abs(_decimal(data["prices"][gross]) - _decimal(data["prices"][net]) * factor) > Decimal("0.02"):
            warnings = data.setdefault("warnings", [])
            if "invalid_price_vat" not in warnings:
                warnings.append("invalid_price_vat")
    if "prices.vat_percent" in changed:
        data["prices"]["vat_source"] = "feed"
    for rule in definition.category_rules:
        if rule.source in (data.get("category_code"), data.get("category")):
            data["target_category_code"] = rule.target_code
            provenance["category"] = {"scope": shop, "source": rule.source, "target_code": rule.target_code, "revision": revision}
            break
    if definition.seo_fallback:
        for key, value in (("seo_title", name[:80]), ("seo_description", plain_text(data.get("short_description") or data.get("description") or name)[:160])):
            if not data.get(key) or fields.get(key, {}).get("fallback") and key not in changed:
                data[key] = value
                fields[key] = {"scope": shop or "feed", "revision": revision, "fallback": True}
    if data.get("group_code"):
        data["variant_relationship"] = "explicit"
    for key in ("description", "short_description", "manufacturer_description", "safety_information"):
        if len(data.get(key) or "") > 1_000_000:
            raise ValueError(f"{key} exceeds the 1 MB text limit")
    from inventory_hub.adapters.paul_lange_catalog import http_url
    if any(not http_url(url) for url in data.get("images", [])) or data.get("url") and not http_url(data["url"]):
        raise ValueError("Images and source URL must use HTTP(S) without embedded credentials")
    if len(data.get("parameters", [])) > 500 or len(data.get("images", [])) > 100:
        raise ValueError("Product exceeds the parameter/image limit")
    data["mapping_revision"] = revision if not shop else data.get("mapping_revision", 0)
    return CatalogProduct.model_validate(data)


async def mapping_row(db, supplier: str, feed_key: str, shop: str = ""):
    return await db.scalar(select(ProductFeedMapping).join(Supplier, Supplier.id == ProductFeedMapping.supplier_id)
                           .where(Supplier.code == supplier, ProductFeedMapping.feed_key == feed_key,
                                  ProductFeedMapping.shop_code == shop))


async def get_mapping(db, supplier: str, feed_key: str = "products", shop: str = ""):
    from inventory_hub.services import catalog
    cfg = catalog.supplier_config(supplier)
    _listing_scope(cfg, feed_key)
    row = await mapping_row(db, supplier, feed_key, shop)
    base = await mapping_row(db, supplier, feed_key) if shop else row
    parser = (cfg.get("adapter_settings", {}).get("catalog") or {}).get("parser") or (supplier if supplier in catalog.PARSERS else None)
    return {"supplier": supplier, "feed_key": feed_key, "shop": shop, "revision": row.revision if row else 0,
            "definition": row.definition if row else MappingDefinition().model_dump(by_alias=True),
            "fields": field_catalog(), "native_parser": parser, "configured": row is not None,
            "base_revision": base.revision if base else 0,
            "inherited_definition": base.definition if shop and base else None}


async def save_mapping(db, supplier: str, request: MappingSave):
    from inventory_hub.services import catalog
    cfg = catalog.supplier_config(supplier)
    _listing_scope(cfg, request.feed_key)
    try:
        validate_definition(request.definition, request.shop)
    except ValueError as error:
        raise _error("feed_mapping_invalid", str(error)) from None
    if request.shop and not await db.scalar(select(Shop.id).where(Shop.code == request.shop, Shop.is_active.is_(True))):
        raise _error("shop_not_found", "Choose an active target shop", 404)
    lock = int.from_bytes(hashlib.sha256(f"feed-mapping:{supplier}:{request.feed_key}".encode()).digest()[:8], "big", signed=True)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
    # Share the feed lock with indexing: a preview never combines two revisions.
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": catalog._lock_key(supplier)})
    feed = await catalog._feed(db, supplier, cfg, request.feed_key, create=True)
    row = await mapping_row(db, supplier, request.feed_key, request.shop)
    if (row.revision if row else 0) != request.expected_revision:
        raise _error("feed_mapping_conflict", "The mapping changed; reload it before saving", 409)
    definition = request.definition.model_dump(mode="json", by_alias=True)
    if row and row.definition == definition:
        return await get_mapping(db, supplier, request.feed_key, request.shop)
    if row is None:
        row = ProductFeedMapping(supplier_id=feed.supplier_id, feed_key=request.feed_key, shop_code=request.shop,
                                 revision=1, definition=definition)
        db.add(row)
        await db.flush()
    else:
        row.revision += 1
        row.definition = definition
        row.updated_at = datetime.now(timezone.utc)
    db.add(ProductFeedMappingRevision(mapping_id=row.id, revision=row.revision, definition=definition))
    await db.flush()
    return await get_mapping(db, supplier, request.feed_key, request.shop)


def upload_path(supplier: str, feed_key: str, sample_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", sample_id) or not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", feed_key):
        raise _error("feed_sample_invalid", "Invalid sample identifier")
    return config_io.supplier_path(supplier).parent / "feeds" / "mapping" / feed_key / (sample_id + ".feed")


async def source_path(db, supplier, feed_key, sample_id=None):
    from inventory_hub.services import catalog
    cfg = catalog.supplier_config(supplier)
    _listing_scope(cfg, feed_key)
    if sample_id:
        path = upload_path(supplier, feed_key, sample_id)
    else:
        feed = await catalog._feed(db, supplier, cfg, feed_key)
        metadata = (feed.mapping_config or {}) if feed else {}
        relative = metadata.get("mapping_source_path") or metadata.get("xml_path")
        if relative:
            root = Path(config_io.DATA_ROOT).resolve()
            path = (root / relative).resolve()
            if not path.is_relative_to(root):
                raise _error("feed_source_invalid", "The saved source location is invalid")
        else:
            source = (cfg.get("feeds", {}).get("sources") or {}).get(feed_key, {})
            if source.get("mode") == "local" and source.get("local_path"):
                path = Path(source["local_path"])
                if not path.is_absolute():
                    path = config_io.supplier_path(supplier).parent / path
            else:
                raise _error("feed_sample_missing", "First download a feed in the catalog or upload a sample here", 404)
    if not path.is_file():
        raise _error("feed_sample_missing", "The saved feed sample is no longer available; upload it again", 404)
    return path


async def inspect_source(db, supplier, feed_key, definition, sample_id=None):
    path = await source_path(db, supplier, feed_key, sample_id)
    try:
        records, kind, record_path = await asyncio.to_thread(read_records, path, definition)
        result = await asyncio.to_thread(inspect_records, records, kind, record_path)
    except ValueError as error:
        raise _error("feed_inspection_invalid", str(error)) from None
    return {**result, "sample_id": sample_id}


async def upload_sample(db, supplier, feed_key, upload, definition):
    from inventory_hub.services import catalog
    _listing_scope(catalog.supplier_config(supplier), feed_key)
    sample_id = uuid4().hex
    destination = upload_path(supplier, feed_key, sample_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with destination.open("xb") as output:
            destination.chmod(0o600)
            while chunk := await upload.read(65536):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise _error("feed_too_large", "Feed exceeds the 100 MB limit", 413)
                output.write(chunk)
        return await inspect_source(db, supplier, feed_key, definition, sample_id)
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def parse_mapped(path, supplier, cfg, feed_key, definition, revision, native_parser=None):
    """Preserve native variant/price semantics; generic records need explicit SKU/name mappings."""
    with path.open("rb") as source:
        kind = detect_format(source.read(4096), definition.format)
    if native_parser and native_source_path(definition, native_parser) and kind == "xml":
        native = native_parser(path, supplier, cfg, feed_key)
        records = [(product, enrich_raw(raw)) for product, raw in native]
    else:
        raw_records, _, _ = read_records(path, definition)
        records = [(None, raw) for raw in raw_records]
    output, seen = [], set()
    for original, raw in records:
        fields = raw.get("fields", {})
        product = apply_definition(original, fields, definition, supplier, feed_key, cfg, revision=revision)
        if product.code in seen:
            raise ValueError(f"Duplicate mapped supplier SKU: {product.code}")
        seen.add(product.code)
        product.source_hash = original.source_hash if original else raw["source_hash"]
        output.append((product, raw))
    return output


def native_source_path(definition, parser):
    if not definition.record_path:
        return True
    if parser.__module__.endswith("paul_lange_catalog"):
        return definition.record_path.strip("/") in ("SHOP/SHOPITEM", "SHOPITEM")
    if parser.__module__.endswith("northfinder_catalog"):
        return definition.record_path.strip("/") in ("products/product", "root/product", "product")
    return True


def enrich_raw(raw):
    if not raw.get("xml"):
        return raw
    node = etree.fromstring(raw["xml"].encode(), etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
    return {**raw, "fields": xml_fields(node)}


async def preview_mapping(db, supplier, request: MappingPreview):
    from inventory_hub.services import catalog
    cfg = catalog.supplier_config(supplier)
    base = await mapping_row(db, supplier, request.feed_key)
    base_definition = MappingDefinition.model_validate(base.definition) if base else MappingDefinition()
    definition = base_definition if request.shop else request.definition
    path = await source_path(db, supplier, request.feed_key, request.sample_id)
    parser_name = ((cfg.get("feeds", {}).get("sources", {}).get(request.feed_key) or {}).get("catalog_parser") or (cfg.get("adapter_settings", {}).get("catalog") or {}).get("parser") or supplier)
    with path.open("rb") as source:
        kind = detect_format(source.read(4096), definition.format)
    native = catalog.PARSERS.get(parser_name) if kind == "xml" else None
    if native and not native_source_path(definition, native):
        native = None
    try:
        validate_definition(request.definition, request.shop)
        if native:
            records = [(product, enrich_raw(raw)) for product, raw in await asyncio.to_thread(native, path, supplier, cfg, request.feed_key)]
        else:
            raws, _, _ = await asyncio.to_thread(read_records, path, definition)
            records = [(None, raw) for raw in raws]
    except ValueError as error:
        raise _error("feed_preview_invalid", str(error)) from None
    items, errors = [], []
    for index, (original, raw) in enumerate(records[:request.limit]):
        try:
            product = apply_definition(original, raw["fields"], definition, supplier, request.feed_key, cfg,
                                       revision=base.revision if base else 0)
            if request.shop:
                product = apply_definition(product, raw["fields"], request.definition, supplier, request.feed_key, cfg, shop=request.shop)
            items.append(product.model_dump(mode="json", exclude={"raw_fields"}))
        except ValueError as error:
            errors.append({"row": index + 1, "message": str(error)})
    return {"items": items, "errors": errors, "mapping_revision": base.revision if base else 0}


async def apply_shop_mapping(db, product: CatalogProduct, shop: str) -> CatalogProduct:
    """Fresh import draft only; never mutates saved products, source rows or overrides."""
    from inventory_hub.services import catalog
    cfg = catalog.supplier_config(product.supplier)
    raw = product.raw_fields
    if not raw and product.id:
        query = select(SupplierFeedItemRaw.raw_data).where(SupplierFeedItemRaw.supplier_product_id == product.id)
        if product.run_id:
            query = query.where(SupplierFeedItemRaw.run_id == product.run_id)
        value = await db.scalar(query.order_by(SupplierFeedItemRaw.id.desc()).limit(1))
        raw = enrich_raw(value or {}).get("fields", {})
    base = await mapping_row(db, product.supplier, product.feed_key)
    target = await mapping_row(db, product.supplier, product.feed_key, shop) if shop else None
    try:
        # A new base revision requires an explicit source remap so canonical identity
        # and catalog search never disagree with the draft's identity.
        if base and product.mapping_revision != base.revision:
            raise _error("feed_mapping_stale", "The feed mapping changed; refresh or reapply it to the source before preparing a new import", 409)
        defaults = MappingDefinition(seo_fallback=base.definition.get("seo_fallback", True) if base else True)
        current = apply_definition(product, raw, defaults, product.supplier, product.feed_key, cfg,
                                   revision=base.revision if base else 0)
        if target:
            current = apply_definition(current, raw, MappingDefinition.model_validate(target.definition),
                                       product.supplier, product.feed_key, cfg, revision=target.revision, shop=shop)
        return current
    except ValueError as error:
        raise _error("feed_mapping_invalid", str(error)) from None
