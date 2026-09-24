"""One durable parent-target claim shared by AI, manual and FIFO cost publishers.

Call immediately before recording a sending intent and commit that intent in
the same transaction. The shared identity lock serializes the publication checks
and claim; sending/uncertain rows keep the fence after the transaction exits.
"""
from sqlalchemy import func, select, text

from inventory_hub.ai_content_models import AiJob
from inventory_hub.db_models import Shop
from inventory_hub.product_editor_models import ProductEditorPublication
from inventory_hub.stock_sync_models import StockSyncItem, StockSyncSettings
from inventory_hub.fifo_cost_models import FifoCostPublication
from inventory_hub.services.catalog import CatalogError
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK


async def require_target_available(db, shop_code, parent_code, *, publication_id=None, ai_job_id=None, fifo_publication_id=None, availability=False):
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": IDENTITY_WRITE_LOCK})
    if availability:
        owned = await db.scalar(select(StockSyncSettings.shop_id).join(Shop, Shop.id == StockSyncSettings.shop_id)
            .where(Shop.code == shop_code, StockSyncSettings.authorized.is_(True)).limit(1))
        pending_stock = await db.scalar(select(StockSyncItem.id).join(Shop, Shop.id == StockSyncItem.shop_id)
            .where(Shop.code == shop_code, StockSyncItem.status.in_(("sending", "uncertain"))).limit(1))
        if owned is not None or pending_stock is not None:
            raise CatalogError("ai_availability_managed_by_stock",
                "Stock synchronization owns this shop's availability. Exclude availability from the AI update.", 409)
    # Parent scope is deliberately conservative: a POS umbrella is not a
    # canonical product family, but concurrent PUTs still share its remote row.
    publications = select(ProductEditorPublication.id).join(Shop, Shop.id == ProductEditorPublication.shop_id).where(
        Shop.code == shop_code,
        func.lower(ProductEditorPublication.document["identity"]["parent_code"].astext) == parent_code.lower(),
        ProductEditorPublication.state.in_(("sending", "uncertain")),
    )
    if publication_id is not None:
        publications = publications.where(ProductEditorPublication.id != publication_id)
    jobs = select(AiJob.id).where(
        AiJob.context["shop"].astext == shop_code,
        func.lower(AiJob.context["code"].astext) == parent_code.lower(),
        AiJob.context["update_preview"]["state"].astext.in_(("sending", "uncertain")),
    )
    if ai_job_id is not None:
        jobs = jobs.where(AiJob.id != ai_job_id)
    fifo_costs = select(FifoCostPublication.id).join(Shop, Shop.id == FifoCostPublication.shop_id).where(
        Shop.code == shop_code, FifoCostPublication.kind == "product",
        func.lower(FifoCostPublication.document["identity"]["parent_code"].astext) == parent_code.lower(),
        FifoCostPublication.status.in_(("sending", "uncertain")),
    )
    if fifo_publication_id is not None:
        fifo_costs = fifo_costs.where(FifoCostPublication.id != fifo_publication_id)
    if (await db.scalar(publications.limit(1)) is not None or await db.scalar(jobs.limit(1)) is not None
            or await db.scalar(fifo_costs.limit(1)) is not None):
        raise CatalogError("merchandising_target_inflight",
            "Another update of this shop product is sending or unresolved. Resolve its original request first.", 409)


async def require_availability_authority_available(db, shop_code):
    """Claim stock ownership before any shop/warehouse locks; commit with settings."""
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": IDENTITY_WRITE_LOCK})
    pending = await db.scalar(select(AiJob.id).where(
        AiJob.context["shop"].astext == shop_code,
        AiJob.context["update_preview"]["fields"].contains(["availability"]),
        AiJob.context["update_preview"]["state"].astext.in_(("sending", "uncertain")),
    ).limit(1))
    if pending is not None:
        raise CatalogError("stock_sync_ai_availability_inflight",
            "An AI availability update is sending or unresolved. Resolve its original request before assigning stock authority.", 409)
