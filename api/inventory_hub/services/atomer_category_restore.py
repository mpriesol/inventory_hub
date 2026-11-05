"""Add only missing categories selected from the pinned private Atomer catalog."""
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Annotated

from lxml import etree, html
from pydantic import BaseModel, ConfigDict, Field, field_validator
import requests
from sqlalchemy import text

from inventory_hub.services import atomer_migration_mapping as mapping
from inventory_hub.services import atomer_new_native as native, stock_publication_source as transport
from inventory_hub.services.atomer_review_targets import ReviewError

# Only the public Atomer storefront is read. No account, authorized session or private feed URL is used.
STOREFRONT = 'https://www.xtrek.sk/'
BATCH = 25
PathName = Annotated[str, Field(min_length=1, max_length=260)]
OPEN = ('pending','sending')


class Selection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    root_id: int = Field(strict=True, gt=0)
    root_code: str = Field(min_length=1,max_length=100)
    paths: list[PathName] = Field(min_length=1,max_length=200)

    @field_validator('paths')
    @classmethod
    def exact_source_paths(cls, paths):
        if (len(set(paths))!=len(paths) or any(any(ord(c)<32 or ord(c)==127 for c in p)
                or any(v!=v.strip() or not v for v in p.split('/')) for p in paths)):
            raise ValueError('invalid_source_category_path')
        return sorted(paths,key=lambda p:(p.count('/'),p))


def stable_code(path):
    return 'atmg-'+sha256(path.encode('utf-8')).hexdigest()[:20]


def menu_from_html(raw):
    """Read only named category nodes from Atomer's public SK navigation tree."""
    tree=html.fromstring(raw,parser=html.HTMLParser(encoding='utf-8',no_network=True))
    roots=tree.xpath('//div[contains(concat(" ",normalize-space(@class)," ")," kategorie ")]/ul[@class="kategorie0"]')
    if len(roots)!=1:
        raise ValueError('unknown_storefront_menu')
    paths={}
    def visit(node,parents):
        for item in node.findall('li'):
            ident=re.search(r'(?:^|\s)cat_([1-9][0-9]*)(?:\s|$)',item.get('class',''))
            names=item.xpath('./a/strong')
            if not ident or len(names)!=1:
                raise ValueError('unknown_storefront_entry')
            name=' '.join(''.join(names[0].itertext()).split())
            if not name or '/' in name or len(parents)>12:
                raise ValueError('unknown_storefront_entry')
            path='/'.join((*parents,name))
            if path in paths:
                raise ValueError('ambiguous_storefront_category')
            paths[path]=int(ident.group(1))
            for children in item.findall('ul'):
                visit(children,(*parents,name))
    visit(roots[0],())
    if not paths or len(set(paths.values()))!=len(paths):
        raise ValueError('ambiguous_storefront_category')
    return paths


def current_menu():
    try:
        with requests.get(STOREFRONT,timeout=(4,12),stream=True,allow_redirects=False) as response:
            if response.status_code!=200 or 'text/html' not in response.headers.get('Content-Type','').lower():
                return {}
            content=bytearray()
            for part in response.iter_content(64*1024):
                content.extend(part)
                if len(content)>2_000_000:
                    return {}
        return menu_from_html(bytes(content))
    except (requests.RequestException,ValueError,TypeError,etree.ParserError):
        return {}


def _catalog_rows(fingerprint):
    client=transport._client('xtrek',fingerprint)
    try:
        rows=mapping.registry_pages(client,'categories')
        mapping.remember_category_snapshot(fingerprint,rows)
        return rows
    finally:
        client.session.close()


def _usable(row):
    return (row.get('active_yn') is True and row.get('target_category_id') is None
            and row.get('type') in ('site','siteWithProducts') and bool(mapping._sk(row)))


def _evidence(path,item,origin,intent):
    return dict(source_path=path,code=item['code'],category_id=item['category_id'],parent_id=item['parent_id'],
                title=mapping._sk(item),origin=origin,show_in_menu_yn=item.get('show_in_menu_yn'),
                menu_basis=intent.get('menu_basis','existing_native_setting'),
                source_menu_id=intent.get('source_menu_id'))


