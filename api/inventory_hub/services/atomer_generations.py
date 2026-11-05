"""Keep two manufacturer generations under an existing xTrek parent without reusing a physical leaf."""
import asyncio
from collections import Counter
from decimal import Decimal
from io import BytesIO
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlsplit
import zlib

from lxml import etree
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from sqlalchemy import func, select, text

from inventory_hub.adapters import atomer_content
from inventory_hub.db_models import Product, ProductIdentifier
from inventory_hub.product_editor_models import AtomerVariantRepair
from inventory_hub.services import atomer_family_batch as batch, atomer_review as work, atomer_variant_repair as repair
from inventory_hub.services import product_publication_source as publication, stock_publication_source as transport
from inventory_hub.services.atomer_review_targets import ReviewError
from inventory_hub.services.product_identity import barcode_keys, verified_barcodes
from inventory_hub.services.identifiers import ProductIdentifierService

AXIS = "Generácia"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Existing(Strict):
    code: repair.Token
    variant_id: int = Field(gt=0)
    ean: repair.Token
    axes: repair.Axis = Field(min_length=1, max_length=20)
    generation: repair.Token


class Addition(Strict):
    code: repair.Token
    ean: repair.Token
    revision: int = Field(ge=0)
    names: dict[repair.Token, repair.Token] = Field(min_length=1, max_length=20)
    generation: repair.Token


class Family(Strict):
    parent: repair.Token
    product_id: int = Field(gt=0)
    family_revision: int = Field(ge=0)
    evidence: HttpUrl
    existing: list[Existing] = Field(min_length=1, max_length=60)
    rows: list[Addition] = Field(min_length=1, max_length=20)


class Selection(Strict):
    run_id: int = Field(gt=0)
    source_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    phase: Literal["label", "extend"]
    families: list[Family] = Field(min_length=1, max_length=10)


def require(condition, code):
    if not condition:
        raise ReviewError(code)


def identity(family):
    anchor = family.existing[0]
    return dict(product_id=family.product_id, parent_code=family.parent, code=anchor.code,
                variant_code=anchor.code, variant_id=anchor.variant_id)


def parameters(axes):
    return [dict(descriptions=[dict(language="sk", name=name)],
                 values=[dict(descriptions=[dict(language="sk", value=value)])]) for name, value in axes.items()]


def key(axes):
    return tuple(sorted((" ".join(n.split()).casefold(), " ".join(v.split()).casefold()) for n,v in axes.items()))


def plan_existing(family, observation, phase):
    native = {leaf["variant_id"]: leaf for leaf in observation["variants"]}
    require(set(native) == {v.variant_id for v in family.existing} and len(native) == len(family.existing),
            "generation_family_changed")
    result, updates = {}, []
    for item in family.existing:
        leaf = native[item.variant_id]
        axes = repair.axis_values(leaf)
        desired = {**item.axes, AXIS: item.generation}
        require(AXIS not in item.axes and leaf["code"] == item.code and leaf.get("ean") == item.ean
                and axes in ([desired] if phase == "extend" else [item.axes, desired]), "generation_leaf_changed")
        result[item.code] = desired
        if axes != desired:
            updates.append(dict(code=item.code, parameters=parameters(desired)))
    return result, updates


def load_source(raw, selection):
    parsed = atomer_content.parse_selected(BytesIO(zlib.decompress(raw)), [f.parent for f in selection.families])
    require(parsed["sha256"] == selection.source_sha, "variant_source_changed")
    return {parent:{row["code"]:row for row in g["rows"]} for parent,g in parsed["groups"].items()}


async def allowed(db, family, item, run, claimed=False):
    detail = await db.scalar(text("SELECT detail FROM atomer_progress_items WHERE run_id=:run AND code=:code AND state='review'"),
                             dict(run=run, code=item.code))
    require(detail and set(detail["reasons"]) == {"occupied_parameters", "ean_differs"}
            and detail["ean"] == item.ean and item.ean in verified_barcodes([item.ean])
            and detail["parent"] == detail["target_parent"] == family.parent
            and detail["product_id"] == str(family.product_id) and detail["hub_id"] is None
            and any(e.code == detail["target_code"] and str(e.variant_id) == detail["variant_id"]
                    and e.ean == detail["target_ean"] for e in family.existing), "generation_source_changed")
    rec = await work._record(db, "item", item.code)
    group = await work._record(db, "family", detail["family_key"])
    require(rec["revision"] == item.revision + int(claimed)
            and rec["status"] in (("working",) if claimed else ("open","blocked"))
            and rec["scope"] != "biketrek" and not rec["instruction"] and group["status"] == "open"
            and group["scope"] != "biketrek" and group["revision"] == family.family_revision, "review_changed")
    return detail


