"""Persistent, shop-specific content jobs on top of the existing catalog importer."""
from __future__ import annotations

import copy
import hashlib
import json
from collections import OrderedDict
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select, text

from inventory_hub.ai_content_models import AiBatch, AiContentRevision, AiJob, AiRuleVersion
from inventory_hub.ai_content_types import BatchRequest, Content, Policy, RuleBook, Scope
from inventory_hub.catalog_types import CatalogProduct, ShopImportPreviewRequest
from inventory_hub.db_models import Shop
from inventory_hub.services import ai_content_provider as provider, ai_content_rules as rules, catalog_import
from inventory_hub.services.ai_content_validation import text_of, validate_content
from inventory_hub.services.catalog import CatalogError, selected_products
from inventory_hub.services.catalog_identity import parent_shop_code
from inventory_hub.services.catalog_sort import variant_sort_key
from inventory_hub.settings import settings


def now():
    return datetime.now(timezone.utc)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def source_digest(products):
    return digest({"facts": facts(products), "relationships": sorted((p.id, p.group_code, p.variant_relationship) for p in products)})


def facts(products):
    # Explicit allowlist: no credentials, stock, prices, manufacturer contact blocks,
    # raw XML, arbitrary source fields, or full supplier URLs are sent to OpenAI.
    output = []
    for p in sorted(products, key=variant_sort_key):
        output.append({"id": p.id, "code": p.shop_code, "manufacturer_code": p.manufacturer_code,
            "eans": p.eans, "name": p.name, "brand": p.brand, "group_name": p.group_name,
            "description": text_of(p.description), "safety_information": text_of(p.safety_information),
            "parameters": [v.model_dump() for v in p.parameters],
            "variant_attributes": [v.model_dump() for v in p.variant_attributes]})
    return output


def event(job, status: str, note: str):
    job.status = status
    job.revision = (job.revision or 0) + 1
    job.updated_at = now()
    job.events = [*(job.events or []), {"at": now().isoformat(), "status": status, "note": note}]


def summary(job, *, detail=False):
    context = job.context
    data = {"id": job.id, "batch_id": job.batch_id, "kind": job.kind, "status": job.status,
        "revision": job.revision, "shop": context.get("shop"), "supplier": context.get("supplier"),
        "code": context.get("code"), "name": context.get("name"), "image": context.get("image"),
        "product_ids": context.get("product_ids", []), "use_ai": context.get("use_ai", True),
        "update_only": bool(context.get("update_only")), "source_kind": context.get("source_kind", "catalog"),
        "update_state": (context.get("update_preview") or {}).get("state"),
        "rules_version": context.get("rules_version"), "category_profile": context.get("category_profile"),
        "policy": context.get("resolved", {}).get("policy", {}),
        "origins": context.get("resolved", {}).get("origins", {}),
        "estimate_usd": context.get("estimate_usd", "0"), "reserved_usd": str(job.reserved_usd or 0),
        "actual_usd": str(job.actual_usd) if job.actual_usd is not None else None,
        "checks": job.checks, "error": job.error, "preview_id": job.preview_id,
        "created_at": job.created_at, "updated_at": job.updated_at, "archived": bool(context.get("archived"))}
    if detail:
        data.update(applied_rules=context.get("resolved", {}).get("instructions", []),
                    resolved_import_policy=context.get("resolved", {}).get("import_policy", {}),
                    parameter_registry=(context.get("resolved", {}).get("category") or {}).get("parameters", []))
        data.update(output=job.output, facts=context.get("facts", []), events=job.events, update_preview=context.get("update_preview"), update_result=context.get("update_result"),
                    usage=job.usage, options=context.get("options"), research=context.get("research"))
        if job.preview_id:
            try:
                doc = catalog_import._load_job(catalog_import._path(context["shop"], job.preview_id))
                data.update(preview=doc["preview"], import_result=doc.get("result"))
            except CatalogError:
                data["preview_unavailable"] = True
    return data


async def get_job(db, id, lock=False):
    statement = select(AiJob).where(AiJob.id == id)
    if lock:
        statement = statement.with_for_update()
    job = await db.scalar(statement)
    if job is None:
        raise CatalogError("ai_job_not_found", "AI job not found", 404)
    return job


def expect(job, revision):
    if job.revision != revision:
        raise CatalogError("ai_job_changed", "This job changed; reload it before saving", 409)


