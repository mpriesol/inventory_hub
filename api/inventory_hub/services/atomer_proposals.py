"""Materialize accepted editor suggestions for exact sellable codes, preserving operator decisions."""
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from inventory_hub.services import atomer_review as review
from inventory_hub.services.atomer_review_targets import ReviewError

INSTRUCTIONS = {
    "identity": "Porovnaj kód, EAN, rodiča a parametre podľa Atomeru. Zapíš zistenú príčinu konfliktu.",
    "card": "Over existujúce skladové karty. Pri jednoznačnej identite použi existujúcu kartu alebo založ chýbajúcu podľa pravidiel skladu.",
    "linkX": "Over zhodu a prepoj existujúcu kartu s presným produktom alebo variantom v xTreku. Potvrď výsledné ID.",
    "targetX": "Priprav chýbajúcu položku v xTreku podľa Atomeru. Over aktivitu produktu, variantu a príznaky pred zverejnením.",
    "checkB": "Skontroluj natívne ID, rodiča a kód väzby BIKETREK. Zapíš výsledok kontroly.",
    "linkB": "Nájdi zhodu v BIKETREK a po overení prepoj presný variant alebo samostatný produkt s kartou.",
    "verify": "Over kartu a väzby v určených obchodoch. Zapíš overené ID a prípadný rozdiel.",
}


def suggestions(row, scope):
    selected = []
    if row['card_state'] == 'review' or row['reasons']:
        selected.append('identity')
    if row['card_state'] == 'missing':
        selected.append('card')
    if scope != 'biketrek':
        if row['state'] == 'needs_link':
            selected.append('linkX')
        if row['state'] in ('needs_both', 'needs_target'):
            selected.append('targetX')
    if scope != 'xtrek' and row['bike_state'] == 'review':
        selected.append('checkB')
    if scope in ('both', 'biketrek') and row['bike_state'] == 'unlinked':
        selected.append('linkB')
    return selected or ['verify']


def owns_undelivered_proposal(record,revision,command,detail,group,job,events):
    """Bind the exact previous machine proposal and its claim to one still-undelivered catalog job."""
    if (record['revision']!=revision or revision not in (2,3) or not job or not job.get('guards') or
            job['stage'] not in ('pending','prepared','blocked','partial') or events.get(revision)!=command or
            events.get(1)!='approve_suggestions' or events.get(2)!='claim'):
        return False
    if any(job['receipt'].get(k) for k in ('dispatched','content_intent','identity_verified','product_id')):
        return False
    scope=record['scope'] if record['scope']!='default' else group['scope']
    wanted='\n'.join(INSTRUCTIONS[k] for k in suggestions(detail,scope))
    return (record['instruction']==wanted and (revision,record['status'],command) in
            ((2,'working','claim'),(3,'blocked','block')) and ['item',detail['code'],2] in job['guards'].get('own',()))


async def accept(db, run, mode, actor):
    await review._lock_current_run(db, run)
    # Explicit withdrawals, active family decisions, saved notes/instructions and historical
    # results take precedence. An accepted group must never spawn overlapping child jobs.
    rows = (await db.execute(text("""
        SELECT i.detail, coalesce(nullif(w.scope,'default'), f.scope,'default') AS scope
        FROM atomer_progress_items i
        LEFT JOIN atomer_review_work w ON w.kind='item' AND w.key=i.code
        LEFT JOIN atomer_review_work f ON f.kind='family' AND f.key=i.family_key
        WHERE i.run_id=:run AND w.key IS NULL AND coalesce(f.status,'open')='open'
        AND coalesce(f.instruction,'')='' AND NOT EXISTS(
          SELECT 1 FROM atomer_review_events e WHERE e.kind='family' AND e.key=i.family_key
          AND e.command='reopen')
        ORDER BY i.code
    """), {'run':run})).all()
    plan, counts = [], Counter()
    for row, scope in rows:
        keys = suggestions(row, scope)
        counts.update(keys)
        plan.append(dict(key=row['code'],scope=scope,
                         instruction='\n'.join(INSTRUCTIONS[k] for k in keys)))
    if mode == 'apply' and plan:
        await db.execute(text("""
            WITH saved AS (
              INSERT INTO atomer_review_work(kind,key,revision,status,scope,decision,note,instruction,result,updated_by)
              SELECT 'item',j->>'key',1,'queued',j->>'scope','undecided','',j->>'instruction','',:actor
              FROM jsonb_array_elements(CAST(:plan AS jsonb)) j ON CONFLICT (kind,key) DO NOTHING
              RETURNING *)
            INSERT INTO atomer_review_events(kind,key,revision,command,actor,detail)
            SELECT kind,key,revision,'approve_suggestions',:actor,to_jsonb(saved) FROM saved
        """),dict(plan=json.dumps(plan, ensure_ascii=False),actor=actor))
        await db.commit()
    return dict(status='verified' if mode=='apply' else 'ready',count=len(plan),
                reason=', '.join(f'{k} {v}' for k,v in sorted(counts.items())))