async def absent_card(db, item):
    owner = await db.scalar(select(Product.id).where(func.lower(Product.sku) == item.code.casefold()).limit(1))
    barcode = await db.scalar(select(ProductIdentifier.product_id).where(
        ProductIdentifier.identifier_type.in_(ProductIdentifierService.BARCODE_TYPES),
        func.ltrim(ProductIdentifier.value,"0") == item.ean.lstrip("0")).limit(1))
    require(owner is None and barcode is None, "generation_card_appeared")


def native_preflight(selection, since, fingerprint):
    """The pinned run excludes competing identities. Check every changed product, including parent SKUs."""
    client = transport._client("xtrek", fingerprint)
    try:
        changes = batch.collect_pages(client, "products", "products", {"last_update_time_from":since.isoformat(timespec="seconds")})
        codes = {r.code.casefold() for f in selection.families for r in f.rows}
        gtins = {k for f in selection.families for r in f.rows for k in barcode_keys([r.ean])}
        for parent in changes:
            for leaf in [parent, *(parent.get("variants") or [])]:
                require((leaf.get("code") or "").casefold() not in codes and not gtins.intersection(barcode_keys([leaf.get("ean")])),
                        "generation_identity_appeared")
        options = publication._options(client)
        require(options["prices_with_vat"] and options["currency"] == "EUR", "generation_price_configuration")
        return options
    finally:
        client.session.close()


def source_axes(node, item):
    attrs = {p.tag:(p.text or "").strip() for p in node.findall("VARIANT_PARAMETERS/*")}
    require(set(attrs) == set(item.names) and len(set(item.names.values())) == len(attrs)
            and AXIS not in item.names.values() and all(attrs.values()), "generation_source_axes_changed")
    return {**{item.names[k]:v for k,v in attrs.items()}, AXIS:item.generation}


def image_paths(node):
    urls = [node.findtext("MAIN_IMAGE"), *[v.text for v in node.findall("ALTER_IMAGES/IMAGE")]]
    paths = []
    for url in dict.fromkeys(v for v in urls if v):
        u = urlsplit(url)
        require(u.scheme=="https" and u.hostname in ("xtrek.sk","www.xtrek.sk") and not u.username and not u.password
                and not u.query and not u.fragment and u.path.startswith("/user/") and "/upload/stuff/" in u.path,
                "generation_source_image")
        paths.append(url)
    return paths


