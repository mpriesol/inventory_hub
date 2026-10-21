"""Persist exact supplier identities without changing canonical products or stock.

A prefix is a creation/matching convention, not the permanent product identity.
Existing supplier-scoped aliases and explicit legacy links can also identify a
product. EAN is only conflicting evidence here, never a discovery mechanism.
"""
from collections import defaultdict

from sqlalchemy import or_, select, text
from sqlalchemy.dialects.postgresql import insert

from inventory_hub import config_io
from inventory_hub.db_models import IdentifierType, Product, ProductIdentifier, Supplier, SupplierProduct
from inventory_hub.db_models_ext import ProductSupplySource
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK, RemoteIdentity, load_identity_index
from inventory_hub.services.supplier_availability_source import AvailabilityError
from inventory_hub.supplier_availability_models import SupplierAvailabilityObservation
from inventory_hub.supplier_prefix import SupplierPrefixError, canonical_supplier_sku, get_supplier_prefix


def _prefix(supplier, cfg=None, *, claim=False):
    try:
        cfg = cfg if cfg is not None else config_io.load_supplier(supplier, write_back_on_load=False)
        prefix = get_supplier_prefix(cfg)
        if not prefix:
            raise AvailabilityError("supplier_prefix_missing", 422)
        return config_io.claim_supplier_prefix(supplier, prefix) if claim else prefix
    except SupplierPrefixError as error:
        raise AvailabilityError(error.code, error.status) from None


async def _inspect(db, supplier, products, prefix, *, match_catalog=False):
    """Batch every candidate and conflicting evidence, including other owners."""
    ids = [product.id for product in products]
    codes = defaultdict(set)
    for product in products:
        if product.sku.startswith(prefix) and len(product.sku) > len(prefix):
            codes[product.id].add(product.sku[len(prefix):])
    for product_id, code in (await db.execute(select(ProductIdentifier.product_id, ProductIdentifier.value).where(
            ProductIdentifier.product_id.in_(ids), ProductIdentifier.supplier_id == supplier.id,
            ProductIdentifier.identifier_type == IdentifierType.supplier_sku))).all():
        codes[product_id].add(code)
    legacy = {product.source_supplier_product_id: product.id for product in products if product.source_supplier_product_id}
    for source_id, code in (await db.execute(select(SupplierProduct.id, SupplierProduct.supplier_sku).where(
            SupplierProduct.id.in_(legacy), SupplierProduct.supplier_id == supplier.id))).all():
        # More than one product can hold a legacy pointer; check all, not only
        # the last product seen in a dictionary.
        for product in products:
            if product.source_supplier_product_id == source_id:
                codes[product.id].add(code)
    matching_issues = {}
    if match_catalog:
        from inventory_hub.services.supplier_catalog_matching import suggestions
        suggested_codes, matching_issues = await suggestions(db, supplier, products)
        for product_id, code in suggested_codes.items():
            if not codes[product_id]:
                codes[product_id].add(code)
    all_codes = {code for values in codes.values() for code in values}
    sources = {source.supplier_sku: source for source in (await db.scalars(select(SupplierProduct).where(
        SupplierProduct.supplier_id == supplier.id, SupplierProduct.supplier_sku.in_(all_codes)))).all()}
    observations = {row.supplier_sku: row for row in (await db.scalars(select(SupplierAvailabilityObservation).where(
        SupplierAvailabilityObservation.supplier_id == supplier.id,
        SupplierAvailabilityObservation.supplier_sku.in_(all_codes)))).all()}
    links = (await db.execute(select(ProductSupplySource, SupplierProduct.supplier_sku).join(
        SupplierProduct, SupplierProduct.id == ProductSupplySource.supplier_product_id).where(
        SupplierProduct.supplier_id == supplier.id,
        or_(ProductSupplySource.product_id.in_(ids), SupplierProduct.supplier_sku.in_(all_codes))))).all()
    by_product, owners = defaultdict(dict), defaultdict(set)
    for link, code in links:
        by_product[link.product_id][code] = link
        owners[code].add(link.product_id)
    # A conflicting old pointer is just as significant as an explicit source.
    source_codes = {source.id: source.supplier_sku for source in sources.values()}
    for product_id, source_id in (await db.execute(select(Product.id, Product.source_supplier_product_id).where(
            Product.source_supplier_product_id.in_([source.id for source in sources.values()])))).all():
        owners[source_codes[source_id]].add(product_id)
    candidates, identities = [], []
    for product in products:
        for code in sorted(codes[product.id]):
            source, observation = sources.get(code), observations.get(code)
            try:
                canonical = canonical_supplier_sku(prefix, code)
                reason = None
            except SupplierPrefixError as error:
                canonical, reason = "", error.code
            identity = RemoteIdentity(0, canonical, barcodes=(source.ean or "",) if source else (),
                supplier_id=supplier.id, supplier_sku=code, expected_product_id=product.id)
            identities.append(identity)
            candidates.append({"product": product, "supplier_sku": code, "source": source,
                "observation": observation, "link": by_product[product.id].get(code), "reason": reason,
                "other_links": bool(by_product[product.id] and code not in by_product[product.id]),
                "owners": owners[code]})
    index = await load_identity_index(db, 0, identities, local=True)
    for candidate, identity in zip(candidates, identities):
        resolution = index.resolve(identity)
        if candidate["reason"]:
            continue
        if resolution.status == "conflict":
            candidate["reason"] = resolution.reasons[0]
        elif candidate["owners"] - {candidate["product"].id}:
            candidate["reason"] = "supplier_source_multiple_products"
        elif not candidate["source"] and not candidate["observation"]:
            # Invoice aliases can differ from the verified feed SKU. An alias
            # without an offer is identity evidence, not a competing source.
            candidate["reason"] = "supplier_item_missing"
        elif candidate["other_links"]:
            candidate["reason"] = "supplier_link_already_configured"
        elif not supplier.is_active or (candidate["source"] is not None and
                (not candidate["source"].is_active or candidate["source"].is_discontinued)):
            candidate["reason"] = "supplier_source_inactive"
    for product in products:
        if product.id in matching_issues and not codes[product.id]:
            candidates.append({"product":product, "supplier_sku":None, "reason":matching_issues[product.id]})
    return candidates


