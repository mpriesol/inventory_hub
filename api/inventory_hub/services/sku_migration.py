"""Read-only migration evidence, not an approval or an executable rename plan.

Existing product IDs, aliases, ledgers, drafts, caches and stock flags are never
changed. Optional bounded Upgates GETs use the existing admitted identity reader.
Unknown administrative rename, advertising and concurrent-writer controls always
remain separate required checks; a successful GET cannot authorize an apply.
"""
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json

from sqlalchemy import select, text

from inventory_hub.db_models import Product, ProductGroup, ProductIdentifier, Shop
from inventory_hub.db_models_ext import ProductVariantAttribute, ShopProduct
from inventory_hub.services import product_link_source as source, sku_policy
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.services.product_identity import verified_barcodes
from inventory_hub.services.product_links import LinkError, axes
from inventory_hub.sku_policy_models import SkuBrand


REQUIRED_CHECKS = [
    "migration_supported_upgates_rename", "migration_advertising_ids", "migration_open_orders_and_carts",
    "migration_concurrent_imports", "migration_backup_and_return", "migration_preserve_ledger_and_native_ids",
    "migration_refresh_shop_data_and_indexes", "migration_separate_write_authorization",
]
REFRESH_REQUIRED = ["shop_product_content", "upgates_products_cache", "catalog_identity_index", "prepared_import_matches"]
UNVERIFIED_IMPACTS = ["catalog_publication_journals_not_inspected", "advertising_feed_ids_not_inspected",
                      "remote_open_orders_not_inspected", "manual_stock_previews_not_inspected"]
MAX_REMOTE_SHOPS = 2


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                             default=str).encode("utf-8")).hexdigest()


def _native(value):
    value = str(value or "")
    return value if value.isascii() and value.isdigit() and value[0:1] != "0" and len(value) <= 20 else None


def _gtins(values):
    return {key for value in verified_barcodes(values) if (key := ProductIdentifierService.gtin_key(value))}


async def _local(db, ids):
    products = {p.id: (p, group) for p, group in (await db.execute(select(Product, ProductGroup.code)
        .outerjoin(ProductGroup, ProductGroup.id == Product.group_id).where(Product.id.in_(ids))
        .execution_options(populate_existing=True))).all()}
    identifiers, mappings, attributes = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in await db.scalars(select(ProductIdentifier).where(ProductIdentifier.product_id.in_(ids)).order_by(ProductIdentifier.id)):
        identifiers[row.product_id].append({"type": getattr(row.identifier_type, "value", row.identifier_type),
            "value": row.value, "supplier_id": row.supplier_id, "origin": "product_identifiers"})
    for row in await db.scalars(select(ProductVariantAttribute).where(ProductVariantAttribute.product_id.in_(ids))
                               .order_by(ProductVariantAttribute.attribute_name)):
        attributes[row.product_id].append({"name": row.attribute_name, "value": row.attribute_value})
    for mapping, shop in (await db.execute(select(ShopProduct, Shop).join(Shop, Shop.id == ShopProduct.shop_id)
        .where(ShopProduct.product_id.in_(ids)).order_by(ShopProduct.id)
        .execution_options(populate_existing=True))).all():
        mappings[mapping.product_id].append({"shop_code": shop.code, "mapping_id": mapping.id,
            "is_variant": mapping.is_variant, "old_code": mapping.external_code,
            "native_id": _native(mapping.native_id), "parent_code": mapping.parent_code if mapping.is_variant else None,
            "parent_native_id": _native(mapping.external_product_id), "remote_status": "not_checked", "reasons": [],
            "remote_identity": None, "remote_hash": None, "_shop_id": shop.id,
            "remote_checked_at": None, "remote_requests": None, "remote_connection_fingerprint": None,
            "retry_after_seconds": None,
            "_shop_usable": shop.is_active and shop.platform == "upgates", "_listed": mapping.is_listed})
    # Project only identity metadata from the cached parent; no full POS family
    # or merchant/customer content is loaded into this response.
    cache = (await db.execute(text("""
        SELECT sp.id, c.data->>'product_id' AS parent_id, c.data->>'code' AS parent_code,
               c.data->'variants_exists_yn' AS has_variants, c.data->'set_yn' AS is_set,
               CASE WHEN jsonb_typeof(c.data->'variants')='array'
                    THEN jsonb_array_length(c.data->'variants') ELSE 0 END AS variant_count
        FROM shop_products sp JOIN shop_product_content c ON c.shop_id=sp.shop_id
         AND c.external_code=CASE WHEN sp.is_variant THEN sp.parent_code ELSE sp.external_code END
        WHERE sp.product_id=ANY(CAST(:ids AS bigint[]))
        """), {"ids": ids})).mappings().all()
    cached = {row["id"]: dict(row) for row in cache}
    groups = {code.casefold() for code in await db.scalars(select(ProductGroup.code).where(
        ProductGroup.id.in_(select(Product.group_id).where(Product.group_id.is_not(None)))))}
    return products, identifiers, mappings, attributes, cached, groups


