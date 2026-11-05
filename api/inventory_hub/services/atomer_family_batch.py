"""Share paginated native reads and a bounded PUT while keeping one durable intent per family."""
import asyncio
from collections import defaultdict

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from inventory_hub.product_editor_models import AtomerVariantRepair
from inventory_hub.services import atomer_recode as recode, atomer_review as work, atomer_variant_repair as repairs
from inventory_hub.services import stock_publication_source as transport
from inventory_hub.services.atomer_content_source import _family
from inventory_hub.services.atomer_review_targets import ReviewError
from inventory_hub.services.product_publication_source import plain


class Pack(BaseModel):
    model_config = ConfigDict(extra="forbid")
    families: list[repairs.Family | recode.BarcodeFamily] = Field(min_length=1, max_length=25)


def collect_pages(client, path, key, filters):
    records = []
    expected = None
    for page in range(1, 26):
        data = transport._request(client,'get',path,params={**filters,'page':page,'current_page_items':100})
        rows = data.get(key)
        totals = data.get('number_of_items'), data.get('number_of_pages')
        if (not isinstance(rows,list) or not transport._messages_ok(data.get('messages'))
                or data.get('current_page') != page or data.get('current_page_items') != len(rows)
                or any(type(n) is not int or n < 0 for n in totals) or totals[1] > 25
                or expected is not None and totals != expected or any(not isinstance(row,dict) for row in rows)):
            raise ReviewError('variant_family_incomplete')
        expected = totals
        records.extend(rows)
        if page >= totals[1]:
            if len(records) != totals[0]:
                raise ReviewError('variant_family_incomplete')
            return records
        if not rows:
            break
    raise ReviewError('variant_family_incomplete')


def read_batch(identities, fingerprint):
    by_id = {identity['product_id']:identity for identity in identities}
    if len(by_id) != len(identities) or not by_id:
        raise ReviewError('variant_family_changed')
    filters = {'product_ids':';'.join(str(pid) for pid in sorted(by_id))}
    client = transport._client('xtrek',fingerprint)
    try:
        parents = collect_pages(client,'products','products',filters)
        leaves = collect_pages(client,'products/variants','variants',filters)
    finally:
        client.session.close()
    if len(parents)!=len(by_id) or {p.get('product_id') for p in parents}!=set(by_id):
        raise ReviewError('variant_family_incomplete')
    by_parent = defaultdict(list)
    seen = set()
    for leaf in leaves:
        pid, vid = leaf.get('product_id'), leaf.get('variant_id')
        if (pid not in by_id or not vid or vid in seen or leaf.get('product_code')!=by_id[pid]['parent_code']
                or not transport._messages_ok(leaf.get('messages'))):
            raise ReviewError('variant_family_changed')
        seen.add(vid)
        by_parent[pid].append(leaf)
    result = {}
    for parent in parents:
        pid = parent['product_id']
        _family(parent,by_id[pid])
        if not transport._messages_ok(parent.get('messages')):
            raise ReviewError('variant_family_incomplete')
        def keys(variants):
            return {(v.get('variant_id'), v.get('code'), v.get('ean')) for v in variants}
        if keys(parent['variants'])!=keys(by_parent[pid]):
            raise ReviewError('variant_family_changed')
        result[pid] = plain({'parent':parent,'variants':by_parent[pid]})
    return result


async def still_owned(db, rows):
    for row in rows:
        saved = await work._record(db,'item',row['code'])
        family = await work._record(db,'family',row['family_key'])
        if (saved['status']!='working' or saved['revision']!=row['revision']+1 or saved['scope']=='biketrek'
                or family['status']!='open' or family['scope']=='biketrek' or family['revision']!=row['family_revision']):
            raise ReviewError('review_changed')
        if 'new_ean' in row:
            await recode.eligible_card(db,row)


