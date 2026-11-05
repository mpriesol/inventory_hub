"""Journal exact sellable Atomer -> Hub card assignments independently of shop quotas."""
import asyncio
import logging

from sqlalchemy import text

from inventory_hub import database
from inventory_hub.services import atomer_catalog_engine as engine
from inventory_hub.services import atomer_new_source as src
from inventory_hub.services.atomer_review_targets import ReviewError

LOGGER=logging.getLogger(__name__)
LOCAL_LOCK=691433153
MAX_ROWS=500
MAX_FAMILIES=100
# Existing targets still require a current direct shop read. This lane processes
# only cards for source leaves for which the complete report found no xTrek target.
ELIGIBLE="""EXISTS (SELECT 1 FROM atomer_progress_items i WHERE i.run_id=:run AND i.family_key=j.family_key
              AND i.state IN ('needs_both','needs_target'))"""


def source_details(index,family,rows,selection,corrections):
    parent=index.canonical(family[2:])
    data,_=index.family(parent)
    leaves=set(index.leaves(parent))
    if leaves!=set(rows):
        raise ReviewError('source_card_family_changed')
    result={};errors={}
    for code in selection:
        try:
            result[code]=_one_subject(index,parent,data,rows,code,corrections)
        except (ReviewError,ValueError,ArithmeticError) as exc:
            errors[code]=dict(status='blocked',reason=getattr(exc,'code','source_card_invalid'))
    return result,errors


def verified_sentinels(index,codes,pins):
    result={}
    for code in pins.keys() & codes:
        if index.parent_for(code)!=code or index.leaves(code)!=(code,) or index.sku_owners(code)!=(code,):
            continue
        family,_=index.family(code)
        row=family[code]
        if src.named(row,'EAN kód')=='0' and src.named(row,'Interné číslo').strip()==pins[code]:
            result[code]=pins[code]
    return result


def _one_subject(index,parent,data,rows,code,corrections):
    detail=rows[code]['detail']
    source=data[code]
    if index.parent_for(code)!=parent or index.sku_owners(code)!=(code,):
        raise ReviewError('source_card_ambiguous_sku')
    raw=src.named(source,'EAN kód').strip()
    root=data[parent]
    raw_weight=src.named(source,'Váha')
    record=dict(code=code,parent=parent if code!=parent else None,
                native_id=src.named(source,'Interné číslo').strip(),name=src.named(source,'Názov tovaru').strip(),
                parent_name=src.named(root,'Názov tovaru').strip(),
                brand=src.named(source,'Výrobca').strip() or src.named(root,'Výrobca').strip(),
                ean=raw,weight_g=src.grams(raw_weight) if raw_weight else None,
                sku_owners=list(index.sku_owners(code)),gtin_owners=list(index.gtin_owners(raw)),
                corrected_gtin_owners=list(index.gtin_owners((corrections.get(code) or {}).get('ean') or '')),
                product_active=src.source.flag(src.named(root,'Viditeľný')),
                variant_active=src.source.flag(src.named(source,'Viditeľný')) if code!=parent else None)
    if raw!=str(detail.get('ean') or '') or not record['native_id']:
        raise ReviewError('source_card_identity_changed')
    return {**detail,'source_card':record}

async def due(db,c):
    return await engine._rows(db,f"""SELECT j.campaign,j.family_key,j.leaf_count,j.stage,j.guards,
              j.source_card_attempts FROM atomer_catalog_jobs j WHERE j.campaign=:cid
              AND j.source_card_stage='pending' AND j.source_card_next_at<=now()
              AND j.stage IN ('pending','prepared','sending','observe','cards','related','fidelity','updating')
              AND {ELIGIBLE} ORDER BY j.priority,j.family_key LIMIT :lim""",
              cid=c['id'],run=c['checked_run_id'],lim=MAX_FAMILIES)


def choose(jobs):
    found=[]; n=0
    for j in jobs:
        if found and n+j['leaf_count']>MAX_ROWS:
            break
        found.append(dict(j));n+=j['leaf_count']
    return found


