"""Protected local merchandising editor; no feeds, shop writes, or ledger edits."""
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import unicodedata
from urllib.parse import urlsplit
from uuid import UUID
from types import SimpleNamespace

from pydantic import ValidationError
from sqlalchemy import case, delete, exists, func, literal, or_, select, text
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.exc import IntegrityError

from inventory_hub.db_models import Product, ProductGroup, ProductIdentifier, Shop, Supplier, SupplierProduct, Warehouse
from inventory_hub.db_models_ext import ProductSupplySource, ProductVariantAttribute, ShopProduct, ShopProductContent, StockBalance, StockMovement
from inventory_hub.product_editor_models import ProductEditorAudit, ProductEditorOverride, ProductEditorSave
from inventory_hub.product_editor_types import EditorRowPatch, ProductEditorSaveRequest
from inventory_hub.fifo_models import FifoLayer, FifoState
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK
from inventory_hub.services.upgates import variant_attributes
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.services.catalog import _http_url
from inventory_hub.services.supplier_availability import project as supplier_availability_projection


class EditorError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def now():
    return datetime.now(timezone.utc)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _decimal(value):
    return format(value, "f") if isinstance(value, Decimal) else None if value is None else str(value)


def _fold(value):
    return "".join(char for char in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(char))


def _sql_fold(value):
    return func.translate(func.lower(value), "áäčďéěíĺľňóôŕřšťúůýž", "aacdeeillnoorrstuuyz")