async def process(db, pack, mode, actor):
    selections = pack.families
    run, sha = selections[0].run_id, selections[0].source_sha
    codes = [r.code for s in selections for r in s.rows]
    if len(codes)>100 or len(set(codes))!=len(codes) or any(s.run_id!=run or s.source_sha!=sha for s in selections):
        raise ReviewError('variant_selection_invalid')
    await work._lock_current_run(db,run)
    head = (await db.execute(text('SELECT source_sha256,checked_at FROM atomer_progress_runs WHERE id=:r'),{'r':run})).one()
    if sha != head[0]:
        raise ReviewError('variant_source_changed')
    fingerprint = repairs.target_fingerprint('xtrek')
    saved, fresh, recovery, parents = {}, {}, {}, set()
    for selection in selections:
        key = repairs.selection_id(selection)
        job = await db.get(AtomerVariantRepair,key,with_for_update=True)
        if job and job.state=='verified':
            continue
        if job and job.state=='blocked':
            return {'status':'blocked','count':len(job.document['rows']),'reason':job.document.get('result','')}
        if job:
            if job.document['fingerprint']!=fingerprint:
                raise ReviewError('variant_family_changed')
            identity = job.document['identity']
            if job.state=='deferred' and all('family_revision' in row for row in job.document['rows']):
                await still_owned(db,job.document['rows'])
                fresh[key] = (selection,job.document['rows'],identity)
                saved[key] = job
            else:
                recovery[key] = job
        else:
            rows = await repairs.select_rows(db,selection)
            identity = repairs.native_identity(rows)
            await repairs.require_target_available(db,'xtrek',identity['parent_code'])
            for row in rows:
                row['family_key'] = await db.scalar(text('SELECT family_key FROM atomer_progress_items WHERE run_id=:r AND code=:c'),
                                                    {'r':run,'c':row['code']})
                row['family_revision'] = (await work._record(db,'family',row['family_key']))['revision']
            fresh[key] = (selection,rows,identity)
        pid = identity['product_id']
        if pid in parents:
            raise ReviewError('variant_family_changed')
        parents.add(pid)
    if not parents:
        return {'status':'verified','count':len(codes)}
    identities = [v[2] for v in fresh.values()] + [j.document['identity'] for j in recovery.values()]
    before = await asyncio.to_thread(read_batch,identities,fingerprint)
    for job in recovery.values():
        observation = before[job.document['identity']['product_id']]
        result = await repairs.check_result(db,job,actor,observation=observation,commit=False)
        if result['status']!='verified':
            await db.commit()
            return result
    barcodes = [row for selection,rows,_ in fresh.values() if isinstance(selection,recode.BarcodeFamily) for row in rows]
    if barcodes:
        await asyncio.to_thread(recode.check_changed_catalog,fingerprint,head[1],barcodes)
    products = []
    jobs = []
    for key,(selection,rows,identity) in fresh.items():
        obs = before[identity['product_id']]
        doc, payload = repairs.prepare(selection,rows,identity,fingerprint,obs,actor)
        job = saved.get(key)
        if job and repairs.projection(obs)!=(job.document['before_protected'],job.document['before_axes']):
            raise ReviewError('variant_preview_changed')
        if mode=='preview':
            continue
        if job is None:
            job = AtomerVariantRepair(id=key,parent_code=identity['parent_code'],state='sending',document=doc)
            db.add(job)
            for row in rows:
                await repairs.outcome(db,row['code'],actor,'working','Overenie a oprava xTrek '+key,'variant_intent')
        else:
            job.state = 'sending'
        jobs.append(key)
        if payload['products'][0]['variants']:
            products.extend(payload['products'])
    await db.commit()
    if not jobs:
        return {'status':'ready' if fresh else 'verified','count':len(codes)}
    await work._lock_current_run(db,run)
    active = [await db.get(AtomerVariantRepair,key,with_for_update=True,populate_existing=True) for key in jobs]
    if any(job.state!='sending' for job in active):
        return {'status':'uncertain','count':len(codes),'reason':'Opakujte iba kontrolu uložených zámerov.'}
    for job in active:
        await still_owned(db,job.document['rows'])
    acknowledged = False
    try:
        if products:
            await asyncio.to_thread(repairs.send,{'products':products},fingerprint)
        acknowledged = True
    except transport.SourceError as exc:
        if exc.status==429 and exc.request_sent is False:
            for job in active:
                job.state = 'deferred'
            await db.commit()
            raise
        # A sent or indeterminate request is always followed by reads, including partial acknowledgements.
    after = await asyncio.to_thread(read_batch,[j.document['identity'] for j in active],fingerprint)
    results = []
    for job in active:
        results.append(await repairs.check_result(db,job,actor,acknowledged,
            observation=after[job.document['identity']['product_id']],commit=False))
    await db.commit()
    return next((r for r in results if r['status']!='verified'),{'status':'verified','count':len(codes)})