async def reconcile_supplier_links(db, supplier_code, *, after_product_id=0, limit=500, product_ids=None, match_catalog=False):
    """One bounded idempotent local repair page; no downloads or stock writes."""
    if not 1 <= limit <= 500 or after_product_id < 0 or (product_ids is not None and len(product_ids) > 500):
        raise AvailabilityError("supplier_link_invalid_page", 422)
    prefix = _prefix(supplier_code)
    supplier = await db.scalar(select(Supplier).where(Supplier.code == supplier_code))
    if supplier is None:
        raise AvailabilityError("supplier_availability_not_configured", 404)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": IDENTITY_WRITE_LOCK})
    query = select(Product).where(Product.id > after_product_id).order_by(Product.id).limit(limit + 1)
    if product_ids is not None:
        query = query.where(Product.id.in_(product_ids))
    products = list((await db.scalars(query)).all())
    more, products = len(products) > limit, products[:limit]
    result = {"supplier": supplier_code, "scanned": len(products), "linked": 0, "existing": 0,
        "skipped": 0, "conflicts": [], "skipped_details": [],
        "next_after_product_id": products[-1].id if products and more else None}
    if not products:
        return result
    candidates = await _inspect(db, supplier, products, prefix, match_catalog=match_catalog)
    claimed = False
    for candidate in candidates:
        product, code, reason = candidate["product"], candidate["supplier_sku"], candidate["reason"]
        detail = {"product_id": product.id, "sku": product.sku, "supplier_sku": code, "reason": reason}
        if reason:
            if reason in ("supplier_item_missing", "supplier_source_inactive", "inactive_product"):
                result["skipped"] += 1
                result["skipped_details"].append(detail)
            else:
                result["conflicts"].append(detail)
            continue
        if not claimed:
            try:
                config_io.claim_supplier_prefix(supplier_code, prefix)
            except SupplierPrefixError as error:
                raise AvailabilityError(error.code, error.status) from None
            claimed = True
        # Preserve both disabled links and every operator-set source policy.
        if candidate["link"] is not None:
            result["existing"] += 1
            continue
        source = candidate["source"]
        if source is None:
            observed = candidate["observation"]
            source = SupplierProduct(supplier_id=supplier.id, supplier_sku=code, name=product.name,
                attributes={"supplier_identity": {"source": "availability_observation", "run_id": observed.run_id}})
            db.add(source)
            await db.flush()
        await db.execute(insert(ProductSupplySource).values(product_id=product.id, supplier_product_id=source.id)
            .on_conflict_do_nothing(constraint="uq_supply_sources"))
        await db.execute(insert(ProductIdentifier).values(product_id=product.id, supplier_id=supplier.id,
            identifier_type=IdentifierType.supplier_sku, value=code, is_primary=False,
            notes="Supplier link: exact canonical/registered identity or configured shop code, brand and barcode checks")
            .on_conflict_do_nothing(index_elements=[ProductIdentifier.supplier_id, ProductIdentifier.value],
                index_where=text("identifier_type = 'supplier_sku'")))
        result["linked"] += 1
    await db.flush()
    return result


