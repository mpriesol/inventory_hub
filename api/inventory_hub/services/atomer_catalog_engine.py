"""Opt-in, durable Atomer catalog migration. Only xTrek catalog records can be written."""
import asyncio
from hashlib import sha256
import json
import logging
from types import SimpleNamespace
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text

from inventory_hub import database
from inventory_hub.services import atomer_catalog_recheck as recheck, atomer_category_restore as restore
from inventory_hub.services import atomer_proposals as proposals, atomer_review as review
from inventory_hub.services import atomer_variant_repair as repair
from inventory_hub.services.atomer_review_targets import ReviewError

LOGGER = logging.getLogger(__name__)
WORKER_LOCK = 691433150
ACTIVE = ('running','waiting')
RUNNABLE = ('pending','prepared','sending','observe','cards','related','fidelity','updating')
MAX_LEAVES = 85
MAX_GROUPS = 30
Digest = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
Token = Annotated[str, Field(min_length=1,max_length=260)]
ID = Annotated[int, Field(gt=0,strict=True)]


class Selection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    run_id: ID
    source_sha: Digest
    admin_sha: Digest
    scope: Literal['catalog','families']
    families: list[Token] = Field(default_factory=list,max_length=500)
    working: dict[Token,ID] = Field(default_factory=dict,max_length=500)
    categories: dict[Token,ID] = Field(default_factory=dict,max_length=2000)
    category_restore: restore.Selection | None = None
    category_aliases: dict[Token,Token] = Field(default_factory=dict,max_length=2000)
    contextual_roots: list[Token] = Field(default_factory=list,max_length=100)
    property_names: dict[Token,Token] = Field(default_factory=dict,max_length=2000)
    axis_names: dict[Token,Token] = Field(default_factory=dict,max_length=2000)
    brands: dict[Token,Token] = Field(default_factory=dict,max_length=500)
    availability: dict[Token,ID] = Field(default_factory=dict,max_length=200)
    labels: dict[Token,ID] = Field(default_factory=dict,max_length=200)
    # Specific trusted mappings from the native comparison and complete source,
    # optionally supplied when source numeric axis IDs cannot be named otherwise.
    named_axes: dict[Token,dict[Token,Token]] = Field(default_factory=dict,max_length=40000)
    barcode_corrections: dict[Token,dict] = Field(default_factory=dict,max_length=4000)
    requeue: dict[Token,Digest] = Field(default_factory=dict,max_length=300)

    @model_validator(mode='after')
    def bounded(self):
        if self.scope=='families' and not self.families:
            raise ValueError('families_required')
        if len(set(self.families)) != len(self.families):
            raise ValueError('duplicate_family')
        if self.requeue and (self.scope!='families' or set(self.requeue)!=set(self.families)):
            raise ValueError('catalog_recheck_requires_exact_families')
        if any(not key.startswith(('v:','s:')) for key in self.families):
            raise ValueError('invalid_family')
        if any(not key.startswith(('item:','family:')) for key in self.working):
            raise ValueError('invalid_work_subject')
        return self


def _json(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':'))


async def _rows(db, sql, **values):
    return (await db.execute(text(sql),values)).mappings().all()


async def _one(db,sql,**values):
    return (await db.execute(text(sql),values)).mappings().one_or_none()


async def _exec(db,sql,**values):
    return await db.execute(text(sql),values)


async def _head(db):
    return await _one(db,'SELECT id,source_sha256,admin_export_sha256,checked_at FROM atomer_progress_runs ORDER BY id DESC LIMIT 1')


def _campaign_id(s):
    return sha256(f'xtrek/{s.source_sha}/{s.admin_sha}'.encode()).hexdigest()


def _wake(c,changed):
    if not c:
        return False
    if c['state']=='paused':
        return True
    reason=(c.get('problem') or '').casefold()
    quota=any(marker in reason for marker in ('limit upgates','rate_limited','budget_deferred','http 429'))
    return c['state']=='waiting' and not quota and bool(changed or c.get('failures',0))


async def process(db,selection,mode,actor):
    if mode not in ('preview','apply'):
        raise ReviewError('catalog_mode_invalid',422)
    await review._lock_current_run(db,selection.run_id)
    head=await _head(db)
    if (head['source_sha256'],head['admin_export_sha256'])!=(selection.source_sha,selection.admin_sha):
        raise ReviewError('variant_source_changed')
    cid=_campaign_id(selection)
    active=await _one(db,"SELECT id FROM atomer_catalog_campaigns WHERE state IN ('running','waiting')")
    if active and active['id']!=cid:
        raise ReviewError('catalog_other_campaign_active')
    current=await _one(db,'SELECT * FROM atomer_catalog_campaigns WHERE id=:c FOR UPDATE',c=cid)
    fp=repair.target_fingerprint('xtrek')
    if not fp or current and fp!=current['shop_fingerprint']:
        raise ReviewError('target_connection_changed')
    merged=dict(current['selection']) if current else {}
    fresh=selection.model_dump(exclude={'requeue'})
    for key in ('categories','category_aliases','property_names','axis_names','brands','availability','labels','named_axes','barcode_corrections','working'):
        fresh[key]={**merged.get(key,{}),**fresh[key]}
    fresh['contextual_roots']=sorted(set(merged.get('contextual_roots',[])+fresh['contextual_roots']))
    old_restore=merged.get('category_restore')
    if old_restore:
        requested=fresh.get('category_restore')
        if requested and (requested['root_id'],requested['root_code'])!=(old_restore['root_id'],old_restore['root_code']):
            raise ReviewError('category_restore_root_changed')
        fresh['category_restore']={**old_restore,'paths':sorted(
                set(old_restore['paths'])|set((requested or {}).get('paths',[])),key=lambda p:(p.count('/'),p))}
        restore.Selection.model_validate(fresh['category_restore'])
    fresh['families']=sorted(set(merged.get('families',[])+fresh['families']))
    if merged.get('scope')=='catalog':
        fresh['scope']='catalog'
    # Do not silently adopt unrelated in-flight work. Only a supplied revision of a
    # current non-destructive saved task in the selection may be adopted.
    for subject,revision in selection.working.items():
        kind,key=subject.split(':',1)
        saved=await review._record(db,kind,key)
        if saved['status']=='working' and saved['revision']==revision and saved['instruction']:
            continue
        if (merged.get('working') or {}).get(subject)==revision:
            continue  # idempotent registration after completion, never a new claim
        raise ReviewError('review_changed')
    entries=await _rows(db,"""
        SELECT i.family_key,count(*) AS size,min(CASE WHEN i.state IN ('needs_both','needs_target') THEN 1 ELSE 4 END) AS p
        FROM atomer_progress_items i WHERE i.run_id=:run AND (:all_ OR i.family_key=ANY(CAST(:keys AS text[])))
        GROUP BY i.family_key ORDER BY i.family_key
    """,run=head['id'],all_=selection.scope=='catalog',keys=selection.families)
    if {r['family_key'] for r in entries}.issuperset(selection.families) is False or not entries:
        raise ReviewError('subject_missing')
    pinned=set(selection.families)
    if selection.working:
        task_keys=[k.split(':',1)[1] for k in selection.working]
        pinned.update(k for k in task_keys if k.startswith(('s:','v:')))
        pinned.update(await db.scalars(text('SELECT DISTINCT family_key FROM atomer_progress_items WHERE run_id=:r AND code=ANY(CAST(:keys AS text[]))'),
                                        dict(r=head['id'],keys=task_keys)))
    count=sum(r['size'] for r in entries)
    options_changed=any(fresh.get(k)!=merged.get(k) for k in ('categories','category_aliases','contextual_roots','availability','labels'))
    if selection.requeue:
        if not current:
            raise ReviewError('catalog_recheck_changed')
        context={**current,'selection':fresh,'checked_run_id':head['id']}
        for key,pin in selection.requeue.items():
            job=await _one(db,'SELECT * FROM atomer_catalog_jobs WHERE campaign=:c AND family_key=:f FOR UPDATE',c=cid,f=key)
            if not job:
                raise ReviewError('catalog_recheck_changed')
            await recheck.reschedule(db,context,dict(job),pin,mode,actor)
        fresh=context['selection']
    if mode=='apply':
        wake=_wake(current,fresh!=merged or selection.requeue)
        await _exec(db,"""INSERT INTO atomer_catalog_campaigns
            (id,run_id,source_sha,admin_sha,shop_fingerprint,actor,selection,state,checked_run_id)
            VALUES(:c,:r,:s,:a,:fp,:actor,CAST(:config AS jsonb),'running',:r)
            ON CONFLICT(id) DO UPDATE SET selection=excluded.selection,
                options=CASE WHEN :new_options THEN '{}'::jsonb ELSE atomer_catalog_campaigns.options END,
                state=CASE WHEN atomer_catalog_campaigns.state='waiting' AND NOT :wake THEN 'waiting' ELSE 'running' END,
                next_at=CASE WHEN :wake THEN now() ELSE atomer_catalog_campaigns.next_at END,
                failures=CASE WHEN :wake THEN 0 ELSE atomer_catalog_campaigns.failures END,
                problem=CASE WHEN :wake THEN '' ELSE atomer_catalog_campaigns.problem END,
                updated_at=now(),checked_run_id=:r
        """,c=cid,r=head['id'],s=selection.source_sha,a=selection.admin_sha,fp=fp,actor=actor,config=_json(fresh),new_options=options_changed,wake=wake)
        if wake:
            await _exec(db,"""UPDATE atomer_catalog_jobs SET next_at=now(),attempts=0
                   WHERE campaign=:c AND stage IN ('pending','fidelity') AND problem LIKE '%stock_publication_source_unavailable%'
                   AND problem NOT ILIKE '%Limit Upgates%' AND problem NOT ILIKE '%HTTP 429%'""",c=cid)
        jobs=[dict(key=r['family_key'],size=r['size'],p=0 if r['family_key'] in pinned else r['p']) for r in entries]
        await _exec(db,"""INSERT INTO atomer_catalog_jobs(campaign,family_key,priority,leaf_count)
            SELECT :c,j->>'key',(j->>'p')::int,(j->>'size')::int FROM jsonb_array_elements(CAST(:jobs AS jsonb)) j
        ON CONFLICT(campaign,family_key) DO UPDATE SET priority=least(atomer_catalog_jobs.priority,excluded.priority),
            leaf_count=excluded.leaf_count
        """,c=cid,jobs=_json(jobs))
        await db.commit()
    return dict(status='verified' if mode=='apply' else 'ready',count=count,families=len(entries),campaign_id=cid,
                reason=f"{len(entries)} rodín, {count} predajných kódov. " + ('Dávka je uložená.' if mode=='apply' else 'Pripravené na spracovanie.'))


