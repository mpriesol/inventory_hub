"""Start a content-only AI job from one current Upgates parent product.

Remote product IDs stay in the shop-source namespace. No catalog rows or IDs are
created, and these jobs must never enter the create-only catalog importer.
"""
from __future__ import annotations

import asyncio
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import Field
from sqlalchemy import select, text

from inventory_hub.ai_content_models import AiBatch, AiJob
from inventory_hub.ai_content_types import Policy, RuleBook, Scope, StrictModel
from inventory_hub.catalog_types import CatalogParameter, CatalogPrices, CatalogProduct, ShopImportOptions
from inventory_hub.db_models import Shop
from inventory_hub.services import ai_content as service, ai_content_rules as rules, catalog_import as imports
from inventory_hub.services.ai_content_validation import text_of
from inventory_hub.services.catalog import CatalogError
from inventory_hub.services.upgates import UpgatesClient
from inventory_hub.settings import settings


class ExistingProductRequest(StrictModel):
    request_id: UUID
    shop: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    code: str = Field(min_length=1, max_length=100)
    supplier: str = Field(default="", pattern=r"^[a-zA-Z0-9_-]{0,50}$")
    brand: str = Field(default="", max_length=100)
    category_profile: str = Field(default="general", pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    category_code: str | None = Field(default=None, max_length=100)
    research: Literal["official", "feed_only"] = "official"


def localized(rows, language):
    return next((d for d in rows or [] if d.get("language") == language), {})


def source_snapshot(remote, language="sk"):
    """Allowlisted factual snapshot: no financial, inventory or secret fields."""
    product_id = remote.get("product_id")
    if isinstance(product_id, bool) or not str(product_id).isdigit() or int(product_id) <= 0:
        raise CatalogError("ai_existing_identity", "The shop response has no valid product ID", 422)
    description = localized(remote.get("descriptions"), language)
    if not description.get("title"):
        raise CatalogError("ai_existing_language", "The product has no title in the selected language", 422)
    manufacturer = remote.get("manufacturer")
    brand = manufacturer if isinstance(manufacturer, str) else (manufacturer or {}).get("name", "")
    parameters = []
    for row in remote.get("parameters") or []:
        name = localized(row.get("descriptions"), language).get("name")
        if name:
            parameters.extend({"name": text_of(name), "value": text_of(value)} for item in row.get("values") or []
                if (value := localized(item.get("descriptions"), language).get("value")))
    return {"product_id": int(product_id), "code": remote.get("code"), "ean": remote.get("ean"),
        "brand": text_of(brand), "descriptions": {key: text_of(description.get(key, "")) for key in
            ("title", "short_description", "long_description", "seo_title", "seo_description")},
        "parameters": parameters,
        "variants": sorted([{"code": v.get("code"), "ean": v.get("ean"),
            "variant_id": v.get("variant_id")} for v in remote.get("variants") or []], key=lambda v: str(v["code"]))}


def assert_source(remote, context):
    if context.get("source_kind") == "shop" and not context.get("source_parameters_loaded"):
        # Old jobs captured an incomplete GET /products response. Never silently
        # treat missing parameters as a trustworthy empty register or advance
        # their frozen baseline: prepare a new source snapshot explicitly.
        raise CatalogError("ai_existing_source_changed", "This older preparation did not capture shop parameters; load the product again before updating", 409)
    snapshot = source_snapshot(remote, context["options"]["language"])
    # A confirmed update advances the comparison baseline while the original
    # facts remain pinned for evidence and audit history.
    if service.digest(snapshot) != context.get("update_source_digest", context["source_digest"]):
        raise CatalogError("ai_existing_source_changed", "Shop content or identity changed; start from the current product again", 409)


def products_from_remote(remote, context):
    snapshot = source_snapshot(remote, context["options"]["language"])
    desc = snapshot["descriptions"]
    return [CatalogProduct(id=snapshot["product_id"], supplier=context.get("supplier", ""),
        feed_key="shop", code=snapshot["code"], shop_code=snapshot["code"],
        eans=[snapshot["ean"]] if snapshot["ean"] else [], name=desc["title"], brand=snapshot["brand"],
        description="\n".join(v for v in (desc["short_description"], desc["long_description"]) if v),
        parameters=[CatalogParameter(**p) for p in snapshot["parameters"]],
        prices=CatalogPrices(currency=context["options"].get("currency", "EUR")))]


async def choices(db):
    shops = (await db.scalars(select(Shop).where(Shop.is_active.is_(True)).order_by(Shop.name))).all()
    return {"shops": [{"code": shop.code, "name": shop.name} for shop in shops if shop.platform == "upgates"]}


def thumbnail(remote):
    for image in remote.get("images") or []:
        url = image.get("url", "") if isinstance(image, dict) else ""
        try:
            parts = urlsplit(url)
            if parts.scheme == "https" and parts.hostname and not (parts.username or parts.password or parts.query or parts.fragment):
                return url
        except ValueError:
            continue
    return None


async def create(db, request: ExistingProductRequest):
    batch_id = request.request_id.hex
    fingerprint = service.digest({"source_kind": "shop", **request.model_dump(mode="json")})
    await db.execute(text("SELECT pg_advisory_xact_lock(691432107)"))
    old = await db.get(AiBatch, batch_id)
    if old:
        if old.request_hash != fingerprint:
            raise CatalogError("ai_request_reused", "Use a new request ID for changed settings", 409)
        job = await db.scalar(select(AiJob).where(AiJob.batch_id == batch_id))
        return service.summary(job, detail=True)
    shop = await db.scalar(select(Shop).where(Shop.code == request.shop, Shop.is_active.is_(True)))
    if shop is None or shop.platform != "upgates":
        raise CatalogError("shop_not_found", "Choose an active Upgates shop", 404)
    cfg = imports.shop_config(request.shop)
    client = UpgatesClient.from_shop(request.shop)
    from inventory_hub.services.ai_content_update import read_product
    remote = await asyncio.to_thread(read_product, client, request.code.strip(), include_parameters=True)
    snapshot = source_snapshot(remote)
    if request.brand and snapshot["brand"] and request.brand.casefold() != snapshot["brand"].casefold():
        raise CatalogError("ai_existing_brand_mismatch", "The chosen brand rule differs from the shop manufacturer", 422)
    published = await rules.published(db)
    book = RuleBook.model_validate(published.book)
    main = next((c.get("code") for c in remote.get("categories") or [] if c.get("main_yn")), None)
    profile = request.category_profile
    if profile == "general" and (request.category_code or main):
        mapped = [c.id for c in book.categories if c.shop_categories.get(request.shop) == (request.category_code or main)]
        if len(mapped) == 1:
            profile = mapped[0]
    # Existing products use an explicit field comparison and confirmation. The
    # published automatic-create switches never authorize updates to live data.
    resolved = rules.resolve(book, Scope(shop=request.shop, supplier=request.supplier,
        brand=request.brand or snapshot["brand"], category=profile, product=snapshot["code"]),
        Policy(review_required=True, show_cost_estimate=True, confirm_import=True))
    if any(p["scope"] == "variant" and p["required"] for p in (resolved.get("category") or {}).get("parameters", [])):
        raise CatalogError("ai_existing_variant_registry", "This entry updates shared content and parent parameters; choose a parent-only category profile", 422)
    mapped = (resolved.get("category") or {}).get("shop_categories", {}).get(request.shop)
    options = ShopImportOptions(category_code=mapped or request.category_code or main)
    ctx = {"source_kind": "shop", "update_only": True, "supplier": request.supplier, "feed_key": "shop",
        "product_ids": [], "run_id": None, "shop": request.shop, "target": imports._target(cfg),
        "code": snapshot["code"], "name": snapshot["descriptions"]["title"], "image": thumbnail(remote),
        "source_snapshot": snapshot, "source_digest": service.digest(snapshot), "source_parameters_loaded": True, "use_ai": True,
        "rules_version": published.id, "resolved": resolved, "category_profile": profile,
        "options": options.model_dump(mode="json"), "research": request.research,
        "sale_price_overrides": {}, "model": settings.AI_CONTENT_MODEL}
    ctx["facts"] = service.facts(products_from_remote(remote, ctx))
    ctx["facts"][0]["source_kind"] = "shop"
    ctx["facts"][0]["existing_seo_title"] = snapshot["descriptions"]["seo_title"]
    ctx["facts"][0]["existing_meta_description"] = snapshot["descriptions"]["seo_description"]
    ctx["facts"][0]["variant_identity"] = snapshot["variants"]
    # The evidence identifier uses the existing schema, but the model is told
    # explicitly that these facts are current shop content, not a supplier feed.
    ctx["resolved"]["instructions"].append({"id": "shop-source", "name": "Existujúci produkt",
        "text": "Facts pochádzajú z aktuálneho produktu e-shopu, nie z dodávateľského feedu. "
        "Cituj ich cez feed:<id> podľa schémy. Existujúci marketingový text nie je nezávislé technické overenie. "
        "Neprenášaj nepodložené tvrdenia; pri oficiálnom výskume over presný model. "
        "Upravuješ spoločný obsah celej existujúcej rodiny, nevymýšľaj rozdiely variantov ani nové varianty. "
        "Z registra vyplň iba parametre s rozsahom parent, pri ktorých product_id=null. Nepovinné parametre variantov vynechaj."})
    ctx["estimate_usd"] = str(service.provider.estimate(ctx))
    db.add(AiBatch(id=batch_id, request_hash=fingerprint))
    await db.flush()
    timestamp = service.now()
    job = AiJob(id=uuid4().hex, batch_id=batch_id, kind="product", context=ctx, status="estimate", revision=1,
        created_at=timestamp, updated_at=timestamp, checks={}, events=[{"at": timestamp.isoformat(),
            "status": "estimate", "note": "Current shop product captured; content-only update requires explicit field confirmation"}])
    db.add(job)
    await db.flush()
    return service.summary(job, detail=True)


async def approved(db, job):
    """Finish content approval without constructing or sending a create request."""
    ctx = job.context
    if not ctx.get("update_only") or ctx.get("source_kind") != "shop":
        raise CatalogError("ai_update_state", "This operation requires an existing-shop job", 409)
    if imports._target(imports.shop_config(ctx["shop"])) != ctx["target"]:
        raise CatalogError("shop_target_changed", "Shop connection changed", 409)
    if ctx.get("approval") not in ("human", "policy") or not job.output:
        raise CatalogError("ai_approval_required", "Approve content before preparing an update", 409)
    from inventory_hub.ai_content_types import Content
    from inventory_hub.services.ai_content_validation import validate_content
    checks = validate_content(Content.model_validate(job.output), ctx, (job.usage or {}).get("opened_sources", []))
    if checks["errors"]:
        raise CatalogError("ai_validation_failed", "; ".join(checks["errors"]), 422)
    job.preview_id = None
    service.event(job, "exists", "Content approved for existing product; choose fields and compare before updating")
    return None