async def _impacts(db, ids):
    """Counts of typed dependencies only; no order details or JSON documents."""
    rows = (await db.execute(text("""
        SELECT p.id,
          (SELECT count(*) FROM stock_balances b WHERE b.product_id=p.id) AS stock_locations,
          (SELECT count(*) FROM stock_tracking t WHERE t.product_id=p.id) AS confirmed_stock_locations,
          (SELECT count(*) FROM stock_movements m WHERE m.product_id=p.id) AS stock_movements,
          (SELECT count(*) FROM fifo_layers f WHERE f.product_id=p.id) AS fifo_layers,
          (SELECT count(*) FROM fifo_states f WHERE f.product_id=p.id) AS fifo_states,
          (SELECT count(*) FROM reservations r WHERE r.product_id=p.id AND r.status::text IN ('reserved','backorder')) AS active_reservations,
          (SELECT count(*) FROM shop_order_items i JOIN shop_orders o ON o.id=i.order_id
             WHERE (i.product_id=p.id OR (i.product_id IS NULL AND i.external_product_code=p.sku))
               AND o.status::text IN ('new','processing')) AS open_order_lines,
          (SELECT count(*) FROM receiving_lines l JOIN receiving_sessions s ON s.id=l.session_id
             WHERE l.product_id=p.id AND s.status::text IN ('new','in_progress','paused')) AS open_receiving_lines,
          (SELECT count(*) FROM opening_stock_lines l JOIN opening_stock_batches b ON b.id=l.batch_id
             WHERE l.product_id=p.id AND b.status='prepared' AND b.expires_at>now()) AS prepared_opening_lines,
          (SELECT count(*) FROM stock_sync_checkpoints c WHERE c.product_id=p.id OR c.sku=p.sku) AS stock_checkpoints,
          (SELECT count(*) FROM product_editor_publications u WHERE u.product_id=p.id AND u.state IN ('prepared','sending','uncertain')) AS pending_content_publications,
          (SELECT count(*) FROM stock_sync_items u WHERE u.sku=p.sku AND u.status IN ('prepared','sending','uncertain')) AS pending_stock_sync_items,
          (SELECT count(*) FROM stock_publication_items u WHERE u.sku=p.sku AND u.status IN ('prepared','sending','uncertain')) AS pending_stock_publications,
          (SELECT count(*) FROM fifo_cost_publications u WHERE u.kind='product'
             AND u.target_key='product:' || p.id::text AND u.status IN ('queued','sending','uncertain')) AS pending_cost_publications,
          (SELECT count(*) FROM shop_sync_outbox u WHERE u.product_id=p.id AND u.status::text IN ('pending','processing','failed')) AS pending_shop_outbox,
          (SELECT count(*) FROM product_import_drafts d WHERE EXISTS (
              SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(d.document->'rows')='array'
                THEN d.document->'rows' ELSE '[]'::jsonb END) AS r
              WHERE r->>'hub_product_id'=p.id::text OR r->>'expected_hub_product_id'=p.id::text
                OR r->'values'->>'code'=p.sku OR r->>'canonical_code'=p.sku
          )) AS matching_import_drafts
        FROM products p WHERE p.id=ANY(CAST(:ids AS bigint[])) ORDER BY p.id
        """), {"ids": ids})).mappings().all()
    results = {}
    review_fields = {"active_reservations", "open_order_lines", "open_receiving_lines", "prepared_opening_lines",
                     "stock_checkpoints", "pending_content_publications", "pending_stock_sync_items",
                     "pending_stock_publications", "pending_cost_publications", "pending_shop_outbox", "matching_import_drafts"}
    for row in rows:
        counts = {key: int(value) for key, value in row.items() if key != "id"}
        confirmed, locations = counts["confirmed_stock_locations"], counts["stock_locations"]
        results[row["id"]] = {"counts": counts,
            "stock_state": "unconfirmed" if not confirmed else "mixed" if confirmed < locations else "confirmed",
            "requires_review": sorted(key for key in review_fields if counts[key]),
            "unverified": list(UNVERIFIED_IMPACTS), "refresh_required": list(REFRESH_REQUIRED)}
    return results