async def status(db):
    campaign=await _one(db,'SELECT * FROM atomer_catalog_campaigns ORDER BY updated_at DESC LIMIT 1')
    if not campaign:
        return dict(campaign_id=None,state='idle',counts={},family_states={},recent=[],blocked=[])
    cid=campaign['id']
    grouped=await _rows(db,"""SELECT j.stage,count(DISTINCT j.family_key) AS n,count(*) AS leaves
          FROM atomer_progress_items i JOIN atomer_catalog_jobs j ON j.campaign=:c AND j.family_key=i.family_key
          WHERE i.run_id=:run GROUP BY j.stage""",c=cid,run=campaign['checked_run_id'])
    counts=dict(families=sum(x['n'] for x in grouped),total=sum(int(x['leaves']) for x in grouped))
    proof_from=""" FROM atomer_catalog_proofs p JOIN atomer_progress_items i ON i.run_id=:run AND i.code=p.code
                  AND i.family_key=p.family_key WHERE p.campaign=:c"""
    proof=await _rows(db,'SELECT p.state,count(*) AS n'+proof_from+' GROUP BY p.state',c=cid,run=campaign['checked_run_id'])
    for state in ('verified','partial','blocked','excluded'):
        counts[state]=next((r['n'] for r in proof if r['state']==state),0)
    scopes=await _one(db,"""SELECT count(*) FILTER(WHERE p.state='verified' AND evidence->'xtrek'->>'product_id' IS NOT NULL
          AND evidence->>'scope' IS DISTINCT FROM 'biketrek') AS xtrek_verified,
          count(*) FILTER(WHERE p.state='verified' AND evidence->>'scope'='biketrek') AS biketrek_only,
          count(*) FILTER(WHERE evidence->>'card_id' IS NOT NULL AND evidence->'biketrek'->>'product_id' IS NOT NULL) AS biketrek_linked,
          count(*) FILTER(WHERE evidence->>'verification_scope'='identity_card_and_mapped_content' AND p.state='verified') AS content_verified"""
          +proof_from,c=cid,run=campaign['checked_run_id'])
    counts.update(dict(scopes))
    counts['pending']=counts['total']-sum(counts[x] for x in ('verified','partial','blocked','excluded'))
    fields='family_key,stage,problem AS reason,leaf_count AS leaves,updated_at'
    recent=await _rows(db,f"SELECT {fields} FROM atomer_catalog_jobs WHERE campaign=:c AND stage<>'pending' ORDER BY updated_at DESC,family_key LIMIT 16",c=cid)
    blocked=await _rows(db,f"SELECT {fields} FROM atomer_catalog_jobs WHERE campaign=:c AND stage IN ('blocked','partial') ORDER BY priority,updated_at DESC,family_key LIMIT 100",c=cid)
    return dict(campaign_id=cid,state=campaign['state'],run_id=campaign['run_id'],checked_run_id=campaign['checked_run_id'],
                updated_at=campaign['updated_at'],next_retry_at=campaign['next_at'] if campaign['state']=='waiting' else None,
                last_problem=campaign['problem'],counts=counts,family_states={r['stage']:r['n'] for r in grouped},
                recent=[dict(r) for r in recent],blocked=[dict(r) for r in blocked])


async def details(db,family_key):
    c=await _one(db,'SELECT id FROM atomer_catalog_campaigns ORDER BY updated_at DESC LIMIT 1')
    if not c:
        return dict(family_key=family_key,items=[])
    rows=await _rows(db,"""SELECT code,state,evidence,checked_at FROM atomer_catalog_proofs
                       WHERE campaign=:c AND family_key=:f ORDER BY code""",c=c['id'],f=family_key)
    j=await _one(db,'SELECT * FROM atomer_catalog_jobs WHERE campaign=:c AND family_key=:f',c=c['id'],f=family_key)
    h=await _head(db)
    token=None
    if j and h and j['stage'] in ('partial','blocked'):
        current,group=await _family(db,h['id'],family_key)
        token=recheck.revision(j,h['id'],current,group)
    return dict(family_key=family_key,run_id=h['id'] if h else None,stage=j['stage'] if j else None,
                recheck_revision=token,items=[dict(r) for r in rows])


async def _job(db,c,f,stage,*,problem='',guards=None,document=None,receipt=None,delay=0,increment=False):
    await _exec(db,"""UPDATE atomer_catalog_jobs SET stage=:stage,problem=:problem,updated_at=now(),
        next_at=now()+make_interval(secs=>:delay),attempts=attempts+:inc,
        guards=coalesce(CAST(:guards AS jsonb),guards),document=coalesce(CAST(:doc AS jsonb),document),
        receipt=coalesce(CAST(:receipt AS jsonb),receipt) WHERE campaign=:c AND family_key=:f""",
        stage=stage,problem=problem[:3900],delay=delay,inc=int(increment),c=c,f=f,
        guards=None if guards is None else _json(guards),doc=None if document is None else _json(document),
        receipt=None if receipt is None else _json(receipt))


async def _proofs(db,c,f,proofs):
    if not proofs:
        return
    records=[dict(code=k,state=state,evidence=v) for k,(state,v) in proofs.items()]
    await _exec(db,"""INSERT INTO atomer_catalog_proofs(campaign,family_key,code,state,evidence)
        SELECT :c,:f,r->>'code',r->>'state',r->'evidence' FROM jsonb_array_elements(CAST(:rows AS jsonb)) r
        ON CONFLICT(campaign,code) DO UPDATE SET state=excluded.state,evidence=excluded.evidence,checked_at=now()
    """,c=c,f=f,rows=_json(records))


