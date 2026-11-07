"""Current identity projection, deliberately separate from historical totals.

An alias is the only permission to collapse two owner IDs. Inactive unrelated
owners remain visible to conflict checks. Explicit retired IDs are never
silently redirected by a writer or a frozen review.
"""
from sqlalchemy import exists, or_, select
from inventory_hub.db_models import Product
from inventory_hub.product_consolidation_models import ProductCanonicalAlias
from inventory_hub.services.stock_publication_gate import StockPublicationHoldError


class ProductRetiredError(StockPublicationHoldError):
    code = 'product_retired'

    def __init__(self, retired_product_id, canonical_product_id):
        self.retired_product_id = retired_product_id
        self.canonical_product_id = canonical_product_id
        super().__init__()


async def aliases_for(db, product_ids):
    """Include both directions so canonical lookups retain all legacy evidence."""
    ids = sorted(set(product_ids))
    if not ids:
        return {}
    result = {}
    for offset in range(0, len(ids), 500):
        batch = ids[offset:offset+500]
        rows = await db.execute(select(ProductCanonicalAlias.retired_product_id,
            ProductCanonicalAlias.canonical_product_id).where(or_(
                ProductCanonicalAlias.retired_product_id.in_(batch),
                ProductCanonicalAlias.canonical_product_id.in_(batch))))
        result.update(rows.all())
    # One hop is a schema invariant. Expanding siblings is needed when the
    # original input reached only one former owner by its exact SKU/barcode.
    keepers = sorted(set(result.values()) - set(ids))
    for offset in range(0, len(keepers), 500):
        result.update((await db.execute(select(ProductCanonicalAlias.retired_product_id,
            ProductCanonicalAlias.canonical_product_id).where(
                ProductCanonicalAlias.canonical_product_id.in_(keepers[offset:offset+500])))).all())
    return result


async def require_current_products(db, product_ids):
    """Call after the caller's Product lock for mutation safety."""
    ids = sorted(set(product_ids))
    if not ids:
        return
    row = (await db.execute(select(ProductCanonicalAlias.retired_product_id,
        ProductCanonicalAlias.canonical_product_id).where(
            ProductCanonicalAlias.retired_product_id.in_(ids)).order_by(
                ProductCanonicalAlias.retired_product_id).limit(1))).first()
    if row:
        raise ProductRetiredError(*row)


async def canonical_owner_products(db, owner_ids):
    """Resolve scanner evidence before active filtering; preserve foreign owners."""
    redirects = await aliases_for(db, owner_ids)
    ids = {redirects.get(pid, pid) for pid in owner_ids}
    return list(await db.scalars(select(Product).where(Product.id.in_(ids)))) if ids else []


def current_product_condition(product_id=Product.id):
    """Current catalog selection; unrelated inactive owners are not hidden."""
    return ~exists(select(ProductCanonicalAlias.retired_product_id).where(
        ProductCanonicalAlias.retired_product_id == product_id))


async def effective_product_ids(db, product_ids):
    """IDs whose preserved identity facts belong to these current canonical IDs."""
    ids = set(product_ids)
    redirects = await aliases_for(db, ids)
    return ids | {old for old, keeper in redirects.items() if keeper in ids}
