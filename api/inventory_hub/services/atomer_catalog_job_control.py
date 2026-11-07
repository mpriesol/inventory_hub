"""Explicit withdrawal/replanning of one proved-unsent, paused catalog job.

No network request, identity allocation or proof deletion occurs here. Unknown
receipt shapes are not a negative proof. Both transitions retain immutable full
preimages and require the same worker/review/identity boundary as dispatch.
"""
from hashlib import sha256
from datetime import datetime
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from inventory_hub.services import atomer_catalog_control as campaigns
from inventory_hub.services import atomer_catalog_engine as engine
from inventory_hub.services import atomer_catalog_source_cards as local
from inventory_hub.services.atomer_review_targets import ReviewError


Action = Literal['withdraw', 'replan']
Token = Annotated[StrictStr, Field(min_length=1, max_length=260)]
MAX_LEAVES = 500
MAX_BYTES = 5_000_000
REFRESH_LOCK = 691433137
# This exact rejection occurs in atomer_source_cards.physical_subject before the
# allocation/enrichment loop. A blocked readback failure can follow a committed
# nested write and is deliberately NOT admitted, even when every ID is null.
PREWRITE_SOURCE_REASONS = {'atomer_source_card_requires_review'}
JOB_FIELDS = {'campaign', 'family_key', 'priority', 'leaf_count', 'stage', 'guards', 'document', 'receipt',
              'problem', 'attempts', 'next_at', 'updated_at', 'source_card_stage', 'source_card_next_at',
              'source_card_attempts', 'source_card_problem', 'operator_withdrawn', 'operator_control_revision',
              'operator_control_id'}
PROOF_FIELDS = {'campaign', 'family_key', 'code', 'state', 'evidence', 'checked_at'}
SOURCE_FIELDS = PROOF_FIELDS | {'run_id', 'card_id', 'created'}


class JobControlRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    family_key: Token
    expected_snapshot_hash: Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
    selected_codes: list[Token] = Field(min_length=1, max_length=MAX_LEAVES)
    reason: Annotated[str, Field(min_length=10, max_length=1000)]
    confirmed: Literal[True]

    @field_validator('confirmed', mode='before')
    @classmethod
    def confirmation(cls, value):
        if value is not True:
            raise ValueError('explicit_confirmation_required')
        return value

    @field_validator('reason')
    @classmethod
    def reason_text(cls, value):
        value = value.strip()
        if len(value) < 10 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('plain_reason_required')
        return value

    @field_validator('selected_codes')
    @classmethod
    def distinct_codes(cls, values):
        if len(values) != len(set(values)):
            raise ValueError('distinct_selected_codes_required')
        return sorted(values)


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _plain(value):
    def encode(item):
        if isinstance(item, datetime):
            return item.isoformat()
        raise TypeError('catalog_job_snapshot_type_unverified')
    return json.loads(json.dumps(value, default=encode))


async def _snapshot(db, campaign_id, family_key, *, lock=False):
    # Proof UPDATE owns its row before its trigger takes job FOR SHARE. Never
    # wait here while holding the inverse job->proof order.
    suffix = ' FOR UPDATE NOWAIT' if lock else ''
    campaign = await engine._one(db, 'SELECT to_jsonb(t) AS data FROM atomer_catalog_campaigns t WHERE id=:c' + suffix,
                                 c=campaign_id)
    if not campaign:
        raise ReviewError('catalog_campaign_not_found', 404)
    job = await engine._one(db, 'SELECT to_jsonb(t) AS data FROM atomer_catalog_jobs t '
                            'WHERE campaign=:c AND family_key=:f' + suffix, c=campaign_id, f=family_key)
    if not job:
        raise ReviewError('catalog_job_not_found', 404)
    data = dict(campaign=campaign['data'], job=job['data'])
    for table, key in (('atomer_catalog_proofs', 'proofs'), ('atomer_catalog_source_cards', 'source_cards')):
        rows = await engine._rows(db, f'SELECT to_jsonb(t) AS data FROM {table} t '
            'WHERE campaign=:c AND family_key=:f ORDER BY code LIMIT 501' + suffix, c=campaign_id, f=family_key)
        data[key] = [row['data'] for row in rows]
    head = await engine._head(db)
    data['head'] = _plain(dict(head)) if head else None
    rows, group = await engine._family(db, data['campaign']['checked_run_id'], family_key)
    # Full rows pin current source identity and every operator-owned sibling.
    data['rows'] = _plain(rows)
    data['group'] = _plain(group)
    # A historic failed readback can have lost its newly allocated card ID in
    # a diagnostic receipt. The source writer allocates the exact source SKU;
    # inspect raw products including inactive/retired cards independently.
    data['source_allocations'] = [dict(row) for row in await engine._rows(db, '''
        SELECT id,sku,created_from_source FROM products
        WHERE created_from_source='atomer' AND sku=ANY(CAST(:codes AS text[])) ORDER BY id''', codes=sorted(rows))]
    # A supported editor rename can remove the current source SKU, but its
    # immutable audit retains it. Never let rename erase a prior allocation.
    data['source_allocation_audits'] = [dict(row) for row in await engine._rows(db, '''
        SELECT a.id,a.product_id,a.before_data->>'sku' AS before_sku,a.after_data->>'sku' AS after_sku,
               md5(to_jsonb(a)::text) AS row_hash
        FROM product_editor_audit a JOIN products p ON p.id=a.product_id
        WHERE p.created_from_source='atomer' AND (a.before_data->>'sku'=ANY(CAST(:codes AS text[]))
             OR a.after_data->>'sku'=ANY(CAST(:codes AS text[]))) ORDER BY a.id''', codes=sorted(rows))]
    return data