async def verify_links(db, run, mode, actor):
    await review._lock_current_run(db, run)
    checked = await db.scalar(text("SELECT checked_at FROM atomer_progress_runs WHERE id=:run"),{'run':run})
    if datetime.now(timezone.utc)-checked>timedelta(hours=3):
        raise ReviewError('refresh_required')
    # The published run verified native leaves. Reconfirm the exact mappings under the
    # shared identity lock before recording receipts. The verification covers links only.
    from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':IDENTITY_WRITE_LOCK})
    rows = (await db.execute(text("""
      SELECT w.key,i.detail,w.scope FROM atomer_review_work w
      JOIN atomer_progress_items i ON i.run_id=:run AND w.kind='item' AND i.code=w.key
      WHERE w.status='queued' AND w.instruction=:instruction AND i.state='linked'
      AND i.detail->>'card_state'='exists' AND i.detail->'reasons'='[]'::jsonb
      AND w.scope IN ('default','both','xtrek')
      AND (coalesce(i.detail->>'bike_state','')='linked' OR
           (w.scope='xtrek' AND i.detail->>'bike_state'='unlinked'))
      AND NOT EXISTS (SELECT 1 FROM atomer_review_work f WHERE f.kind='family' AND f.key=i.family_key
                      AND (f.status<>'open' OR f.scope='biketrek'))
      AND EXISTS (SELECT 1 FROM shop_products s JOIN shops sh ON sh.id=s.shop_id JOIN products p ON p.id=s.product_id
          WHERE sh.code='xtrek' AND s.product_id=(i.detail->>'hub_id')::integer
          AND p.sku=i.detail->>'hub_sku' AND s.external_product_id=i.detail->>'product_id'
          AND s.external_variant_id IS NOT DISTINCT FROM i.detail->>'variant_id'
          AND s.external_code=i.detail->>'target_code' AND NOT coalesce(s.push_pending,false))
      AND (i.detail->>'bike_state'<>'linked' OR EXISTS (
          SELECT 1 FROM shop_products s JOIN shops sh ON sh.id=s.shop_id WHERE sh.code='biketrek'
          AND s.product_id=(i.detail->>'hub_id')::integer AND s.id=(i.detail->'bike'->>'link_id')::integer
          AND s.external_product_id=i.detail->'bike'->>'product_id'
          AND s.external_variant_id IS NOT DISTINCT FROM i.detail->'bike'->>'variant_id'
          AND s.external_code=i.detail->'bike'->>'code' AND NOT coalesce(s.push_pending,false)))
    """),{'run':run,'instruction':INSTRUCTIONS['verify']})).all()
    receipts = {}
    for key, row, scope in rows:
        def ids(p,v):
            return f'{p}/{v}' if v else str(p)
        bike = row.get('bike') or {}
        other = ids(bike['product_id'],bike.get('variant_id')) if bike.get('product_id') else 'bez väzby'
        receipts[key] = (f"Kontrola väzieb č. {run}, {checked:%Y-%m-%d %H:%M UTC}. {key}, EAN {row.get('ean') or '—'}. "
                         f"Karta {row['hub_id']} ({row['hub_sku']}), xTrek {ids(row['product_id'],row.get('variant_id'))}, BIKETREK {other}.")
    if mode=='apply' and receipts:
        for stage, command in [('working','claim'),('done','complete')]:
            await db.execute(text("""
                WITH saved AS (UPDATE atomer_review_work w SET status=:stage,revision=revision+1,
                  result=CASE WHEN :stage='done' THEN r.value ELSE w.result END,updated_at=now(),updated_by=:actor
                  FROM jsonb_each_text(CAST(:rows AS jsonb)) r WHERE w.kind='item' AND w.key=r.key RETURNING w.*)
                INSERT INTO atomer_review_events(kind,key,revision,command,actor,detail)
                SELECT kind,key,revision,:command,:actor,to_jsonb(saved) FROM saved
            """),dict(stage=stage,command=command,rows=json.dumps(receipts,ensure_ascii=False),actor=actor))
        await db.commit()
    return dict(status='verified' if mode=='apply' else 'ready',count=len(receipts))