async def _family(db,run,key):
    rows=await _rows(db,"""SELECT i.code,i.detail,w.revision,w.status,w.scope,w.instruction,w.result,
                  (SELECT command FROM atomer_review_events e WHERE e.kind='item' AND e.key=w.key
                   AND e.revision=w.revision) AS command
           FROM atomer_progress_items i LEFT JOIN atomer_review_work w ON w.kind='item' AND w.key=i.code
           WHERE i.run_id=:r AND i.family_key=:k ORDER BY i.code""",r=run,k=key)
    group=await review._record(db,'family',key)
    if group.get('revision') and group['status']=='open':
        group['command']=await db.scalar(text("SELECT command FROM atomer_review_events WHERE kind='family' AND key=:key AND revision=:rev"),
                                         dict(key=key,rev=group['revision']))
    return {r['code']:dict(r) for r in rows},group


def _guard(row):
    return [row.get('revision') or 0,row.get('status') or 'open',row.get('scope') or 'default']


def _blocked_work(row,kind,key,allow):
    state=row.get('status') or 'open'
    if state=='working' and allow.get(f'{kind}:{key}')!=row['revision']:
        return 'Úlohu spracúva iný pracovník.'
    if state=='blocked':
        return row.get('result') or 'Existujúca blokovaná úloha vyžaduje vyriešenie.'
    if state=='open' and (row.get('revision') or 0)>0 and row.get('command')!='save':
        return 'Otvorená úloha bola ručne odvolaná alebo upravená.'
    if state=='queued':
        instruction=row.get('instruction') or ''
        if not instruction or any(line not in proposals.INSTRUCTIONS.values() for line in instruction.splitlines()):
            return 'Vlastný uložený pokyn vyžaduje samostatné spracovanie.'
    return None


def _receipt_text(code,proof):
    if proof.get('reason'):
        return str(proof['reason'])[:3600]
    return f"{code} · karta {proof.get('card_id')}; xTrek {proof.get('xtrek')}; BIKETREK {proof.get('biketrek')}. Overené v migračnom behu."


async def _mutate_work(db,kind,key,row,command,actor,result=''):
    before=row.get('revision') or 0
    if command=='save' and before:
        effect=await _exec(db,"""UPDATE atomer_review_work SET revision=revision+1,status='queued',instruction=:i,
                     updated_at=now(),updated_by=:actor WHERE kind=:kind AND key=:key AND status='open' AND revision=:rev""",
                     i=result,actor=actor,kind=kind,key=key,rev=before)
        if effect.rowcount!=1:
            raise ReviewError('review_changed')
    elif command=='save':
        await _exec(db,"""INSERT INTO atomer_review_work(kind,key,revision,status,scope,decision,note,instruction,result,updated_by)
                  VALUES(:kind,:key,1,'queued',:scope,'undecided','',:instruction,'',:actor)""",
                  kind=kind,key=key,scope=row.get('scope') or 'default',instruction=result,actor=actor)
    else:
        stage={'claim':'working','catalog_verify':'working','complete':'done','block':'blocked'}[command]
        effect=await _exec(db,"""UPDATE atomer_review_work SET revision=revision+1,status=:stage,result=:result,
                   updated_by=:actor,updated_at=now() WHERE kind=:kind AND key=:key AND revision=:rev AND status=:expected""",
                   stage=stage,result=result,actor=actor,kind=kind,key=key,rev=before,expected='done' if command=='catalog_verify' else 'queued' if command=='claim' else 'working')
        if effect.rowcount!=1:
            raise ReviewError('review_changed')
    await review._audit(db,kind,key,command,actor)
    new_status={'save':'queued','claim':'working','catalog_verify':'working','complete':'done','block':'blocked'}[command]
    return {**row,'revision':before+1,'status':new_status}


async def _claim(db,c,j,rows,group):
    """Lock current selection and snapshot every sibling, including operator-owned exceptions."""
    f=j['family_key']; allow=c['selection']['working']; own=[]; skip={}; problems={}
    if group['status']=='done':
        skip={k:'Ručne uzavretá rodina.' for k in rows}
    elif (reason:=_blocked_work(group,'family',f,allow)):
        problems={k:reason for k in rows}
    else:
        group_manages=group['status'] in ('working','queued')
        for code,r in rows.items():
            if r['status']=='done' and r.get('command')=='done':
                skip[code]='Ručne uzavretá položka.'
            elif (reason:=_blocked_work(r,'item',code,allow)):
                problems[code]=reason
        # A family's one explicit instruction is claimed only if its children have no separate active instructions.
        if group_manages and any(r['status'] in ('working','queued','blocked') for r in rows.values()):
            problems.update({k:'Rodina má samostatné pokyny variantov alebo iný rozsah.' for k in rows if k not in skip})
        if group_manages and not problems and len(skip)<len(rows):
            if group['status']=='queued':
                group=await _mutate_work(db,'family',f,group,'claim',c['actor'])
            own.append(['family',f,group['revision']])
        if not group_manages:
            for k,r in rows.items():
                if k in skip or k in problems:
                    continue
                if r['status']=='done':
                    command=r.get('command') or ''
                    known_complete=command=='complete' and r.get('instruction')==proposals.INSTRUCTIONS['verify']
                    if not (known_complete or command.startswith(('generation_','verified_','catalog_'))):
                        skip[k]='Existujúce ručné uzavretie.'
                        continue
                    # Preserve the completed action in the audit; this claim covers
                    # the newly approved all-available-shop identity verification.
                    r=await _mutate_work(db,'item',k,r,'catalog_verify',c['actor'])
                if not r['status'] or r['status']=='open':
                    scope=r.get('scope') if r.get('scope') not in (None,'default') else group['scope']
                    instr='\n'.join(proposals.INSTRUCTIONS[t] for t in proposals.suggestions(r['detail'],scope))
                    r=await _mutate_work(db,'item',k,{**r,'scope':scope},'save',c['actor'],instr)
                if r['status']=='queued':
                    r=await _mutate_work(db,'item',k,r,'claim',c['actor'])
                if r['status']=='working':
                    own.append(['item',k,r['revision']])
                rows[k]=r
    scopes={k:r.get('scope') if r.get('scope') not in (None,'default') else group['scope'] for k,r in rows.items()}
    guards=dict(group=_guard(group),rows={k:_guard(r) for k,r in rows.items()},scope=scopes,own=own,excluded=skip,blocked=problems)
    await _proofs(db,c['id'],f,{**{k:('excluded',dict(reason=v)) for k,v in skip.items()},
                               **{k:('blocked',dict(reason=v)) for k,v in problems.items()}})
    return guards


def _selected(guards):
    return [k for k in guards['rows'] if k not in guards['excluded'] and k not in guards['blocked']]


async def _guarded(db,c,j):
    rows,group=await _family(db,c['checked_run_id'],j['family_key'])
    g=j['guards']
    if _guard(group)!=g['group'] or {k:_guard(r) for k,r in rows.items()}!=g['rows']:
        return None
    return rows


async def _finish(db,c,j,proofs):
    g=j['guards']; f=j['family_key']
    proof_states=[value[0] for value in proofs.values()]
    complete=all(s=='verified' for s in proof_states) and not g['blocked']
    parts=list(dict.fromkeys(str(v.get('reason','')) for s,v in proofs.values() if s!='verified' and v.get('reason')))
    parts.extend(reason for reason in g['blocked'].values() if reason not in parts)
    parts=list(dict.fromkeys(parts)); reason=' | '.join(parts)[:3600]
    for kind,key,rev in g['own']:
        if kind=='family':
            family_receipt=' | '.join(_receipt_text(k,v) for k,(s,v) in proofs.items() if s=='verified')
            cmd='complete' if complete else 'block'
            msg=family_receipt if complete else (reason or 'Treba doriešiť položky v rodine.')
        elif key in proofs:
            s,v=proofs[key]; cmd='complete' if s=='verified' else 'block'; msg=_receipt_text(key,v)
        else:
            continue
        await _mutate_work(db,kind,key,{'revision':rev},cmd,c['actor'],msg[:3900])
    await _proofs(db,c['id'],f,proofs)
    if complete:
        stage='verified' if proofs else 'excluded'
    else:
        stage='partial' if any(s in ('partial','verified') for s in proof_states) else 'blocked'
    await _job(db,c['id'],f,stage,problem=reason)