def _guard(value):
    return (isinstance(value, list) and len(value) == 3 and type(value[0]) is int and value[0] >= 0
            and value[1] in ('open', 'queued', 'working', 'blocked', 'done')
            and value[2] in ('default', 'both', 'xtrek', 'biketrek'))


def _selection(guards):
    if not isinstance(guards, dict) or set(guards) != {'group', 'rows', 'scope', 'own', 'excluded', 'blocked'}:
        return None
    if not _guard(guards['group']) or not all(isinstance(guards[k], dict) for k in ('rows', 'scope', 'excluded', 'blocked')):
        return None
    rows = guards['rows']
    if not rows or len(rows) > MAX_LEAVES or not all(isinstance(k, str) and 0 < len(k) <= 260 and _guard(v) for k, v in rows.items()):
        return None
    if set(guards['scope']) != set(rows) or any(v not in ('default', 'both', 'xtrek', 'biketrek') for v in guards['scope'].values()):
        return None
    if any(not set(guards[k]) <= set(rows) or any(not isinstance(v, str) or len(v) > 4000 for v in guards[k].values())
           for k in ('excluded', 'blocked')):
        return None
    own = guards['own']
    if not isinstance(own, list) or len(own) > MAX_LEAVES + 1:
        return None
    if any(not isinstance(v, list) or len(v) != 3 or v[0] not in ('family', 'item')
           or not isinstance(v[1], str) or type(v[2]) is not int or v[2] < 0 for v in own):
        return None
    return sorted(engine._selected(guards))


def _negative_evidence(value, *, source=False):
    # These are the current writers' no-effect diagnostic shapes. In particular
    # null card/native fields are not interchangeable with an omitted attempt.
    allowed = {'reason', 'ean', 'status'} | ({'xtrek', 'biketrek', 'card_id', 'hub_sku', 'source_card_input'} if source else set())
    if not isinstance(value, dict) or set(value) - allowed or not isinstance(value.get('reason'), str) or not value['reason']:
        return False
    if source and value['reason'] not in PREWRITE_SOURCE_REASONS:
        return False
    if value.get('status', 'blocked') not in ('blocked', 'excluded'):
        return False
    if any(value.get(k) is not None for k in ('xtrek', 'biketrek', 'card_id', 'hub_sku')):
        return False
    return all(v is None or isinstance(v, str) and len(v) <= 4000 for v in value.values())