def _image(value):
    if not isinstance(value, str) or len(value) > 2000 or any(ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password else None
    except ValueError:
        return None


async def _warehouse(db, code, *, lock=False):
    if code is None:
        return None
    statement = select(Warehouse).where(Warehouse.code == code, Warehouse.is_active.is_(True))
    if lock:
        statement = statement.with_for_update(read=True)
    row = await db.scalar(statement.execution_options(populate_existing=True))
    if row is None:
        raise EditorError("product_editor_warehouse_missing", 404)
    return {"id": row.id, "code": row.code, "name": row.name}


async def options(db):
    shops = (await db.scalars(select(Shop).where(Shop.is_active.is_(True), Shop.platform == "upgates").order_by(Shop.code))).all()
    warehouses = (await db.scalars(select(Warehouse).where(Warehouse.is_active.is_(True)).order_by(Warehouse.code))).all()
    effective_brand = func.coalesce(ProductEditorOverride.data["common"]["brand"].astext, Product.brand)
    brands = (await db.scalars(select(effective_brand).select_from(Product).outerjoin(ProductEditorOverride,
        ProductEditorOverride.product_id == Product.id).where(effective_brand.is_not(None), effective_brand != "")
        .distinct().order_by(effective_brand))).all()
    return {"shops": [{"id": row.id, "code": row.code, "name": row.name} for row in shops],
            "warehouses": [{"id": row.id, "code": row.code, "name": row.name} for row in warehouses],
            "brands": brands, "page_sizes": [25, 50, 100], "currency": "EUR", "price_basis": "incl_vat",
            "external_write_enabled": False}


# Extract only selected fields in SQL. A POS parent can have thousands of
# variants; its complete JSON and sibling images never leave PostgreSQL here.
OBSERVED_SQL = """
SELECT sp.id,
 substring(coalesce(jsonb_path_query_first(leaf.value, '$.descriptions[*] ? (@.language == "sk").title') #>> '{}',
   jsonb_path_query_first(c.data, '$.descriptions[*] ? (@.language == "sk").title') #>> '{}'), 1, 500) AS name,
 CASE WHEN c.data->'active_yn' = 'false'::jsonb THEN false
      WHEN jsonb_typeof(coalesce(leaf.value->'active_yn', c.data->'active_yn')) = 'boolean'
      THEN (coalesce(leaf.value->'active_yn', c.data->'active_yn') #>> '{}')::boolean ELSE NULL END AS visible,
 substring(coalesce(jsonb_path_query_first(leaf.value, '$.images[*] ? (@.main_yn == true).url') #>> '{}',
   leaf.value #>> '{images,0,url}', jsonb_path_query_first(c.data, '$.images[*] ? (@.main_yn == true).url') #>> '{}',
   leaf.value #>> '{image,url}', CASE WHEN jsonb_typeof(leaf.value->'image') = 'string' THEN leaf.value->>'image' END, c.data #>> '{images,0,url}'), 1, 2001) AS image_url,
 coalesce(leaf.value->'parameters_new', leaf.value->'parameters', '[]'::jsonb) AS parameters,
 substring(coalesce(jsonb_path_query_first(leaf.value, '$.descriptions[*] ? (@.language == "sk").url') #>> '{}',
   jsonb_path_query_first(c.data, '$.descriptions[*] ? (@.language == "sk").url') #>> '{}',
   c.data #>> '{descriptions,0,url}', c.data->>'url'), 1, 2001) AS shop_url,
 substring(coalesce(leaf.value->>'admin_url', c.data->>'admin_url'), 1, 2001) AS shop_admin_url
FROM shop_products sp
JOIN shop_product_content c ON c.shop_id = sp.shop_id
 AND c.external_code = CASE WHEN sp.is_variant THEN sp.parent_code ELSE sp.external_code END
LEFT JOIN LATERAL (SELECT jsonb_path_query_first(c.data, '$.variants[*] ? (@.code == $code)',
 jsonb_build_object('code', sp.variant_code)) AS value) leaf ON sp.is_variant
WHERE sp.product_id = ANY(:ids)
"""


def _make_row(facts, data, revision, warehouse, stock):
    common = {"name": facts["name"], "brand": facts["brand"], "internal_note": None, "image_url": facts["image_url"], **data.get("common", {})}
    variant = {"sale_price_gross": None, "vat_rate": None, "note": None, "attributes": facts["attributes"], "eans": facts["eans"], "sku": facts["sku"], **data.get("variant", {})}
    warehouse_values = ({"code": warehouse["code"], "location": None, "min_quantity": facts.get("warehouse_min_quantity"),
                         **data.get("warehouses", {}).get(warehouse["code"], {})} if warehouse else None)
    shops = []
    for shop in facts["shops"]:
        overrides = {"name": None, "sale_price_gross": None, "visible": None,
                     **data.get("shops", {}).get(shop["shop_code"], {})}
        effective = {"name": overrides["name"] if overrides["name"] is not None else common["name"],
                     "sale_price_gross": overrides["sale_price_gross"] if overrides["sale_price_gross"] is not None else variant["sale_price_gross"],
                     "visible": overrides["visible"] if overrides["visible"] is not None else shop["observed"]["visible"]}
        desired = set()
        shop_overrides = data.get("shops", {}).get(shop["shop_code"], {})
        for key in ("name", "sale_price_gross", "visible"):
            if key in shop_overrides or key in data.get("common", {}) or key in data.get("variant", {}):
                desired.add(key)
        desired.update({"ean"} if "eans" in data.get("variant", {}) else set())
        desired.update({"attributes"} if "attributes" in data.get("variant", {}) else set())
        desired.update({"image_url"} if "image_url" in data.get("common", {}) else set())
        values = publication_values(common, variant, effective)
        receipts = data.get("published", {}).get(shop["shop_code"], {})
        published = sorted(key for key, receipt in receipts.items() if receipt.get("value") == values.get(key))
        shops.append({**shop, "overrides": overrides, "effective": effective, "published_fields": published,
                      "state": ("published" if desired <= set(published) else "saved_unpublished") if desired else "inherited"})
    return {"id": facts["id"], "sku": facts["sku"], "is_active": facts["is_active"], "group": facts["group"],
            "attributes": variant["attributes"], "eans": facts["eans"], "supplier_codes": facts["supplier_codes"],
            "image_url": common["image_url"], "revision": revision,
            "snapshot_hash": _digest({"warehouse_code": warehouse["code"] if warehouse else None,
                                      "facts": {key: value for key, value in facts.items() if key != "supplier_availability"}}),
            "supplier_availability": facts.get("supplier_availability"),
            "common": common, "variant": variant, "warehouse": warehouse_values, "stock": stock,
            "shops": shops, "overrides": deepcopy(data), "_facts": facts, "_warehouse": warehouse}


def _public(row):
    return {key: value for key, value in row.items() if not key.startswith("_")}


async def _rows(db, ids, warehouse):
    if not ids:
        return {}
    products = (await db.execute(select(Product.id, Product.sku, Product.name, Product.brand, Product.is_active,
        ProductGroup.id.label("group_id"), ProductGroup.code.label("group_code"), ProductGroup.name.label("group_name"),
        func.substr(ProductGroup.main_image_url, 1, 2001).label("group_image"))
        .outerjoin(ProductGroup, Product.group_id == ProductGroup.id).where(Product.id.in_(ids)))).all()
    overrides = {row.product_id: row for row in (await db.scalars(select(ProductEditorOverride)
        .where(ProductEditorOverride.product_id.in_(ids)).execution_options(populate_existing=True))).all()}
    identifiers = defaultdict(list)
    for row, supplier_code in (await db.execute(select(ProductIdentifier, Supplier.code).outerjoin(Supplier,
            ProductIdentifier.supplier_id == Supplier.id).where(ProductIdentifier.product_id.in_(ids))
            .order_by(ProductIdentifier.is_primary.desc(), ProductIdentifier.id))).all():
        identifiers[row.product_id].append((row, supplier_code))
    attributes = defaultdict(list)
    for row in (await db.scalars(select(ProductVariantAttribute).where(ProductVariantAttribute.product_id.in_(ids))
            .order_by(ProductVariantAttribute.display_order, ProductVariantAttribute.attribute_name))).all():
        attributes[row.product_id].append({"name": row.attribute_name, "value": row.attribute_value})
    shops = (await db.scalars(select(Shop).where(Shop.is_active.is_(True), Shop.platform == "upgates").order_by(Shop.code))).all()
    mappings = defaultdict(dict)
    for row in (await db.scalars(select(ShopProduct).where(ShopProduct.product_id.in_(ids)).order_by(ShopProduct.id))).all():
        mappings[row.product_id][row.shop_id] = row
    observed = {row["id"]: row for row in (await db.execute(text(OBSERVED_SQL), {"ids": list(ids)})).mappings().all()}
    sources = defaultdict(list)
    supplier_query = select(Product.id, Supplier.code, SupplierProduct.supplier_sku,
        func.substr(func.coalesce(SupplierProduct.images[0]["url"].astext, SupplierProduct.images[0].astext), 1, 2001),
        SupplierProduct.attributes["catalog"]["variant_attributes"]).select_from(Product)
    supplier_query = supplier_query.join(SupplierProduct, or_(SupplierProduct.id == Product.source_supplier_product_id,
        exists(select(ProductSupplySource.id).where(ProductSupplySource.product_id == Product.id,
            ProductSupplySource.supplier_product_id == SupplierProduct.id).correlate(Product, SupplierProduct)))).join(Supplier, SupplierProduct.supplier_id == Supplier.id)
    for product_id, supplier_code, code, image_url, supplier_attributes in (await db.execute(supplier_query.where(Product.id.in_(ids))
            .order_by(Supplier.code, SupplierProduct.supplier_sku))).all():
        sources[product_id].append((supplier_code, code, image_url, supplier_attributes))
    supplier_availability = await supplier_availability_projection(db, ids)
    balances, fifo_states, fifo_values = {}, set(), {}
    if warehouse:
        evidence = exists(select(StockMovement.id).where(StockMovement.product_id == StockBalance.product_id,
            StockMovement.warehouse_id == StockBalance.warehouse_id))
        for balance, verified in (await db.execute(select(StockBalance, evidence).where(StockBalance.product_id.in_(ids),
                StockBalance.warehouse_id == warehouse["id"]))).all():
            balances[balance.product_id] = (balance, verified)
        fifo_states = set(await db.scalars(select(FifoState.product_id).where(FifoState.product_id.in_(ids),
            FifoState.warehouse_id == warehouse["id"])))
        if fifo_states:
            remaining = FifoLayer.quantity_remaining
            sums = select(FifoLayer.product_id,
                func.sum(remaining).label("quantity"),
                func.sum(case((FifoLayer.stock_status == "quarantine", remaining), else_=0)).label("quarantine"),
                func.sum(case((FifoLayer.cost_status == "known", func.round(remaining * FifoLayer.unit_cost, 4)), else_=0)).label("known_value"),
                func.sum(case((FifoLayer.cost_status == "provisional", func.round(remaining * FifoLayer.unit_cost, 4)), else_=0)).label("provisional_value"),
                func.sum(case((FifoLayer.cost_status == "unknown", remaining), else_=0)).label("unknown_qty"),
                func.sum(case((FifoLayer.cost_status == "provisional", remaining), else_=0)).label("provisional_qty"))
            fifo_values = {row.product_id: row for row in (await db.execute(sums.where(FifoLayer.product_id.in_(fifo_states),
                FifoLayer.warehouse_id == warehouse["id"]).group_by(FifoLayer.product_id))).all()}
    result = {}
    for product in products:
        group = SimpleNamespace(id=product.group_id, code=product.group_code, name=product.group_name,
                                main_image_url=product.group_image) if product.group_id else None
        codes = {(code, identifier.value) for identifier, code in identifiers[product.id]
                 if str(getattr(identifier.identifier_type, "value", identifier.identifier_type)) == "supplier_sku"}
        codes.update((supplier, sku) for supplier, sku, _, _ in sources[product.id])
        shop_values, image_urls = [], [group.main_image_url] if group else []
        fallback_attributes = []
        for shop in shops:
            mapping = mappings[product.id].get(shop.id)
            raw = observed.get(mapping.id, {}) if mapping else {}
            image_urls.append(raw.get("image_url"))
            if not fallback_attributes:
                fallback_attributes = variant_attributes({"parameters": raw.get("parameters")})
            shop_values.append({"shop_code": shop.code, "mapped": bool(mapping and mapping.is_listed),
                "mapping": {"id": mapping.id, "external_id": mapping.external_id, "code": mapping.external_code,
                    "variant_code": mapping.variant_code, "parent_code": mapping.parent_code, "is_variant": mapping.is_variant} if mapping else None,
                "shop_url": _http_url(raw.get("shop_url")), "shop_admin_url": _http_url(raw.get("shop_admin_url")),
                "observed": {"name": raw.get("name"), "price": _decimal(mapping.shop_price) if mapping else None,
                             "price_basis": "unknown", "visible": raw.get("visible")}})
        image_urls.extend(url for _, _, url, _ in sources[product.id])
        for _, _, _, attrs in sources[product.id]:
            if not fallback_attributes and isinstance(attrs, list):
                fallback_attributes = variant_attributes({"parameters": attrs})
        balance, verified = balances.get(product.id, (None, False))
        minimum = (str(int(balance.min_quantity)) if balance.min_quantity == balance.min_quantity.to_integral_value()
                   else _decimal(balance.min_quantity)) if balance is not None else None
        facts = {"id": product.id, "sku": product.sku, "name": product.name, "brand": product.brand,
            "is_active": product.is_active, "group": {"id": group.id, "code": group.code, "name": group.name} if group else None,
            "attributes": attributes[product.id] or fallback_attributes, "eans": [identifier.value for identifier, _ in identifiers[product.id]
                if str(getattr(identifier.identifier_type, "value", identifier.identifier_type)) in ("ean", "upc", "unverified_barcode")],
            "supplier_codes": [{"supplier_code": supplier, "code": sku} for supplier, sku in sorted(codes, key=lambda item: (item[0] or "", item[1]))],
            "shops": shop_values, "warehouse_min_quantity": minimum,
            "supplier_availability": supplier_availability.get(product.id),
            "image_url": next((url for value in image_urls if (url := _image(value))), None)}
        stock = {"known": False, "qty_on_hand": None, "qty_reserved": None, "qty_quarantined": None,
                 "qty_available": None, "avg_cost": None, "total_value": None, "valuation_complete": False,
                 "known_value": None, "provisional_value": None, "unknown_qty": None, "provisional_qty": None}
        if balance is not None and verified:
            quarantine = getattr(balance, "qty_quarantined", Decimal("0"))
            layer = fifo_values.get(product.id)
            complete = balance.avg_cost is not None and balance.total_value is not None
            known_value, provisional_value, unknown_qty, provisional_qty = balance.total_value, Decimal("0"), Decimal("0"), Decimal("0")
            if product.id in fifo_states:
                known_value = layer.known_value if layer else Decimal("0")
                provisional_value = layer.provisional_value if layer else Decimal("0")
                unknown_qty = layer.unknown_qty if layer else Decimal("0")
                provisional_qty = layer.provisional_qty if layer else Decimal("0")
                complete = (complete and unknown_qty == 0 and provisional_qty == 0
                    and (layer.quantity if layer else 0) == balance.qty_on_hand
                    and (layer.quarantine if layer else 0) == quarantine)
            stock = {"known": True, "qty_on_hand": _decimal(balance.qty_on_hand), "qty_reserved": _decimal(balance.qty_reserved),
                     "qty_quarantined": _decimal(quarantine), "qty_available": _decimal(balance.qty_on_hand - balance.qty_reserved - quarantine),
                     "avg_cost": _decimal(balance.avg_cost) if complete else None,
                     "total_value": _decimal(balance.total_value) if complete else None, "valuation_complete": complete,
                     "known_value": _decimal(known_value), "provisional_value": _decimal(provisional_value),
                     "unknown_qty": _decimal(unknown_qty), "provisional_qty": _decimal(provisional_qty)}
        override = overrides.get(product.id)
        result[product.id] = _make_row(facts, override.data if override else {}, override.revision if override else 0, warehouse, stock)
    return result


async def list_products(db, q="", brand=None, shop_code=None, warehouse_code=None, page=1, page_size=50, sort="group_sku", direction="asc"):
    if page < 1 or page_size not in (25, 50, 100) or sort not in ("group_sku", "sku", "name") or direction not in ("asc", "desc"):
        raise EditorError("product_editor_invalid_request", 422)
    warehouse = await _warehouse(db, warehouse_code)
    name = func.coalesce(ProductEditorOverride.data["common"]["name"].astext, Product.name)
    effective_brand = func.coalesce(ProductEditorOverride.data["common"]["brand"].astext, Product.brand)
    statement = select(Product.id).outerjoin(ProductEditorOverride, ProductEditorOverride.product_id == Product.id)
    statement = statement.outerjoin(ProductGroup, ProductGroup.id == Product.group_id)
    if q:
        pattern = "%" + _fold(q).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        supplier_match = exists(select(SupplierProduct.id).where(_sql_fold(SupplierProduct.supplier_sku).like(pattern, escape="\\"),
            or_(SupplierProduct.id == Product.source_supplier_product_id, exists(select(ProductSupplySource.id).where(
                ProductSupplySource.product_id == Product.id, ProductSupplySource.supplier_product_id == SupplierProduct.id)
                .correlate(Product, SupplierProduct)))))
        identifier_match = exists(select(ProductIdentifier.id).where(ProductIdentifier.product_id == Product.id,
            _sql_fold(ProductIdentifier.value).like(pattern, escape="\\")))
        statement = statement.where(or_(_sql_fold(Product.sku).like(pattern, escape="\\"), _sql_fold(name).like(pattern, escape="\\"),
                                       identifier_match, supplier_match))
    if brand is not None:
        statement = statement.where(effective_brand == brand)
    if shop_code:
        statement = statement.where(exists(select(ShopProduct.id).join(Shop, ShopProduct.shop_id == Shop.id)
            .where(ShopProduct.product_id == Product.id, Shop.code == shop_code, ShopProduct.is_listed.is_(True))))
    total = await db.scalar(select(func.count()).select_from(statement.subquery()))
    natural = func.product_editor_natural_key
    if sort == "group_sku":
        # Sort canonical variant axes before pagination. Shop parent membership,
        # including the POS umbrella, is never used as a product family.
        attribute_name = _sql_fold(func.trim(ProductVariantAttribute.attribute_name))
        axes = select(ProductVariantAttribute.product_id,
            func.min(case((attribute_name.in_(("farba", "barva", "color", "colour")), ProductVariantAttribute.attribute_value))).label("color"),
            func.min(case((attribute_name.in_(("velkost", "velikost", "size")), ProductVariantAttribute.attribute_value))).label("size"),
            func.string_agg(ProductVariantAttribute.attribute_value, aggregate_order_by(literal(" "),
                ProductVariantAttribute.display_order, ProductVariantAttribute.attribute_name)).label("attributes"))
        axes = axes.group_by(ProductVariantAttribute.product_id).subquery()
        statement = statement.outerjoin(axes, axes.c.product_id == Product.id)
        color, size = axes.c.color, axes.c.size
        normalized_size = func.upper(func.trim(size))
        size_rank = case(
            (normalized_size.in_(("XXXS", "3XS")), 0), (normalized_size.in_(("XXS", "2XS")), 1),
            (normalized_size == "XS", 2), (normalized_size == "S", 3), (normalized_size == "M", 4),
            (normalized_size == "L", 5), (normalized_size == "XL", 6),
            (normalized_size.in_(("XXL", "2XL")), 7), (normalized_size.in_(("XXXL", "3XL")), 8),
            (normalized_size.in_(("XXXXL", "4XL")), 9), (normalized_size.in_(("XXXXXL", "5XL")), 10), else_=100)
        keys = [natural(func.coalesce(ProductGroup.name, name)), func.coalesce(ProductGroup.id, -Product.id),
                natural(color), size_rank, natural(size), natural(axes.c.attributes), natural(Product.sku), Product.id]
    else:
        keys = [natural(Product.sku if sort == "sku" else name), Product.id]
    ids = (await db.scalars(statement.order_by(*(key.desc() if direction == "desc" else key.asc() for key in keys))
                            .offset((page - 1) * page_size).limit(page_size))).all()
    rows = await _rows(db, ids, warehouse)
    return {"items": [_public(rows[identifier]) for identifier in ids], "total": total, "page": page, "page_size": page_size,
            "warehouse": warehouse, "currency": "EUR", "price_basis": "incl_vat", "external_write_enabled": False}


async def detail(db, product_id, warehouse_code=None):
    warehouse = await _warehouse(db, warehouse_code)
    row = (await _rows(db, [product_id], warehouse)).get(product_id)
    if row is None:
        raise EditorError("product_editor_product_missing", 404)
    audit = (await db.scalars(select(ProductEditorAudit).where(ProductEditorAudit.product_id == product_id)
                            .order_by(ProductEditorAudit.id.desc()).limit(20))).all()
    return {**_public(row), "audit": [{"id": item.id, "request_id": item.request_id, "revision": item.revision,
        "before": item.before_data, "after": item.after_data, "created_at": item.created_at.isoformat()} for item in audit]}


async def effective(db, product_ids, warehouse_code=None):
    """Bounded display integration: manual values override imported facts, never mutate them."""
    if len(product_ids) > 100:
        raise EditorError("product_editor_invalid_request", 422)
    rows = await _rows(db, list(product_ids), await _warehouse(db, warehouse_code))
    return {identifier: _public(row) for identifier, row in rows.items()}


async def effective_names(db, product_ids=None):
    """Lightweight display overrides, including legacy unpaged stock lists."""
    if product_ids is not None and not product_ids:
        return {}
    statement = select(ProductEditorOverride.product_id, ProductEditorOverride.data["common"]["name"].astext,
                       ProductEditorOverride.data["common"]["brand"].astext)
    if product_ids is not None:
        statement = statement.where(ProductEditorOverride.product_id.in_(product_ids))
    return {identifier: {key: value for key, value in (("name", name), ("brand", brand)) if value is not None}
            for identifier, name, brand in (await db.execute(statement)).all()}


def _apply_patch(data, patch, warehouse_code):
    result = deepcopy(data)
    def merge(container, changes):
        for key, value in changes.items():
            if value is None:
                container.pop(key, None)
            else:
                container[key] = value
    for scope in ("common", "variant"):
        value = getattr(patch, scope)
        if value is not None:
            merge(result.setdefault(scope, {}), value.model_dump(exclude_unset=True, exclude={"sku"} if scope == "variant" else set()))
    if patch.warehouse is not None:
        merge(result.setdefault("warehouses", {}).setdefault(warehouse_code, {}), patch.warehouse.model_dump(exclude_unset=True))
    for shop, changes in (patch.shops or {}).items():
        merge(result.setdefault("shops", {}).setdefault(shop, {}), changes.model_dump(exclude_unset=True))
    # Empty override scopes are inheritance, including after an explicit reset.
    for scope in ("warehouses", "shops"):
        if scope in result:
            result[scope] = {key: value for key, value in result[scope].items() if value}
    return {key: value for key, value in result.items() if value}


def _audit_values(row):
    return {key: deepcopy(row[key]) for key in ("sku", "eans", "attributes", "image_url", "common", "variant", "warehouse", "shops", "overrides", "snapshot_hash")}


async def get_save(db, request_id):
    try:
        identifier = str(UUID(str(request_id)))
    except ValueError:
        raise EditorError("product_editor_save_missing", 404) from None
    row = await db.get(ProductEditorSave, identifier)
    if row is None:
        raise EditorError("product_editor_save_missing", 404)
    return row.result


async def save(db, payload: ProductEditorSaveRequest):
    identifier = str(payload.request_id)
    serialized = payload.model_dump(mode="json")
    if len(json.dumps(serialized, ensure_ascii=True)) > 1_000_000:
        raise EditorError("product_editor_invalid_request", 422)
    digest = _digest(serialized)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": int(_digest(["product-editor", identifier])[:15], 16)})
    old = await db.get(ProductEditorSave, identifier)
    if old:
        if old.input_hash != digest:
            raise EditorError("product_editor_request_reused")
        return old.result
    # Imported product and mapping writers already use this identity lock.
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": IDENTITY_WRITE_LOCK})
    warehouse = await _warehouse(db, payload.warehouse_code, lock=True)
    ids = [item.get("product_id") for item in payload.changes if type(item.get("product_id")) is int and item["product_id"] > 0]
    duplicates = {key for key, count in Counter(ids).items() if count > 1}
    await db.scalars(select(Product.id).where(Product.id.in_(ids)).order_by(Product.id).with_for_update())
    await db.scalars(select(ShopProduct).where(ShopProduct.product_id.in_(ids)).order_by(ShopProduct.id).with_for_update(read=True))
    await db.scalars(select(ShopProductContent.id).where(exists(select(ShopProduct.id).where(ShopProduct.product_id.in_(ids),
        ShopProduct.shop_id == ShopProductContent.shop_id,
        func.coalesce(ShopProduct.parent_code, ShopProduct.external_code) == ShopProductContent.external_code)))
        .order_by(ShopProductContent.id).with_for_update(read=True))
    rows = await _rows(db, ids, warehouse)
    shops = set(await db.scalars(select(Shop.code).where(Shop.is_active.is_(True), Shop.platform == "upgates")))
    request = ProductEditorSave(request_id=identifier, input_hash=digest, result={})
    db.add(request)
    await db.flush()
    results = []
    for raw in payload.changes:
        product_id = raw.get("product_id") if type(raw.get("product_id")) is int else None
        def error(status, code, row=None):
            return {"product_id": product_id, "status": status, "errors": [{"code": code}],
                    **({"row": _public(row)} if row else {})}
        try:
            patch = EditorRowPatch.model_validate(raw)
        except ValidationError as invalid:
            response = error("invalid", "product_editor_invalid_value")
            allowed = {"product_id", "expected_revision", "snapshot_hash", "common", "variant", "warehouse", "shops",
                       "name", "brand", "internal_note", "sale_price_gross", "vat_rate", "note", "location", "min_quantity", "visible", "image_url", "eans", "attributes", "sku", "value"} | shops
            response["errors"] = [{"code": "product_editor_invalid_value", **({"field": ".".join(item["loc"])}
                if item["loc"] and all(isinstance(part, str) and part in allowed for part in item["loc"]) else {})}
                for item in invalid.errors(include_input=False)]
            results.append(response)
            continue
        if product_id in duplicates:
            results.append(error("invalid", "product_editor_duplicate_product"))
            continue
        row = rows.get(product_id)
        if row is None:
            results.append(error("missing", "product_editor_product_missing"))
            continue
        if patch.expected_revision != row["revision"] or patch.snapshot_hash != row["snapshot_hash"]:
            results.append(error("conflict", "product_editor_changed", row))
            continue
        if patch.warehouse is not None and warehouse is None:
            results.append(error("invalid", "product_editor_warehouse_required"))
            continue
        if set(patch.shops or {}) - shops:
            results.append(error("invalid", "product_editor_shop_missing"))
            continue
        changed = _apply_patch(row["overrides"], patch, payload.warehouse_code)
        identity_fields = patch.variant.model_fields_set & {"eans", "sku"} if patch.variant else set()
        identity_changed = bool(identity_fields and any(getattr(patch.variant, field) is not None
            and getattr(patch.variant, field) != row[field] for field in identity_fields))
        if changed == row["overrides"] and not identity_changed:
            results.append({"product_id": product_id, "status": "saved", "row": _public(row), "errors": []})
            continue
        try:
            async with db.begin_nested():
                if identity_changed:
                    await _edit_identity(db, product_id, patch.variant, row)
                override = await db.get(ProductEditorOverride, product_id)
                revision = row["revision"] + 1
                if override is None:
                    override = ProductEditorOverride(product_id=product_id, revision=revision, data=changed, updated_at=now())
                    db.add(override)
                else:
                    override.revision, override.data, override.updated_at = revision, changed, now()
                facts = deepcopy(row["_facts"])
                if identity_changed:
                    for field in identity_fields:
                        if getattr(patch.variant, field) is not None:
                            facts[field] = getattr(patch.variant, field)
                saved = _make_row(facts, changed, revision, warehouse, row["stock"])
                db.add(ProductEditorAudit(request_id=identifier, product_id=product_id, revision=revision,
                    before_data=_audit_values(row), after_data=_audit_values(saved)))
                await db.flush()
            results.append({"product_id": product_id, "status": "saved", "row": _public(saved), "errors": []})
        except EditorError as invalid:
            results.append(error("invalid", invalid.code))
        except IntegrityError:
            results.append(error("invalid", "product_editor_identity_conflict" if identity_changed else "product_editor_save_failed"))
    request.result = {"request_id": identifier, "status": "completed", "results": results, "external_write_enabled": False}
    await db.flush()
    response = request.result
    await db.commit()
    return response


async def _edit_identity(db, product_id, patch, row):
    """Caller holds the shared identity lock and per-product lock; audit is atomic."""
    if patch.sku is not None and patch.sku != row["sku"]:
        if any(shop.get("mapping") for shop in row["shops"]):
            raise EditorError("product_editor_mapped_sku_rename")
        if (await db.scalar(select(Product.id).where(Product.sku == patch.sku, Product.id != product_id))
                or await db.scalar(select(ProductIdentifier.id).where(ProductIdentifier.value == patch.sku, ProductIdentifier.product_id != product_id))):
            raise EditorError("product_editor_identity_conflict")
        product = await db.get(Product, product_id)
        product.sku = patch.sku
    if patch.eans is not None and patch.eans != row["eans"]:
        if await db.scalar(select(ProductIdentifier.id).where(ProductIdentifier.value.in_(patch.eans),
                ProductIdentifier.product_id != product_id)):
            raise EditorError("product_editor_identity_conflict")
        await db.execute(delete(ProductIdentifier).where(ProductIdentifier.product_id == product_id,
            ProductIdentifier.identifier_type.in_(ProductIdentifierService.BARCODE_TYPES)))
        for index, code in enumerate(patch.eans):
            db.add(ProductIdentifier(product_id=product_id, value=code,
                identifier_type=ProductIdentifierService.classify_barcode(code), is_primary=index == 0))
        await db.flush()


def publication_values(common, variant, effective):
    return {"name": effective["name"], "sale_price_gross": effective["sale_price_gross"],
        "visible": effective["visible"], "ean": list(variant["eans"]),
        "attributes": sorted(variant["attributes"], key=lambda item: item["name"]),
        "image_url": common["image_url"]}
