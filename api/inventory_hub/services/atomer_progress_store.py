"""Persist complete private runs, publishing them only after successful reconciliation."""
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.db_models import Product, ProductIdentifier, Shop
from inventory_hub.db_models_ext import ShopProduct
from inventory_hub.product_editor_models import ProductEditorOverride
from inventory_hub.services import atomer_progress as progress
from inventory_hub.services.product_identity import IdentityIndex


class ProgressError(ValueError):
    pass


async def latest(db):
    query = text("""
        SELECT r.id, r.source_sha256, r.checked_at, r.totals, s.source_rows, s.imported_at
        FROM atomer_progress_runs r JOIN atomer_progress_sources s ON s.sha256 = r.source_sha256
        ORDER BY r.id DESC LIMIT 1
    """)
    return (await db.execute(query)).mappings().first()


async def refresh(db: AsyncSession, source, expected, only_if_stale=False):
    await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
    if not await db.scalar(text("SELECT pg_try_advisory_xact_lock(691433137)")):
        raise ProgressError("refresh_in_progress")
    last = await latest(db)
    if (last["id"] if last else 0) != expected:
        raise ProgressError("run_changed")
    if only_if_stale and not source and last and datetime.now(timezone.utc) - last["checked_at"] < timedelta(days=1):
        return {"id":last["id"],"totals":last["totals"]}
    if source:
        digest, count, identities = source
        await db.execute(text("""
            INSERT INTO atomer_progress_sources(sha256, source_rows, identities)
            VALUES (:digest, :count, CAST(:identities AS jsonb)) ON CONFLICT (sha256) DO NOTHING
        """), {"digest":digest, "count":count, "identities":json.dumps(identities, ensure_ascii=False)})
    else:
        if not last:
            raise ProgressError("source_required")
        digest = last["source_sha256"]
        identities = await db.scalar(text("SELECT identities FROM atomer_progress_sources WHERE sha256=:sha"),
                                     {"sha":digest})
    remote, bike_targets = await asyncio.to_thread(progress.remote_catalog)
    shop_ids = dict((await db.execute(select(Shop.code, Shop.id).where(Shop.code.in_(("xtrek","biketrek"))))).all())
    if set(shop_ids) != {"biketrek", "xtrek"}:
        raise ProgressError("shops_unavailable")
    cards = (await db.scalars(select(Product))).all()
    identifiers = (await db.scalars(select(ProductIdentifier))).all()
    links = (await db.scalars(select(ShopProduct).where(ShopProduct.shop_id.in_(shop_ids.values())))).all()
    overrides = dict((await db.execute(select(ProductEditorOverride.product_id, ProductEditorOverride.data))).all())
    card_index = IdentityIndex(shop_ids["xtrek"], cards, (), identifiers)
    bike_links = defaultdict(list)
    for link in links:
        if link.shop_id == shop_ids["biketrek"]:
            bike_links[link.product_id].append(link)
    rows = await asyncio.to_thread(progress.reconcile, identities, remote, card_index,
                                  [l for l in links if l.shop_id == shop_ids["xtrek"]], overrides, bike_links, bike_targets)
    totals = {"items":len(rows), "kinds":dict(Counter(r["kind"] for r in rows)),
              "states":dict(Counter(r["state"] for r in rows)), "bike":dict(Counter(r["bike_state"] for r in rows))}
    # checked_at is the start of the consistent local snapshot. Upgates reads follow it.
    run_id = await db.scalar(text("""
        INSERT INTO atomer_progress_runs(source_sha256, checked_at, totals)
        VALUES (:sha, transaction_timestamp(), CAST(:totals AS jsonb)) RETURNING id
    """), {"sha":digest, "totals":json.dumps(totals)})
    await db.execute(text("""
        INSERT INTO atomer_progress_items(run_id, code, kind, state, family_key, detail)
        SELECT :run, d->>'code', d->>'kind', d->>'state', d->>'family_key', d
        FROM jsonb_array_elements(CAST(:rows AS jsonb)) AS d
    """), {"run":run_id, "rows":json.dumps(rows,ensure_ascii=False)})
    await db.commit()
    return {"id":run_id, "totals":totals}


async def page(db, kind, state, bike, card, search, offset, limit):
    head = await latest(db)
    if not head:
        return {"run":None, "total":0, "families":0, "groups":[]}
    where = ["run_id=:run"]
    params = {"run":head["id"]}
    for key,value,column in (("kind",kind,"kind"), ("state",state,"state"), ("bike",bike,"detail->>'bike_state'"), ("card",card,"detail->>'card_state'")):
        if value:
            where.append(f"{column}=:{key}")
            params[key] = value
    if search:
        where.append("""strpos(lower(concat_ws(' ', code, detail->>'name', detail->>'ean', detail->>'parent',
            detail->>'hub_id', detail->>'hub_sku', detail->>'target_code', detail->>'product_id', detail->>'variant_id',
            detail->'bike'->>'code', detail->'bike'->>'product_id', detail->'bike'->>'variant_id')), lower(:search))>0""")
        params["search"] = search
    clause = " FROM atomer_progress_items WHERE " + " AND ".join(where)
    group_query = "SELECT family_key, min(detail->>'family_name') AS name"+clause+" GROUP BY family_key"
    counts = (await db.execute(text("SELECT count(*) AS items, count(DISTINCT family_key) AS families"+clause),params)).one()
    selected = (await db.execute(text(group_query+" ORDER BY lower(min(detail->>'family_name')), family_key LIMIT :limit OFFSET :offset"),
                                 dict(params,limit=limit,offset=offset))).all()
    groups = {key:{"key":key, "name":name, "items":[]} for key,name in selected}
    if groups:
        children = await db.execute(text("SELECT family_key, detail"+clause+
            " AND family_key = ANY(CAST(:keys AS text[])) ORDER BY code"),dict(params,keys=list(groups)))
        for key,row in children:
            groups[key]["items"].append(row)
    return {"run":dict(head), "total":counts[0], "families":counts[1], "groups":list(groups.values())}