def inspect(data):
    c, j = data['campaign'], data['job']
    problems = []
    if set(j) != JOB_FIELDS:
        problems.append('catalog_job_schema_unverified')
    selected = _selection(j.get('guards'))
    if not c.get('operator_paused') or c.get('state') != 'paused':
        problems.append('catalog_not_operator_paused')
    if selected is None or not selected:
        problems.append('catalog_job_selection_unverified')
    if (not data['head'] or data['head']['id'] != c['checked_run_id']
            or data['head']['source_sha256'] != c['source_sha']
            or data['head']['admin_export_sha256'] != c['admin_sha']):
        problems.append('catalog_job_source_changed')
    if selected is not None and (engine._guard(data['group']) != j['guards']['group']
            or {k: engine._guard(v) for k, v in data['rows'].items()} != j['guards']['rows']
            or j['leaf_count'] != len(data['rows'])):
        problems.append('catalog_job_reviews_changed')
    if selected is not None and any(v[1] != j['family_key'] if v[0] == 'family' else v[1] not in j['guards']['rows']
                                   for v in j['guards']['own']):
        problems.append('catalog_job_selection_unverified')
    if j.get('document') is not None:
        problems.append('catalog_job_document_present')
    receipt = j.get('receipt')
    if (not isinstance(receipt, dict) or set(receipt) - {'isolate', 'isolate_phase', 'dispatched'}
            or 'isolate' in receipt and type(receipt['isolate']) is not bool
            or 'isolate_phase' in receipt and receipt['isolate_phase'] != 'pending'
            or 'dispatched' in receipt and receipt['dispatched'] is not False):
        problems.append('catalog_job_effects_unverified')
    if j.get('source_card_stage') not in ('pending', 'blocked'):
        problems.append('catalog_job_source_lane_unverified')
    if data['source_allocations'] or data['source_allocation_audits']:
        problems.append('catalog_job_prior_source_allocation')
    raw_rows = j.get('guards', {}).get('rows') if isinstance(j.get('guards'), dict) else None
    scope = set(raw_rows) if isinstance(raw_rows, dict) else set()
    for key in ('proofs', 'source_cards'):
        if len(data[key]) > MAX_LEAVES:
            problems.append('catalog_job_evidence_limit')
        for row in data[key]:
            if (set(row) != (SOURCE_FIELDS if key == 'source_cards' else PROOF_FIELDS)
                    or row['code'] not in scope or row['state'] not in ('blocked', 'excluded')
                    or not _negative_evidence(row['evidence'], source=key == 'source_cards')
                    or key == 'source_cards' and (row['card_id'] is not None or row['created'] is not None)):
                problems.append('catalog_job_effects_unverified')
    if len(json.dumps(data, ensure_ascii=False).encode()) > MAX_BYTES:
        problems.append('catalog_job_evidence_limit')
    withdrawn = j.get('operator_withdrawn') is True
    valid_stage = j['stage'] == ('excluded' if withdrawn else 'pending')
    if not valid_stage:
        problems.append('catalog_job_not_unsent_pending')
    return dict(snapshot_hash=_digest(data), campaign_id=c['id'], family_key=j['family_key'],
                campaign_revision=c['operator_control_revision'], operator_paused=c['operator_paused'],
                job_revision=j['operator_control_revision'], operator_withdrawn=withdrawn,
                last_control_request=j['operator_control_id'], stage=j['stage'], selected_codes=selected or [],
                eligible=not problems and not withdrawn, replan_eligible=not problems and withdrawn,
                blockers=sorted(set(problems)), proof_count=len(data['proofs']), source_card_count=len(data['source_cards']),
                remote_requests=0, remote_write_performed=False)


async def get(db, campaign_id, family_key):
    await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
    return inspect(await _snapshot(db, campaign_id, family_key))


async def receipt(db, request_id):
    row = await engine._one(db, 'SELECT result FROM atomer_catalog_job_controls WHERE request_id=:id', id=str(request_id))
    if not row:
        raise ReviewError('catalog_job_control_not_found', 404)
    return row['result']


async def _replay(db, request_id, request_hash):
    row = await engine._one(db, 'SELECT request_hash,result FROM atomer_catalog_job_controls WHERE request_id=:id', id=str(request_id))
    if row and row['request_hash'] != request_hash:
        raise ReviewError('catalog_job_control_request_conflict')
    return row['result'] if row else None