async def reconcile_source_codes(db, supplier_code, raw_codes, cfg=None):
    """Link relevant rows after accepted feed data, including supplier aliases."""
    try:
        prefix = _prefix(supplier_code, cfg)
    except AvailabilityError as error:
        # Reading an existing feed without a configured identity prefix remains
        # possible. Its observations alone do not authorize product matching.
        if error.code == "supplier_prefix_missing":
            return []
        raise
    supplier_id = await db.scalar(select(Supplier.id).where(Supplier.code == supplier_code))
    ids = set()
    codes = sorted(set(raw_codes))
    for start in range(0, len(codes), 500):
        chunk = codes[start:start + 500]
        canonical = []
        for code in chunk:
            try:
                canonical.append(canonical_supplier_sku(prefix, code))
            except SupplierPrefixError:
                continue
        ids.update((await db.scalars(select(Product.id).where(Product.sku.in_(canonical)))).all())
        ids.update((await db.scalars(select(ProductIdentifier.product_id).where(
            ProductIdentifier.supplier_id == supplier_id, ProductIdentifier.value.in_(chunk),
            ProductIdentifier.identifier_type == IdentifierType.supplier_sku))).all())
        ids.update((await db.scalars(select(Product.id).join(SupplierProduct,
            SupplierProduct.id == Product.source_supplier_product_id).where(
                SupplierProduct.supplier_id == supplier_id, SupplierProduct.supplier_sku.in_(chunk)))).all())
    reports = []
    selected = sorted(ids)
    for start in range(0, len(selected), 500):
        reports.append(await reconcile_supplier_links(db, supplier_code, product_ids=selected[start:start + 500]))
    return reports