def new_leaf(row, item, evidence, parent, old_leaf, axes, options):
    node = etree.fromstring(row["raw_xml"].encode(), parser=etree.XMLParser(resolve_entities=False, no_network=True))
    state = evidence["source_state"]
    require(row["ean"] == item.ean and row["parent_code"] == parent["code"]
            and row["manufacturer"].casefold() == (parent.get("manufacturer") or "").casefold()
            and state["product_origin"] == state["variant_origin"] == "admin_csv"
            and isinstance(state["variant"], bool) and parent["active_yn"] is state["product"]
            and (node.findtext("VISIBILITY") or "").strip() == str(int(state["variant"]))
            and not parent.get("archived_yn"), "generation_visibility_changed")
    require(all(values and all(str(v)=="0" for v in values.values()) for values in
                [state["product_flags"],state["variant_flags"]]) and not parent.get("action_currently_yn"),
            "generation_flags_require_mapping")
    axes_new = source_axes(node, item)
    require({k:v for k,v in axes_new.items() if k!=AXIS} == {k:v for k,v in axes.items() if k!=AXIS}
            and axes_new[AXIS]!=axes[AXIS], "generation_occupied_axes_changed")
    vat = publication.money(node.findtext("VAT"))
    old_prices = [p for p in old_leaf.get("prices",[]) if p.get("language") == "sk" and p.get("currency")=="EUR"]
    require(len(old_prices)==1 and publication.money(old_prices[0]["vat"])==vat, "generation_vat_changed")
    gross = publication.money(node.findtext("PRICE_VAT"))
    common = publication.money(node.findtext("PRICE_OLD") or "0")
    weight = publication.money(node.findtext("WEIGHT"))
    availability = (node.findtext("AVAILABILITY") or "").strip()
    require(gross>0 and availability and len(availability)<=80, "generation_price_or_availability_unknown")
    prices = [{"language":"sk","price_common":float(common),
               "pricelists":[{"name":options["pricelist"],"price_original":float(gross)}]}]
    images = image_paths(node)
    payload = dict(code=item.code, ean=item.ean, active_yn=state["variant"], weight=float(weight),
                   prices=prices, stock=0, availability=availability, parameters=parameters(axes_new))
    if images:
        payload["images"] = [{"url":url, "position":i+1, "main_yn":i==0} for i,url in enumerate(images)]
    expected = dict(axes=axes_new, ean=item.ean, visible=state["variant"], gross=str(gross), common=str(common),
                    weight=str(weight), availability=availability, pricelist=options["pricelist"], images=images)
    return payload, expected


def inspect_new(leaf, expected):
    if leaf.get("ean")!=expected["ean"] or leaf.get("active_yn") is not expected["visible"]:
        return "EAN alebo viditeľnosť"
    if repair.axis_values(leaf)!=expected["axes"]:
        return "parametre vrátane generácie"
    if (leaf.get("availability") or "").casefold()!=expected["availability"].casefold():
        return "dostupnosť "+str(leaf.get("availability"))+" (Atomer "+expected["availability"]+")"
    prices = [p for p in leaf.get("prices") or [] if p.get("language")=="sk" and p.get("currency")=="EUR"]
    lists = [p for group in prices for p in group.get("pricelists") or [] if p.get("name")==expected["pricelist"]]
    if (len(prices)!=1 or len(lists)!=1 or publication.money(lists[0].get("price_with_vat"))!=Decimal(expected["gross"])
            or publication.money(prices[0].get("price_common"))!=Decimal(expected["common"])):
        return "cena"
    if publication.money(leaf.get("weight")) != Decimal(expected["weight"]):
        return "hmotnosť"
    if transport._quantity(leaf.get("stock"))!="0":
        return "počiatočná zásoba novej karty"
    if leaf.get("action_currently_yn") or any(label.get("active_currently_yn") for label in leaf.get("labels") or []):
        return "príznaky variantu"
    if expected["images"]:
        actual = leaf.get("images") or []
        for i,url in enumerate(expected["images"]):
            name = PurePosixPath(urlsplit(url).path).name
            matches = [img for img in actual if PurePosixPath(urlsplit(img.get("url") or "").path).name in (name,)
                       or (img.get("url") or "").endswith("-"+name)]
            if len(matches)!=1 or i==0 and not matches[0].get("main_yn"):
                return "obrázok "+name
    return ""


def observe(doc, obs):
    protected, axes = repair.projection(obs)
    original = doc["before_protected"]
    existing = {str(v["variant_id"]):v for v in doc["family"]["existing"]}
    require(protected["parent"]==original["parent"] and all(protected["leaves"].get(vid)==original["leaves"][vid]
            for vid in existing), "generation_protected_field_changed")
    actual = {v["code"]:v for v in obs["variants"]}
    for vid, old in existing.items():
        require(axes[vid]==doc["expected_axes"][old["code"]], "generation_axis_not_confirmed")
    require(set(actual)=={v["code"] for v in existing.values()}|set(doc["additions"]), "generation_native_members_differ")
    ids, issues = {}, []
    for code, expected in doc["additions"].items():
        leaf = actual[code]
        ids[code] = leaf["variant_id"]
        problem = inspect_new(leaf,expected)
        if problem:
            issues.append(f"{code} / {leaf['variant_id']} – {problem}")
    return ids, issues


