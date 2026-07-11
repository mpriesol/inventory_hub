# inventory_hub/routers/upgates_sync.py
"""
"Stiahnuť z Upgates" — pull products from an Upgates shop into the local DB.

Flow (per approved design):
1. GET  /shops/{shop}/upgates/products/preview
   Fetches all products from the Upgates API, compares with the local DB
   and returns which product codes are NEW (not yet in DB).
2. POST /shops/{shop}/upgates/products/import  {"codes": [...]} or {"all": true}
   Imports the selected products:
   - product with variants -> ProductGroup + one Product row PER VARIANT
     (the scanner reads variant EANs, so a variant is our stock unit)
   - simple product -> one Product row
   - provenance: created_from_source = 'upgates:{shop}'
   - EANs -> product_identifiers
   - shop_products row per Product (external/variant/parent code, pulled
     availability/stock snapshot, last_pull_at)
   Descriptions/prices stay owned by Upgates — we import identity only.

Local stock_balances are NOT touched: local DB is the source of truth for
our stock; the Upgates STOCK value is stored only as shop_products.shop_stock
(pulled snapshot).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.database import get_session
from inventory_hub.db_models import Product, ProductGroup, Shop, IdentifierType
from inventory_hub.db_models_ext import ShopProduct
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
    """Product code + all variant codes (these become our products.sku)."""
    codes = []
    variants = p.get("variants") or []
    if variants:
        for v in variants:
            if isinstance(v, dict) and v.get("code"):
                codes.append(str(v["code"]))
    if p.get("code"):
        codes.append(str(p["code"]))
    return codes


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
        variants = p.get("variants") or []
        new_items.append({
            "code": str(p.get("code") or ""),
            "title": product_title(p),
            "manufacturer": p.get("manufacturer") or "",
            "variants_count": len(variants),
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
    """
    Import selected products from Upgates into the local DB.
    Body: {"codes": ["CODE1", ...]} or {"all": true}.
    Idempotent: already-imported codes are skipped and reported.
    """
    shop = await _get_shop(db, shop_code)
    wanted_codes = payload.get("codes") or []
    import_all = bool(payload.get("all"))
    if not wanted_codes and not import_all:
        raise HTTPException(400, detail="Zadaj 'codes' alebo 'all': true")

    upgates_products = _fetch_upgates_products(shop_code)
    by_code = {str(p.get("code")): p for p in upgates_products if p.get("code")}

    if import_all:
        selected = list(by_code.values())
        missing: List[str] = []
    else:
        selected, missing = [], []
        for c in wanted_codes:
            if c in by_code:
                selected.append(by_code[c])
            else:
                missing.append(c)

    identifier_service = ProductIdentifierService(db)
    now = datetime.utcnow()
    source = f"upgates:{shop_code}"

    imported_products = 0
    imported_variants = 0
    skipped: List[Dict[str, str]] = [{"code": c, "reason": "not found in Upgates"} for c in missing]

    async def _create_product_row(
        sku: str, name: str, brand: str,
        group_id: Optional[int],
        ean: str,
        parent_code: Optional[str],
        variant_code: Optional[str],
        availability: str,
        stock_val: Any,
        price_val: Any = None,
    ) -> bool:
        existing = (await db.execute(select(Product).where(Product.sku == sku))).scalar_one_or_none()
        if existing:
            return False
        product = Product(
            sku=sku,
            name=(name or sku)[:500],
            brand=(brand or None),
            group_id=group_id,
            created_from_source=source,
        )
        db.add(product)
        await db.flush()
        if ean:
            try:
                await identifier_service.add_identifier(product.id, ean, is_primary=True)
            except Exception:
                pass  # EAN attached to another product — leave for validation
        try:
            stock_dec = Decimal(str(stock_val)) if stock_val not in (None, "") else None
        except Exception:
            stock_dec = None
        db.add(ShopProduct(
            shop_id=shop.id,
            product_id=product.id,
            external_code=parent_code or sku,
            variant_code=variant_code,
            parent_code=parent_code,
            is_variant=variant_code is not None,
            shop_availability=(availability or None),
            shop_stock=stock_dec,
            last_pull_at=now,
        ))
        await db.flush()
        return True

    for p in selected:
        code = str(p.get("code"))
        title = product_title(p)
        brand = str(p.get("manufacturer") or "")
        variants = [v for v in (p.get("variants") or []) if isinstance(v, dict) and v.get("code")]

        if variants:
            group = (await db.execute(select(ProductGroup).where(ProductGroup.code == code))).scalar_one_or_none()
            if not group:
                group = ProductGroup(code=code, name=title[:500], brand=brand or None)
                db.add(group)
                await db.flush()
            created_any = False
            for v in variants:
                vcode = str(v["code"])
                ptxt = variant_params_text(v)
                vname = f"{title} – {ptxt}" if ptxt else f"{title} – {vcode}"
                created = await _create_product_row(
                    sku=vcode, name=vname, brand=brand, group_id=group.id,
                    ean=first_ean(v), parent_code=code, variant_code=vcode,
                    availability=str(v.get("availability") or ""),
                    stock_val=v.get("stock"),
                )
                if created:
                    imported_variants += 1
                    created_any = True
            if created_any:
                imported_products += 1
            else:
                skipped.append({"code": code, "reason": "all variants already in DB"})
        else:
            created = await _create_product_row(
                sku=code, name=title, brand=brand, group_id=None,
                ean=first_ean(p), parent_code=None, variant_code=None,
                availability=str(p.get("availability") or ""),
                stock_val=p.get("stock"),
            )
            if created:
                imported_products += 1
            else:
                skipped.append({"code": code, "reason": "already in DB"})

    return {
        "shop": shop_code,
        "imported_products": imported_products,
        "imported_variants": imported_variants,
        "skipped": skipped,
        "message": f"Importovaných {imported_products} produktov ({imported_variants} variantov) z Upgates",
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