def _remote_identity(leaf):
    return {key: leaf[key] for key in ("code", "is_variant", "external_id", "parent_code", "parent_id", "eans", "attributes")}


def _verify_mapping(mapping, item, read):
    reasons = mapping["reasons"]
    old, proposed = mapping["old_code"], mapping["proposed_code"]
    matches = [leaf for leaf in read["leaves"] if leaf["code"] == old and leaf["is_variant"] == mapping["is_variant"]
        and (not mapping["is_variant"] or leaf["parent_code"] == mapping["parent_code"])]
    if len(matches) != 1:
        reasons.append("migration_remote_leaf_missing_or_ambiguous")
    else:
        leaf = matches[0]
        mapping["remote_identity"] = _remote_identity(leaf)
        mapping["remote_hash"] = digest(mapping["remote_identity"])
        if not mapping["native_id"] or leaf["external_id"] != mapping["native_id"]:
            reasons.append("migration_remote_native_id_changed")
        if mapping["parent_native_id"] and leaf["parent_id"] != mapping["parent_native_id"]:
            reasons.append("migration_remote_parent_changed")
        if mapping["is_variant"] and leaf["parent_code"] != mapping["parent_code"]:
            reasons.append("migration_remote_parent_changed")
        # Parent native ID is observed fresh and retained in the review, never
        # inferred from a URL or assigned into a local mapping by this endpoint.
        mapping["parent_native_id"] = leaf["parent_id"]
        local_gtins, remote_gtins = _gtins(item["identity"]["barcodes"]), _gtins(leaf.get("eans", []))
        if local_gtins and not remote_gtins:
            reasons.append("migration_remote_barcode_missing")
        elif local_gtins and remote_gtins and not local_gtins.intersection(remote_gtins):
            reasons.append("migration_remote_barcode_conflict")
        try:
            local_axes, remote_axes = axes(item["_attributes"]), axes(leaf.get("attributes", []))
            axes_conflict = any(key in remote_axes and remote_axes[key] != value for key, value in local_axes.items())
        except LinkError:
            axes_conflict = True
        if axes_conflict:
            reasons.append("migration_remote_variant_conflict")
    occupied = [row for row in read.get("occupied_codes", []) if row["code"].casefold() == proposed.casefold()]
    if old == proposed:
        occupied = [row for row in occupied if row["native_id"] != mapping["native_id"]
                    or (row["kind"] == "variant") != mapping["is_variant"]]
    if occupied:
        reasons.append("migration_remote_target_occupied")
    if "occupied_codes" not in read:
        reasons.append("migration_remote_target_collision_unverified")
    mapping["reasons"] = sorted(set(reasons))
    mapping["remote_status"] = "conflict" if reasons else "verified"