def inspect(selection, source_paths, native_rows, aliases, pinned, ledger, menu=None):
    """Decide from a complete native snapshot; return no remote side effects."""
    source_paths=set(source_paths)
    if set(selection.paths)-source_paths:
        raise ReviewError('category_restore_source_path_missing')
    # Validates unique IDs, complete ancestry, and cycles before any decision.
    resolved,_=mapping.category_options(native_rows,source_paths,aliases)
    by_id={r['category_id']:r for r in native_rows}
    siblings=defaultdict(list); codes=defaultdict(list)
    for r in native_rows:
        codes[mapping._fold(r.get('code'))].append(r)
        siblings[(r.get('parent_id'),mapping._fold(mapping._sk(r)))].append(r)
    root=by_id.get(selection.root_id,{})
    if (not _usable(root) or root.get('parent_id') is not None or root.get('code')!=selection.root_code
            or mapping._fold(mapping._sk(root))!='top menu'):
        raise ReviewError('category_restore_root_changed')
    for path,nid in pinned.items():
        if not _usable(by_id.get(nid,{})) or path in resolved and resolved[path]!=nid:
            raise ReviewError('new_category_mapping_changed')
        resolved[path]=nid
    result={}
    for path in selection.paths:
        parent,_,name=path.rpartition('/')
        row=ledger.get(path,{})
        journal=row.get('intent') or {}
        if row.get('stage')=='blocked':
            result[path]=('blocked',{'reason':row.get('problem') or 'category_restore_blocked'})
            continue
        parent_id=resolved.get(parent) if parent else root['category_id']
        if parent and parent in result:
            state,data=result[parent]
            if state!='verified':
                result[path]=('blocked',{'reason':'category_restore_parent_blocked'}) if state=='blocked' else ('waiting',{})
                continue
            parent_id=data['category_id']
        if not parent_id:
            result[path]=('blocked',{'reason':'category_restore_parent_unmapped'})
            continue
        code=stable_code(path)
        by_code=codes.get(mapping._fold(code),[])
        same_name=siblings.get((parent_id,mapping._fold(name)),[])
        expected=journal.get('payload') or {}
        recorded=(row.get('proof') or {}).get('category_id') or (row.get('ack') or {}).get('category_id')
        if len(by_code)>1 or len(same_name)>1 or (by_code and same_name and by_code[0]!=same_name[0]):
            result[path]=('blocked',{'reason':'category_restore_identity_collision'})
            continue
        match=(by_code or same_name or [None])[0]
        if match is not None:
            if (not _usable(match) or mapping._sk(match)!=name or match.get('parent_id')!=parent_id
                    or not match.get('code') or by_code and match.get('code')!=code
                    or recorded is not None and recorded!=match['category_id']
                    or path in resolved and resolved[path]!=match['category_id']
                    or expected and (match.get('code')!=code or match.get('type')!=expected['type']
                        or match.get('show_in_menu_yn') is not expected['show_in_menu_yn'])):
                result[path]=('blocked',{'reason':'category_restore_identity_changed'})
            else:
                resolved[path]=match['category_id']
                origin='created_verified' if journal else 'adopted_existing'
                result[path]=('verified',_evidence(path,match,origin,journal))
            continue
        if row.get('stage')=='verified' or recorded or path in resolved:
            result[path]=('blocked',{'reason':'category_restore_identity_changed'})
        elif row.get('stage')=='sending':
            result[path]=('unconfirmed',{'reason':'category_restore_not_observed'})
        else:
            source_id=(menu or {}).get(path)
            payload=dict(code=code,parent_id=parent_id,active_yn=True,type='siteWithProducts',
                         show_in_menu_yn=source_id is not None,descriptions=[dict(language='sk',name=name)])
            result[path]=('create',dict(payload=payload,source_path=path,source_menu_id=source_id,
                menu_basis='visible_in_atomer_storefront' if source_id is not None else 'unverified_hidden_from_menu'))
    return result


def _post(intents,fingerprint):
    client=transport._client('xtrek',fingerprint)
    try:
        return native._post(client,{'categories':[i['payload'] for i in intents.values()]},route='categories')
    finally:
        mapping.invalidate_categories(fingerprint)
        client.session.close()