def _ready_batch(jobs):
    """Choose one phase per pass, subject to the native leaf and parent limits."""
    if not jobs:
        return []
    chosen=[]; size=0
    for j in jobs:
        n=j['leaf_count']
        if j['stage']!=jobs[0]['stage'] or len(chosen)>=MAX_GROUPS or (chosen and (j.get('receipt',{}).get('isolate') or chosen[0].get('receipt',{}).get('isolate'))):
            break
        if chosen and size+n>MAX_LEAVES:
            break
        chosen.append(dict(j)); size+=n
    return chosen


_CATALOG={}
async def _source(db,c):
    from inventory_hub.services import atomer_new_source as src
    key=c['source_sha'],c['admin_sha']
    if _CATALOG.get('key')!=key:
        archives=await _rows(db,"SELECT sha256,compressed FROM atomer_review_exports WHERE sha256 IN (:s,:a)",s=key[0],a=key[1])
        blobs={r['sha256']:bytes(r['compressed']) for r in archives}
        if set(blobs)!=set(key):
            raise ReviewError('catalog_archives_missing')
        catalog=await asyncio.to_thread(src.read_catalog,blobs[key[0]],blobs[key[1]],*key)
        _CATALOG.clear();_CATALOG.update(key=key,index=catalog)
    return _CATALOG['index']


def _registry(c,index):
    from inventory_hub.services import atomer_migration_mapping as mapping, stock_publication_source as transport
    selection=c['selection']
    client=transport._client('xtrek',c['shop_fingerprint'])
    try:
        reg=mapping.read_registry(client,availability_ids=selection.get('availability',{}).values())
    finally:
        client.session.close()
    rows=reg.pop('category_registry')
    allowed={r['category_id'] for r in rows if r.get('active_yn') is True and not r.get('target_category_id')}
    cats,issues=mapping.category_options(rows,index.category_paths,selection['category_aliases'])
    for name,native_id in selection['categories'].items():
        if native_id not in allowed or name in cats and cats[name]!=native_id:
            raise ReviewError('new_category_mapping_changed')
        cats[name]=native_id;issues.pop(name,None)
    for field in ('labels','availability'):
        verified={k.casefold():v for k,v in reg[field].items()}
        for name,vid in selection[field].items():
            if verified.get(name.casefold())!=vid:
                raise ReviewError('new_registry_binding_changed')
    return dict(**reg,categories=cats,category_issues=issues,category_context=mapping.category_contexts(
                rows,index.category_paths,cats,selection.get('contextual_roots',[])))


async def _options(db,c,index):
    if c['options']:
        return c['options']
    options=await asyncio.to_thread(_registry,c,index)
    await _exec(db,'UPDATE atomer_catalog_campaigns SET options=CAST(:o AS jsonb) WHERE id=:c',
                o=_json(options),c=c['id'])
    c['options']=options
    return options


async def _root_index(db,c):
    """Translate Atomer's own parent references to verified xTrek catalog codes."""
    pairs=await _rows(db,"""
       SELECT DISTINCT family_key,detail->>'target_parent' AS parent,detail->>'target_code' AS leaf
       FROM atomer_progress_items WHERE run_id=:r AND detail->>'product_id' IS NOT NULL
       AND state IN ('linked','needs_card','needs_link')
    """,r=c['checked_run_id'])
    canonical={}
    for p in pairs:
        k=p['family_key'][2:]; v=p['parent'] or p['leaf']
        if v:
            canonical.setdefault(k,set()).add(v)
    created=await _rows(db,"""SELECT family_key FROM atomer_catalog_jobs WHERE campaign=:c
           AND receipt ? 'product_id' AND (receipt->>'identity_verified')='true'""",c=c['id'])
    mapping={k:next(iter(v)) for k,v in canonical.items() if len(v)==1}
    mapping.update({r['family_key'][2:]:r['family_key'][2:] for r in created})
    return mapping


def _make_document(index,f,rows,g,options,selection,roots):
    from inventory_hub.services import atomer_new_source as src
    key=index.canonical(f[2:])
    csv,xml=index.family(key)
    family=SimpleNamespace(parent=key,brand=selection['brands'].get(key,''),buyers=bool(xml),
                           axis_names=selection['axis_names'],named_axes=selection.get('named_axes',{}))
    csv_refs={ref:index.parent_for(ref) for ref in [x.strip() for x in src.named(csv[key],'Súvisiace tovary').split(',') if x.strip()]}
    refs={ref:roots[parent] for ref,parent in csv_refs.items() if parent in roots}
    cfg={**options,'reference_codes':{**roots,**refs},'property_names':selection.get('property_names',{}),'barcode_corrections':selection.get('barcode_corrections',{}),'named_axes':selection.get('named_axes',{})}
    doc=src.build(family,{k:r['detail'] for k,r in rows.items()},csv,xml,cfg,set(roots.values()))
    wanted={k for k in _selected(g) if g['scope'][k]!='biketrek'}
    product=doc['payload']
    if product.get('variants'):
        product['variants']=[v for v in product['variants'] if v['code'] in wanted]
        if not product['variants']:
            raise ReviewError('catalog_scope_empty')
        if not any(v.get('main_yn') for v in product['variants']):
            product['variants'][0]['main_yn']=True
    if len(_json(doc).encode('utf-8'))>15_000_000:
        raise ReviewError('catalog_family_exceeds_native_limit')
    return doc


async def _prepare(db,c,jobs):
    index=None; options=None; roots=None
    for j in jobs:
        f=j['family_key']; rows,group=await _family(db,c['checked_run_id'],f)
        if not rows:
            await _job(db,c['id'],f,'blocked',problem='Skupina chýba v súčasnom zdroji.')
            continue
        if j['guards'] is not None and not await _checked(db,c,[j]):
            continue
        guards=await _claim(db,c,j,rows,group) if j['guards'] is None else j['guards']
        j['guards']=guards
        await _job(db,c['id'],f,'pending',guards=guards)
        keys=_selected(guards)
        if not keys:
            await _finish(db,c,j,{})
            continue
        target_keys=[k for k in keys if guards['scope'][k]!='biketrek']
        has_target=[k for k in target_keys if rows[k]['detail'].get('product_id')]
        if len(has_target)==len(target_keys):
            await _job(db,c['id'],f,'cards')
            continue
        if any(rows[k]['detail']['state']=='review' for k in target_keys):
            proof={k:('blocked',dict(reason='Treba vyriešiť identitu z aktuálneho reportu: '+', '.join(rows[k]['detail']['reasons']),
                                      ean=rows[k]['detail']['ean'])) for k in keys}
            await _finish(db,c,j,proof)
            continue
        if index is None:
            index=await _source(db,c)
            options=await _options(db,c,index)
            roots=await _root_index(db,c)
        try:
            doc=await asyncio.to_thread(_make_document,index,f,rows,guards,options,c['selection'],roots)
        except (ReviewError, ValueError, ArithmeticError) as err:
            code=getattr(err,'code','new_source_value_invalid')
            await _finish(db,c,j,{k:('blocked',dict(reason=str(code),ean=rows[k]['detail']['ean'])) for k in keys})
            continue
        if has_target:
            pids={rows[k]['detail']['product_id'] for k in has_target}
            if len(pids)!=1:
                await _finish(db,c,j,{k:('blocked',dict(reason='Varianty majú rôznych existujúcich rodičov xTrek.')) for k in keys})
                continue
            doc['extension']=dict(product_id=int(next(iter(pids))),existing={
                k:dict(product_id=int(rows[k]['detail']['product_id']),variant_id=rows[k]['detail']['variant_id'])
                for k in has_target})
        elif len(doc['payload'].get('variants',[]))>MAX_LEAVES:
            # Preserve the real main variant in the initial tranche. The complete,
            # immutable planned family is reconstructed from the remaining leaves.
            variants=doc['payload']['variants']
            ordered=sorted(variants,key=lambda v:not v.get('main_yn'))
            doc['payload']['variants']=ordered[:MAX_LEAVES]
            doc['remaining_create_variants']=ordered[MAX_LEAVES:]
        await _job(db,c['id'],f,'prepared',document=doc,receipt={**j['receipt'],'dispatched':False})