async def _remote(items):
    by_shop = defaultdict(list)
    for item in items:
        if not item.get("proposed_sku"):
            continue
        for mapping in item["mappings"]:
            if mapping["_shop_usable"] and mapping["old_code"] and not any(
                    reason in item["reasons"] for reason in ("migration_pos_separate_review", "migration_parent_not_stock_leaf")):
                by_shop[mapping["shop_code"]].append((item, mapping))
    for index, (shop, entries) in enumerate(sorted(by_shop.items())):
        if index >= MAX_REMOTE_SHOPS:
            for _item, mapping in entries:
                mapping["remote_status"] = "unavailable"
                mapping["reasons"].append("migration_remote_shop_limit")
            continue
        codes = sorted({code for _item, mapping in entries for code in (mapping["old_code"], mapping["proposed_code"])})
        try:
            read = await source.inspect_codes(shop, codes, max_requests=8)
            checked_at = datetime.now(timezone.utc).isoformat()
            for item, mapping in entries:
                _verify_mapping(mapping, item, read)
                mapping.update(remote_checked_at=checked_at, remote_requests=read["requests"],
                    remote_connection_fingerprint=read["fingerprint"], remote_hash=digest(read))
        except source.SourceError as error:
            for _item, mapping in entries:
                mapping["remote_status"] = "unavailable"
                mapping["reasons"].append(error.code)
                mapping["remote_checked_at"] = datetime.now(timezone.utc).isoformat()
                mapping["retry_after_seconds"] = error.retry_after