async def bind_explicit_supplier_source(db, product_id, supplier_id, supplier_sku, *, barcodes=(), brand=None, actor="manual-card"):
    """Attach a reviewed supplier code without interpreting a legacy SKU prefix.

    The caller owns the identity lock. A manual/no-feed source is a supplier
    relationship, never a stock observation or a second physical product. Existing
    source policies and raw catalog values remain unchanged.
    """
    from inventory_hub.services.identifiers import ProductIdentifierService
    from inventory_hub.services.product_identity import verified_barcodes

    supplier = await db.get(Supplier, supplier_id)
    product = await db.get(Product, product_id)
    if supplier is None or not supplier.is_active or product is None:
        raise AvailabilityError("supplier_source_inactive", 409)
    source = await db.scalar(select(SupplierProduct).where(
        SupplierProduct.supplier_id == supplier_id, SupplierProduct.supplier_sku == supplier_sku))
    source_eans = ()
    if source is not None:
        catalog = (source.attributes or {}).get("catalog") or {}
        source_eans = verified_barcodes((source.ean or "", *(catalog.get("eans") or [])))
        owners = set(await db.scalars(select(ProductSupplySource.product_id).where(
            ProductSupplySource.supplier_product_id == source.id)))
        owners.update(await db.scalars(select(Product.id).where(Product.source_supplier_product_id == source.id)))
        if owners - {product_id}:
            raise AvailabilityError("supplier_source_multiple_products", 409)
        if not source.is_active or source.is_discontinued:
            raise AvailabilityError("supplier_source_inactive", 409)
        from inventory_hub.product_editor_models import ProductEditorOverride
        from inventory_hub.services.sku_policy import brand_key, registered_brand_id
        from inventory_hub.services import product_links
        override = await db.get(ProductEditorOverride, product_id)
        local_brand = ((override.data if override else {}).get("common") or {}).get("brand") or product.brand or brand
        source_brands = [value for value in (source.brand, catalog.get("brand")) if value]
        if local_brand:
            local_brand_id = await registered_brand_id(db, local_brand)
            for source_brand in source_brands:
                if brand_key(local_brand) != brand_key(source_brand) and (
                        local_brand_id is None or await registered_brand_id(db, source_brand) != local_brand_id):
                    raise AvailabilityError("sku_brand_conflict", 409)
        if catalog.get("variant_attributes"):
            facts = (await product_links._facts(db, [product_id], exclude_supplier_product_id=source.id))[product_id]
            try:
                local_axes = product_links.axes(facts["attributes"])
                source_axes = product_links.axes(catalog["variant_attributes"])
            except product_links.LinkError:
                raise AvailabilityError("variant_axes_ambiguous", 409) from None
            if any(local_axes[key] != source_axes[key] for key in local_axes.keys() & source_axes.keys()):
                raise AvailabilityError("variant_axes_conflict", 409)
    other_sources = select(ProductSupplySource.supplier_product_id).join(SupplierProduct,
        SupplierProduct.id == ProductSupplySource.supplier_product_id).where(
            ProductSupplySource.product_id == product_id, SupplierProduct.supplier_id == supplier_id)
    if source is not None:
        other_sources = other_sources.where(ProductSupplySource.supplier_product_id != source.id)
    if await db.scalar(other_sources):
        raise AvailabilityError("supplier_link_already_configured", 409)
    if product.source_supplier_product_id and (source is None or product.source_supplier_product_id != source.id):
        legacy_supplier = await db.scalar(select(SupplierProduct.supplier_id).where(
            SupplierProduct.id == product.source_supplier_product_id))
        if legacy_supplier == supplier_id:
            raise AvailabilityError("supplier_link_already_configured", 409)
    supplied = verified_barcodes(barcodes)
    supplied_keys = {ProductIdentifierService.gtin_key(value) for value in supplied}
    source_keys = {ProductIdentifierService.gtin_key(value) for value in source_eans}
    if supplied and source_eans and not supplied_keys & source_keys:
        raise AvailabilityError("supplier_card_links_source_ean_conflict", 409)
    identity = RemoteIdentity(0, "", barcodes=tuple(dict.fromkeys((*supplied, *source_eans))),
        supplier_id=supplier_id, supplier_sku=supplier_sku, expected_product_id=product_id)
    result = (await load_identity_index(db, 0, [identity], local=True)).resolve(identity)
    if result.status == "conflict" or result.product_id != product_id:
        raise AvailabilityError("supplier_card_links_identity_conflict", 409)
    if source is None:
        source = SupplierProduct(supplier_id=supplier_id, supplier_sku=supplier_sku, name=product.name,
            attributes={"supplier_identity": {"source": "manual_card", "actor": actor}})
        db.add(source)
        await db.flush()
    alias = await db.scalar(select(ProductIdentifier.id).where(ProductIdentifier.product_id == product_id,
        ProductIdentifier.supplier_id == supplier_id, ProductIdentifier.value == supplier_sku,
        ProductIdentifier.identifier_type == IdentifierType.supplier_sku))
    if alias is None:
        await ProductIdentifierService(db).add_identifier(product_id, supplier_sku, IdentifierType.supplier_sku,
            supplier_id=supplier_id, notes="Reviewed card creation: explicit supplier code; canonical SKU retained")
    link = await db.scalar(select(ProductSupplySource).where(ProductSupplySource.product_id == product_id,
        ProductSupplySource.supplier_product_id == source.id))
    if link is None:
        db.add(ProductSupplySource(product_id=product_id, supplier_product_id=source.id))
    await db.flush()
    return {"product_id": product_id, "supplier_product_id": source.id, "created_link": link is None}


async def reconcile_product_links(db, product_ids):
    """After shop pull, consider configured suppliers for these products only."""
    if not product_ids:
        return []
    suppliers = (await db.scalars(select(Supplier.code).order_by(Supplier.code))).all()
    reports = []
    ids = sorted(set(product_ids))
    for supplier in suppliers:
        if not config_io.supplier_path(supplier).is_file():
            continue
        try:
            _prefix(supplier)
        except AvailabilityError:
            continue
        for start in range(0, len(ids), 500):
            reports.append(await reconcile_supplier_links(db, supplier, product_ids=ids[start:start + 500]))
    return reports


async def missing_link_diagnostics(db, product_ids):
    """Explain unlinked rows without making a GET mutate product identity."""
    if not product_ids:
        return {}
    products = list((await db.scalars(select(Product).where(Product.id.in_(product_ids)))).all())
    suppliers = (await db.scalars(select(Supplier).order_by(Supplier.code))).all()
    result = {}
    for supplier in suppliers:
        if not config_io.supplier_path(supplier.code).is_file():
            continue
        try:
            prefix = _prefix(supplier.code)
        except AvailabilityError:
            continue
        for candidate in await _inspect(db, supplier, products, prefix):
            reason = candidate["reason"]
            conflict = reason and reason not in ("supplier_item_missing", "supplier_source_inactive", "inactive_product")
            item = {"status": "conflict" if conflict else "missing_link",
                "reason": reason or "supplier_link_missing", "supplier": supplier.code,
                "supplier_sku": candidate["supplier_sku"]}
            previous = result.get(candidate["product"].id)
            if previous is None or conflict:
                result[candidate["product"].id] = item
    return result
