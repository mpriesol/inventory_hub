"""Operator-selected xTrek review rows. Only variant parameter values are writable."""
import asyncio
from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from inventory_hub.db_models import Shop
from inventory_hub.db_models_ext import ShopProduct
from inventory_hub.product_editor_models import AtomerVariantRepair
from inventory_hub.services import atomer_review as work, atomer_recode as recode
from inventory_hub.services import stock_publication_source as transport
from inventory_hub.services.atomer_content_source import _family
from inventory_hub.services.atomer_review_targets import ReviewError
from inventory_hub.services.merchandising_write_guard import require_target_available
from inventory_hub.services.order_collection import target_fingerprint
from inventory_hub.services.product_publication_source import plain

Token = Annotated[str, Field(min_length=1, max_length=255)]
Axis = dict[Token, Token]


class ExactRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Token
    revision: int = Field(ge=0)
    before: Axis
    names: dict[Annotated[str, Field(pattern=r"^PARAM_[0-9]+$")], Token] = Field(min_length=1, max_length=30)


class Family(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: int = Field(gt=0)
    source_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    rows: list[ExactRow] = Field(min_length=1, max_length=100)


class Block(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    reason: str = Field(min_length=10,max_length=3500)


class Blocks(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: int = Field(gt=0)
    items: dict[Token, Block] = Field(min_length=1,max_length=200)


def digest(obj):
    return sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def selection_id(selection):
    data = selection.model_dump(mode="json")
    for row in data['rows']:
        if row.get('names',False) is None:
            del row['names']
    return digest(data)


def axis_values(variant):
    result = {}
    params = variant.get('parameters')
    if not isinstance(params, list):
        raise ReviewError('variant_axes_unavailable')
    for axis in params:
        names, values = axis.get('name') or {}, axis.get('values') or []
        if (set(names) != {'sk'} or not names['sk'] or names['sk'] in result or len(values) != 1
                or set(values[0]) != {'sk'} or not isinstance(values[0]['sk'],str)):
            raise ReviewError('variant_axes_ambiguous')
        result[names['sk']] = values[0]['sk']
    return result


def read_family(identity, fingerprint):
    client = transport._client('xtrek', fingerprint)
    try:
        params = {'product_ids':identity['product_id'], 'page':1,'current_page_items':100}
        parent = transport._one(transport._request(client,'get','products',params=params),'products')
        _family(parent, identity)
        data = transport._request(client,'get','products/variants',params=params)
        variants = data.get('variants') or []
        if (not transport._messages_ok(data.get('messages')) or data.get('current_page') != 1
                or data.get('number_of_pages') != 1 or data.get('number_of_items') != len(variants)
                or data.get('current_page_items') != len(variants) or not variants):
            raise ReviewError('variant_family_incomplete')
        keys = {(v['variant_id'],v['code'],v.get('ean')) for v in parent['variants']}
        if (len(keys) != len(variants) or keys != {(v.get('variant_id'),v.get('code'),v.get('ean')) for v in variants}
                or any(v.get('product_code') != identity['parent_code'] or v.get('product_id') != identity['product_id']
                       or not transport._messages_ok(v.get('messages')) for v in variants)):
            raise ReviewError('variant_family_changed')
        return plain({'parent':parent,'variants':variants})
    finally:
        client.session.close()


def projection(observation):
    # Generated display titles on variants may include parameters. Parent content, stock,
    # prices, identities and visibility are independently compared; the raw backup retains every field.
    fields = ('code','product_code','product_id','variant_id','ean','active_yn','archived_yn','stock','stocks',
              'stock_position','availability','availability_id','prices','labels','images','main_yn','metas','weight')
    parent = observation['parent']
    protected = {'parent':{k:parent[k] for k in (*fields,'descriptions','categories','files','parameters') if k in parent},
                 'leaves':{}}
    axes = {}
    for leaf in [*parent['variants'], *observation['variants']]:
        vid = str(leaf['variant_id'])
        protected['leaves'].setdefault(vid, []).append({k:leaf[k] for k in fields if k in leaf})
    for leaf in observation['variants']:
        axes[str(leaf['variant_id'])] = axis_values(leaf)
    return protected, axes


def desired_patch(observation, selected):
    _, before = projection(observation)
    after = deepcopy(before)
    leaves = []
    native = {str(v['variant_id']):v for v in observation['variants']}
    for entry in selected:
        vid = entry['vid']
        leaf = native.get(vid)
        if (not leaf or leaf['code'] != entry['target'] or (leaf.get('ean') or '') != (entry['ean'] or '')
                or before[vid] != entry['before']):
            raise ReviewError('variant_preview_changed')
        patch = {'code':leaf['code']}
        if 'new_ean' in entry:
            patch['ean'] = entry['new_ean']
        after[vid].update(entry.get('values',{}))
        if before[vid] != after[vid]:
            params = [{'descriptions':[{'language':'sk','name':name}],
                       'values':[{'descriptions':[{'language':'sk','value':value}]}]}
                      for name,value in after[vid].items()]
            patch['parameters'] = params
        if len(patch)>1:
            leaves.append(patch)
    def key(axes):
        return tuple(sorted((' '.join(n.split()).casefold(),' '.join(v.split()).casefold()) for n,v in axes.items()))
    counts = Counter(key(a) for a in after.values())
    if any(counts[key(after[r['vid']])] != 1 for r in selected):
        raise ReviewError('variant_axis_collision')
    return after, {'products':[{'code':observation['parent']['code'],'variants':leaves}]}


def send(payload,fingerprint):
    client = transport._client('xtrek', fingerprint)
    try:
        # A response, including a partial acknowledgement, is followed by a native read.
        reply = transport._request(client,'put','products',payload=payload)
        if not isinstance(reply,dict) or not transport._messages_ok(reply.get('messages')):
            raise transport.SourceError('atomer_variant_write_unconfirmed',uncertain=True)
    finally:
        client.session.close()


async def subject(db, run, code, revision):
    row = await db.scalar(text("SELECT detail FROM atomer_progress_items WHERE run_id=:r AND code=:c AND state='review'"),
                          {'r':run,'c':code})
    if not row:
        raise ReviewError('subject_missing')
    item = await work._record(db,'item',code)
    family = await work._record(db,'family',row['family_key'])
    if (item['revision'] != revision or item['status'] != 'open' or item['instruction'] or
            family['status'] != 'open' or item['scope']=='biketrek' or family['scope']=='biketrek'):
        raise ReviewError('review_changed')
    return row


async def outcome(db,code,actor,status,result,command):
    await db.execute(text("""
        INSERT INTO atomer_review_work AS w(kind,key,revision,status,scope,decision,note,instruction,result,updated_by)
        VALUES ('item',:code,1,:status,'default','undecided','','',:result,:actor)
        ON CONFLICT(kind,key) DO UPDATE SET revision=w.revision+1,status=EXCLUDED.status,
          result=EXCLUDED.result,updated_at=now(),updated_by=EXCLUDED.updated_by
    """),dict(code=code,actor=actor,status=status,result=result))
    await work._audit(db,'item',code,command,actor)


async def record_blocks(db, change, actor):
    await work._lock_current_run(db,change.run_id)
    for code,b in change.items.items():
        saved = await work._record(db,'item',code)
        if saved['status']=='blocked' and saved['revision']==b.revision+1 and saved['result']==b.reason:
            continue
        await subject(db,change.run_id,code,b.revision)
        await outcome(db,code,actor,'blocked',b.reason,'review_block')
    await db.commit()
    return {'status':'blocked','count':len(change.items)}


async def select_rows(db, selection):
    rows = []
    sources = dict((await db.execute(text("""
        SELECT s->>'code',s FROM atomer_progress_sources p CROSS JOIN LATERAL jsonb_array_elements(p.identities) s
        WHERE p.sha256=:sha AND s->>'code'=ANY(CAST(:codes AS text[]))
    """),dict(sha=selection.source_sha,codes=[v.code for v in selection.rows]))).all())
    for requested in selection.rows:
        r = await subject(db,selection.run_id,requested.code,requested.revision)
        s = sources.get(requested.code)
        if isinstance(requested,recode.BarcodeRow):
            rows.append(await recode.entry_for(db,requested,r,s))
            continue
        if (r['reasons'] != ['parameters_differ'] or not s or s['conflict'] or s['parent']!=r['parent']
                or not r['variant_id'] or not r['target_parent'] or r.get('match')!='code_or_ean'
                or set(s['axes'])!=set(requested.names) or len(set(requested.names.values()))!=len(s['axes'])
                or any(not v or len(v)>255 for v in s['axes'].values())):
            raise ReviewError('variant_source_changed')
        owners = (await db.execute(select(ShopProduct.product_id,ShopProduct.external_product_id,ShopProduct.external_code)
            .join(Shop, Shop.id==ShopProduct.shop_id).where(Shop.code=='xtrek',
            ShopProduct.external_variant_id==r['variant_id']))).all()
        if owners and list(owners) != [(r['hub_id'],r['product_id'],r['target_code'])]:
            raise ReviewError('variant_card_changed')
        rows.append(dict(code=r['code'],parent=r['target_parent'],pid=r['product_id'],vid=r['variant_id'],
                         target=r['target_code'],ean=r['target_ean'],card=r['hub_id'],revision=requested.revision,
                         before=requested.before,values={requested.names[k]:v for k,v in s['axes'].items()}))
    if len({(r['pid'],r['parent']) for r in rows})!=1 or len({r['vid'] for r in rows})!=len(rows):
        raise ReviewError('variant_family_changed')
    return rows


async def check_result(db, job, actor, acknowledged=False, *, observation=None, commit=True):
    doc = job.document
    retry_after = None
    try:
        remote = observation if observation is not None else await asyncio.to_thread(read_family,doc['identity'],doc['fingerprint'])
        protected, actual = projection(remote)
        if acknowledged and protected != doc['protected'] and protected==doc.get('before_protected') and actual==doc['before_axes']:
            state, reason = 'blocked', 'Upgates po zápise stále vracia pôvodný EAN.'
        elif protected != doc['protected']:
            state, reason = 'uncertain', 'Zmenil sa aj chránený údaj rodiny. Porovnaj uloženú zálohu.'
        elif actual == doc['after']:
            async with db.begin_nested():
                for item in doc['rows']:
                    if 'new_ean' in item:
                        await recode.eligible_card(db,item,record=True)
            state, reason = 'verified', ''
        elif acknowledged and actual == doc['before_axes']:
            state, reason = 'blocked', 'Upgates prijal požiadavku, ale po zápise vracia pôvodné parametre.'
        else:
            state, reason = 'uncertain', 'Kontrolné načítanie ešte nepotvrdilo všetky očakávané parametre.'
    except (ReviewError,transport.SourceError) as exc:
        state,reason = 'uncertain', 'Overenie Upgates: '+exc.code
        if getattr(exc,'status',None)==429:
            retry_after=getattr(exc,'retry_after',None) or 60
    job.state = state
    job.document = {**doc,'result':reason}
    for entry in doc['rows']:
        current = await work._record(db,'item',entry['code'])
        if current['status']!='working' or current['revision']!=entry['revision']+1:
            continue
        if state=='uncertain':
            continue
        ref = f"xTrek {entry['pid']}/{entry['vid']}, kód {entry['target']}, karta {entry['card'] or '—'}. "
        note = ('Parametre overené podľa Atomeru: '+', '.join(f'{k} = {v}' for k,v in entry['values'].items())+'.'
                if state=='verified' else reason)
        if state=='verified' and 'new_ean' in entry:
            note = (f"EAN {entry['ean']} → {entry['new_ean']}. Oba kódy sú na pôvodnej karte. "
                    if entry['ean'] else f"Doplnený chýbajúci EAN {entry['new_ean']} z existujúcej karty. ")
            if entry['values']:
                note += 'Parametre podľa Atomeru: '+', '.join(f'{k} = {v}' for k,v in entry['values'].items())+'. '
            note += 'Overené v xTreku. '+entry['proof']
        await outcome(db,entry['code'],actor,'open' if state=='verified' else 'blocked',ref+note,'variant_'+state)
    if commit:
        await db.commit()
    return {'status':state,'count':len(doc['rows']),'reason':reason,'id':job.id,'retry_after':retry_after}


def native_identity(rows):
    first = rows[0]
    return {'code':first['target'],'parent_code':first['parent'],'variant_code':first['target'],
            'product_id':int(first['pid']),'variant_id':int(first['vid'])}


def prepare(selection, rows, identity, fingerprint, before, actor):
    protected, axes = projection(before)
    after, payload = desired_patch(before,rows)
    expected = recode.protect_expected(protected,rows) if isinstance(selection,recode.BarcodeFamily) else protected
    doc = {'identity':identity,'fingerprint':fingerprint,'rows':rows,'backup':before,'before_axes':axes,
           'protected':expected,'before_protected':protected,'after':after,'actor':actor,
           'run_id':selection.run_id,'source_sha':selection.source_sha}
    return doc, payload


async def process(db, selection, mode, actor):
    job_id = selection_id(selection)
    current = await db.scalar(text("SELECT max(id) FROM atomer_progress_runs"))
    await work._lock_current_run(db,current)
    row = await db.get(AtomerVariantRepair,job_id,with_for_update=True)
    if row:
        if row.state in ('sending','uncertain'):
            return await check_result(db,row,actor)
        if row.state=='deferred':
            doc=row.document
            if current!=doc['run_id']:
                raise ReviewError('run_changed')
            for item in doc['rows']:
                saved=await work._record(db,'item',item['code'])
                if saved['status']!='working' or saved['revision']!=item['revision']+1:
                    raise ReviewError('review_changed')
                if 'new_ean' in item:
                    await recode.eligible_card(db,item)
            before=await asyncio.to_thread(read_family,doc['identity'],doc['fingerprint'])
            if projection(before)!=(doc.get('before_protected',doc['protected']),doc['before_axes']):
                return await check_result(db,row,actor)
            if mode=='preview':
                return {'id':row.id,'status':'ready','count':len(doc['rows'])}
            _,payload=desired_patch(before,doc['rows'])
            row.state='sending'
            await db.commit()
            await work._lock_current_run(db,current)
            row=await db.get(AtomerVariantRepair,job_id,with_for_update=True,populate_existing=True)
            if row.state!='sending':
                return await check_result(db,row,actor)
            return await send_intent(db,row,actor,payload,doc['fingerprint'])
        return {'id':row.id,'status':row.state,'count':len(row.document['rows']),'reason':row.document.get('result','')}
    if current!=selection.run_id:
        raise ReviewError('run_changed')
    head_sha = await db.scalar(text("SELECT source_sha256 FROM atomer_progress_runs WHERE id=:r"),{'r':selection.run_id})
    if selection.source_sha!=head_sha:
        raise ReviewError('variant_source_changed')
    selected = await select_rows(db,selection)
    first = selected[0]
    await require_target_available(db,'xtrek',first['parent'])
    identity = native_identity(selected)
    fingerprint = target_fingerprint('xtrek')
    before = await asyncio.to_thread(read_family,identity,fingerprint)
    doc, payload = prepare(selection,selected,identity,fingerprint,before,actor)
    if isinstance(selection,recode.BarcodeFamily):
        since = await db.scalar(text("SELECT checked_at FROM atomer_progress_runs WHERE id=:run"),{'run':selection.run_id})
        await asyncio.to_thread(recode.check_changed_catalog,fingerprint,since,selected)
    if mode=='preview':
        return {'status':'ready','id':job_id,'count':len(selected)}
    job = AtomerVariantRepair(id=job_id,parent_code=first['parent'],state='sending',document=doc)
    db.add(job)
    for item in selected:
        await outcome(db,item['code'],actor,'working','Overenie a zápis parametrov xTrek '+job_id,'variant_intent')
    await db.commit()
    # Never resend an existing intent. Re-lock after the durable commit; read-only recovery may have finished it.
    await work._lock_current_run(db,selection.run_id)
    job = await db.get(AtomerVariantRepair,job_id,with_for_update=True,populate_existing=True)
    if job.state!='sending':
        return await check_result(db,job,actor)
    for item in selected:
        saved = await work._record(db,'item',item['code'])
        if saved['status']!='working' or saved['revision']!=item['revision']+1:
            return await check_result(db,job,actor)
    return await send_intent(db,job,actor,payload,fingerprint)


async def send_intent(db,job,actor,payload,fingerprint):
    acknowledged = False
    try:
        if payload['products'][0]['variants']:
            await asyncio.to_thread(send,payload,fingerprint)
        acknowledged = True
    except transport.SourceError as exc:
        if exc.status==429 and exc.request_sent is False:
            # Hub admission denied locally. Only this positive signal permits a fresh attempt.
            job.state='deferred'
            await db.commit()
            raise
        pass  # A readback is authoritative even if the transport lost its acknowledgement.
    return await check_result(db,job,actor,acknowledged)
