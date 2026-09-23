"""AI content is a separate API from feed search and create-only shop import."""
import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.ai_content_models import AiJob, AiRuleVersion
from inventory_hub.ai_content_types import BatchRequest, ContentReview, JobAction, UpdatePreviewRequest, UpdateConfirm, JobFork, PriceReview, PublishRequest, RuleProposal, RuleSave, SelectionRequest
from inventory_hub.database import get_session
from inventory_hub.routers.catalog import CatalogRoute
from inventory_hub.services import ai_content as service, ai_content_rules as rules
from inventory_hub.services import ai_content_existing as existing
from inventory_hub.services.ai_content_existing import ExistingProductRequest
from inventory_hub.services.catalog import CatalogError
from inventory_hub.settings import settings
from inventory_hub.access import ai_access as access


router = APIRouter(prefix="/ai-content", tags=["AI content"], route_class=CatalogRoute)
protected = APIRouter(dependencies=[Depends(access)], route_class=CatalogRoute)
DB = Annotated[AsyncSession, Depends(get_session)]


@router.get("/status")
def status():
    return {"enabled": settings.AI_CONTENT_ENABLED, "key_configured": bool(settings.OPENAI_API_KEY.get_secret_value()),
            "access_configured": len(settings.AI_CONTENT_ACCESS_TOKEN.get_secret_value()) >= 24,
            "model": settings.AI_CONTENT_MODEL, "monthly_limit_usd": str(settings.AI_CONTENT_MONTHLY_USD),
            "job_limit_usd": str(settings.AI_CONTENT_JOB_USD)}


@protected.get("/rules")
async def get_rules(db: DB):
    version = await rules.published(db)
    versions = (await db.scalars(select(AiRuleVersion).order_by(AiRuleVersion.id.desc()).limit(40))).all()
    return {"published_id": version.id, "book": version.book, "used_usd": str(await service.budget(db)),
        "versions": [{"id": v.id, "note": v.note, "origin": v.origin, "created_at": v.created_at} for v in versions]}


@protected.get("/rules/{version_id}")
async def get_version(version_id: int, db: DB):
    version = await db.get(AiRuleVersion, version_id)
    if not version:
        raise CatalogError("ai_version_not_found", "Rule version not found", 404)
    return {"id": version.id, "book": version.book, "note": version.note}


@protected.post("/rules")
async def save_rules(request: RuleSave, db: DB):
    published = await rules.published(db)
    if published.id != request.expected_published:
        raise CatalogError("ai_rules_changed", "Published rules changed; reload before saving", 409)
    version = AiRuleVersion(book=request.book.model_dump(), note=request.note, origin="operator")
    db.add(version)
    await db.flush()
    return {"id": version.id, "book": version.book, "published": False}


@protected.post("/rules/{version_id}/publish")
async def publish_rules(version_id: int, request: PublishRequest, db: DB):
    version = await rules.publish(db, version_id, request.expected_published)
    return {"published_id": version.id, "book": version.book}


@protected.post("/rules/proposal")
async def proposal(request: RuleProposal, db: DB):
    return await service.propose_rule(db, request)


@protected.post("/jobs/{id}/accept-proposal")
async def accept(id: str, request: JobAction, db: DB):
    return await service.accept_proposal(db, await service.get_job(db, id, True), request.expected_revision)


@protected.post("/batches")
async def create_batch(request: BatchRequest, db: DB):
    return {"batch_id": request.request_id.hex, "jobs": await service.create_batch(db, request)}


@protected.get("/existing-products/options")
async def existing_options(db: DB):
    return await existing.choices(db)


@protected.get("/shops/{shop}/parameter-registry")
async def parameter_registry(shop: str):
    from inventory_hub.services.ai_content_upgates import parameter_registry as load_registry
    from inventory_hub.services.upgates import UpgatesClient
    service.catalog_import.shop_config(shop)
    return await asyncio.to_thread(load_registry, shop, UpgatesClient.from_shop(shop))


@protected.post("/existing-products")
async def existing_product(request: ExistingProductRequest, db: DB):
    return {"job": await existing.create(db, request)}


@protected.post("/selection")
async def selection(request: SelectionRequest, db: DB):
    products = await service.selected_products(db, request.supplier, request.feed_key, request.product_ids, request.run_id)
    groups = {}
    for product in products:
        groups.setdefault(service.parent_shop_code(product), []).append(product)
    return [{"code": code, "name": items[0].group_name or items[0].name,
             "image": items[0].images[0] if items[0].images else None,
             "product_ids": [p.id for p in items], "products": sorted(items, key=service.variant_sort_key)}
            for code, items in groups.items()]


@protected.post("/jobs/{id}/prices")
async def prices(id: str, request: PriceReview, db: DB):
    job = await service.get_job(db, id, True)
    service.expect(job, request.expected_revision)
    if job.status not in ("ready", "import_blocked"):
        raise CatalogError("ai_import_state", "Prices can be edited before import only", 409)
    from inventory_hub.catalog_types import ShopImportPreviewRequest
    checked = ShopImportPreviewRequest(supplier=job.context["supplier"], product_ids=job.context["product_ids"],
        sale_price_overrides=request.sale_price_overrides)
    if set(checked.sale_price_overrides) - set(job.context["product_ids"]):
        raise CatalogError("price_override_not_selected", "Prices must belong to the selected family", 422)
    job.context = {**job.context, "sale_price_overrides": checked.model_dump(mode="json")["sale_price_overrides"]}
    job.preview_id = None
    await service.prepare_import(db, job)
    # Price editing always returns a reviewable preview, even for an automatic profile.
    if job.status == "import_queued":
        service.event(job, "ready", "Manual price changes require confirmation of this preview")
    return service.summary(job, detail=True)


@protected.get("/jobs")
async def jobs(db: DB, batch_id: str | None = Query(None, pattern=r"^[a-f0-9]{32}$"),
               limit: int = Query(100, ge=1, le=500), archived: bool = False):
    statement = select(AiJob).order_by(AiJob.created_at.desc(), AiJob.id).limit(limit)
    statement = statement.where(func.coalesce(AiJob.context["archived"].as_boolean(), False) == archived)
    if batch_id:
        statement = statement.where(AiJob.batch_id == batch_id)
    return [service.summary(j) for j in (await db.scalars(statement)).all()]


@protected.get("/jobs/{id}")
async def job(id: str, db: DB):
    return service.summary(await service.get_job(db, id), detail=True)


@protected.post("/jobs/{id}/review")
async def review(id: str, request: ContentReview, db: DB):
    return await service.review(db, await service.get_job(db, id, True), request)


@protected.post("/jobs/{id}/action")
async def action(id: str, request: JobAction, db: DB):
    return await service.action(db, await service.get_job(db, id, True), request)


@protected.post("/jobs/{id}/fork")
async def fork(id: str, request: JobFork, db: DB):
    return await service.fork_job(db, await service.get_job(db, id, True), request)


@protected.post("/jobs/{id}/update-preview")
async def update_preview(id: str, request: UpdatePreviewRequest, db: DB):
    from inventory_hub.services import ai_content_update
    return await ai_content_update.prepare(db, await service.get_job(db, id, True), request)


@protected.post("/jobs/{id}/update-confirm")
async def update_confirm(id: str, request: UpdateConfirm, db: DB):
    from inventory_hub.services import ai_content_update
    return await ai_content_update.confirm(db, await service.get_job(db, id, True), request)


router.include_router(protected)