async def budget(db):
    beginning = now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Uncertain requests retain their reservation; all known usage remains charged.
    used = await db.scalar(select(func.coalesce(func.sum(func.coalesce(AiJob.actual_usd, AiJob.reserved_usd)), 0))
        .where(AiJob.created_at >= beginning))
    return Decimal(used or 0)


async def start(db, job):
    if job.status != "estimate":
        return
    if job.context.get("source_kind") == "shop" and not job.context.get("source_parameters_loaded"):
        raise CatalogError("ai_existing_source_changed", "This older preparation did not capture shop parameters; load the product again before starting AI", 409)
    if job.context.get("use_ai", True):
        if not settings.AI_CONTENT_ENABLED or not settings.OPENAI_API_KEY.get_secret_value():
            raise CatalogError("ai_not_configured", "Enable AI and configure the API key on the server first", 503)
        amount = Decimal(job.context["estimate_usd"])
        if amount > Decimal(str(settings.AI_CONTENT_JOB_USD)):
            raise CatalogError("ai_job_budget", "The estimated cost exceeds the per-job limit", 422)
        await db.execute(text("SELECT pg_advisory_xact_lock(691432106)"))
        if await budget(db) + amount > Decimal(str(settings.AI_CONTENT_MONTHLY_USD)):
            raise CatalogError("ai_monthly_budget", "Monthly AI budget including reserved requests is exhausted", 422)
        job.reserved_usd = amount
        event(job, "queued", "Explicit request accepted; budget reserved")
    else:
        event(job, "preparing_import", "Original supplier content selected")
    await db.flush()


async def create_batch(db, request: BatchRequest):
    batch_id, fingerprint = request.request_id.hex, digest(request.model_dump(mode="json"))
    await db.execute(text("SELECT pg_advisory_xact_lock(691432107)"))
    old = await db.get(AiBatch, batch_id)
    if old:
        if old.request_hash != fingerprint:
            raise CatalogError("ai_request_reused", "Use a new request ID for a changed selection", 409)
        return [summary(j) for j in (await db.scalars(select(AiJob).where(AiJob.batch_id == batch_id).order_by(AiJob.created_at, AiJob.id))).all()]
    published = await rules.published(db)
    book = RuleBook.model_validate(published.book)
    products = await selected_products(db, request.supplier, request.feed_key, request.product_ids, request.run_id)
    groups = OrderedDict()
    for product in products:
        groups.setdefault(parent_shop_code(product), []).append(product)
    if len(groups) * len(request.targets) > 100:
        raise CatalogError("ai_batch_too_large", "Start with at most 100 product/shop jobs per batch", 422)
    db.add(AiBatch(id=batch_id, request_hash=fingerprint))
    await db.flush()
    jobs = []
    for target in request.targets:
        if not await db.scalar(select(Shop.id).where(Shop.code == target.shop, Shop.is_active.is_(True))):
            raise CatalogError("shop_not_found", "Choose an active Hub shop", 404)
        cfg = catalog_import.shop_config(target.shop)
        for code, group in groups.items():
            ids = {p.id for p in group}
            ai_ids = ids & set(request.ai_product_ids)
            if ai_ids and ai_ids != ids:
                raise CatalogError("ai_partial_family", "Apply AI to all selected variants of a family or none; shared descriptions belong to the parent", 422)
            profiles = {request.category_profiles.get(p.id, "general") for p in group}
            if len(profiles) != 1:
                raise CatalogError("ai_family_category_conflict", "Selected variants must share a category profile", 422)
            profile = profiles.pop()
            if profile == "general" and target.options.category_code:
                mapped_profiles = [c.id for c in book.categories if c.shop_categories.get(target.shop) == target.options.category_code]
                if len(mapped_profiles) == 1:
                    profile = mapped_profiles[0]
            resolved = rules.resolve(book, Scope(shop=target.shop, supplier=request.supplier, category=profile,
                brand=group[0].brand or "", product=code), target.policy)
            options = target.options.model_copy()
            mapped = (resolved.get("category") or {}).get("shop_categories", {}).get(target.shop)
            if mapped:
                options.category_code = mapped
            if ai_ids and options.language != "sk":
                raise CatalogError("ai_language_unsupported", "The current approved content rules support Slovak", 422)
            ctx = {"supplier": request.supplier, "feed_key": request.feed_key, "product_ids": sorted(ids),
                "run_id": request.run_id, "shop": target.shop, "target": catalog_import._target(cfg),
                "code": code, "name": group[0].group_name or group[0].name,
                "image": group[0].images[0] if group[0].images else None,
                "source_digest": source_digest(group), "facts": facts(group), "use_ai": bool(ai_ids),
                "rules_version": published.id, "resolved": resolved, "category_profile": profile,
                "options": options.model_dump(mode="json"), "research": request.research,
                "sale_price_overrides": {str(k): v for k, v in request.sale_price_overrides.items() if k in ids},
                "model": settings.AI_CONTENT_MODEL}
            # Validate prices at the existing API boundary, without making any shop calls.
            ShopImportPreviewRequest(supplier=request.supplier, product_ids=sorted(ids), options=options,
                sale_price_overrides=ctx["sale_price_overrides"])
            ctx["estimate_usd"] = str(provider.estimate(ctx) if ai_ids else Decimal(0))
            job = AiJob(id=uuid4().hex, batch_id=batch_id, context=ctx, status="estimate", revision=1,
                        events=[{"at": now().isoformat(), "status": "estimate", "note": "Selection and rule revision frozen"}])
            db.add(job)
            await db.flush()
            if not resolved["policy"]["show_cost_estimate"] or not ai_ids:
                await start(db, job)
            jobs.append(job)
    return [summary(j) for j in jobs]


