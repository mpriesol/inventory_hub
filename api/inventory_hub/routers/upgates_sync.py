# inventory_hub/routers/upgates_sync.py
"""
"Stiahnuť z Upgates" — pull products from an Upgates shop into the local DB.

Import scope (per approved design):
- COMPLETE raw product payload (descriptions, images incl. titles, prices,
  metas, SEO, labels, parameters, variants...) is stored losslessly in
  shop_product_content — the vault used later for transferring products to
  another shop (xTrek on Upgates, Atomer export).
- Structured fields initialize NEW canonical products only:
  products.name/brand/weight_g, variant parameters ->
  product_variant_attributes. Remote parent groups remain shop-specific;
  pull never creates or changes canonical ProductGroup membership.
  Per-shop prices, availability and remote quantities remain shop_products
  snapshots; existing canonical product fields are preserved. Consistent,
  previously missing verified EAN/UPC identifiers may be appended.
- Identity uses explicit shop mapping, then unique exact shared SKU, then
  validated EAN/UPC. Contradictory evidence is reported per family.
- NEW products and EXISTING products (update_existing=true): product data
  only. Remote stock remains a shop snapshot, never a physical receipt or
  an acquisition cost. Opening stock needs a separate audited workflow.
- Legacy include_stock=true requests are rejected before any import work.

Endpoints:
  GET  /shops/{shop}/upgates/products/preview
  POST /shops/{shop}/upgates/products/import
       body: {"codes": [...] | "all": true,
              "update_existing": bool = false,
              "include_stock": false}
  GET  /shops/{shop}/upgates/status
"""
from __future__ import annotations

import json
import asyncio
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select, func, text
from sqlalchemy.exc import IntegrityError, DataError
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.database import get_session
from inventory_hub.settings import settings
from inventory_hub.db_models import (
    Product, ProductIdentifier, Shop,
)
from inventory_hub.db_models_ext import (
    ShopProduct, ShopProductContent, ProductVariantAttribute,
    StockBalance,
)
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.services.product_identity import (
    IDENTITY_WRITE_LOCK, RemoteIdentity, load_identity_index, verified_barcodes,
)
from inventory_hub.services.upgates import (
    UpgatesClient, UpgatesError, product_title, variant_params_text,
)

router = APIRouter(prefix="/shops", tags=["upgates-sync"])


# ── Helpers ──────────────────────────────────────────────────────────────

async def _get_shop(db: AsyncSession, shop_code: str) -> Shop:
    result = await db.execute(select(Shop).where(Shop.code == shop_code))
    shop = result.scalar_one_or_none()
    if not shop:
        raise HTTPException(404, detail=f"Shop not found in DB: {shop_code}")
    return shop


# ── Catalog cache ────────────────────────────────────────────────────────
# A full Upgates listing costs ~1 API call per 100 products (17 calls for
# ~1700 products) and API calls are billed. We cache the pulled catalog on
# disk per shop; import and post-import refresh read the cache (0 calls).
# A fresh pull happens only when the cache is older than the TTL or the
# user explicitly asks for it (refresh=true).

PREVIEW_CACHE_TTL_S = 600     # preview reuses cache up to 10 min old
IMPORT_CACHE_TTL_S = 1800     # import reuses cache up to 30 min old


def _cache_path(shop_code: str) -> Path:
    return Path(settings.INVENTORY_DATA_ROOT) / "shops" / shop_code / "cache" / "upgates_products.json"


def _cache_load(shop_code: str) -> Optional[Dict[str, Any]]:
    path = _cache_path(shop_code)
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _cache_save(shop_code: str, products: List[Dict[str, Any]]) -> None:
    path = _cache_path(shop_code)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "pulled_at": datetime.utcnow().isoformat(),
            "products": products,
        }, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _cache_age_s(cache: Dict[str, Any]) -> Optional[int]:
    try:
        pulled = datetime.fromisoformat(cache["pulled_at"])
        return int((datetime.utcnow() - pulled).total_seconds())
    except Exception:
        return None