async def finish(db, job, observation, actor, acknowledged=False, upstream=""):
    doc = job.document
    try:
        ids, issues = observe(doc, observation)
        state = "uncertain" if issues else "verified"
        reason = ", ".join(issues) if issues else ""
    except (ReviewError,transport.SourceError) as err:
        ids = {v["code"]:v["variant_id"] for v in observation["variants"] if v["code"] in doc["additions"]}
        unchanged = repair.projection(observation)==(doc["before_protected"],doc["before_axes"])
        state = "blocked" if acknowledged and unchanged else "uncertain"
        reason = str(err.code)
    if upstream and state!="verified":
        reason = (reason+" · "+upstream)[:1800]
    job.state = state
    job.document = {**doc,"observed_ids":ids,"result":reason}
    for item in doc["family"]["rows"]:
        saved = await work._record(db,"item",item["code"])
        if saved["status"]!="working" or saved["revision"]!=item["revision"]+1:
            continue
        if state=="uncertain":
            continue
        label = doc["phase"]=="label"
        if state=="verified" and label:
            msg = "Generácia je overená na pôvodných variantoch xTrek "+str(doc["identity"]["product_id"])+". Čaká vytvorenie nového SKU "+item["code"]+"."
        elif state=="verified":
            msg = f"xTrek {doc['identity']['product_id']}/{ids[item['code']]}, {item['code']}, EAN {item['ean']}, Generácia {item['generation']}. Existujúce varianty overené. "+doc["family"]["evidence"]
        else:
            msg = "Upgates neuskutočnil opravu generácie: "+reason
        await repair.outcome(db,item["code"],actor,"blocked" if label or state=="blocked" else "open",msg,"generation_"+state)
    return {"status":state,"count":len(doc["family"]["rows"]),"reason":reason,"ids":ids}


def send(products, fingerprint):
    client = transport._client("xtrek", fingerprint)
    try:
        response = transport._request(client,"put","products",payload={"products":products})
        messages = []
        for obj in [response, *(response.get("products") or [])]:
            for nested in [obj, *(obj.get("variants") or [])]:
                for msg in nested.get("messages") or []:
                    if isinstance(msg,dict) and msg.get("level")!="info":
                        messages.append(str(msg.get("message") or msg.get("property") or "Upgates")[:200])
        return " / ".join(dict.fromkeys(messages))[:1200]
    finally:
        client.session.close()


def doc_id(selection, family):
    return repair.digest(dict(operation="variant_generation",phase=selection.phase,run=selection.run_id,
                              source=selection.source_sha,family=family.model_dump(mode="json")))