async def review(db, job, request):
    expect(job, request.expected_revision)
    if (job.context.get("update_preview") or {}).get("state") in ("sending", "uncertain"):
        raise CatalogError("ai_update_uncertain", "Reconcile the previous update first", 409)
    if job.status not in ("review", "blocked", "ready") or not job.context.get("use_ai"):
        raise CatalogError("ai_review_state", "Content cannot be edited in this state", 409)
    checks = validate_content(request.content, job.context, (job.usage or {}).get("opened_sources", []))
    if request.approve and checks["errors"]:
        raise CatalogError("ai_validation_failed", "; ".join(checks["errors"]), 422)
    job.output, job.checks, job.error, job.preview_id = request.content.model_dump(), checks, None, None
    event(job, "preparing_import" if request.approve else "blocked" if checks["errors"] else "review",
          "Human content approval" if request.approve else "Content draft edited")
    job.context = {**job.context, "approval": "human" if request.approve else None, "update_preview":None}
    db.add(AiContentRevision(job_id=job.id, revision=job.revision, content=job.output,
                            decision="human_approved" if request.approve else "draft"))
    return summary(job, detail=True)


async def prepare_import(db, job):
    ctx = job.context
    if catalog_import._target(catalog_import.shop_config(ctx["shop"])) != ctx["target"]:
        raise CatalogError("shop_target_changed", "The shop connection changed; prepare a new batch", 409)
    if ctx.get("update_only"):
        from inventory_hub.services import ai_content_existing
        return await ai_content_existing.approved(db, job)
    products = await selected_products(db, ctx["supplier"], ctx["feed_key"], ctx["product_ids"], None)
    if source_digest(products) != ctx["source_digest"]:
        raise CatalogError("ai_source_changed", "Supplier data changed after AI preparation; prepare a new batch", 409)
    if ctx["use_ai"]:
        if ctx.get("approval") not in ("human", "policy") or not job.output:
            raise CatalogError("ai_approval_required", "Content has not been approved", 409)
        checks = validate_content(Content.model_validate(job.output), ctx, (job.usage or {}).get("opened_sources", []))
        if checks["errors"]:
            raise CatalogError("ai_validation_failed", "; ".join(checks["errors"]), 422)
    enrichment = {"job_id": job.id, "revision": job.revision, "rules_version": ctx["rules_version"],
        "active_after_import": ctx["resolved"]["policy"]["active_after_import"],
        "content": job.output if ctx["use_ai"] else None,
        "supplier_name": ctx["resolved"].get("import_policy", {}).get("supplier_name") or {"paul-lange": "Paul Lange", "northfinder": "Northfinder"}.get(ctx["supplier"], ctx["supplier"]),
        "import_policy": ctx["resolved"].get("import_policy", {}),
        "safety": "\n".join(dict.fromkeys(p.safety_information for p in products if p.safety_information)),
        "registered_parameters": bool((ctx["resolved"].get("category") or {}).get("parameters"))}
    preview = await catalog_import.create_preview(db, ctx["shop"], ShopImportPreviewRequest(
        supplier=ctx["supplier"], feed_key=ctx["feed_key"], product_ids=ctx["product_ids"],
        options=ctx["options"], sale_price_overrides=ctx["sale_price_overrides"]), enrichment=enrichment)
    job.preview_id = preview.preview_id
    if preview.errors or any(i.status == "invalid" for i in preview.items):
        event(job, "import_blocked", "Target preview contains errors; no product sent")
    elif all(i.status == "exists" for i in preview.items):
        event(job, "exists", "Already present in the selected shop")
    else:
        event(job, "ready" if ctx["resolved"]["policy"]["confirm_import"] else "import_queued",
              "Preview ready; awaiting confirmation" if ctx["resolved"]["policy"]["confirm_import"] else "Automatic import authorized by frozen policy")
    return preview