def acknowledgements(intents,response):
    rows=response.get('categories') or []
    if not isinstance(rows,list) or not transport._messages_ok(response.get('messages')):
        return {}
    by_code=defaultdict(list)
    for row in rows:
        if isinstance(row,dict):
            by_code[row.get('code')].append(row)
    expected={i['payload']['code'] for i in intents.values()}
    if set(by_code)-expected:
        return {}
    result={}
    for path,intent in intents.items():
        matched=by_code.get(intent['payload']['code'],[])
        if len(matched)==1:
            row=matched[0]
            if (row.get('inserted_yn') is True and mapping._id(row.get('category_id'))
                    and transport._messages_ok(row.get('messages'))):
                result[path]={'category_id':row['category_id'],'code':row['code']}
    return result


def _json(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':'))


async def _set(db,c,path,stage,*,problem='',intent=None,proof=None,ack=None,delay=0,observe=False):
    await db.execute(text("""UPDATE atomer_catalog_category_restores SET stage=:stage,problem=:problem,
        intent=coalesce(CAST(:intent AS jsonb),intent),proof=coalesce(CAST(:proof AS jsonb),proof),
        ack=coalesce(CAST(:ack AS jsonb),ack),reads=reads+:observed,
        next_at=now()+make_interval(secs=>:delay),updated_at=now() WHERE campaign=:c AND path=:path"""),
        dict(c=c['id'],path=path,stage=stage,problem=problem,delay=delay,observed=int(observe),
             intent=_json(intent) if intent is not None else None,proof=_json(proof) if proof is not None else None,
             ack=_json(ack) if ack is not None else None))


async def next_retry(db,campaign_id):
    return await db.scalar(text("SELECT min(next_at) FROM atomer_catalog_category_restores WHERE campaign=:c AND stage IN ('pending','sending')"),{'c':campaign_id})


def _retry(exc):
    if getattr(exc,'status',None)==429 or type(exc).__name__=='AdmissionDenied':
        return max(1,min(86400,int(getattr(exc,'retry_after',None) or 300)))
    return None


async def _defer(db,c,candidates,exc):
    """Suspend only categories; already connected cards can still be processed."""
    delay=_retry(exc)
    if delay is None:
        return False
    for path,row in candidates.items():
        await _set(db,c,path,row['stage'],delay=delay,problem='category_restore_rate_limited')
    return True


async def _record_matches(db,c,proofs):
    if not proofs:
        return
    fresh={path:proof['category_id'] for path,proof in proofs.items()}
    for path,proof in proofs.items():
        await _set(db,c,path,'verified',proof=proof)
    await db.execute(text("""UPDATE atomer_catalog_campaigns SET
        selection=jsonb_set(selection,'{categories}',coalesce(selection->'categories','{}'::jsonb) || CAST(:ids AS jsonb)),
        options='{}'::jsonb,updated_at=now() WHERE id=:c"""),{'c':c['id'],'ids':_json(fresh)})
    c['selection']['categories']={**c['selection'].get('categories',{}),**fresh}
    c['options']={}


async def _current(db,c,plan):
    row=(await db.execute(text('SELECT state,selection FROM atomer_catalog_campaigns WHERE id=:c FOR UPDATE'),
                          {'c':c['id']})).mappings().one()
    if row['state'] not in ('running','waiting'):
        raise ReviewError('category_restore_selection_changed')
    current=row['selection']
    if not current.get('category_restore') or Selection.model_validate(current['category_restore'])!=plan:
        raise ReviewError('category_restore_selection_changed')
    c['selection']=dict(current)