async def _checked(db,c,jobs):
    checked=[]
    for j in jobs:
        rows=await _guarded(db,c,j)
        if rows is None:
            reason='Medzitým sa zmenil uložený pokyn alebo rozsah. Overiť poslednú revíziu.'
            await _job(db,c['id'],j['family_key'],'blocked',problem=reason)
            await _proofs(db,c['id'],j['family_key'],{k:('blocked',dict(reason=reason)) for k in _selected(j['guards'])})
        else:
            checked.append(j)
    return checked


async def _absent_in_report(db,c,jobs):
    """A newer comparable report may have paired an SKU or GTIN under another identity."""
    remaining=[]
    for job in jobs:
        records,_=await _family(db,c['checked_run_id'],job['family_key'])
        codes=recheck.prior_codes(job['document'])
        current=[records[k]['detail'] for k in codes if k in records]
        if len(current)==len(codes) and all(
                d['state'] in ('needs_both','needs_target') and not any(d.get(k) for k in
                ('product_id','variant_id','reasons','candidates','target_code','target_ean')) for d in current):
            remaining.append(job)
            continue
        reason='Aktuálny report už nepotvrdzuje voľnú identitu celej rodiny. Vytvorenie sa neodoslalo.'
        await _job(db,c['id'],job['family_key'],job['stage'],receipt={**job['receipt'],'dispatched':False})
        await _finish(db,c,job,{k:('blocked',dict(reason=reason)) for k in _selected(job['guards'])})
    return remaining


async def _updated_barcodes(db,c,jobs):
    """A corrected GTIN was not necessarily part of the original complete report."""
    from inventory_hub.services import atomer_card_batch as cards
    from inventory_hub.services.product_identity import barcode_keys
    wanted={}
    for j in jobs:
        doc=j['document']
        corrected=doc.get('barcode_corrections') or {}
        present=set((doc.get('extension') or {}).get('existing') or {})
        for code,proof in corrected.items():
            if code not in present:
                for key in barcode_keys([proof['ean']]):
                    wanted.setdefault(key,set()).add(j['family_key'])
    if not wanted:
        return jobs
    # Compare every older xTrek identity, including native-only siblings. The
    # ordinary preflight adds any catalog changes since this fresh full read.
    live=await cards.load_shop_catalog('xtrek')
    conflicts={k for owners in wanted.values() if len(owners)>1 for k in owners}
    for product in live:
        for leaf in [product,*(product.get('variants') or [])]:
            for gtin in barcode_keys([leaf.get('ean')]):
                conflicts.update(wanted.get(gtin,()))
    remaining=[]
    for j in jobs:
        if j['family_key'] in conflicts:
            await _finish(db,c,j,{k:('blocked',dict(reason='Doložený GTIN je už obsadený v naraz pripravenej dávke alebo v xTreku.'))
                                  for k in _selected(j['guards'])})
        else:
            remaining.append(j)
    return remaining


async def _lock_current(db,c):
    head=await _head(db)
    if not head or (c['source_sha'],c['admin_sha'])!=(head['source_sha256'],head['admin_export_sha256']):
        raise ReviewError('catalog_source_changed')
    await review._lock_current_run(db,head['id'])
    if repair.target_fingerprint('xtrek')!=c['shop_fingerprint']:
        raise ReviewError('catalog_connection_changed')
    c['checked_run_id']=head['id']; c['checked_at']=head['checked_at']
    return head


def _budget_error(e):
    return (getattr(e,'status',None)==429 or getattr(e,'status_code',None)==429 or
            getattr(e,'code',None)=='stock_publication_rate_limited' or
            'budget_deferred' in str(e) or type(e).__name__=='AdmissionDenied')


async def _dispatch(db,c,jobs):
    from inventory_hub.services import atomer_new_native as native
    jobs=await _checked(db,c,jobs)
    jobs=await _updated_barcodes(db,c,jobs)
    if not jobs:
        return
    # Extensions are journaled separately; never send an existing parent to the create endpoint.
    extending=[j for j in jobs if j['document'].get('extension')]
    new=[j for j in jobs if not j['document'].get('extension')]
    for j in extending:
        if not hasattr(native,'prepare_extensions'):
            reason='Existujúci rodič vyžaduje overenie chýbajúcich variantov pred rozšírením.'
            await _finish(db,c,j,{k:('blocked',dict(reason=reason)) for k in _selected(j['guards'])})
        else:
            await _extend(db,c,j)
    new=await _absent_in_report(db,c,new)
    if not new:
        return
    checked=await asyncio.to_thread(native.preflight,[j['document'] for j in new],c['shop_fingerprint'],c['checked_at'])
    selected=[]
    for j in new:
        doc=j['document']; code=doc['payload']['code']
        issues=checked[code]
        if issues:
            await _finish(db,c,j,{k:('blocked',dict(reason='; '.join(issues),ean=next((v.get('ean') for v in doc['payload'].get('variants',[]) if v['code']==k),doc['payload'].get('ean'))))
                                  for k in _selected(j['guards'])})
        else:
            await _job(db,c['id'],j['family_key'],'sending',receipt={**j['receipt'],'dispatched':True})
            selected.append(j)
    if not selected:
        return
    # The intent is durable before the outbound POST. A worker observing 'sending'
    # after a crash can only read. A definitive local quota denial can be retried.
    send_run=c['checked_run_id']
    await db.commit()
    await _lock_current(db,c)
    selected=await _checked(db,c,selected)
    if selected and c['checked_run_id']!=send_run:
        # Nothing has been sent. Re-prepare against the newer run even if its
        # work revisions are equal, including its full-catalog duplicate baseline.
        for j in selected:
            await _job(db,c['id'],j['family_key'],'prepared',receipt={**j['receipt'],'dispatched':False})
        return
    if not selected:
        return
    try:
        result=await asyncio.to_thread(native.send,[j['document'] for j in selected],c['shop_fingerprint'])
    except Exception as err:
        if _budget_error(err) and getattr(err,'request_sent',None) is False:
            for j in selected:
                await _job(db,c['id'],j['family_key'],'prepared',receipt={**j['receipt'],'dispatched':False})
            await db.commit()
            raise
        for j in selected:
            await _job(db,c['id'],j['family_key'],'observe',problem='Odpoveď na vytvorenie treba overiť v xTreku.')
        return
    for j in selected:
        ack=result.get(j['document']['payload']['code'],{})
        await _job(db,c['id'],j['family_key'],'observe',receipt={**j['receipt'],'ack':ack,'dispatched':True})