async def action(db, job, request):
    expect(job, request.expected_revision)
    if (job.context.get("update_preview") or {}).get("state") in ("sending", "uncertain"):
        raise CatalogError("ai_update_uncertain", "Reconcile the pending update before changing this job", 409)
    if request.action in ("archive", "restore"):
        if job.status in ("queued", "generating", "preparing_import", "import_queued", "importing"):
            raise CatalogError("ai_job_running", "Wait for the running operation before archiving", 409)
        job.context = {**job.context, "archived": request.action == "archive"}
        event(job, job.status, "Archived from work list" if request.action == "archive" else "Restored to work list")
    elif request.action == "reopen":
        if (job.context.get("update_preview") or {}).get("state") in ("sending", "uncertain"):
            raise CatalogError("ai_update_uncertain", "Reconcile the previous update first", 409)
        if job.status not in ("import_blocked", "exists", "completed", "cancelled") or not job.output:
            raise CatalogError("ai_review_state", "Use reconciliation for an unconfirmed import", 409)
        job.preview_id, job.error = None, None
        job.context = {**job.context, "approval": None, "update_preview": None}
        event(job, "review", "Reopened content for editing")
    elif request.action == "cancel":
        if job.status in ("generating", "importing", "completed", "exists", "uncertain"):
            raise CatalogError("ai_cancel_state", "A running, completed or uncertain operation cannot be cancelled", 409)
        if job.status in ("estimate", "queued"):
            job.reserved_usd = 0
        event(job, "cancelled", "Cancelled by operator")
    elif request.action == "start":
        await start(db, job)
    elif request.action in ("import", "retry_import"):
        if job.context.get("update_only"):
            raise CatalogError("ai_update_only", "This preparation can update an existing product only", 409)
        allowed = ("ready",) if request.action == "import" else ("import_failed", "import_blocked")
        if job.status not in allowed:
            raise CatalogError("ai_import_state", "This job cannot be imported in its current state", 409)
        if request.action == "retry_import" and job.preview_id:
            job.context = {**job.context, "retry_import": True}
        event(job, "import_queued" if job.preview_id else "preparing_import", "Import explicitly requested")
    await db.flush()
    return summary(job, detail=True)


async def propose_rule(db, request):
    await db.execute(text("SELECT pg_advisory_xact_lock(691432107)"))
    id, fingerprint = request.request_id.hex, digest(request.model_dump(mode="json"))
    old = await db.get(AiBatch, id)
    if old:
        if old.request_hash != fingerprint:
            raise CatalogError("ai_request_reused", "Use a new request ID for a changed proposal", 409)
        return summary(await db.scalar(select(AiJob).where(AiJob.batch_id == id)), detail=True)
    version = await rules.published(db)
    entries = version.book["categories" if request.category else "rules"]
    current = next((e for e in entries if e["id"] == request.rule_id), None)
    if current is None:
        raise CatalogError("ai_rule_not_found", "Select a published rule or category first", 404)
    ctx = {"rules_version": version.id, "current": current, "proposal_request": request.request,
           "category": request.category, "use_ai": True, "model": settings.AI_CONTENT_MODEL,
           "name": current["name"], "resolved": {"policy": rules.DEFAULT_POLICY, "origins": {}}}
    ctx["estimate_usd"] = str(provider.estimate(ctx, "rules"))
    db.add(AiBatch(id=id, request_hash=fingerprint))
    await db.flush()
    job = AiJob(id=uuid4().hex, batch_id=id, kind="rules", status="estimate", revision=1, context=ctx)
    db.add(job)
    await db.flush()
    return summary(job, detail=True)