async def process(db, selection, mode, actor):
    families = selection.families
    additions = [r for f in families for r in f.rows]
    require(len({f.product_id for f in families})==len(families) and len({f.parent.casefold() for f in families})==len(families)
            and len({r.code.casefold() for r in additions})==len(additions)
            and len({r.ean.lstrip("0") for r in additions})==len(additions)
            and sum(len(f.existing)+len(f.rows)+1 for f in families)<=100
            and all(f.evidence.scheme=="https" for f in families), "generation_selection_invalid")
    await work._lock_current_run(db,selection.run_id)
    head = (await db.execute(text("SELECT source_sha256,checked_at FROM atomer_progress_runs WHERE id=:r"),
                             dict(r=selection.run_id))).one()
    require(head[0]==selection.source_sha,"variant_source_changed")
    fp = repair.target_fingerprint("xtrek")
    jobs, fresh, deferred, recover = {}, [], set(), []
    for family in families:
        job = await db.get(AtomerVariantRepair,doc_id(selection,family),with_for_update=True)
        if job:
            require(job.document["fingerprint"]==fp,"variant_family_changed")
            if job.state=="blocked":
                return dict(status="blocked",count=len(family.rows),reason=job.document.get("result",""))
            if job.state=="verified":
                continue
            if job.state!="deferred":
                recover.append((family,job))
                continue
            deferred.add(family.parent)
        else:
            await repair.require_target_available(db,"xtrek",family.parent)
        jobs[family.parent] = job
        fresh.append(family)
    active = fresh + [f for f,j in recover]
    if not active:
        return dict(status="verified",count=len(additions))
    before = await asyncio.to_thread(batch.read_batch,[identity(f) for f in active],fp)
    for f, job in recover:
        result = await finish(db,job,before[f.product_id],actor)
        if result["status"]!="verified":
            await db.commit()
            return result
    extend = selection.phase=="extend"
    groups, options = {}, {}
    if fresh:
        raw = await db.scalar(text("SELECT compressed FROM atomer_review_exports WHERE sha256=:s AND format='buyers'"),
                              dict(s=selection.source_sha))
        require(raw is not None,"variant_source_changed")
        groups = await asyncio.to_thread(load_source,raw,selection)
        if extend:
            current = selection.model_copy(update={"families":fresh})
            options = await asyncio.to_thread(native_preflight,current,head[1],fp)
    sending, payload = [], []
    for family in fresh:
        before_family = before[family.product_id]
        axes, patches = plan_existing(family,before_family,selection.phase)
        new, sources = {}, {}
        for item in family.rows:
            detail = await allowed(db,family,item,selection.run_id,family.parent in deferred)
            row = groups[family.parent].get(item.code)
            require(row and row["ean"]==item.ean,"generation_source_changed")
            target = detail["target_code"]
            proposed = source_axes(etree.fromstring(row["raw_xml"].encode()),item)
            require({k:v for k,v in proposed.items() if k!=AXIS}=={k:v for k,v in axes[target].items() if k!=AXIS}
                    and proposed[AXIS]!=axes[target][AXIS],"generation_occupied_axes_changed")
            sources[item.code] = dict(raw=row["raw_xml"],state=detail["source_state"])
            if extend:
                await absent_card(db,item)
                old_leaf = next(v for v in before_family["parent"]["variants"] if v["code"]==target)
                patch, new[item.code] = new_leaf(row,item,detail,before_family["parent"],old_leaf,axes[target],options)
                patches.append(patch)
            axes[item.code] = proposed
        require(all(n==1 for n in Counter(key(a) for a in axes.values()).values()), "variant_axis_collision")
        protected, before_axes = repair.projection(before_family)
        doc = dict(phase=selection.phase, family=family.model_dump(mode="json"),identity=identity(family),fingerprint=fp,
                   run_id=selection.run_id,source_sha=selection.source_sha,actor=actor,backup=before_family,
                   before_protected=protected,before_axes=before_axes,expected_axes=axes,additions=new,sources=sources)
        job = jobs[family.parent]
        if job:
            require((protected,before_axes)==(job.document["before_protected"],job.document["before_axes"]),"variant_preview_changed")
        if mode=="preview":
            continue
        if job is None:
            job = AtomerVariantRepair(id=doc_id(selection,family),parent_code=family.parent,state="sending",document=doc)
            db.add(job)
            for item in family.rows:
                await repair.outcome(db,item.code,actor,"working","Rozlíšenie generácií xTrek "+job.id,"generation_intent")
        else:
            job.state = "sending"
        sending.append((family,job.id))
        if patches:
            payload.append({"code":family.parent,"variants":patches})
    await db.commit()
    if not sending:
        return dict(status="ready" if fresh else "verified",count=len(additions))
    await work._lock_current_run(db,selection.run_id)
    locked = []
    for family, jid in sending:
        job = await db.get(AtomerVariantRepair,jid,with_for_update=True,populate_existing=True)
        require(job.state=="sending","generation_intent_changed")
        for item in family.rows:
            await allowed(db,family,item,selection.run_id,claimed=True)
            if extend:
                await absent_card(db,item)
        locked.append(job)
    acknowledged, upstream = False, ""
    try:
        upstream = await asyncio.to_thread(send,payload,fp) if payload else ""
        acknowledged = True
    except transport.SourceError as exc:
        if exc.status==429 and exc.request_sent is False:
            for job in locked:
                job.state="deferred"
            await db.commit()
            raise
        upstream = exc.code
    after = await asyncio.to_thread(batch.read_batch,[job.document["identity"] for job in locked],fp)
    results = [await finish(db,job,after[job.document["identity"]["product_id"]],actor,acknowledged,upstream) for job in locked]
    await db.commit()
    return next((r for r in results if r["status"]!="verified"),dict(status="verified",count=len(additions),
                reason=", ".join(f"{c} / {v}" for r in results for c,v in r["ids"].items())))
