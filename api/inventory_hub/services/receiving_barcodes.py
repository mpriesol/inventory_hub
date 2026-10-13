"""Read-only barcode presentation for invoice lines; never changes identity."""
from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.db_models import SupplierProduct
from inventory_hub.db_models_ext import ReceivingLine
from inventory_hub.services.product_identity import RemoteIdentity, load_identity_index, verified_barcodes


async def line_barcode_display(
    db: AsyncSession, supplier_id: int, lines: list[ReceivingLine], prefix: str,
) -> dict[int, dict]:
    """Prefer invoice data, then an unambiguous product, then this supplier's feed.

    Catalog barcodes are display evidence only: they do not populate the invoice
    EAN, change its fingerprint, bind a product, or register a scanner alias.
    All lookups are batched, including detection of conflicting feed evidence.
    """
    result = {line.id: {"display_ean": line.ean or "",
                        "ean_source": "invoice" if line.ean else None} for line in lines}
    missing = [line for line in lines if not (line.ean or "").strip()]
    if not missing:
        return result
    supplier_skus = sorted({line.supplier_sku.strip() for line in missing if line.supplier_sku})
    catalog = {}
    for start in range(0, len(supplier_skus), 500):
        rows = await db.execute(select(SupplierProduct).where(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.supplier_sku.in_(supplier_skus[start:start + 500]),
            SupplierProduct.is_active.is_(True),
        ))
        catalog.update((row.supplier_sku, verified_barcodes((row.ean,))) for row in rows.scalars())

    identities, probes = {}, {}
    for line in missing:
        sku = (line.supplier_sku or "").strip()
        identity = RemoteIdentity(0, f"{prefix}{sku}" if sku else "",
            supplier_id=supplier_id, supplier_sku=sku, expected_product_id=line.product_id)
        identities[line.id] = identity
        if catalog.get(sku):
            probes[line.id] = replace(identity, barcodes=catalog[sku])
    index = await load_identity_index(db, 0, [*identities.values(), *probes.values()], local=True)
    for line in missing:
        resolution = index.resolve(identities[line.id])
        if resolution.status == "conflict":
            continue
        barcodes = sorted(index.product_barcodes.get(resolution.product_id, ()))
        source = "product"
        if not barcodes and line.id in probes:
            probe = probes[line.id]
            if index.resolve(probe).status != "conflict":
                barcodes = probe.barcodes
                source = "supplier_catalog"
        if barcodes:
            result[line.id] = {"display_ean": "; ".join(barcodes), "ean_source": source}
    return result
