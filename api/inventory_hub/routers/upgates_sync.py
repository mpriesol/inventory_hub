# inventory_hub/routers/upgates_sync.py
"""
"Stiahnuť z Upgates" — pull products from an Upgates shop into the local DB.

Import scope (per approved design):
- COMPLETE raw product payload (descriptions, images incl. titles, prices,
  metas, SEO, labels, parameters, variants...) is stored losslessly in
  shop_product_content — the vault used later for transferring products to
  another shop (xTrek on Upgates, Atomer export).
- Structured fields with a proper home are also written:
  products.name/brand/weight_g, variant parameters ->
  product_variant_attributes (created if missing), EAN ->
  product_identifiers, main image -> product_groups.main_image_url,
  main price -> shop_products.shop_price, availability/stock snapshot ->
  shop_products.
- NEW products: everything, optionally including local stock
  (include_stock, default true) -> INITIAL stock movement + balance.
- EXISTING products (update_existing=true): everything EXCEPT local stock;
  stock is written only with include_stock AND only when the product has
  no stock movements yet (protects the ledger from being overwritten).

Endpoints:
  GET  /shops/{shop}/upgates/products/preview
  POST /shops/{shop}/upgates/products/import
       body: {"codes": [...] | "all": true,
              "update_existing": bool = false,
              "include_stock": bool = true}
  GET  /shops/{shop}/upgates/status
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.database import get_session
from inventory_hub.db_models import (
    Product, ProductGroup, Shop, Warehouse, MovementType, ProductIdentifier,
)
from inventory_hub.db_models_ext import (
    ShopProduct, ShopProductContent, ProductVariantAttribute,
    StockMovement, StockBalance,
)
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.services.upgates import (
    UpgatesClient, UpgatesError, product_title, variant_params_text, first_ean,
)

router = APIRouter(prefix="/shops", tags=["upgates-sync"])


# ── Helpers ──────────────────────────────────────────────────────────────

async def _get_shop(db: AsyncSession, shop_code: str) -> Shop:
    result = await db.execute(select(Shop).where(Shop.code == shop_code))
    shop = result.scalar_one_or_none()
    if not shop:
        raise HTTPException(404, detail=f"Shop not found in DB: {shop_code}")
    return shop


async def _default_warehouse(db: AsyncSession) -> Warehouse:
    result = await db.execute(select(Warehouse).where(Warehouse.is_default == True))  # noqa: E712
    wh = result.scalar_one_or_none()
    if not wh:
        raise HTTPException(500, detail="No default warehouse in DB")
    return wh


def _fetch_upgates_products(shop_code: str) -> List[Dict[str, Any]]:
    try:
        client = UpgatesClient.from_shop(shop_code)
        return list(client.iter_products())
    except UpgatesError as e:
        raise HTTPException(502, detail=str(e))


async def _known_skus(db: AsyncSession, skus: List[str]) -> set:
    known: set = set()
    CHUNK = 500
    for i in range(0, len(skus), CHUNK):
        chunk = skus[i:i + CHUNK]
        result = await db.execute(select(Product.sku).where(Product.sku.in_(chunk)))
        known.update(r[0] for r in result.all())
    return known


def _all_codes_of(p: Dict[str, Any]) -> List[str]:
    """Product code + all variant codes (these are our products.sku)."""
    codes = []
    for v in p.get("variants") or []:
        if isinstance(v, dict) and v.get("code"):
            codes.append(str(v["code"]))
    if p.get("code"):
        codes.append(str(p["code"]))
    return codes


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


def _main_image_url(p: Dict[str, Any]) -> Optional[str]:
    images = p.get("images") or []
    for img in images:
        if isinstance(img, dict) and img.get("main_yn") and img.get("url"):
            return str(img["url"])
    for img in images:
        if isinstance(img, dict) and img.get("url"):
            return str(img["url"])
    return None


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


async def _add_identifier_safe(
    db: AsyncSession,
    identifier_service: ProductIdentifierService,
    product_id: int,
    value: str,
    is_primary: bool = False,
) -> bool:
    """
    Add an identifier without ever poisoning the outer transaction.

    Real shop data contains duplicate EANs (e.g. the same EAN on two colour
    variants). EAN/UPC values are globally unique in our DB, so a plain
    insert would raise and abort the whole import transaction. We pre-check
    the exact value and additionally guard the insert with a SAVEPOINT
    (begin_nested), so any residual conflict rolls back only this one insert.
    Returns True if added, False if skipped.
    """
    value = (value or "").strip()
    if not value:
        return False
    existing = (await db.execute(
        select(ProductIdentifier.id).where(ProductIdentifier.value == value).limit(1)
    )).scalar_one_or_none()
    if existing is not None:
        return False
    try:
        async with db.begin_nested():
            await identifier_service.add_identifier(product_id, value, is_primary=is_primary)
        return True
    except Exception:
        return False


# ── Endpoints ────────────────────────────────────────────────────────────

@router.get("/{shop_code}/upgates/products/preview")
async def preview_upgates_products(
    shop_code: str,
    db: AsyncSession = Depends(get_session),
):
    """Compare Upgates products with local DB; list products not yet imported."""
    await _get_shop(db, shop_code)
    upgates_products = _fetch_upgates_products(shop_code)

    all_skus: List[str] = []
    for p in upgates_products:
        all_skus.extend(_all_codes_of(p))
    known = await _known_skus(db, all_skus)

    new_items: List[Dict[str, Any]] = []
    known_count = 0
    for p in upgates_products:
        codes = _all_codes_of(p)
        if not codes:
            continue
        if any(c in known for c in codes):
            known_count += 1
            continue
        new_items.append({
            "code": str(p.get("code") or ""),
            "title": product_title(p),
            "manufacturer": p.get("manufacturer") or "",
            "variants_count": len(p.get("variants") or []),
            "availability": p.get("availability") or "",
            "stock": p.get("stock"),
        })

    return {
        "shop": shop_code,
        "total_in_upgates": len(upgates_products),
        "already_in_db": known_count,
        "new_count": len(new_items),
        "new_products": new_items,
    }


@router.post("/{shop_code}/upgates/products/import")
async def import_upgates_products(
    shop_code: str,
    payload: Dict[str, Any] = Body(default={}),
    db: AsyncSession = Depends(get_session),
):
    """Import/update products from Upgates. See module docstring for scope."""
    shop = await _get_shop(db, shop_code)
    warehouse = await _default_warehouse(db)

    wanted_codes = payload.get("codes") or []
    import_all = bool(payload.get("all"))
    update_existing = bool(payload.get("update_existing", False))
    include_stock = bool(payload.get("include_stock", True))
    if not wanted_codes and not import_all and not update_existing:
        raise HTTPException(400, detail="Zadaj 'codes', 'all': true alebo 'update_existing': true")

    upgates_products = _fetch_upgates_products(shop_code)
    by_code = {str(p.get("code")): p for p in upgates_products if p.get("code")}

    all_skus: List[str] = []
    for p in upgates_products:
        all_skus.extend(_all_codes_of(p))
    known = await _known_skus(db, all_skus)

    def is_known(p: Dict[str, Any]) -> bool:
        return any(c in known for c in _all_codes_of(p))

    if import_all:
        selected = list(by_code.values())
        missing: List[str] = []
    else:
        selected = [by_code[c] for c in wanted_codes if c in by_code]
        missing = [c for c in wanted_codes if c not in by_code]
        if update_existing:
            chosen = {str(p.get("code")) for p in selected}
            selected += [p for p in by_code.values() if is_known(p) and str(p.get("code")) not in chosen]

    identifier_service = ProductIdentifierService(db)
    now = datetime.utcnow()
    source = f"upgates:{shop_code}"

    stats = {"created_products": 0, "created_variants": 0, "updated_products": 0,
             "content_saved": 0, "stock_initialized": 0, "ean_conflicts": 0}
    ean_conflicts: List[Dict[str, str]] = []
    skipped: List[Dict[str, str]] = [{"code": c, "reason": "not found in Upgates"} for c in missing]

    async def _save_content(parent_code: str, p: Dict[str, Any]) -> None:
        existing = (await db.execute(select(ShopProductContent).where(
            ShopProductContent.shop_id == shop.id,
            ShopProductContent.external_code == parent_code,
        ))).scalar_one_or_none()
        if existing:
            existing.data = p
            existing.pulled_at = now
        else:
            db.add(ShopProductContent(shop_id=shop.id, external_code=parent_code, data=p, pulled_at=now))
        await db.flush()
        stats["content_saved"] += 1

    async def _upsert_attributes(product_id: int, params: List[Tuple[str, str]]) -> None:
        if not params:
            return
        await db.execute(delete(ProductVariantAttribute).where(
            ProductVariantAttribute.product_id == product_id))
        for order, (name, value) in enumerate(params):
            db.add(ProductVariantAttribute(
                product_id=product_id, attribute_name=name,
                attribute_value=value, display_order=order))
        await db.flush()

    async def _init_stock(product: Product, stock_val: Any, unit_price: Optional[Decimal]) -> bool:
        """INITIAL movement + balance. Only when the product has no movements yet."""
        qty = _to_decimal(stock_val)
        if qty is None or qty <= 0:
            return False
        cnt = (await db.execute(select(func.count()).where(
            StockMovement.product_id == product.id))).scalar() or 0
        if cnt:
            skipped.append({"code": product.sku,
                            "reason": "stock not imported — product already has stock movements"})
            return False
        balance = (await db.execute(select(StockBalance).where(
            StockBalance.product_id == product.id,
            StockBalance.warehouse_id == warehouse.id))).scalar_one_or_none()
        if balance is None:
            balance = StockBalance(product_id=product.id, warehouse_id=warehouse.id)
            db.add(balance)
            await db.flush()
        avg = unit_price if unit_price is not None else Decimal("0")
        movement = StockMovement(
            idempotency_key=f"upgates-init:{shop.id}:{product.id}",
            product_id=product.id, warehouse_id=warehouse.id,
            movement_type=MovementType.INITIAL, quantity=qty,
            unit_cost=unit_price,
            reference_type="upgates_import", reference_id=product.sku,
            reference_source=source,
            balance_after=qty, avg_cost_after=avg, created_by="upgates_import",
        )
        db.add(movement)
        await db.flush()
        balance.qty_on_hand = qty
        balance.avg_cost = avg
        balance.total_value = qty * avg
        balance.last_movement_at = now
        balance.last_movement_id = movement.id
        stats["stock_initialized"] += 1
        return True

    async def _upsert_shop_product(product: Product, parent_code: str,
                                   variant_code: Optional[str], obj: Dict[str, Any]) -> None:
        sp = (await db.execute(select(ShopProduct).where(
            ShopProduct.shop_id == shop.id, ShopProduct.product_id == product.id,
        ))).scalar_one_or_none()
        if sp is None:
            sp = ShopProduct(shop_id=shop.id, product_id=product.id)
            db.add(sp)
        sp.external_code = parent_code
        sp.variant_code = variant_code
        sp.parent_code = parent_code if variant_code else None
        sp.is_variant = variant_code is not None
        sp.shop_availability = str(obj.get("availability") or "") or None
        sp.shop_stock = _to_decimal(obj.get("stock"))
        price = _main_price(obj)
        if price is not None:
            sp.shop_price = price
        sp.last_pull_at = now
        await db.flush()

    async def _upsert_product_row(
        sku: str, name: str, brand: str, group_id: Optional[int],
        obj: Dict[str, Any], parent_code: str, variant_code: Optional[str],
        params: List[Tuple[str, str]],
    ) -> Tuple[Optional[Product], bool]:
        if len(sku) > 100:
            skipped.append({"code": sku[:100], "reason": "SKU longer than 100 chars — skipped"})
            return None, False
        product = (await db.execute(select(Product).where(Product.sku == sku))).scalar_one_or_none()
        created = product is None
        if created:
            product = Product(sku=sku, created_from_source=source)
            db.add(product)
        # Content fields refresh on both create and update (Upgates owns content)
        product.name = (name or sku)[:500]
        product.brand = (brand or product.brand or "")[:100] or None
        if group_id:
            product.group_id = group_id
        w = _weight_g(obj)
        if w is not None:
            product.weight_g = w
        await db.flush()

        ean = first_ean(obj)
        if ean:
            added = await _add_identifier_safe(db, identifier_service, product.id, ean, is_primary=created)
            if not added:
                stats["ean_conflicts"] += 1
                if len(ean_conflicts) < 100:
                    ean_conflicts.append({"sku": sku, "ean": ean})
        await _upsert_attributes(product.id, params)
        await _upsert_shop_product(product, parent_code, variant_code, obj)
        return product, created

    for p in selected:
        code = str(p.get("code"))
        title = product_title(p)
        brand = str(p.get("manufacturer") or "")
        known_product = is_known(p)

        if known_product and not update_existing and not import_all:
            # explicit codes may include known ones; without update_existing skip
            skipped.append({"code": code, "reason": "already in DB (update_existing=false)"})
            continue
        if known_product and import_all and not update_existing:
            skipped.append({"code": code, "reason": "already in DB (update_existing=false)"})
            continue

        await _save_content(code, p)
        variants = [v for v in (p.get("variants") or []) if isinstance(v, dict) and v.get("code")]

        if variants:
            group = (await db.execute(select(ProductGroup).where(
                ProductGroup.code == code))).scalar_one_or_none()
            if not group:
                group = ProductGroup(code=code, name=title[:500], brand=(brand or "")[:100] or None)
                db.add(group)
                await db.flush()
            group.name = title[:500]
            group.brand = (brand or group.brand or "")[:100] or None
            img = _main_image_url(p)
            if img:
                group.main_image_url = img

            created_any = False
            for v in variants:
                vcode = str(v["code"])
                params = _variant_params(v)
                ptxt = variant_params_text(v)
                vname = f"{title} – {ptxt}" if ptxt else f"{title} – {vcode}"
                product, created = await _upsert_product_row(
                    sku=vcode, name=vname, brand=brand, group_id=group.id,
                    obj=v, parent_code=code, variant_code=vcode, params=params,
                )
                if product is None:
                    continue
                if created:
                    stats["created_variants"] += 1
                    created_any = True
                if include_stock:
                    await _init_stock(product, v.get("stock"), _main_price(v) or _main_price(p))
            if created_any:
                stats["created_products"] += 1
            elif known_product:
                stats["updated_products"] += 1
        else:
            product, created = await _upsert_product_row(
                sku=code, name=title, brand=brand, group_id=None,
                obj=p, parent_code=code, variant_code=None, params=[],
            )
            if product is None:
                continue
            if created:
                stats["created_products"] += 1
            else:
                stats["updated_products"] += 1
            if include_stock:
                await _init_stock(product, p.get("stock"), _main_price(p))

    msg = (
        f"Nové: {stats['created_products']} produktov "
        f"({stats['created_variants']} variantov), "
        f"aktualizované: {stats['updated_products']}, "
        f"sklad inicializovaný: {stats['stock_initialized']}"
    )
    if stats["ean_conflicts"]:
        msg += (
            f" · ⚠ {stats['ean_conflicts']} duplicitných EAN preskočených "
            f"(EAN patrí inému produktu — oprav v Upgates)"
        )
    return {
        "shop": shop_code,
        **stats,
        "ean_conflict_details": ean_conflicts,
        "skipped": skipped,
        "message": msg,
    }


@router.get("/{shop_code}/upgates/status")
async def upgates_connection_status(shop_code: str, db: AsyncSession = Depends(get_session)):
    """Quick connection check (1 item) — verifies credentials and base URL."""
    await _get_shop(db, shop_code)
    try:
        client = UpgatesClient.from_shop(shop_code)
        return client.check_connection()
    except UpgatesError as e:
        raise HTTPException(502, detail=str(e))