async def preview(db, payload):
    ids = [item.product_id for item in payload.items]
    products, identifiers, mappings, attributes, cached, parent_codes = await _local(db, ids)
    impacts = await _impacts(db, ids)
    # Compare all currently allocated SKU/legacy aliases with Python casefold;
    # this also catches Unicode case collisions which SQL lower cannot model.
    occupied = defaultdict(set)
    for pid, code in (await db.execute(select(Product.id, Product.sku))).all():
        occupied[code.casefold()].add(pid)
    for pid, value in (await db.execute(select(ProductIdentifier.product_id, ProductIdentifier.value)
        .where(ProductIdentifier.identifier_type == "internal_sku"))).all():
        occupied[value.casefold()].add(pid)
    items = []
    for request in payload.items:
        identity = request.sku_identity.model_dump(mode="json")
        item = {"product_id": request.product_id, "name": None, "old_sku": None, "proposed_sku": None,
            "status": "blocked", "reasons": [], "candidate_product_ids": [], "identity": identity,
            "aliases": identifiers[request.product_id], "mappings": mappings[request.product_id],
            "impacts": impacts.get(request.product_id, {"counts": {}, "stock_state": "unconfirmed",
                "requires_review": [], "unverified": list(UNVERIFIED_IMPACTS), "refresh_required": list(REFRESH_REQUIRED)}),
            "remote_verified": False, "_attributes": attributes[request.product_id]}
        identity["barcodes"] = sorted(verified_barcodes(row["value"] for row in item["aliases"] if row["type"] in ("ean", "upc")))
        if request.product_id not in products:
            item["reasons"].append("migration_product_missing")
            identity["source_hash"] = digest(identity)
            items.append(item)
            continue
        product, group = products[request.product_id]
        item.update(name=product.name, old_sku=product.sku)
        identity["group_code"] = group
        brand_id = await sku_policy.registered_brand_id(db, request.sku_identity.brand)
        brand = await db.get(SkuBrand, brand_id) if brand_id else None
        if brand:
            item["proposed_sku"] = f"{brand.code}-{request.sku_identity.source_code}"
        result = await sku_policy.preview_sku(db, request.sku_identity, barcodes=identity["barcodes"],
                                              expected_product_id=product.id)
        if result["status"] != "existing" or result["product_id"] != product.id:
            item["reasons"].extend(result["reasons"] or ["migration_identity_unresolved"])
            item["candidate_product_ids"] = result["candidate_product_ids"]
        if group and group.casefold() == "xtrek" or any((mapping["parent_code"] or "").casefold() == "xtrek" for mapping in item["mappings"]):
            item["reasons"].append("migration_pos_separate_review")
        if product.sku.casefold() in parent_codes:
            item["reasons"].append("migration_parent_not_stock_leaf")
        if item["proposed_sku"]:
            collision = occupied[item["proposed_sku"].casefold()] - {product.id}
            if collision:
                item["reasons"].append("migration_target_owned_by_other_card")
                item["candidate_product_ids"] = sorted(set(item["candidate_product_ids"]) | collision)
        for mapping in item["mappings"]:
            mapping["proposed_code"] = item["proposed_sku"]
            if not mapping["_shop_usable"]:
                mapping["reasons"].append("migration_shop_unavailable")
            if not mapping["native_id"]:
                mapping["reasons"].append("migration_native_id_missing")
            if not mapping["old_code"] or mapping["old_code"] != product.sku:
                mapping["reasons"].append("migration_mapping_code_disagrees")
            if mapping["is_variant"] and not mapping["parent_code"]:
                mapping["reasons"].append("migration_parent_missing")
            cache = cached.get(mapping["mapping_id"])
            if cache:
                cached_parent_id = _native(cache["parent_id"])
                if mapping["parent_native_id"] and cached_parent_id and mapping["parent_native_id"] != cached_parent_id:
                    mapping["reasons"].append("migration_cached_parent_conflict")
                if cache["parent_code"] != (mapping["parent_code"] if mapping["is_variant"] else mapping["old_code"]):
                    mapping["reasons"].append("migration_cached_parent_conflict")
                if cache["is_set"] is True or not mapping["is_variant"] and (cache["has_variants"] is True or cache["variant_count"]):
                    item["reasons"].append("migration_parent_not_stock_leaf")
            item["reasons"].extend(mapping["reasons"])
        if item["impacts"]["requires_review"]:
            item["reasons"].append("migration_operational_dependencies")
        identity["source_hash"] = digest({"product_id": product.id, "sku": product.sku, "brand": product.brand,
            "group": group, "is_active": product.is_active, "aliases": item["aliases"], "mappings": item["mappings"],
            "attributes": item["_attributes"], "input": request.sku_identity.model_dump(mode="json"), "impacts": item["impacts"]})
        items.append(item)
    targets = defaultdict(list)
    for item in items:
        if item["proposed_sku"]:
            targets[item["proposed_sku"].casefold()].append(item)
    for group in targets.values():
        if len(group) > 1:
            for item in group:
                item["reasons"].append("migration_batch_target_collision")
                item["candidate_product_ids"] = sorted({row["product_id"] for row in group} | set(item["candidate_product_ids"]))
    if payload.verify_remote:
        await _remote(items)
    for item in items:
        item["remote_verified"] = bool(item["mappings"]) and all(mapping["remote_status"] == "verified" for mapping in item["mappings"])
        for mapping in item["mappings"]:
            item["reasons"].extend(mapping["reasons"])
            for key in list(mapping):
                if key.startswith("_"):
                    del mapping[key]
        item["reasons"] = sorted(set(item["reasons"]))
        item["status"] = "blocked" if item["reasons"] else "unchanged" if item["old_sku"] == item["proposed_sku"] else "review"
        del item["_attributes"]
    result = {"created_at": datetime.now(timezone.utc).isoformat(), "can_apply": False,
              "items": items, "required_checks": list(REQUIRED_CHECKS)}
    result["preview_hash"] = digest(result)
    return result