async def _extend(db,c,j):
    # Implemented using the same ownership and crash recovery rules as a create.
    from inventory_hub.services import atomer_new_native as native
    doc=j['document']
    try:
        code=doc['payload']['code']
        anchor=doc['extension']
        expected={code:dict(product_id=anchor['product_id'],variant_ids={k:int(v['variant_id']) for k,v in anchor['existing'].items() if v.get('variant_id')})}
        new_codes=[v['code'] for v in doc['payload']['variants'] if v['code'] not in anchor['existing']][:MAX_LEAVES]
        prepared=(await asyncio.to_thread(native.prepare_extensions,[doc],c['shop_fingerprint'],c['checked_at'],expected,{code:new_codes}))[code]
        if prepared['status']!='ready':
            await _finish(db,c,j,{k:('blocked',dict(reason='; '.join(prepared['issues']))) for k in _selected(j['guards'])})
            return
    except ReviewError as e:
        await _finish(db,c,j,{k:('blocked',dict(reason=e.code)) for k in _selected(j['guards'])})
        return
    doc={**doc,'extension_plan':prepared}
    if len(_json(prepared.get('payload',{})).encode('utf-8'))>7_500_000:
        await _finish(db,c,j,{k:('blocked',dict(reason='Dávka je väčšia než overený limit rozhrania.')) for k in _selected(j['guards'])})
        return
    await _job(db,c['id'],j['family_key'],'sending',document=doc,receipt={**j['receipt'],'dispatched':True})
    await db.commit()
    await _lock_current(db,c)
    if not await _checked(db,c,[j]):
        return
    try:
        ack=await asyncio.to_thread(native.send_extensions,{doc['payload']['code']:prepared},c['shop_fingerprint'])
        if ack[doc['payload']['code']]['status']=='conflict':
            reason='; '.join(ack[doc['payload']['code']]['issues'])
            await _finish(db,c,j,{k:('blocked',dict(reason=reason)) for k in _selected(j['guards'])})
            return
    except Exception as e:
        if _budget_error(e) and getattr(e,'request_sent',None) is False:
            await _job(db,c['id'],j['family_key'],'prepared')
            await db.commit()
            raise
    await _job(db,c['id'],j['family_key'],'observe')


def _expected(j):
    r=j['receipt']; ack=r.get('ack') or {}
    pid=r.get('product_id') or ack.get('product_id')
    return dict(product_id=int(pid),variant_ids=r.get('variants') or ack.get('variant_ids') or {}) if pid else None


async def _observe(db,c,jobs):
    from inventory_hub.services import atomer_new_native as native
    jobs=await _checked(db,c,jobs)
    if not jobs:
        return
    normal=[j for j in jobs if not j['document'].get('extension_plan')]
    by_code={j['document']['payload']['code']:j for j in normal}
    exp={k:_expected(j) for k,j in by_code.items() if _expected(j)}
    seen=await asyncio.to_thread(native.observe,[j['document'] for j in normal],c['shop_fingerprint'],exp) if normal else {}
    for j in jobs:
        doc=j['document']; code=doc['payload']['code']
        if doc.get('extension_plan'):
            obs=(await asyncio.to_thread(native.observe_extensions,[doc],c['shop_fingerprint'],{code:doc['extension_plan']}))[code]
        else:
            obs=seen[code]
        summary={k:v for k,v in obs.items() if k!='native'}
        receipt={**j['receipt'],**summary}
        reason='; '.join(str(x) for x in obs.get('issues',[]) + (j['receipt'].get('ack') or {}).get('issues',[]))
        if receipt.get('rebuild_after_observation'):
            active={k for k in _selected(j['guards']) if _scope_of(j['guards'],k)!='biketrek'}
            if active!=recheck.prior_codes(doc):
                # The previous commitment is now observed. A scope revision may
                # prohibit parent/leaf writes previously authorized by that intent.
                why='Zmenil sa rozsah variantov od pôvodného odoslania. Stav xTrek bol načítaný, ďalší zápis je zastavený.'
                await _job(db,c['id'],j['family_key'],j['stage'],receipt=receipt)
                await _finish(db,c,j,{k:('partial' if obs['identity_verified'] else 'blocked',
                            dict(reason=why,product_id=obs.get('product_id'),variants=obs.get('variants')))
                            for k in _selected(j['guards'])})
                continue
            if obs['identity_verified']:
                receipt.update(content_requested=True,rebuild_after_observation=False)
        if obs['identity_verified'] and (doc.get('remaining_create_variants') or obs.get('remaining_variants')):
            # Each completed tranche supplies its actual PID/VID anchors for the
            # next add-only request. Keep the same product and every old variant.
            all_variants=[*doc['payload']['variants'],*doc.get('remaining_create_variants',[])]
            wanted={v['code'] for v in all_variants}
            existing={k:dict(product_id=obs['product_id'],variant_id=vid)
                      for k,vid in obs['variants'].items() if k in wanted}
            if len(existing)<len(wanted):
                next_doc={k:v for k,v in doc.items() if k not in ('extension_plan','remaining_create_variants')}
                next_doc['payload']={**doc['payload'],'variants':all_variants}
                next_doc['extension']=dict(product_id=obs['product_id'],existing=existing)
                await _job(db,c['id'],j['family_key'],'prepared',receipt=receipt,document=next_doc)
                continue
        if obs['identity_verified']:
            await _job(db,c['id'],j['family_key'],'cards',receipt=receipt,problem=reason)
        elif obs['status']=='missing' and j['attempts']<2:
            await _job(db,c['id'],j['family_key'],'observe',receipt=receipt,problem=reason or 'Čaká sa na potvrdenie z xTreku.',
                       delay=60*(1+j['attempts']),increment=True)
        else:
            reason=reason or 'Po odoslaní sa nepotvrdila presná natívna identita. Opätovné vytvorenie je zastavené.'
            await _job(db,c['id'],j['family_key'],j['stage'],receipt=receipt)
            await _finish(db,c,j,{k:('blocked',dict(reason=reason,product_id=obs.get('product_id'),variants=obs.get('variants'))) for k in _selected(j['guards'])})


def _scope_of(guards,code):
    return guards['scope'].get(code) or 'default'


async def _cards(db,c,jobs):
    from inventory_hub.services import atomer_card_batch as cards
    jobs=await _checked(db,c,jobs)
    codes=[]; native_ids={}; scopes={}; families={}
    for j in jobs:
        rows,g=await _family(db,c['checked_run_id'],j['family_key'])
        keys=_selected(j['guards'])
        eligible=[]
        for k in keys:
            # A successfully closed earlier verification is not an authority to
            # re-open its review. Read its concrete receipt from the pinned run.
            r=rows[k]
            if r['status']=='done':
                continue
            eligible.append(k);scopes[k]=_scope_of(j['guards'],k)
            if j['receipt'].get('product_id') and scopes[k]!='biketrek':
                native_ids[k]=(j['receipt']['product_id'],(j['receipt'].get('variants') or {}).get(k))
        codes.extend(eligible); families[j['family_key']]=(keys,rows)
    result={}
    corrections=c['selection'].get('barcode_corrections') or {}
    for pos in range(0,len(codes),cards.MAX_BATCH):
        chunk=codes[pos:pos+cards.MAX_BATCH]
        result.update(await cards.reconcile(db,c['checked_run_id'],chunk,apply=True,actor=c['actor'],
                      native_ids={k:native_ids[k] for k in chunk if k in native_ids},include_managed=True,
                      scopes={k:scopes[k] for k in chunk},**({'barcode_corrections':{k:corrections[k] for k in chunk if k in corrections}} if corrections else {})))
    for j in jobs:
        f=j['family_key']; keys,rows=families[f]; stored=j['receipt']; doc=j['document']
        proofs={}
        for k in keys:
            r=rows[k]; detail=r['detail']
            if r['status']=='done':
                # Explicitly preserve historical closures and show the snapshot IDs,
                # including incomplete ones, without inventing a new verification.
                proofs[k]=('excluded',dict(reason='Predtým uzavretá položka, výsledok: '+(r['result'] or '—'),
                                         card_id=detail.get('hub_id'),ean=detail.get('ean'),xtrek=detail.get('product_id'),
                                         biketrek=detail.get('bike')))
                continue
            receipt={**result[k],'ean':detail.get('ean'),'scope':scopes[k],'source_state':detail.get('source_state'),
                         'verification_scope':'identity_card_and_mapped_content' if doc and stored.get('verified_fields') else 'identity_and_card'}
            if receipt['status']!='verified' or not receipt.get('card_id'):
                proofs[k]=('partial' if stored.get('identity_verified') else 'blocked',receipt)
            elif doc and not stored.get('verified_fields'):
                proofs[k]=('partial',{**receipt,'reason':'; '.join(stored.get('issues') or ['Ďalšie natívne údaje čakajú na kontrolu.'])})
            else:
                proofs[k]=('verified',receipt)
        attach={s:{'state':st,'evidence':v} for s,(st,v) in proofs.items()}
        stored={**stored,'cards':attach};j['receipt']=stored
        need_content=any(v.get('status')=='verified' and v.get('card_id') and v.get('scope')!='biketrek'
                         and (v.get('xtrek') or {}).get('product_id') for _,v in proofs.values())
        if need_content and (not doc or stored.get('content_requested')):
            await _proofs(db,c['id'],f,{k:('partial' if st=='verified' and v.get('scope')!='biketrek' else st,v)
                                        for k,(st,v) in proofs.items()})
            await _job(db,c['id'],f,'fidelity',receipt={**stored,'content_requested':True})
        elif doc and stored.get('identity_verified') and not stored.get('verified_fields') and (
            stored.get('deferred') or j['attempts']<1):
            await _proofs(db,c['id'],f,proofs)
            await _job(db,c['id'],f,'related',receipt=stored,problem='; '.join(stored.get('issues',[])),
                       delay=15 if j['attempts']==0 else 60)
        else:
            await _job(db,c['id'],f,'cards',receipt=stored)
            await _finish(db,c,j,proofs)