async def advance(db,c,load_source,lock_current):
    """Advance one bounded level under the campaign worker's cross-transaction advisory lock.

    Acknowledgements never complete the operation. Every completion or restart verifies the
    full shop hierarchy and original source, and only an unsent POST may return to pending.
    """
    if not c['selection'].get('category_restore'):
        return False
    plan=Selection.model_validate(c['selection']['category_restore'])
    await _current(db,c,plan)
    await db.execute(text("""INSERT INTO atomer_catalog_category_restores(campaign,path)
        SELECT :c,unnest(CAST(:paths AS text[])) ON CONFLICT DO NOTHING"""),{'c':c['id'],'paths':plan.paths})
    rows=(await db.execute(text("""SELECT *,next_at<=now() AS due FROM atomer_catalog_category_restores
        WHERE campaign=:c AND path=ANY(CAST(:paths AS text[])) ORDER BY path FOR UPDATE"""),
        {'c':c['id'],'paths':plan.paths})).mappings().all()
    ledger={row['path']:dict(row) for row in rows}
    due={p:r for p,r in ledger.items() if r['stage'] in OPEN and r['due']}
    if not due:
        return False
    index=await load_source()
    if set(plan.paths)-set(index.category_paths):
        raise ReviewError('category_restore_source_path_missing')
    latest=c['selection']
    args=(plan,index.category_paths)
    bindings=(latest.get('category_aliases',{}),latest.get('categories',{}))
    menu=await asyncio.to_thread(current_menu) if any(r['stage']=='pending' for r in due.values()) else {}
    try:
        # A complete tree read by the campaign seconds ago can identify candidate
        # additions. It never proves adoption, a conflict, or an earlier POST.
        snapshot=mapping.recent_category_snapshot(c['shop_fingerprint'])
        try:
            findings=inspect(*args,snapshot,*bindings,ledger,menu) if snapshot is not None else {}
        except ReviewError:
            findings={}
        if not findings or any(findings[path][0] not in ('create','waiting') for path in due):
            snapshot=await asyncio.to_thread(_catalog_rows,c['shop_fingerprint'])
            findings=inspect(*args,snapshot,*bindings,ledger,menu)
    except Exception as exc:
        if await _defer(db,c,due,exc):
            return False
        raise
    changed=False; intents={}; proofs={}
    for path in plan.paths:
        if path not in due:
            continue
        state,info=findings[path]
        row=due[path]
        if state=='verified':
            proofs[path]=info;changed=True
        elif state=='blocked':
            await _set(db,c,path,'blocked',problem=info['reason']);changed=True
        elif state=='unconfirmed':
            if row['reads']>=2:
                await _set(db,c,path,'blocked',problem='category_restore_create_unconfirmed',observe=True)
            else:
                await _set(db,c,path,'sending',problem=info['reason'],delay=45,observe=True)
            changed=True
        elif state=='create' and len(intents)<BATCH:
            intents[path]=dict(**info,source_sha=c['source_sha'],admin_sha=c['admin_sha'],
                               shop_fingerprint=c['shop_fingerprint'],actor=c['actor'],
                               recorded_at=datetime.now(timezone.utc).isoformat())
        elif state=='waiting':
            # This level is waiting for independent evidence of its parent's native identity.
            await _set(db,c,path,'pending',delay=45)
    await _record_matches(db,c,proofs)
    if not intents:
        return changed
    for path,intent in intents.items():
        await _set(db,c,path,'sending',intent=intent,ack={})
    # A terminated worker leaves 'sending' on disk. A later worker observes that code and never resends it blindly.
    await db.commit()
    await lock_current()
    await _current(db,c,plan)
    try:
        before=await asyncio.to_thread(_catalog_rows,c['shop_fingerprint'])
    except Exception as exc:
        delay=_retry(exc)
        if delay:
            for path in intents:
                await _set(db,c,path,'pending',delay=delay,problem='category_restore_rate_limited')
            return False
        raise
    for path,intent in intents.items():
        ledger[path]={**ledger[path],'stage':'pending','intent':intent,'ack':{}}
    fresh=c['selection']
    check=inspect(*args,before,fresh.get('category_aliases',{}),fresh.get('categories',{}),ledger,menu)
    send={}; existing={}
    for path,intent in intents.items():
        state,observed=check[path]
        if state=='create' and observed['payload']==intent['payload']:
            send[path]=intent
        elif state=='verified':
            existing[path]={**observed,'origin':'observed_before_send'}
        else:
            await _set(db,c,path,'blocked',problem='category_restore_changed_before_send')
    await _record_matches(db,c,existing)
    if not send:
        return True
    try:
        answer=await asyncio.to_thread(_post,send,c['shop_fingerprint'])
    except Exception as exc:
        delay=_retry(exc)
        unsent=getattr(exc,'request_sent',None) is False or type(exc).__name__=='AdmissionDenied'
        if not isinstance(exc,transport.SourceError) and not delay:
            raise
        for path in send:
            await _set(db,c,path,'pending' if unsent and delay else 'sending',
                       delay=delay or 20,problem=getattr(exc,'code','category_restore_create_unconfirmed'))
        return not bool(delay)
    acknowledged=acknowledgements(send,answer)
    for path in send:
        await _set(db,c,path,'sending',ack=acknowledged.get(path,{}),
                   problem='' if path in acknowledged else 'category_restore_ack_unconfirmed')
    return True