async def _receipts(db,c,f,codes):
    if not codes:
        return
    records=[]
    for k,v in codes.items():
        created=v.get('created') if isinstance(v.get('created'),bool) else {'created':True,'reused':False}.get(v.get('action'))
        ready=v['status']=='card_ready' and bool(v.get('card_id'))
        records.append(dict(code=k,ok=ready,state='card_ready' if ready else 'blocked',
                       card_id=int(v['card_id']) if ready else None,created=created if ready else None,data=v))
    await engine._exec(db,"""INSERT INTO atomer_catalog_source_cards(campaign,family_key,code,run_id,state,card_id,created,evidence)
          SELECT :c,:f,d->>'code',:run,d->>'state',(d->>'card_id')::bigint,(d->>'created')::boolean,d->'data'
          FROM jsonb_array_elements(CAST(:records AS jsonb)) d
          ON CONFLICT(campaign,code) DO UPDATE SET family_key=excluded.family_key,run_id=excluded.run_id,state=excluded.state,
              card_id=excluded.card_id,created=coalesce(atomer_catalog_source_cards.created,excluded.created),
              evidence=excluded.evidence,checked_at=now()""",c=c['id'],f=f,run=c['checked_run_id'],records=engine._json(records))
    await engine._exec(db,"""INSERT INTO atomer_catalog_proofs(campaign,family_key,code,state,evidence)
          SELECT :c,:f,d->>'code','partial',(d->'data')||'{"verification_scope":"source_card_only","reason":"Karta bola overená v Hube. Natívna väzba čaká na overenie."}'::jsonb
          FROM jsonb_array_elements(CAST(:records AS jsonb)) d WHERE (d->>'ok')::boolean
          ON CONFLICT(campaign,code) DO UPDATE SET
            evidence=atomer_catalog_proofs.evidence || jsonb_build_object('source_card',excluded.evidence),
            checked_at=now() WHERE atomer_catalog_proofs.family_key=excluded.family_key""",c=c['id'],f=f,records=engine._json(records))


async def prepare(db,c,jobs):
    from inventory_hub.services import atomer_card_batch as cards
    corrections=c['selection'].get('barcode_corrections') or {}
    pending={};sources={};scopes={};result={}
    index=None
    for j in jobs:
        f=j['family_key']
        rows,group=await engine._family(db,c['checked_run_id'],f)
        g=j['guards']
        if g is None:
            g=await engine._claim(db,c,j,rows,group)
            await engine._exec(db,"UPDATE atomer_catalog_jobs SET guards=CAST(:guards AS jsonb) WHERE campaign=:c AND family_key=:f",
                              guards=engine._json(g),c=c['id'],f=f)
        elif (engine._guard(group)!=g['group'] or {k:engine._guard(r) for k,r in rows.items()}!=g['rows']):
            await engine._exec(db,"""UPDATE atomer_catalog_jobs SET source_card_stage='blocked',
                      source_card_problem='Zmenil sa pokyn operátora.' WHERE campaign=:c AND family_key=:f""",c=c['id'],f=f)
            continue
        keys=[k for k in engine._selected(g) if engine._scope_of(g,k)!='biketrek'
              and rows[k]['detail']['state'] in ('needs_both','needs_target')]
        pending[f]=(j,keys)
        if not keys:
            continue
        if index is None:
            index=await engine._source(db,c)
        try:
            inputs,invalid=await asyncio.to_thread(source_details,index,f,rows,keys,corrections)
            sources.update(inputs);result.update(invalid)
            scopes.update({k:engine._scope_of(g,k) for k in keys})
        except (ReviewError,ValueError,ArithmeticError) as exc:
            result.update({k:dict(status='blocked',reason=getattr(exc,'code','source_card_invalid')) for k in keys})
    for start in range(0,len(sources),MAX_ROWS):
        keys=list(sources)[start:start+MAX_ROWS]
        pins=c['selection'].get('zero_barcode_sentinels',{})
        zero=verified_sentinels(index,set(keys),pins) if set(keys)&pins.keys() else {}
        result.update(await cards.source_cards(db,c['checked_run_id'],{k:sources[k] for k in keys},actor=c['actor'],
                         scopes={k:scopes[k] for k in keys},barcode_corrections={k:corrections[k] for k in keys if k in corrections},
                         **({'zero_barcode_sentinels':zero} if zero else {})))
    for f,(j,keys) in pending.items():
        outcomes={k:result[k] for k in keys}
        await _receipts(db,c,f,outcomes)
        failures=list(dict.fromkeys(str(v.get('reason') or 'source_card_unconfirmed') for v in outcomes.values()
                                   if v.get('status')!='card_ready'))
        ready=any(v.get('status')=='card_ready' for v in outcomes.values())
        stage='checked' if ready or not failures else 'blocked'
        await engine._exec(db,"""UPDATE atomer_catalog_jobs SET source_card_stage=:s,source_card_problem=:p
                            WHERE campaign=:c AND family_key=:f""",
                           s=stage,p=' | '.join(failures)[:2000],c=c['id'],f=f)