async def _related(db,c,jobs):
    """Only revisit original source references. Record intent before any additive relation PUT."""
    from inventory_hub.services import atomer_new_native as native
    jobs=await _checked(db,c,jobs)
    if not jobs:
        return
    roots=await _root_index(db,c)
    index=await _source(db,c)
    known=set(roots.values())
    ready=[]; changed=[]
    for j in jobs:
        d=j['document']; root=d['payload']['code']
        wanted=set(d.get('related',[])); resolved=set(); missing=[]
        for source_ref in wanted:
            val=roots.get(index.parent_for(source_ref)) or (source_ref if source_ref in known else None)
            if val and val!=root:
                resolved.add(val)
            elif val!=root:
                missing.append(source_ref)
        prev=set(d['payload'].get('related') or [])
        # A disappearing target is not permission to remove an existing relation.
        resolved|=prev
        doc={**d,'payload':{**d['payload'],'related':sorted(resolved)},'missing_related':sorted(missing)}
        old=j['receipt']; signature=sha256(_json(sorted(resolved)).encode()).hexdigest()
        needs=(resolved!=prev or 'related' in old.get('deferred',[])) and old.get('related_intent')!=signature
        if needs:
            old={**old,'related_intent':signature}
            changed.append(j)
        j.update(document=doc,receipt=old)
        await _job(db,c['id'],j['family_key'],'related',receipt=old,document=doc)
        ready.append(j)
    if changed:
        await db.commit()
        await _lock_current(db,c)
        changed=await _checked(db,c,changed)
        if changed:
            expected={j['document']['payload']['code']:_expected(j) for j in changed}
            try:
                await asyncio.to_thread(native.complete_related,[j['document'] for j in changed],c['shop_fingerprint'],expected)
            except Exception as e:
                if _budget_error(e) and getattr(e,'request_sent',None) is False:
                    for j in changed:
                        r={k:v for k,v in j['receipt'].items() if k!='related_intent'}
                        await _job(db,c['id'],j['family_key'],'related',receipt=r)
                    await db.commit()
                    raise
                # Ambiguous response: observe the journalled desired relation set only.
    ready=await _checked(db,c,ready)
    if not ready:
        return
    expected={j['document']['payload']['code']:_expected(j) for j in ready}
    seen=await asyncio.to_thread(native.observe,[j['document'] for j in ready],c['shop_fingerprint'],expected)
    other=await db.scalar(text("""SELECT EXISTS(SELECT 1 FROM atomer_catalog_jobs WHERE campaign=:c
         AND stage IN ('pending','prepared','sending','observe','cards'))"""),{'c':c['id']})
    for j in ready:
        d=j['document'];obs=seen[d['payload']['code']]
        prev_cards=j['receipt'].get('cards',{})
        r={**j['receipt'],**{k:v for k,v in obs.items() if k!='native'}}
        missing=d.get('missing_related',[])
        if obs['identity_verified'] and missing and other:
            await _job(db,c['id'],j['family_key'],'related',document=d,receipt=r,
                       problem='Čaká sa na migráciu súvisiacich produktov: '+', '.join(missing),delay=900)
            continue
        if obs.get('deferred') and 'images' in obs['deferred'] and j['attempts']<4:
            await _job(db,c['id'],j['family_key'],'related',receipt=r,problem='; '.join(obs['issues']),
                       delay=120*(1+j['attempts']),increment=True)
            continue
        final={}
        for code,v in prev_cards.items():
            base=v['evidence']; state=v['state']
            if state=='excluded':
                final[code]=(state,base)
            elif base.get('status')!='verified' or not base.get('card_id'):
                final[code]=(state,base)
            elif obs['status']=='verified':
                final[code]=('verified',{k:v for k,v in base.items() if k!='reason'})
            else:
                final[code]=('partial',{**base,'reason':'; '.join(obs['issues']) or 'Natívny produkt vyžaduje kontrolu.'})
        await _job(db,c['id'],j['family_key'],'related',receipt=r)
        if obs['identity_verified'] and not obs['verified_fields'] and any(
                v.get('status')=='verified' and v.get('card_id') and v.get('scope')!='biketrek' for _,v in final.values()):
            await _proofs(db,c['id'],j['family_key'],final)
            await _job(db,c['id'],j['family_key'],'fidelity',receipt={**r,'content_requested':True})
        else:
            await _finish(db,c,j,final)


