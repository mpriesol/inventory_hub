"""One reviewed leaf identity across creation paths; no stock or shop writes.

The registry normalizes brand spelling only. Source codes remain exact text.
Callers serialize creation with IDENTITY_WRITE_LOCK and commit their own unit of
work. Preview is read-only; recording a proof never renames an existing card.
"""
from hashlib import sha256
import json

from sqlalchemy import select, text

from inventory_hub.db_models import Product, Supplier
from inventory_hub.sku_policy_models import ProductSkuIdentity, SkuBrand, SkuBrandAlias
from inventory_hub.sku_policy_types import BrandAliasCreate, BrandCreate, SkuIdentityInput
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK, RemoteIdentity, load_identity_index, verified_barcodes


class SkuPolicyError(Exception):
    def __init__(self, code, status=409, *, reasons=(), candidate_product_ids=()):
        super().__init__(code)
        self.code = code
        self.status = status
        self.reasons = list(reasons) or [code]
        self.candidate_product_ids = list(candidate_product_ids)


def brand_key(value):
    """Case/spacing aliases only; punctuation is never silently discarded."""
    return " ".join(value.casefold().split())


def identity_key(brand_id, item):
    code = item.source_code.zfill(14) if item.code_kind == "gtin" else item.source_code
    scope = item.supplier_id if item.code_kind == "supplier" else None
    return sha256(json.dumps([brand_id, item.code_kind, scope, code], ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")).hexdigest()


async def _brand(db, name):
    return await db.scalar(select(SkuBrand).join(SkuBrandAlias, SkuBrandAlias.brand_id == SkuBrand.id)
                           .where(SkuBrandAlias.alias_key == brand_key(name)))


async def registered_brand_id(db, name):
    brand = await _brand(db, name)
    return brand.id if brand else None


async def _brand_result(db, brand):
    aliases = list((await db.scalars(select(SkuBrandAlias.alias).where(SkuBrandAlias.brand_id == brand.id)
                                   .order_by(SkuBrandAlias.alias))).all())
    return {"id": brand.id, "code": brand.code, "name": brand.name, "kind": brand.kind, "aliases": aliases}


async def list_brands(db):
    brands = list((await db.scalars(select(SkuBrand).order_by(SkuBrand.name, SkuBrand.id))).all())
    aliases = list((await db.scalars(select(SkuBrandAlias).order_by(SkuBrandAlias.alias))).all())
    return {"brands": [{"id": row.id, "code": row.code, "name": row.name, "kind": row.kind,
                        "aliases": [alias.alias for alias in aliases if alias.brand_id == row.id]}
                       for row in brands]}


async def create_brand(db, item: BrandCreate, *, actor="operator-token"):
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": IDENTITY_WRITE_LOCK})
    aliases = {brand_key(value): value for value in [item.name, *item.aliases]}
    if await db.scalar(select(SkuBrand.id).where(SkuBrand.code == item.code)):
        raise SkuPolicyError("sku_brand_code_taken")
    if await db.scalar(select(SkuBrandAlias.brand_id).where(SkuBrandAlias.alias_key.in_(aliases))):
        raise SkuPolicyError("sku_brand_alias_taken")
    brand = SkuBrand(code=item.code, name=item.name, kind=item.kind, source_ref=item.source_ref, created_by=actor)
    db.add(brand)
    await db.flush()
    db.add_all([SkuBrandAlias(alias_key=key, brand_id=brand.id, alias=value,
                             source_ref=item.source_ref, created_by=actor) for key, value in aliases.items()])
    await db.flush()
    return await _brand_result(db, brand)


async def add_brand_alias(db, brand_id, item: BrandAliasCreate, *, actor="operator-token"):
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": IDENTITY_WRITE_LOCK})
    brand = await db.get(SkuBrand, brand_id)
    if not brand:
        raise SkuPolicyError("sku_brand_not_found", 404)
    key = brand_key(item.alias)
    existing = await db.get(SkuBrandAlias, key)
    if existing and existing.brand_id != brand.id:
        raise SkuPolicyError("sku_brand_alias_taken")
    if not existing:
        db.add(SkuBrandAlias(alias_key=key, brand_id=brand.id, alias=item.alias,
                             source_ref=item.source_ref, created_by=actor))
        await db.flush()
    return await _brand_result(db, brand)


def _result(status, *, sku=None, product_id=None, reasons=(), candidates=(), brand_id=None):
    return {"status": status, "sku": sku, "product_id": product_id, "reasons": list(reasons),
            "candidate_product_ids": sorted(set(candidates)), "brand_id": brand_id}


async def preview_sku(db, item: SkuIdentityInput | None, *, barcodes=(), supplier_id=None,
                      supplier_sku="", expected_product_id=None, sku="", item_type="stock"):
    """Resolve current identity first; allocate only a documented concrete leaf.

    A missing policy input can find existing cards but cannot allocate a new
    code. Brand namespaces generate the prefix; source evidence remains scoped.
    Optional verified proofs are checked, never inferred from a feed field name.
    """
    if item is not None and not item.leaf_confirmed and (barcodes or expected_product_id or supplier_id and supplier_sku):
        # An incomplete new-code proposal must not make an already identified
        # card depend on brand registration or a missing manufacturer code.
        # Only independent current relationships are used, never a guessed SKU.
        existing_identity = RemoteIdentity(0, barcodes=tuple(barcodes), supplier_id=supplier_id,
            supplier_sku=supplier_sku, expected_product_id=expected_product_id)
        existing_index = await load_identity_index(db, 0, [existing_identity], local=True)
        existing_resolution = existing_index.resolve(existing_identity)
        if existing_resolution.status == 'conflict':
            return _result('conflict', reasons=existing_resolution.reasons,
                           candidates=existing_resolution.candidate_product_ids)
        existing_product = existing_index.products.get(existing_resolution.product_id)
        if existing_product is not None:
            return _result('existing', sku=existing_product.sku, product_id=existing_product.id,
                           candidates=[existing_product.id])
    brand = None
    claims = []
    key = None
    claim_owner = None
    proposed = sku
    if item is not None:
        brand = await _brand(db, item.brand)
        if brand is None:
            return _result("blocked", reasons=["sku_brand_unregistered"])
        proposed = f"{brand.code}-{item.source_code}"
        if (brand.kind == "brand" and item.code_kind not in ("manufacturer", "supplier", "gtin", "service")
                or brand.kind != "brand" and item.code_kind != brand.kind):
            return _result("blocked", sku=proposed, brand_id=brand.id, reasons=["sku_namespace_kind_mismatch"])
        if item_type != "service" and (brand.kind == "service" or item.code_kind == "service"):
            return _result("blocked", sku=proposed, brand_id=brand.id, reasons=["sku_item_type_conflict"])
        if item.code_kind == "supplier":
            if not item.supplier_id or supplier_id and item.supplier_id != supplier_id:
                return _result("blocked", sku=proposed, brand_id=brand.id, reasons=["sku_supplier_scope_required"])
            if await db.get(Supplier, item.supplier_id) is None:
                return _result("blocked", sku=proposed, brand_id=brand.id, reasons=["sku_supplier_not_found"])
            supplier_id = item.supplier_id
            supplier_sku = supplier_sku or item.source_code
        if item.code_kind == "gtin" and item.source_code not in verified_barcodes((item.source_code,)):
            return _result("blocked", sku=proposed, brand_id=brand.id, reasons=["sku_gtin_invalid"])
        if item.code_kind == "gtin":
            barcodes = (*barcodes, item.source_code)
        key = identity_key(brand.id, item)
        claims = list((await db.scalars(select(ProductSkuIdentity).where(
            ProductSkuIdentity.brand_id == brand.id,
            ProductSkuIdentity.source_code_fold == item.source_code.casefold()))).all())
        # The exact proof key also covers equivalent, validated GTIN padding.
        claim = await db.scalar(select(ProductSkuIdentity).where(ProductSkuIdentity.identity_key == key))
        if claim is not None:
            claim_owner = claim.product_id
        if any(row.source_code != item.source_code and row.code_kind != "gtin" for row in claims):
            return _result("conflict", sku=proposed, brand_id=brand.id,
                           reasons=["sku_source_case_collision"], candidates=[row.product_id for row in claims])
        if expected_product_id and claim_owner and expected_product_id != claim_owner:
            return _result("conflict", sku=proposed, brand_id=brand.id,
                           reasons=["sku_verified_identity_conflict"], candidates=[expected_product_id, claim_owner])
    identity = RemoteIdentity(0, proposed, barcodes=tuple(barcodes), supplier_id=supplier_id,
                              supplier_sku=supplier_sku, expected_product_id=expected_product_id or claim_owner)
    index = await load_identity_index(db, 0, [identity], local=True)
    resolution = index.resolve(identity)
    if resolution.status == "conflict":
        return _result("conflict", sku=proposed or None, brand_id=brand.id if brand else None,
                       reasons=resolution.reasons, candidates=resolution.candidate_product_ids)
    product = index.products.get(resolution.product_id)
    if product is not None:
        if brand:
            stored_brands = set((await db.scalars(select(ProductSkuIdentity.brand_id).where(
                ProductSkuIdentity.product_id == product.id))).all())
            if stored_brands - {brand.id}:
                return _result("conflict", sku=proposed, brand_id=brand.id,
                               reasons=["sku_brand_conflict"], candidates=[product.id])
        if brand and product.brand and brand_key(item.brand) != brand_key(product.brand):
            stored_brand = await _brand(db, product.brand)
            if stored_brand is None or stored_brand.id != brand.id:
                return _result("conflict", sku=proposed, brand_id=brand.id,
                               reasons=["sku_brand_conflict"], candidates=[product.id])
        if brand and item.code_kind == "manufacturer":
            known_codes = list((await db.scalars(select(ProductSkuIdentity).where(
                ProductSkuIdentity.product_id == product.id,
                ProductSkuIdentity.brand_id == brand.id,
                ProductSkuIdentity.code_kind == "manufacturer"))).all())
            if any(row.source_code != item.source_code for row in known_codes):
                return _result("conflict", sku=proposed, brand_id=brand.id,
                               reasons=["sku_manufacturer_code_conflict"], candidates=[product.id])
        if brand and (claim_owner != product.id or any(row.identity_key != key for row in claims)):
            # A computed code can collide with a historical supplier prefix.
            # Require independent scoped/GTIN/assigned evidence when the exact
            # proof is not already owned, not computed SKU equality alone.
            independent = RemoteIdentity(0, barcodes=tuple(barcodes), supplier_id=supplier_id,
                supplier_sku=supplier_sku, expected_product_id=expected_product_id or claim_owner)
            proof_index = await load_identity_index(db, 0, [independent], local=True)
            proof = proof_index.resolve(independent)
            if proof.status == "conflict" or proof.product_id != product.id:
                return _result("conflict", sku=proposed, brand_id=brand.id,
                               reasons=["sku_source_scope_collision"], candidates=[product.id, *proof.candidate_product_ids])
        return _result("existing", sku=product.sku, product_id=product.id,
                       candidates=[product.id], brand_id=brand.id if brand else None)
    if item is None:
        return _result("blocked", reasons=["sku_identity_required"])
    # Supplier aliases and GTINs can still resolve an existing card above. They
    # never supply the manufacturer product code for a newly allocated SKU.
    if item_type != "service":
        if brand.kind != "brand" or item.code_kind != "manufacturer":
            return _result("blocked", brand_id=brand.id, reasons=["sku_manufacturer_code_required"])
        if not item.leaf_confirmed:
            return _result("blocked", brand_id=brand.id, reasons=["sku_manufacturer_code_unverified"])
    return _result("new", sku=proposed, brand_id=brand.id)


async def record_sku_identity(db, product: Product, item: SkuIdentityInput, *, actor="operator-token"):
    """Append proof for this card under the shared lock, keeping its current SKU."""
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": IDENTITY_WRITE_LOCK})
    result = await preview_sku(db, item, expected_product_id=product.id,
                               item_type=getattr(product, "item_type", "stock"))
    if result["status"] != "existing" or result["product_id"] != product.id:
        raise SkuPolicyError("sku_identity_conflict", reasons=result["reasons"],
                             candidate_product_ids=result["candidate_product_ids"])
    # Choosing an operational code is not verification of a manufacturer code.
    # Preserve explicit historical proofs; ordinary allocation uses aliases/FKs.
    if not item.leaf_confirmed:
        return None
    key = identity_key(result["brand_id"], item)
    existing = await db.scalar(select(ProductSkuIdentity).where(ProductSkuIdentity.identity_key == key))
    if existing is not None:
        if existing.product_id != product.id:
            raise SkuPolicyError("sku_verified_identity_conflict", candidate_product_ids=[existing.product_id, product.id])
        return existing
    record = ProductSkuIdentity(product_id=product.id, brand_id=result["brand_id"], source_code=item.source_code,
        source_code_fold=item.source_code.casefold(),
        code_kind=item.code_kind, supplier_id=item.supplier_id if item.code_kind == "supplier" else None,
        identity_key=key, source_ref=item.source_ref, brand_raw=item.brand,
        allocated_sku=product.sku, created_by=actor)
    db.add(record)
    await db.flush()
    return record
