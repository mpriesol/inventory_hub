"""Read-only, shop-scoped identity resolution shared by product pulls and order audit.

BIKETREK and xTrek share exact canonical product/variant codes. Explicit shop
mappings take precedence, then a unique exact SKU, then validated EAN/UPC.
Conflicting evidence is reported; existing products and mappings never merge.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.db_models import IdentifierType, Product, ProductIdentifier
from inventory_hub.db_models_ext import ShopProduct
from inventory_hub.services.identifiers import ProductIdentifierService


# Shared with catalog_import.register_created; callers hold it for mapping writes.
IDENTITY_WRITE_LOCK = 643128702193
VERIFIED_BARCODE_TYPES = (IdentifierType.ean, IdentifierType.upc)


def verified_barcodes(values) -> tuple[str, ...]:
    """Preserve exact strings/leading zeros; never promote an unverified barcode."""
    result = []
    for raw in values or ():
        for value, kind in ProductIdentifierService.split_compound_ean(str(raw or "")):
            if value.isascii() and value.strip("0") and kind in VERIFIED_BARCODE_TYPES and value not in result:
                result.append(value)
    return tuple(result)


@dataclass(frozen=True)
class RemoteIdentity:
    shop_id: int
    code: str = ""
    is_variant: bool | None = None
    external_id: str | None = None
    parent_code: str | None = None
    barcodes: tuple[str, ...] = ()


@dataclass
class IdentityResolution:
    status: str
    product_id: int | None = None
    matched_by: str | None = None
    candidate_product_ids: list[int] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def mapping_code(mapping: ShopProduct) -> str:
    # Legacy pull external_code is the parent; catalog writes the variant code.
    # variant_code is the unambiguous leaf in both historical conventions.
    return str((mapping.variant_code if mapping.is_variant else mapping.external_code) or "").strip()


class IdentityIndex:
    def __init__(self, shop_id: int, products=(), mappings=(), identifiers=()):
        self.shop_id = shop_id
        self.products = {row.id: row for row in products}
        self.by_sku = defaultdict(set)
        for row in self.products.values():
            self.by_sku[row.sku.casefold()].add(row.id)
        self.by_code = defaultdict(list)
        self.by_external_id = defaultdict(list)
        self.by_product = defaultdict(list)
        self.by_barcode = defaultdict(set)
        self.product_barcodes = defaultdict(set)
        for mapping in mappings:
            self.add_mapping(mapping)
        for identifier in identifiers:
            if (identifier.identifier_type in VERIFIED_BARCODE_TYPES
                    and verified_barcodes((identifier.value,))):
                self.by_barcode[identifier.value].add(identifier.product_id)
                self.product_barcodes[identifier.product_id].add(identifier.value)

    def add_mapping(self, mapping: ShopProduct) -> None:
        """Index a committed-to-the-current-transaction mapping after a family succeeds."""
        if mapping.shop_id != self.shop_id:
            return
        self.by_product[mapping.product_id].append(mapping)
        code = mapping_code(mapping)
        if code:
            self.by_code[code.casefold()].append(mapping)
        if mapping.external_id and mapping.external_id != "None":
            self.by_external_id[(bool(mapping.is_variant), str(mapping.external_id))].append(mapping)

    def resolve(self, identity: RemoteIdentity) -> IdentityResolution:
        if identity.shop_id != self.shop_id:
            raise ValueError("Identity belongs to another shop")
        code = identity.code.strip()
        codes = self.by_code.get(code.casefold(), []) if code else []
        remote_ids = self.by_external_id.get((identity.is_variant, identity.external_id), []) \
            if identity.external_id and identity.is_variant is not None else []
        mappings = {row.id: row for row in [*codes, *remote_ids]}
        barcodes = set(verified_barcodes(identity.barcodes))
        barcode_ids = set().union(*(self.by_barcode.get(value, set()) for value in barcodes))
        mapped_ids = {row.product_id for row in mappings.values()}
        sku_ids = self.by_sku.get(code.casefold(), set()) if code else set()
        exact_sku_ids = {product_id for product_id in sku_ids if self.products[product_id].sku == code}
        candidates = sorted(mapped_ids | barcode_ids | sku_ids)

        def conflict(reason):
            return IdentityResolution("conflict", candidate_product_ids=candidates, reasons=[reason])

        if len(mappings) > 1:
            return conflict("ambiguous_shop_mapping")
        if len(barcode_ids) > 1:
            return conflict("identifier_conflict")
        if len(sku_ids) > 1:
            return conflict("unmapped_sku_collision")
        if mappings:
            mapping = next(iter(mappings.values()))
            if (identity.is_variant is not None and identity.is_variant != mapping.is_variant
                    or code and mapping_code(mapping) != code
                    or identity.external_id and mapping.external_id not in (None, "", "None", identity.external_id)):
                return conflict("mapping_identity_changed")
            if sku_ids and sku_ids != {mapping.product_id}:
                return conflict("mapping_identifier_conflict")
            if barcode_ids and barcode_ids != {mapping.product_id}:
                return conflict("mapping_identifier_conflict")
            stored_barcodes = self.product_barcodes.get(mapping.product_id, set())
            if barcodes and stored_barcodes and not barcodes.intersection(stored_barcodes):
                return conflict("mapping_identifier_conflict")
            return IdentityResolution("mapped", mapping.product_id, "shop_mapping", [mapping.product_id])
        if sku_ids:
            if exact_sku_ids != sku_ids:
                return conflict("unmapped_sku_collision")
            product_id = next(iter(exact_sku_ids))
            if barcode_ids and barcode_ids != {product_id}:
                return conflict("identifier_conflict")
            stored_barcodes = self.product_barcodes.get(product_id, set())
            if barcodes and stored_barcodes and not barcodes.intersection(stored_barcodes):
                return conflict("identifier_conflict")
            if self.by_product.get(product_id):
                return conflict("local_mapping_conflict")
            return IdentityResolution("identified", product_id, "shared_sku", [product_id])
        if barcode_ids:
            product_id = next(iter(barcode_ids))
            if self.by_product.get(product_id):
                return conflict("local_mapping_conflict")
            return IdentityResolution("identified", product_id, "validated_barcode", [product_id])
        return IdentityResolution("unresolved")


async def load_identity_index(db: AsyncSession, shop_id: int, identities: list[RemoteIdentity]) -> IdentityIndex:
    """Batch reads only: no network, mappings, products or identifiers are written."""
    if any(identity.shop_id != shop_id for identity in identities):
        raise ValueError("All identities must belong to the requested shop")
    mappings = list((await db.execute(select(ShopProduct).where(ShopProduct.shop_id == shop_id))).scalars())
    codes = sorted({identity.code.strip().lower() for identity in identities if identity.code.strip()})
    values = sorted({value for identity in identities for value in verified_barcodes(identity.barcodes)})
    identifiers = []
    products = {}
    for start in range(0, len(values), 500):
        rows = await db.execute(select(ProductIdentifier).where(
            ProductIdentifier.identifier_type.in_(VERIFIED_BARCODE_TYPES),
            ProductIdentifier.value.in_(values[start:start + 500]),
        ))
        identifiers.extend(rows.scalars())
    for start in range(0, len(codes), 500):
        rows = await db.execute(select(Product).where(func.lower(Product.sku).in_(codes[start:start + 500])))
        products.update((row.id, row) for row in rows.scalars())
    missing_ids = sorted(({row.product_id for row in mappings} | {row.product_id for row in identifiers}) - products.keys())
    for start in range(0, len(missing_ids), 500):
        rows = await db.execute(select(Product).where(Product.id.in_(missing_ids[start:start + 500])))
        products.update((row.id, row) for row in rows.scalars())
    # Include SKU-only candidates before loading stored barcodes: an unowned
    # incoming EAN must still conflict with that product's different known EAN.
    product_ids = sorted(products)
    for start in range(0, len(product_ids), 500):
        rows = await db.execute(select(ProductIdentifier).where(
            ProductIdentifier.identifier_type.in_(VERIFIED_BARCODE_TYPES),
            ProductIdentifier.product_id.in_(product_ids[start:start + 500]),
        ))
        identifiers.extend(rows.scalars())
    return IdentityIndex(shop_id, products.values(), mappings, identifiers)