def _fetch_upgates_products(
    shop_code: str, ttl_s: int, force_refresh: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return (products, meta). meta: source cache/api, pulled_at, cache_age_s."""
    if not force_refresh:
        cache = _cache_load(shop_code)
        if cache:
            age = _cache_age_s(cache)
            if age is not None and age <= ttl_s:
                return cache.get("products") or [], {
                    "source": "cache", "pulled_at": cache.get("pulled_at"), "cache_age_s": age,
                }
    try:
        client = UpgatesClient.from_shop(shop_code)
        products = list(client.iter_products())
    except UpgatesError as e:
        raise HTTPException(502, detail=str(e))
    _cache_save(shop_code, products)
    return products, {"source": "api", "pulled_at": datetime.utcnow().isoformat(), "cache_age_s": 0}


def _product_key(p: Dict[str, Any]) -> str:
    """
    Stable selection key for an Upgates product. Real data contains products
    with an EMPTY parent code whose variants do have codes — key falls back
    to the first variant code so such products can be listed and imported.
    """
    code = str(p.get("code") or "").strip()
    if code:
        return code
    for v in p.get("variants") if isinstance(p.get("variants"), list) else []:
        if isinstance(v, dict) and str(v.get("code") or "").strip():
            return str(v["code"]).strip()
    return ""


def _variant_params(v: Dict[str, Any]) -> List[Tuple[str, str]]:
    """[(name, value), ...] tolerant to per-language value lists."""
    out: List[Tuple[str, str]] = []
    for prm in v.get("parameters") or []:
        if not isinstance(prm, dict):
            continue
        name = prm.get("name")
        if isinstance(name, list):
            name = next((x.get("name") or x.get("value") for x in name if isinstance(x, dict)), None)
        val = prm.get("value")
        if isinstance(val, list):
            val = next((x.get("value") for x in val if isinstance(x, dict) and x.get("value")), None)
        if name and val:
            out.append((str(name)[:100], str(val)[:255]))
    return out


def _main_price(obj: Dict[str, Any]) -> Optional[Decimal]:
    """First price_with_vat found in the prices structure (tolerant)."""
    def walk(node: Any):
        if isinstance(node, dict):
            for key in ("price_with_vat", "price"):
                if key in node and node[key] not in (None, ""):
                    try:
                        return Decimal(str(node[key]))
                    except Exception:
                        pass
            for v in node.values():
                r = walk(v)
                if r is not None:
                    return r
        elif isinstance(node, list):
            for v in node:
                r = walk(v)
                if r is not None:
                    return r
        return None
    return walk(obj.get("prices"))


def _weight_g(obj: Dict[str, Any]) -> Optional[int]:
    w = obj.get("weight")
    try:
        return int(Decimal(str(w))) if w not in (None, "") else None
    except Exception:
        return None


def _to_decimal(val: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(val)) if val not in (None, "") else None
    except Exception:
        return None


def _remote_id(value: Any) -> Optional[str]:
    return str(value).strip() if value not in (None, "", "None", 0) else None


def _remote_barcodes(obj: Dict[str, Any]) -> tuple[str, ...]:
    value = obj.get("ean") or []
    return tuple(str(v) for v in (value if isinstance(value, list) else [value]) if v)


def _pull_families(shop_id: int, products: List[Dict[str, Any]]) -> list[dict]:
    """Preflight the whole remote listing; never silently collapse duplicate codes."""
    families = []
    selection_codes, leaf_codes, ids, barcodes = {}, {}, {}, {}

    def register(index, key, position, reason):
        if not key:
            return
        if key in index:
            families[position]["reasons"].add(reason)
            families[index[key]]["reasons"].add(reason)
        else:
            index[key] = position

    for position, raw in enumerate(products):
        p = raw if isinstance(raw, dict) else {}
        key = _product_key(p)
        family = {"code": key, "payload": p, "leaves": [], "reasons": set()}
        families.append(family)
        variants = p.get("variants") or []
        if not key or len(key) > 100 or not isinstance(variants, list):
            family["reasons"].add("remote_identity_invalid")
            continue
        register(selection_codes, key.casefold(), position, "remote_code_duplicate")
        if variants and p.get("code"):
            register(leaf_codes, str(p["code"]).strip().casefold(), position, "remote_code_duplicate")
        # Parent IDs identify the remote family, even though only variants sell.
        register(ids, (False, _remote_id(p.get("product_id"))) if _remote_id(p.get("product_id")) else None,
                 position, "remote_id_duplicate")
        for obj in variants if variants else [p]:
            if not isinstance(obj, dict):
                family["reasons"].add("remote_identity_invalid")
                continue
            code = str(obj.get("code") or "").strip()
            external_id = _remote_id(obj.get("variant_id") if variants else p.get("product_id"))
            if not code or len(code) > 100 or external_id and len(external_id) > 100:
                family["reasons"].add("remote_identity_invalid")
                continue
            identity = RemoteIdentity(shop_id, code, bool(variants), external_id,
                                      key if variants else None, _remote_barcodes(obj))
            family["leaves"].append((identity, obj))
            register(leaf_codes, code.casefold(), position, "remote_code_duplicate")
            if variants and external_id:
                register(ids, (True, external_id), position, "remote_id_duplicate")
            for barcode in verified_barcodes(identity.barcodes):
                register(barcodes, barcode, position, "remote_barcode_duplicate")
        if not family["leaves"]:
            family["reasons"].add("remote_identity_invalid")
    return families


def _mark_resolved_duplicates(families: list[dict], index) -> None:
    owners = {}
    for family in families:
        for identity, _ in family["leaves"]:
            resolution = index.resolve(identity)
            if resolution.product_id is None:
                continue
            if resolution.product_id in owners:
                family["reasons"].add("local_mapping_conflict")
                owners[resolution.product_id]["reasons"].add("local_mapping_conflict")
            else:
                owners[resolution.product_id] = family


def _family_resolution(family: dict, index) -> tuple[list, Optional[dict]]:
    resolutions = [index.resolve(identity) for identity, _ in family["leaves"]]
    reasons = set(family["reasons"])
    candidates = set()
    for resolution in resolutions:
        reasons.update(resolution.reasons)
        candidates.update(resolution.candidate_product_ids)
    # The database allows one leaf per canonical product in each shop.
    matched = [r.product_id for r in resolutions if r.product_id is not None]
    if len(matched) != len(set(matched)):
        reasons.add("local_mapping_conflict")
    conflict = {"code": family["code"], "reasons": sorted(reasons),
                "candidate_product_ids": sorted(candidates)} if reasons else None
    return resolutions, conflict


@router.get("/{shop_code}/upgates/products/preview")
async def preview_upgates_products(
    shop_code: str,
    refresh: bool = False,
    db: AsyncSession = Depends(get_session),
):
    """Preview new/linkable families and explicit conflicts without database writes."""
    shop = await _get_shop(db, shop_code)
    products, meta = await asyncio.to_thread(_fetch_upgates_products, shop_code,
                                             ttl_s=PREVIEW_CACHE_TTL_S, force_refresh=refresh)
    families = _pull_families(shop.id, products)
    index = await load_identity_index(db, shop.id, [i for f in families for i, _ in f["leaves"]])
    _mark_resolved_duplicates(families, index)
    new_items, conflicts = [], []
    known_count = 0
    for family in families:
        resolutions, conflict = _family_resolution(family, index)
        if conflict:
            conflicts.append(conflict)
            continue
        if all(r.status == "mapped" for r in resolutions):
            known_count += 1
            continue
        p = family["payload"]
        identity_status = "partial" if any(r.status == "mapped" for r in resolutions) else (
            "identified" if any(r.status == "identified" for r in resolutions) else "new")
        new_items.append({
            "key": family["code"], "code": family["code"], "title": product_title(p),
            "manufacturer": p.get("manufacturer") or "", "variants_count": len(p.get("variants") or []),
            "availability": p.get("availability") or "", "stock": p.get("stock"),
            "identity_status": identity_status,
        })
    return {
        "shop": shop_code, "total_in_upgates": len(products), "already_in_db": known_count,
        "without_any_code": sum(not f["code"] for f in families),
        "new_count": len(new_items), "new_products": new_items,
        "conflict_count": len(conflicts), "conflicts": conflicts,
        "catalog_source": meta["source"], "catalog_pulled_at": meta["pulled_at"], "catalog_age_s": meta["cache_age_s"],
    }


@router.post("/{shop_code}/upgates/products/import")
async def import_upgates_products(
    shop_code: str,
    payload: Dict[str, Any] = Body(default={}),
    db: AsyncSession = Depends(get_session),
):
    """Link/create products and refresh per-shop snapshots; never overwrite canonical content or stock."""
    if payload.get("include_stock", False) is not False:
        raise HTTPException(400, detail=("Import produktov nemení fyzický sklad. Pošli 'include_stock': false "
                                       "alebo tento parameter vynechaj. Počiatočné zásoby vyžadujú "
                                       "samostatný overený príjem s nákupnou cenou."))
    wanted = payload.get("codes") or []
    if not isinstance(wanted, list) or any(not isinstance(code, str) for code in wanted):
        raise HTTPException(400, detail="'codes' musí byť zoznam produktových kódov")
    import_all = payload.get("all") is True
    update_existing = payload.get("update_existing") is True
    if not wanted and not import_all and not update_existing:
        raise HTTPException(400, detail="Zadaj 'codes', 'all': true alebo 'update_existing': true")
    shop = await _get_shop(db, shop_code)
    products, _ = await asyncio.to_thread(_fetch_upgates_products, shop_code, ttl_s=IMPORT_CACHE_TTL_S)
    families = _pull_families(shop.id, products)
    # Same transaction lock as catalog registration; do not race mappings across shops.
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": IDENTITY_WRITE_LOCK})
    identities = [i for f in families for i, _ in f["leaves"]]
    index = await load_identity_index(db, shop.id, identities)
    _mark_resolved_duplicates(families, index)
    selected = [f for f in families if import_all or f["code"] in wanted or (
        update_existing and any(index.by_code.get(i.code.casefold()) or
                                index.by_external_id.get((i.is_variant, i.external_id))
                                for i, _ in f["leaves"]))]
    missing = sorted(set(wanted) - {f["code"] for f in families})
    skipped = [{"code": code, "reason": "not found in Upgates"} for code in missing]
    conflicts = []
    stats = {"created_products": 0, "created_variants": 0, "updated_products": 0,
             "linked_products": 0, "content_saved": 0, "stock_initialized": 0, "ean_conflicts": 0}
    now = datetime.utcnow()
    source = f"upgates:{shop_code}"

    for family in selected:
        resolutions, conflict = _family_resolution(family, index)
        if not conflict and all(r.status == "mapped" for r in resolutions) and not update_existing:
            skipped.append({"code": family["code"], "reason": "already mapped (update_existing=false)"})
            continue
        if conflict:
            conflicts.append(conflict)
            skipped.append({"code": family["code"], "reason": ", ".join(conflict["reasons"])})
            continue
        p, code = family["payload"], family["code"]
        variants = bool(p.get("variants"))
        created_rows, mapping_rows, new_identifiers = [], [], []
        linked = 0
        try:
            # Any failed variant rolls back this complete family, including its vault content.
            async with db.begin_nested():
                # A POS parent may contain thousands of unrelated products.
                # Only ShopProduct records channel grouping; existing canonical
                # groups stay intact and new leaves have no inferred group.
                leaf_products = []
                for (identity, obj), resolution in zip(family["leaves"], resolutions):
                    if resolution.product_id is None:
                        title = product_title(p)
                        suffix = variant_params_text(obj) or identity.code
                        product = Product(sku=identity.code, name=(f"{title} – {suffix}" if variants else title or identity.code)[:500],
                                          brand=str(p.get("manufacturer") or "")[:100] or None,
                                          weight_g=_weight_g(obj), group_id=None, created_from_source=source)
                        created_rows.append(product)
                    else:
                        product = index.products[resolution.product_id]
                    leaf_products.append(product)
                if created_rows:
                    db.add_all(created_rows)
                    # Obtain IDs in one batched ORM flush, not once per leaf.
                    await db.flush()
                for (identity, obj), resolution, product in zip(family["leaves"], resolutions, leaf_products):
                    if resolution.product_id is None:
                        params = _variant_params(obj) if variants else []
                        for order, (name, value) in enumerate(params):
                            db.add(ProductVariantAttribute(product_id=product.id, attribute_name=name,
                                                           attribute_value=value, display_order=order))
                    # The shared resolver has approved ownership and existing
                    # barcode evidence. Append missing verified identifiers only;
                    # never replace an existing identifier or its primary flag.
                    for position, barcode in enumerate(verified_barcodes(identity.barcodes)):
                        if barcode not in index.product_barcodes.get(product.id, set()):
                            identifier = ProductIdentifier(
                                product_id=product.id, value=barcode,
                                identifier_type=ProductIdentifierService.classify_barcode(barcode),
                                is_primary=resolution.product_id is None and position == 0,
                            )
                            db.add(identifier)
                            new_identifiers.append(identifier)
                    mappings = index.by_product.get(product.id, [])
                    mapping = mappings[0] if mappings else ShopProduct(shop_id=shop.id, product_id=product.id)
                    if not mappings:
                        db.add(mapping)
                        if resolution.status == "identified":
                            linked += 1
                    # Preserve both legacy external_code conventions on existing mappings.
                    if not mapping.external_code:
                        mapping.external_code = identity.code
                    mapping.variant_code = identity.code if variants else None
                    mapping.parent_code = code if variants else None
                    mapping.is_variant, mapping.is_listed = variants, True
                    if identity.external_id:
                        mapping.external_id = identity.external_id
                    mapping.shop_availability = str(obj.get("availability") or "") or None
                    mapping.shop_stock = _to_decimal(obj.get("stock"))
                    price = _main_price(obj)
                    if price is not None:
                        mapping.shop_price = price
                    mapping.last_pull_at = now
                    mapping_rows.append(mapping)
                content = (await db.execute(select(ShopProductContent).where(
                    ShopProductContent.shop_id == shop.id, ShopProductContent.external_code == code,
                ))).scalar_one_or_none()
                if content is None:
                    db.add(ShopProductContent(shop_id=shop.id, external_code=code, data=p, pulled_at=now))
                else:
                    content.data, content.pulled_at = p, now
                await db.flush()
        except (IntegrityError, DataError):
            conflict = {"code": code, "reasons": ["identity_changed"], "candidate_product_ids": []}
            conflicts.append(conflict)
            skipped.append({"code": code, "reason": "identity_changed"})
            # Savepoint rollback can expire previously mapped ORM rows. Reload only on this exceptional path.
            index = await load_identity_index(db, shop.id, identities)
            continue
        for product in created_rows:
            index.products[product.id] = product
            index.by_sku[product.sku.casefold()].add(product.id)
        for identifier in new_identifiers:
            index.by_barcode[identifier.value].add(identifier.product_id)
            index.product_barcodes[identifier.product_id].add(identifier.value)
        for mapping in mapping_rows:
            index.add_mapping(mapping)
        stats["created_products"] += bool(created_rows)
        stats["created_variants"] += len(created_rows) if variants else 0
        stats["updated_products"] += not bool(created_rows)
        stats["linked_products"] += linked
        stats["content_saved"] += 1

    return {"shop": shop_code, **stats, "ean_conflict_details": [], "skipped": skipped,
            "conflict_count": len(conflicts), "conflicts": conflicts,
            "message": (f"Nové: {stats['created_products']} produktov ({stats['created_variants']} variantov), "
                        f"priradené: {stats['linked_products']}, obnovené záznamy: {stats['updated_products']}, "
                        f"konflikty: {len(conflicts)}; fyzický sklad nezmenený")}


@router.get("/{shop_code}/upgates/status")
async def upgates_connection_status(shop_code: str, db: AsyncSession = Depends(get_session)):
    """Quick connection check (1 item) — verifies credentials and base URL."""
    await _get_shop(db, shop_code)
    try:
        client = UpgatesClient.from_shop(shop_code)
        return client.check_connection()
    except UpgatesError as e:
        raise HTTPException(502, detail=str(e))


# ============================================================================
# PUSH: upload products from local DB into a target shop (BikeTrek / xTrek)
# ============================================================================

SERVER_ASSIGNED_KEYS = {"product_id", "url", "urls", "admin_url", "seo_url"}
VARIANT_SERVER_KEYS = {"variant_id", "product_id", "url"}
REMOTE_STOCK_KEYS = {"stock", "stocks", "stock_increment", "stock_position", "variants_stock"}


def _build_push_payload(content: Dict[str, Any], local_stock: Dict[str, float]) -> Dict[str, Any]:
    """
    Upgates product payload for POST /products, built from the vault content.
    Server-assigned identifiers are stripped; stock values are replaced by
    LOCAL stock (local DB is the source of truth for quantities).
    """
    p = {k: v for k, v in content.items() if k not in SERVER_ASSIGNED_KEYS | REMOTE_STOCK_KEYS}
    code = str(p.get("code") or "")
    if not p.get("variants") and code in local_stock:
        p["stock"] = local_stock[code]
    variants = []
    for v in p.get("variants") or []:
        if not isinstance(v, dict):
            continue
        nv = {k: x for k, x in v.items() if k not in VARIANT_SERVER_KEYS | REMOTE_STOCK_KEYS}
        vcode = str(nv.get("code") or "")
        if vcode in local_stock:
            nv["stock"] = local_stock[vcode]
        variants.append(nv)
    if variants:
        p["variants"] = variants
    return p


def _push_family_has_canonical_codes(data: dict, products: list[Product]) -> bool:
    if not str(data.get("code") or "").strip():
        return False
    leaves = data.get("variants") or [data]
    if not isinstance(leaves, list) or any(not isinstance(leaf, dict) for leaf in leaves):
        return False
    codes = [str(leaf.get("code") or "") for leaf in leaves]
    return bool(codes) and all(codes) and len(codes) == len(set(codes)) and set(codes) == {p.sku for p in products}


@router.post("/{shop_code}/upgates/products/push")
async def push_products_to_shop(
    shop_code: str,
    payload: Dict[str, Any] = Body(default={}),
    db: AsyncSession = Depends(get_session),
):
    """
    Upload selected local products into the target shop via Upgates API
    (POST /products, batches of max 100 per request per API docs).

    Body: {"skus": ["PL-ABC", ...]}
    Content source: shop_product_content vault (pulled from any shop).
    Products without vault content (e.g. created from an invoice and not
    yet listed anywhere) are skipped with a reason. Stock values sent are
    the LOCAL stock balances. Raw request/response of every batch is
    logged to shops/{shop}/logs/ for diagnosis.
    """
    shop = await _get_shop(db, shop_code)
    skus: List[str] = payload.get("skus") or []
    if not isinstance(skus, list) or not skus or any(not isinstance(sku, str) for sku in skus):
        raise HTTPException(400, detail="Zadaj 'skus'")
    selected_skus = set(skus)

    try:
        client = UpgatesClient.from_shop(shop_code)
    except UpgatesError as e:
        raise HTTPException(502, detail=str(e))

    # Resolve SKUs -> parent external codes + collect local stock per sku
    parents: Dict[str, Dict[str, Any]] = {}   # parent_code -> vault data
    local_stock: Dict[str, float] = {}
    skipped: List[Dict[str, str]] = []
    resolved_products: Dict[str, List[Product]] = {}  # parent -> local products

    for sku in skus:
        product = (await db.execute(select(Product).where(Product.sku == sku))).scalar_one_or_none()
        if not product:
            skipped.append({"sku": sku, "reason": "nie je v lokálnej DB"})
            continue
        sp = (await db.execute(select(ShopProduct).where(
            ShopProduct.product_id == product.id).limit(1))).scalar_one_or_none()
        parent = (sp.parent_code or sp.external_code) if sp else None
        if not parent:
            skipped.append({"sku": sku, "reason": "bez obsahu — produkt zatiaľ nie je zalistovaný v žiadnom shope (vznikol z faktúry)"})
            continue
        if parent not in parents:
            content = (await db.execute(select(ShopProductContent).where(
                ShopProductContent.shop_id == sp.shop_id,
                ShopProductContent.external_code == parent).limit(1))).scalar_one_or_none()
            if not content or not isinstance(content.data, dict):
                skipped.append({"sku": sku, "reason": f"chýba uložený obsah pre {parent} — spusti Stiahnuť z Upgates"})
                continue
            parents[parent] = content.data
            # Map ALL local sibling variants of this parent — the push sends
            # the complete product incl. every variant, so every local row
            # must get a target-shop mapping.
            siblings = (await db.execute(
                select(Product)
                .join(ShopProduct, ShopProduct.product_id == Product.id)
                .where(ShopProduct.shop_id == sp.shop_id,
                       (ShopProduct.parent_code == parent) | (ShopProduct.external_code == parent))
                .distinct()
            )).scalars().all()
            resolved_products[parent] = list(siblings)
        if not any(pr.id == product.id for pr in resolved_products.get(parent, [])):
            resolved_products.setdefault(parent, []).append(product)

    # Duplicate-target guard: skip parents already mapped in the target shop
    to_send: List[Dict[str, Any]] = []
    for parent, data in parents.items():
        family_products = resolved_products[parent]
        if any(product.sku not in selected_skus for product in family_products):
            skipped.append({"sku": parent, "reason": "selection_expands_family"})
            continue
        if not _push_family_has_canonical_codes(data, family_products):
            skipped.append({"sku": parent, "reason": "identity_alias_push_blocked"})
            continue
        existing = (await db.execute(select(ShopProduct.id).where(
            ShopProduct.shop_id == shop.id,
            ((ShopProduct.external_code == parent) | (ShopProduct.parent_code == parent) |
             ShopProduct.product_id.in_([product.id for product in family_products]))).limit(1))).scalar_one_or_none()
        if existing is not None:
            skipped.append({"sku": parent, "reason": f"už existuje v shope {shop_code}"})
            continue
        # Every sibling is sent, so every quantity must come from canonical balances.
        for product in family_products:
            bal = (await db.execute(select(func.coalesce(func.sum(StockBalance.qty_on_hand - StockBalance.qty_reserved - StockBalance.qty_quarantined), 0)).where(
                StockBalance.product_id == product.id))).scalar()
            local_stock[product.sku] = float(bal or 0)
        to_send.append(_build_push_payload(data, local_stock))

    now = datetime.utcnow()
    pushed = 0
    batches: List[Dict[str, Any]] = []
    log_dir = Path(settings.INVENTORY_DATA_ROOT) / "shops" / shop_code / "logs"

    for i in range(0, len(to_send), 100):
        batch = to_send[i:i + 100]
        try:
            resp = await asyncio.to_thread(client.post, "products", {"products": batch})
        except UpgatesError as e:
            batches.append({"batch": i // 100 + 1, "sent": len(batch), "error": str(e)})
            continue
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / f"upgates_push_batch{i // 100 + 1}.json").write_text(
                json.dumps({"request": {"products": batch}, "response": resp},
                           ensure_ascii=False, indent=2)[:2_000_000], encoding="utf-8")
        except Exception:
            pass
        msgs = resp.get("messages") or []
        batches.append({"batch": i // 100 + 1, "sent": len(batch), "messages": msgs})
        # Optimistic mapping for sent parents without error messages
        sent_codes = {str(p.get("code")) for p in batch}
        error_codes = {str(m.get("code")) for m in msgs if isinstance(m, dict) and m.get("code")}
        for parent in sent_codes - error_codes:
            for product in resolved_products.get(parent, []):
                sp = (await db.execute(select(ShopProduct).where(
                    ShopProduct.shop_id == shop.id,
                    ShopProduct.product_id == product.id).limit(1))).scalar_one_or_none()
                if sp is None:
                    db.add(ShopProduct(
                        shop_id=shop.id, product_id=product.id,
                        external_code=parent,
                        variant_code=product.sku if product.sku != parent else None,
                        parent_code=parent if product.sku != parent else None,
                        is_variant=product.sku != parent,
                        last_pull_at=now,
                    ))
            pushed += 1
    await db.flush()

    return {
        "shop": shop_code,
        "pushed_products": pushed,
        "skipped": skipped,
        "batches": batches,
        "message": f"Odoslaných {pushed} produktov do {shop_code}"
                   + (f", preskočených {len(skipped)}" if skipped else ""),
    }