async def cycle():
    if database._engine is None:
        return False
    async with database.get_session_context() as db:
        # The existing review mutex fences all revisions. A remote phase may
        # hold it; do not fill the connection pool waiting for that phase.
        if not await db.scalar(text('SELECT pg_try_advisory_xact_lock(:k)'),{'k':LOCAL_LOCK}):
            return False
        if not await db.scalar(text('SELECT pg_try_advisory_xact_lock(691433138)')):
            return False
        c=await engine._one(db,"SELECT * FROM atomer_catalog_campaigns WHERE state IN ('running','waiting') LIMIT 1")
        if not c:
            return False
        c=dict(c)
        await engine._lock_current(db,c)
        jobs=choose(await due(db,c))
        if not jobs:
            return False
        try:
            await prepare(db,c,jobs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await db.rollback()
            # This change controls scheduling only. Never commit any partial card
            # creation or an ownership revision from a failed transaction.
            reason=engine._diagnostic({'phase':'source_card'},exc)
            await engine._exec(db,"""UPDATE atomer_catalog_jobs SET source_card_problem=:p,
                 source_card_attempts=source_card_attempts+1,source_card_next_at=now()+make_interval(secs=>:delay),
                 source_card_stage=CASE WHEN source_card_attempts>=5 THEN 'blocked' ELSE source_card_stage END
                 WHERE campaign=:c AND source_card_stage='pending' AND family_key=ANY(CAST(:keys AS text[]))""",
                 p=reason,c=c['id'],keys=[j['family_key'] for j in jobs],
                 delay=min(300,5*2**min(6,max(j['source_card_attempts'] for j in jobs))))
        return True


async def run():
    while True:
        try:
            worked=await cycle()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning('Atomer local card batch unavailable (%s)',type(exc).__name__)
            worked=False
        await asyncio.sleep(0.4 if worked else 3)


async def counts(db,c):
    # Distinct cards still present in the Hub, with a receipt for a sellable SKU
    # in the current Atomer report. A pre-existing report ID alone is not proof.
    data=await engine._one(db,"""WITH bound AS (
        SELECT i.code,i.family_key,p.state,p.evidence,l.state AS local_state,l.card_id AS local_id,l.created
        FROM atomer_progress_items i JOIN atomer_catalog_jobs j ON j.campaign=:c AND j.family_key=i.family_key
        LEFT JOIN atomer_catalog_proofs p ON p.campaign=j.campaign AND p.family_key=i.family_key AND p.code=i.code
        LEFT JOIN atomer_catalog_source_cards l ON l.campaign=j.campaign AND l.family_key=i.family_key AND l.code=i.code
        WHERE i.run_id=:r
    ), claims AS (
        SELECT local_id AS id,created FROM bound WHERE local_state='card_ready'
        UNION ALL
        SELECT (evidence->>'card_id')::bigint AS id,
               CASE evidence->>'card_action' WHEN 'created' THEN true WHEN 'reused' THEN false END AS created
        FROM bound WHERE (evidence->>'card_id') ~ '^[1-9][0-9]{0,17}$'
              AND evidence->>'status' IN ('verified','card_ready')
    ), actual AS (
        SELECT c.id,bool_or(c.created) AS created FROM claims c JOIN products p ON p.id=c.id GROUP BY c.id
    ) SELECT count(*) AS hub_cards,count(*) FILTER(WHERE created IS TRUE) AS cards_created,
             count(*) FILTER(WHERE created IS FALSE) AS cards_reused,
             count(*) FILTER(WHERE created IS NULL) AS card_origin_unknown,
        (SELECT count(*) FROM bound WHERE local_state='card_ready') AS source_card_ready,
        (SELECT count(*) FROM bound WHERE local_state='blocked') AS source_card_blocked,
        (SELECT count(DISTINCT family_key) FROM (
            SELECT family_key FROM bound WHERE evidence->'xtrek'->>'product_id' IS NOT NULL
                  AND evidence->>'status'='verified'
            UNION SELECT j.family_key FROM atomer_catalog_jobs j WHERE campaign=:c
                  AND receipt->>'identity_verified'='true' AND receipt->>'product_id' IS NOT NULL
                  AND EXISTS(SELECT 1 FROM bound b WHERE b.family_key=j.family_key)
         ) native) AS native_families,
        (SELECT count(*) FROM bound b WHERE b.state='excluded' AND EXISTS (
           SELECT 1 FROM atomer_review_work w JOIN atomer_review_events e
             ON (e.kind,e.key,e.revision)=(w.kind,w.key,w.revision)
             WHERE w.status='done' AND e.command='done'
              AND (w.kind='item' AND w.key=b.code OR w.kind='family' AND w.key=b.family_key))) AS manual_exclusions
        FROM actual""",c=c['id'],r=c['checked_run_id'])
    return dict(data)