async def change(db, campaign_id, action: Action, payload: JobControlRequest, actor):
    if action not in ('withdraw', 'replan'):
        raise ReviewError('catalog_job_control_action_invalid', 422)
    if not isinstance(actor, str) or not actor or len(actor) > 200:
        raise ReviewError('operator_required', 403)
    request = dict(campaign_id=campaign_id, action=action, **payload.model_dump(mode='json'))
    request_hash = _digest(request)
    try:
        saved = await _replay(db, payload.request_id, request_hash)
        if saved:
            await db.commit()
            return saved
        await campaigns._lock(db)
        if not await db.scalar(text('SELECT pg_try_advisory_xact_lock_shared(:key)'), {'key': REFRESH_LOCK}):
            raise ReviewError('refresh_in_progress')
        if not await db.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key': local.LOCAL_LOCK}):
            raise ReviewError('catalog_source_worker_busy')
        saved = await _replay(db, payload.request_id, request_hash)
        if saved:
            await db.commit()
            return saved
        data = await _snapshot(db, campaign_id, payload.family_key, lock=True)
        before = inspect(data)
        if before['snapshot_hash'] != payload.expected_snapshot_hash or before['selected_codes'] != payload.selected_codes:
            raise ReviewError('catalog_job_control_changed')
        if not before['eligible' if action == 'withdraw' else 'replan_eligible']:
            raise ReviewError(before['blockers'][0] if before['blockers'] else 'catalog_job_control_state_changed')
        stamp = await db.scalar(text('SELECT to_jsonb(clock_timestamp())'))
        old = data['job']
        new = {**old, 'operator_withdrawn': action == 'withdraw',
               'operator_control_revision': old['operator_control_revision'] + 1,
               'operator_control_id': str(payload.request_id), 'updated_at': stamp}
        if action == 'withdraw':
            new.update(stage='excluded', source_card_stage='blocked', problem='operator_withdrawn_unsent',
                       source_card_problem='operator_withdrawn_unsent')
        else:
            # Same complete source and review guards are freshly checked above.
            # There was never an outbound payload; none is restored or replayed.
            new.update(stage='pending', source_card_stage='pending', receipt={}, attempts=0,
                       source_card_attempts=0, problem='', source_card_problem='', next_at=stamp, source_card_next_at=stamp)
        result = dict(status='completed', action=action, request_id=str(payload.request_id), request_hash=request_hash,
                      campaign_id=campaign_id, family_key=payload.family_key, actor=actor, reason=payload.reason,
                      selected_codes=before['selected_codes'], before_snapshot_hash=before['snapshot_hash'],
                      before_job_hash=_digest(old), after_job_hash=_digest(new), job_revision=new['operator_control_revision'],
                      operator_withdrawn=new['operator_withdrawn'], stage=new['stage'],
                      proofs_preserved=True, review_history_preserved=True, original_job_preserved=True,
                      campaign_remains_paused=True, remote_write_performed=False, physical_stock_changed=False)
        # The immutable audit is inserted first in the same transaction. SQL's
        # job fence permits only its exact before/after pair and request UUID.
        await engine._exec(db, '''INSERT INTO atomer_catalog_job_controls
            (request_id,campaign,family_key,action,request_hash,actor,request,before_job,after_job,evidence,result)
            VALUES (:id,:c,:f,:a,:h,:actor,CAST(:request AS jsonb),CAST(:before AS jsonb),CAST(:after AS jsonb),
                    CAST(:evidence AS jsonb),CAST(:result AS jsonb))''',
            id=str(payload.request_id), c=campaign_id, f=payload.family_key, a=action, h=request_hash, actor=actor,
            request=engine._json(request), before=engine._json(old), after=engine._json(new),
            evidence=engine._json({k: v for k, v in data.items() if k != 'job'}), result=engine._json(result))
        await db.execute(text("SELECT set_config('app.atomer_job_control_request',:id,true)"), {'id': str(payload.request_id)})
        await engine._exec(db, '''UPDATE atomer_catalog_jobs SET
            stage=:stage,source_card_stage=:source_stage,receipt=CAST(:receipt AS jsonb),
            attempts=:attempts,source_card_attempts=:source_attempts,problem=:problem,source_card_problem=:source_problem,
            next_at=CAST(:next_at AS timestamptz),source_card_next_at=CAST(:source_next AS timestamptz),
            updated_at=CAST(:stamp AS timestamptz),operator_withdrawn=:withdrawn,
            operator_control_revision=:revision,operator_control_id=:id
            WHERE campaign=:c AND family_key=:f''',
            c=campaign_id, f=payload.family_key, stage=new['stage'], source_stage=new['source_card_stage'],
            receipt=engine._json(new['receipt']), attempts=new['attempts'], source_attempts=new['source_card_attempts'],
            problem=new['problem'], source_problem=new['source_card_problem'], next_at=datetime.fromisoformat(new['next_at']),
            source_next=datetime.fromisoformat(new['source_card_next_at']), stamp=datetime.fromisoformat(stamp), withdrawn=new['operator_withdrawn'],
            revision=new['operator_control_revision'], id=str(payload.request_id))
        await db.commit()
        return result
    except DBAPIError as error:
        await db.rollback()
        if getattr(error.orig, 'sqlstate', None) == '55P03':
            raise ReviewError('catalog_job_rows_busy') from None
        raise
    except Exception:
        await db.rollback()
        raise