async def _step(db,c):
    from inventory_hub.services import atomer_catalog_fidelity as fidelity
    previous_run=c['checked_run_id']
    await _lock_current(db,c)
    if previous_run!=c['checked_run_id'] and c['selection']['scope']=='catalog':
        await _exec(db,"""INSERT INTO atomer_catalog_jobs(campaign,family_key,priority,leaf_count)
             SELECT :c,family_key,min(CASE WHEN state IN ('needs_both','needs_target') THEN 1 ELSE 4 END),count(*)
             FROM atomer_progress_items WHERE run_id=:r GROUP BY family_key
             ON CONFLICT(campaign,family_key) DO UPDATE SET leaf_count=excluded.leaf_count""",c=c['id'],r=c['checked_run_id'])
    await _exec(db,'UPDATE atomer_catalog_campaigns SET checked_run_id=:r WHERE id=:c',r=c['checked_run_id'],c=c['id'])
    if c['selection'].get('category_restore'):
        c['phase']='category_restore'
        if await restore.advance(db,c,lambda:_source(db,c),lambda:_lock_current(db,c)):
            return True
    # Finish committed/uncertain writes before starting new work. Deferred relations
    # yield to the missing parents they depend on.
    candidates=await _rows(db,"""SELECT * FROM atomer_catalog_jobs WHERE campaign=:c
        AND stage=ANY(CAST(:stages AS text[])) AND next_at<=now()
        ORDER BY CASE stage WHEN 'sending' THEN 0 WHEN 'updating' THEN 0 WHEN 'observe' THEN 1 WHEN 'cards' THEN 2
                  WHEN 'prepared' THEN 3 WHEN 'pending' THEN 5 ELSE CASE WHEN priority=0 THEN 4 ELSE 6 END END,priority,family_key LIMIT 55""",
        c=c['id'],stages=list(RUNNABLE))
    batch=_ready_batch(candidates)
    if not batch:
        next_at=await db.scalar(text("""SELECT min(next_at) FROM atomer_catalog_jobs WHERE campaign=:c
                                   AND stage=ANY(CAST(:stages AS text[]))"""),dict(c=c['id'],stages=list(RUNNABLE)))
        if c['selection'].get('category_restore'):
            category_at=await restore.next_retry(db,c['id'])
            if category_at and (not next_at or category_at<next_at):
                next_at=category_at
        if next_at:
            await _exec(db,"UPDATE atomer_catalog_campaigns SET state='waiting',next_at=:n,problem=coalesce(nullif(problem,''),'Čaká sa na spätnú kontrolu.'),updated_at=now() WHERE id=:c",n=next_at,c=c['id'])
        else:
            residual=await db.scalar(text("SELECT count(*) FROM atomer_catalog_jobs WHERE campaign=:c AND stage IN ('partial','blocked')"),dict(c=c['id']))
            await _exec(db,"UPDATE atomer_catalog_campaigns SET state=:s,problem=:p,updated_at=now() WHERE id=:c",
                        s='review' if residual else 'done',p=f'Zostáva {residual} rodín na kontrolu.' if residual else '',c=c['id'])
        return False
    action={'pending':_prepare,'prepared':_dispatch,'sending':_observe,'observe':_observe,'cards':_cards,'related':_related,
            'fidelity':fidelity.prepare,'updating':fidelity.observe}[batch[0]['stage']]
    phase=c['phase']=batch[0]['stage']
    try:
        await action(db,c,batch)
    except ReviewError as exc:
        if exc.code in ('catalog_source_changed','catalog_connection_changed','new_category_mapping_changed',
                        'new_registry_binding_changed','new_native_filter_unconfirmed','refresh_in_progress',
                        'run_changed','target_connection_changed','shops_unavailable'):
            raise
        # A bad family must never stop another family's migration. Preserve committed
        # write intents on rollback, then retry only in the phase actually on disk.
        await db.rollback()
        await _lock_current(db,c)
        if len(batch)>1:
            for candidate in batch:
                j=dict(await _one(db,'SELECT * FROM atomer_catalog_jobs WHERE campaign=:c AND family_key=:f',
                                  c=c['id'],f=candidate['family_key']))
                if j['stage']!=phase:
                    continue
                await _exec(db,"""UPDATE atomer_catalog_jobs SET receipt=receipt || jsonb_build_object('isolate',true),
                              problem=:p WHERE campaign=:c AND family_key=:f""",
                            p=exc.code,c=c['id'],f=j['family_key'])
        else:
            j=dict(await _one(db,'SELECT * FROM atomer_catalog_jobs WHERE campaign=:c AND family_key=:f',
                              c=c['id'],f=batch[0]['family_key']))
            if j['stage']!=batch[0]['stage']:
                return True
            rows,group=await _family(db,c['checked_run_id'],j['family_key'])
            if j['guards'] is None:
                j['guards']=await _claim(db,c,j,rows,group)
            if await _checked(db,c,[j]):
                await _finish(db,c,j,{k:('partial' if j['receipt'].get('identity_verified') else 'blocked',
                               dict(reason=exc.code,ean=rows[k]['detail'].get('ean'))) for k in _selected(j['guards'])})
    except Exception as error:
        phase=batch[0]['stage']
        quota=_budget_error(error)
        registry_unavailable=(phase in ('pending','fidelity') and bool(getattr(error,'catalog_resource',None))
                              and getattr(error,'status',None) in (502,503,504))
        if not quota and not registry_unavailable:
            raise
        # Pause this bounded kind of work. Card verification or a different
        # shop/read lane may still have capacity. Durable send states are kept.
        await db.rollback()
        await _lock_current(db,c)
        if registry_unavailable and not quota and batch[0]['attempts']>=6:
            reason=_diagnostic(c,error)
            for candidate in batch:
                j=dict(await _one(db,'SELECT * FROM atomer_catalog_jobs WHERE campaign=:c AND family_key=:f',
                                  c=c['id'],f=candidate['family_key']))
                if j['stage']!=phase:
                    continue
                if j['guards'] is None:
                    rows,group=await _family(db,c['checked_run_id'],j['family_key'])
                    j['guards']=await _claim(db,c,j,rows,group)
                if await _checked(db,c,[j]):
                    await _job(db,c['id'],j['family_key'],j['stage'],guards=j['guards'])
                    if j['receipt'].get('cards'):
                        await fidelity.conclude(db,c,j,dict(issues=[reason]))
                    else:
                        await _finish(db,c,j,{k:('blocked',dict(reason=reason)) for k in _selected(j['guards'])})
            return True
        delay=max(15,min(86400,int(getattr(error,'retry_after',None) or (300 if quota else 30*2**min(batch[0]['attempts'],6)))))
        label=_diagnostic(c,error)
        await _exec(db,"""UPDATE atomer_catalog_jobs SET next_at=greatest(next_at,now()+make_interval(secs=>:delay)),
                         problem=:problem,attempts=attempts+:inc
                         WHERE campaign=:c AND stage=:s AND
                         (stage<>'pending' OR priority<4 OR priority=:prio)""",
                    c=c['id'],delay=delay,s=phase,prio=batch[0]['priority'],problem=label,inc=int(not quota))
        await _exec(db,"""UPDATE atomer_catalog_campaigns SET state='running',failures=0,problem=:p,
                        next_at=now(),updated_at=now() WHERE id=:c""",p=label,c=c['id'])
        return True
    await _exec(db,"UPDATE atomer_catalog_campaigns SET state='running',failures=0,problem='',next_at=now(),updated_at=now() WHERE id=:c",c=c['id'])
    return True


def _diagnostic(c,exc):
    import re
    code=getattr(exc,'code',None) or (str(exc) if re.fullmatch(r'[a-z0-9_]{3,100}',str(exc)) else type(exc).__name__)
    original=getattr(exc,'orig',None)
    sqlstate=next((getattr(obj,'sqlstate',None) for obj in (original,getattr(original,'__cause__',None),exc)
                   if re.fullmatch(r'[0-9A-Z]{5}',str(getattr(obj,'sqlstate','') or ''))),None)
    resource=getattr(exc,'catalog_resource','')
    if not isinstance(resource,str) or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_./-]{0,64}',resource):
        resource=''
    upstream=getattr(exc,'upstream_status',None)
    http=f'HTTP {upstream}' if type(upstream) is int and 100<=upstream<=599 else None
    return ' · '.join(str(v) for v in (c.get('phase','campaign'),'Limit Upgates' if _budget_error(exc) else '',code,resource,http,sqlstate) if v)[:300]


async def _record_failure(c,exc):
    code=getattr(exc,'code',None)
    quota=_budget_error(exc)
    source_changed=code in ('catalog_source_changed','catalog_connection_changed')
    failures=c['failures']+(0 if quota else 1)
    stop=source_changed or failures>=8
    delay=getattr(exc,'retry_after',None) or (300 if quota else min(3600,10*2**min(failures,8)))
    problem=_diagnostic(c,exc)
    progress='source_changed' if source_changed else 'paused' if stop else 'waiting'
    async with database.get_session_context() as db:
        await _exec(db,"""UPDATE atomer_catalog_campaigns SET state=:s,problem=:p,failures=:n,
                 next_at=now()+make_interval(secs=>:d),updated_at=now() WHERE id=:c""",
                 s=progress,p=problem[:300],n=failures,d=max(1,min(86400,int(delay))),c=c['id'])


async def cycle():
    if database._engine is None:
        await database.init_db()
    # Serialize against all worker replicas even across the intent transaction boundary.
    async with database._engine.connect() as connection:
        locked=await connection.scalar(text('SELECT pg_try_advisory_lock(:k)'),{'k':WORKER_LOCK})
        if not locked:
            return False
        try:
            async with database.get_session_context() as db:
                active=await _one(db,"SELECT * FROM atomer_catalog_campaigns WHERE state IN ('running','waiting') AND next_at<=now() ORDER BY updated_at LIMIT 1")
                if not active:
                    return False
                c=dict(active)
                try:
                    async with asyncio.timeout(420):
                        return await _step(db,c)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await db.rollback()
                    LOGGER.warning('Atomer catalog batch deferred (%s)',type(exc).__name__)
                    await _record_failure(c,exc)
                    return False
        finally:
            await connection.execute(text('SELECT pg_advisory_unlock(:k)'),{'k':WORKER_LOCK})
            await connection.commit()


async def run():
    while True:
        try:
            worked=await cycle()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning('Atomer campaign worker unavailable (%s)',type(exc).__name__)
            worked=False
        await asyncio.sleep(1 if worked else 12)
