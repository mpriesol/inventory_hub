"""Explicit CAS-based recovery of a terminal catalog family in the same private campaign."""
from hashlib import sha256
import json

from sqlalchemy import text

from inventory_hub.services import atomer_review as work
from inventory_hub.services.atomer_review_targets import ReviewError


def revision(job, run_id, rows, group):
    """Scope the approval to the actual terminal family, current run and all operator revisions."""
    stamp=job['updated_at'].isoformat()
    data=[job['campaign'],job['family_key'],job['stage'],stamp,run_id,group['revision'],
          sorted((key,r.get('revision') or 0,r.get('status') or 'open') for key,r in rows.items())]
    return sha256(json.dumps(data,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def prior_codes(document):
    payload=document['payload']
    variants=[*(payload.get('variants') or []),*(document.get('remaining_create_variants') or [])]
    return {v['code'] for v in variants} if variants else {payload['code']}


def next_phase(candidate):
    receipt=candidate['receipt']
    if receipt.get('content_intent'):
        return 'updating'
    if receipt.get('content_requested'):
        return 'fidelity'
    if candidate.get('document') and candidate['receipt'].get('dispatched',True):
        return 'observe'
    return 'pending'


async def reschedule(db,c,candidate,expected,mode,actor):
    from inventory_hub.services import atomer_catalog_engine as engine
    family=candidate['family_key']
    rows,group=await engine._family(db,c['checked_run_id'],family)
    if not rows or candidate['stage'] not in ('partial','blocked') or expected!=revision(candidate,c['checked_run_id'],rows,group):
        raise ReviewError('catalog_recheck_changed')
    if mode!='apply':
        return
    guards=await claim_again(db,c,candidate,actor,commands={'block','complete'})
    # An uncertain write can only be observed. Absence of a receipt on older
    # journals is ambiguous and therefore never makes an attempted POST repeatable.
    stage=next_phase(candidate)
    note={'previous_stage':candidate['stage'],'previous_problem':candidate['problem'],'run_id':c['checked_run_id'],
          'before_revision':expected,'actor':actor}
    receipt={**candidate['receipt'],'last_recheck':note,'rebuild_after_observation':stage in ('observe','updating')}
    receipt['reexpand']=bool(candidate['receipt'].get('catalog_deferred'))
    await db.execute(text("""UPDATE atomer_catalog_jobs SET stage=:s,guards=CAST(:guards AS jsonb),
             document=CASE WHEN :s='pending' THEN NULL ELSE document END,
             receipt=CAST(:receipt AS jsonb),attempts=0,next_at=now(),updated_at=now(),priority=0,problem='',
             source_card_stage='pending',source_card_next_at=now(),source_card_attempts=0,source_card_problem=''
             WHERE campaign=:c AND family_key=:f"""),dict(s=stage,c=c['id'],f=family,
             guards=engine._json(guards),receipt=engine._json(receipt)))
    active=engine._selected(guards)
    if not active:
        await engine._finish(db,c,{**candidate,'guards':guards},{})
        return
    await db.execute(text("""UPDATE atomer_catalog_proofs SET state='partial',evidence=evidence ||
             jsonb_build_object('recheck_revision',CAST(:rev AS text)),checked_at=now()
             WHERE campaign=:c AND family_key=:f AND code=ANY(CAST(:codes AS text[]))"""),
             dict(rev=expected,c=c['id'],f=family,codes=active))


async def claim_again(db,c,candidate,actor,commands):
    from inventory_hub.services import atomer_catalog_engine as engine
    # Re-claim only the exact review record this campaign closed. A subsequent
    # edit, withdrawal, manual closure or scope change always outranks that receipt.
    owned=(candidate.get('guards') or {}).get('own') or []
    accepted=dict(c['selection']['working'])
    for kind,key,prior_rev in owned:
        r=await work._record(db,kind,key)
        if r['revision']!=prior_rev+1 or r['status'] not in ('blocked','done') or r['updated_by']!=c['actor']:
            continue
        command=await db.scalar(text("""SELECT command FROM atomer_review_events
                 WHERE kind=:kind AND key=:key AND revision=:rev"""),dict(kind=kind,key=key,rev=r['revision']))
        if command not in commands or (r['status'],command) not in (('blocked','block'),('done','complete')):
            continue
        changed=await db.execute(text("""UPDATE atomer_review_work SET status='working',revision=revision+1,
                       updated_by=:actor,updated_at=now() WHERE kind=:kind AND key=:key AND revision=:rev
                       AND status=:prior_status RETURNING revision"""),
                       dict(kind=kind,key=key,rev=r['revision'],actor=actor,prior_status=r['status']))
        new_revision=changed.scalar_one()
        await work._audit(db,kind,key,'catalog_recheck',actor)
        accepted[f'{kind}:{key}']=new_revision
    local={**c,'selection':{**c['selection'],'working':accepted}}
    rows,group=await engine._family(db,c['checked_run_id'],candidate['family_key'])
    return await engine._claim(db,local,candidate,rows,group)