async def accept_proposal(db, job, expected_revision):
    expect(job, expected_revision)
    if job.kind != "rules" or job.status != "review" or not job.output:
        raise CatalogError("ai_proposal_state", "A completed rule proposal is required", 409)
    current = await rules.published(db)
    if current.id != job.context["rules_version"]:
        raise CatalogError("ai_rules_changed", "Published rules changed; review a fresh proposal", 409)
    book = copy.deepcopy(current.book)
    collection = book["categories" if job.context["category"] else "rules"]
    entry = next(e for e in collection if e["id"] == job.context["current"]["id"])
    entry["instructions"] = job.output["instructions"]
    if job.context["category"] and job.output.get("parameters") is not None:
        entry["parameters"] = job.output["parameters"]
    version = AiRuleVersion(book=RuleBook.model_validate(book).model_dump(),
        origin="ai_proposal:" + job.id, note=job.output["reason"][:500] or "AI proposal")
    db.add(version)
    await db.flush()
    event(job, "completed", "Proposal saved as draft; publication still requires an operator")
    return {"version_id": version.id, "book": version.book, "published": False}


async def fork_job(db, job, request):
    expect(job, request.expected_revision)
    if job.context.get("update_only"):
        raise CatalogError("ai_update_only", "Start another preparation from the existing product", 409)
    if (job.context.get("update_preview") or {}).get("state") in ("sending", "uncertain"):
        raise CatalogError("ai_update_uncertain", "Reconcile the pending update first", 409)
    if job.kind != "product" or job.status in ("queued", "generating", "preparing_import", "import_queued", "importing"):
        raise CatalogError("ai_job_running", "Wait for the current operation", 409)
    detail = summary(job, detail=True)
    if (job.status == "import_failed" and not detail.get("import_result")) or any(i["status"] == "uncertain" for i in (detail.get("import_result") or {}).get("items", [])):
        raise CatalogError("import_outcome_unknown", "Reconcile the previous import before preparing another create", 409)
    ids = set(request.product_ids)
    if len(ids) != len(request.product_ids) or not ids <= set(job.context["product_ids"]):
        raise CatalogError("ai_selection_invalid", "Select products from this job", 422)
    ctx = job.context
    req = BatchRequest(request_id=uuid4(), supplier=ctx["supplier"], feed_key=ctx["feed_key"],
        product_ids=sorted(ids), ai_product_ids=sorted(ids) if request.use_ai else [],
        category_profiles={id:ctx["category_profile"] for id in ids},
        targets=[{"shop":ctx["shop"], "options":ctx["options"],
                  "policy":{**ctx["resolved"]["policy"], "show_cost_estimate":True, "review_required":True, "confirm_import":True}}],
        research=ctx["research"], sale_price_overrides={int(k):v for k,v in ctx["sale_price_overrides"].items() if int(k) in ids})
    created = await create_batch(db, req)
    fresh = await get_job(db, created[0]["id"], True)
    if request.reuse_content and request.use_ai and job.output:
        output = copy.deepcopy(job.output)
        output["parameters"] = [p for p in output["parameters"] if p["product_id"] is None or p["product_id"] in ids]
        # Parent text may describe excluded variants: explicit review is mandatory.
        output["evidence"] = [e for e in output["evidence"] if not e["source"].startswith("feed:") or e["source"] in {f"feed:{id}" for id in ids}]
        if not output["evidence"]:
            raise CatalogError("ai_partial_evidence", "Remaining products require a new AI preparation", 422)
        fresh.output, fresh.usage, fresh.actual_usd = output, {"opened_sources":(job.usage or {}).get("opened_sources", [])}, Decimal(0)
        fresh.checks = validate_content(Content.model_validate(output), fresh.context, fresh.usage["opened_sources"])
        event(fresh, "blocked" if fresh.checks["errors"] else "review", "Copied content; check the remaining variants before approval. No AI call.")
    fresh.context = {**fresh.context, "source_job_id":job.id}
    event(job, job.status, "Selected products copied to a new preparation")
    await db.flush()
    return summary(fresh, detail=True)
