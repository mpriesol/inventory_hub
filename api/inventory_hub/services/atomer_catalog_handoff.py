"""Terminal handoff of a paused plan while retaining its actual local effects.

This is not unsent withdrawal or publication completion. The only admitted
current job contains no retained native instruction: pending, SQL NULL document,
literal empty receipt, and no earlier lifecycle reset. Known local allocations
and conflicts are acknowledged and preserved. Historical native effects remain
undetermined; all other current writer documents must be proved closed/disjoint.
"""
from copy import deepcopy
import asyncio
from datetime import datetime
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from inventory_hub.services import atomer_catalog_job_control as controls
from inventory_hub.services import atomer_catalog_job_evidence as evidence
from inventory_hub.services import atomer_catalog_handoff_scope as scope_inventory
from inventory_hub.services import product_dependency_scan as scan
from inventory_hub.services.atomer_catalog_job_control import campaigns, engine, local
from inventory_hub.services.atomer_review_targets import ReviewError


VERSION = 'atomer-retained-job-handoff-v1'
LOCAL_BLOCKERS = {'atomer_source_card_requires_review', 'local_mapping_conflict'}
LOCAL_KEYS = {'status', 'reason', 'card_id', 'hub_sku', 'ean', 'gtins', 'source_native_id', 'source_ean',
    'created', 'action', 'gtin_added', 'barcode_evidence', 'cached_native_target', 'source_card_input',
    'verification_scope', 'source_card', 'creation_evidence', 'xtrek', 'biketrek'}
READY_FIELDS = {'status', 'card_id', 'hub_sku', 'ean', 'gtins', 'source_native_id', 'source_ean', 'created', 'action'}


def _proof_hash(value):
    if len(json.dumps(value, ensure_ascii=False).encode()) > controls.MAX_BYTES:
        raise ReviewError('catalog_handoff_scope_limit')
    return controls._digest(value)


class HandoffRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    family_key: controls.Token
    expected_snapshot_hash: Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
    scope_codes: list[controls.Token] = Field(min_length=1, max_length=controls.MAX_LEAVES)
    purpose: Literal['identity_migration']
    acknowledge_preserved_local_effects: Literal[True]
    acknowledge_review_changes: Literal[True]
    reason: Annotated[str, Field(min_length=10, max_length=1000)]
    confirmed: Literal[True]

    @field_validator('confirmed', 'acknowledge_preserved_local_effects', 'acknowledge_review_changes', mode='before')
    @classmethod
    def explicit(cls, value):
        if value is not True:
            raise ValueError('explicit_confirmation_required')
        return value

    @field_validator('scope_codes')
    @classmethod
    def distinct(cls, values):
        if len(values) != len(set(values)):
            raise ValueError('distinct_scope_codes_required')
        return sorted(values)

    @field_validator('reason')
    @classmethod
    def plain_reason(cls, value):
        return controls.JobControlRequest.reason_text(value)


def _local_evidence(value, *, source=False, partial=False, depth=0):
    if not isinstance(value, dict) or set(value) - LOCAL_KEYS or depth > 8:
        return False
    if any(value.get(k) is not None for k in ('xtrek', 'biketrek')):
        return False
    ready = value.get('status') == 'card_ready'
    if ready:
        if (not READY_FIELDS <= value.keys() or type(value.get('card_id')) is not int or value['card_id'] <= 0
                or value.get('action') not in ('created', 'reused')
                or value.get('created') is not (value['action'] == 'created')
                or value.get('verification_scope') not in (None, 'source_card_only')
                or partial and value.get('verification_scope') != 'source_card_only'):
            return False
    else:
        allowed = {'reason', 'status', 'ean', 'xtrek', 'biketrek', 'card_id', 'hub_sku',
                   'source_card_input', 'source_card', 'creation_evidence'}
        if (set(value) - allowed or not isinstance(value.get('reason'), str) or not value['reason']
                or value.get('status', 'blocked') not in ('blocked', 'excluded')
                or any(value.get(k) is not None for k in ('card_id', 'hub_sku'))
                or source and value['reason'] not in LOCAL_BLOCKERS or partial):
            return False
    return all(isinstance(value[key], dict) and value[key].get('status') == 'card_ready'
               and _local_evidence(value[key], partial=key == 'source_card', depth=depth + 1)
               for key in ('source_card', 'creation_evidence') if key in value)


def inspect(data):
    """Validate retained local facts; never infer absent writes from attempts/errors."""
    c, j = data['campaign'], data['job']
    blockers = []
    guards = j.get('guards')
    selected = controls._selection(guards)
    saved = set(guards['rows']) if selected is not None else set()
    codes = sorted(saved | set(data['rows']) | {r['code'] for r in data['proofs'] + data['source_cards']})
    if not codes or len(codes) > controls.MAX_LEAVES or any(len(data[k]) > controls.MAX_LEAVES for k in ('proofs', 'source_cards')):
        blockers.append('catalog_handoff_scope_limit')
    if (set(j) != controls.JOB_FIELDS or selected is None or not selected
            or selected is not None and any(v[1] != j['family_key'] if v[0] == 'family' else v[1] not in saved
                                           for v in guards['own'])):
        blockers.append('catalog_handoff_job_shape_unverified')
    if c.get('operator_paused') is not True or c.get('state') != 'paused':
        blockers.append('catalog_not_operator_paused')
    if (not data['head'] or data['head']['id'] != c['checked_run_id']
            or data['head']['source_sha256'] != c['source_sha'] or data['head']['admin_export_sha256'] != c['admin_sha']):
        blockers.append('catalog_job_source_changed')
    if (j.get('stage') != 'pending' or data['document_is_sql_null'] is not True
            or j.get('document') is not None or j.get('receipt') != {} or not isinstance(j.get('receipt'), dict)
            or j.get('operator_withdrawn') is not False or type(j.get('operator_control_revision')) is not int
            or j['operator_control_revision'] != 0 or j.get('operator_control_id') is not None):
        blockers.append('catalog_handoff_current_intent_unverified')
    if j.get('source_card_stage') not in ('pending', 'checked', 'blocked'):
        blockers.append('catalog_handoff_source_lane_unverified')
    for key in ('attempts', 'source_card_attempts'):
        if type(j.get(key)) is not int or j[key] < 0:
            blockers.append('catalog_handoff_job_shape_unverified')
    try:
        # Full selection validation keeps unknown fields fail-closed. The
        # diagnostic projector's 500-row family cap must not truncate a valid
        # campaign-wide correction map; interpret the exact family slice while
        # retaining/hash-binding the complete original selection separately.
        engine.Selection.model_validate(c['selection'])
        projection = evidence.Projection()
        campaign = deepcopy(c)
        for key in ('barcode_corrections', 'zero_barcode_sentinels'):
            value = campaign['selection'].get(key)
            if value is not None:
                campaign['selection'][key] = {k: v for k, v in value.items() if k in set(codes) | {j['family_key'][2:]}}
        for key, value, spec in (('campaign', campaign, 'campaign'), ('job', j, 'job'),
                ('head', data['head'], evidence.optional('head')), ('group', data['group'], 'group'),
                ('rows', data['rows'], evidence.mapping('row')),
                ('proofs', data['proofs'], evidence.array('proof')),
                ('source_cards', data['source_cards'], evidence.array('source_card'))):
            projection.project(value, spec, '/' + key)
        if projection.problems:
            blockers.append('catalog_handoff_history_shape_unverified')
    except (ValidationError, ValueError, TypeError, KeyError, RecursionError):
        blockers.append('catalog_handoff_history_shape_unverified')
    for key in ('proofs', 'source_cards'):
        for row in data[key]:
            value = row['evidence'] if isinstance(row['evidence'], dict) else {}
            if (row['campaign'] != c['id'] or row['family_key'] != j['family_key']
                    or row['state'] not in (('partial', 'blocked', 'excluded') if key == 'proofs' else ('card_ready', 'blocked'))
                    or key == 'source_cards' and row['run_id'] != c['checked_run_id']
                    or not _local_evidence(row['evidence'], source=key == 'source_cards', partial=row['state'] == 'partial')):
                blockers.append('catalog_handoff_retained_effects_unverified')
            if ((row['state'] in ('partial', 'card_ready')) != (value.get('status') == 'card_ready')
                    or row['state'] in ('blocked', 'excluded') and value.get('status', row['state']) != row['state']):
                blockers.append('catalog_handoff_retained_effects_unverified')
            if key == 'source_cards' and row['state'] == 'card_ready' and (
                    row['card_id'] != value.get('card_id') or row['created'] is not value.get('created')):
                creation = value.get('creation_evidence')
                creation = creation if isinstance(creation, dict) else {}
                # _receipts intentionally retains created=true when the same
                # originally allocated card is subsequently reused. Its full
                # original creation proof is preserved, never normalized away.
                sticky = (row['card_id'] == value.get('card_id') and row['created'] is True
                    and value.get('created') is False and creation.get('created') is True
                    and creation.get('card_id') == row['card_id'])
                if not sticky:
                    blockers.append('catalog_handoff_retained_effects_unverified')
    if len(json.dumps(data, ensure_ascii=False).encode()) > controls.MAX_BYTES:
        blockers.append('catalog_handoff_scope_limit')
    current_guards = {code: engine._guard(row) for code, row in data['rows'].items()}
    drift = selected is not None and (current_guards != guards['rows'] or engine._guard(data['group']) != guards['group'])
    return dict(scope_codes=codes, selected_codes=selected or [], blockers=sorted(set(blockers)), review_changed=drift,
        current_codes=sorted(data['rows']), saved_codes=sorted(saved),
        local_effects=dict(source_rows=len(data['source_cards']), proof_rows=len(data['proofs']),
            created=sum(r.get('created') is True for r in data['source_cards']),
            reused=sum(r.get('created') is False for r in data['source_cards']),
            blocked=sum(r['state'] == 'blocked' for r in data['source_cards'])))


async def _capture(db, campaign_id, family_key, columns, *, lock=False):
    data = await controls._snapshot(db, campaign_id, family_key, lock=lock, allocations=False)
    own = inspect(data)
    codes = own['scope_codes']
    # Replace the old read-only control's current-family-only allocation query
    # with the entire saved/current/proof/source union, including renamed cards.
    data['source_allocations'] = await scope_inventory.rows(db, '''SELECT id,sku,created_from_source FROM products
        WHERE created_from_source='atomer' AND lower(sku)=ANY(CAST(:codes AS text[])) ORDER BY id''',
        codes=sorted({code.casefold() for code in codes}))
    data['source_allocation_audits'] = await scope_inventory.rows(db, '''SELECT a.id,a.product_id,
        a.before_data->>'sku' AS before_sku,a.after_data->>'sku' AS after_sku,md5(to_jsonb(a)::text) AS row_hash
        FROM product_editor_audit a JOIN products p ON p.id=a.product_id
        WHERE p.created_from_source='atomer' AND (lower(a.before_data->>'sku')=ANY(CAST(:codes AS text[]))
             OR lower(a.after_data->>'sku')=ANY(CAST(:codes AS text[]))) ORDER BY a.id''',
        codes=sorted({code.casefold() for code in codes}))
    data = controls._plain(data)
    own = inspect(data)
    result = dict(schema=VERSION, campaign_id=campaign_id, family_key=family_key,
        stage=data['job']['stage'], operator_paused=data['campaign']['operator_paused'],
        job_revision=data['job']['operator_control_revision'], **own,
        eligible=False, complete=False, historical_native_effects='not_determined',
        source_publication_completed=False, content_publication_completed=False,
        remote_requests=0, remote_write_performed=False)
    result['blockers'] = [dict(code=code) for code in own['blockers']]
    proof = dict(version=VERSION, retained=data)
    if not own['blockers']:
        try:
            scope, identities = await scope_inventory.closure(db, data, codes)
        except scope_inventory.ScopeIssue as error:
            result['blockers'] = [error.blocker]
            result['snapshot_hash'] = _proof_hash(proof)
            return data, result, proof
        proof['identities'] = identities
        row_hash = await db.scalar(text('''SELECT encode(sha256(convert_to(to_jsonb(t)::text,'UTF8')),'hex')
            FROM atomer_catalog_jobs t WHERE campaign=:c AND family_key=:f'''), dict(c=campaign_id, f=family_key))
        candidate = dict(row_identity=dict(campaign=campaign_id, family_key=family_key), row_hash=row_hash,
            proof_hash=_proof_hash(proof))
        writers = await scope_inventory.writers(db, columns, scope, identities, candidate)
        proof['current_writers'] = {k: v for k, v in writers.items() if k != 'diagnostics'}
        result.update(complete=writers['complete'], eligible=writers['complete'] and not writers['blockers'],
            blockers=writers['blockers'], writer_inventory=writers,
            retained_card_ids=sorted(scope.hubs), mapping_count=len(identities['mappings']))
    result['snapshot_hash'] = _proof_hash(proof)
    return data, result, proof


async def get(db, campaign_id, family_key):
    await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
    try:
        async with asyncio.timeout(scan.MAX_SCAN_SECONDS):
            columns = await scope_inventory.schema(db)
            _, result, _ = await _capture(db, campaign_id, family_key, columns)
        return result
    except scan.InventoryTimeout as error:
        # The scanner already rolled back. Never issue a later query/read from
        # an expired ORM object and never supply an applicable confirmation hash.
        return dict(schema=VERSION, campaign_id=campaign_id, family_key=family_key, eligible=False,
            complete=False, snapshot_hash=None, blockers=error.report['blockers'],
            historical_native_effects='not_determined', source_publication_completed=False,
            content_publication_completed=False, remote_requests=0, remote_write_performed=False)
    except TimeoutError:
        await db.rollback()
        return dict(schema=VERSION, campaign_id=campaign_id, family_key=family_key, eligible=False,
            complete=False, snapshot_hash=None, blockers=[dict(code='catalog_handoff_inventory_incomplete')],
            historical_native_effects='not_determined', source_publication_completed=False,
            content_publication_completed=False, remote_requests=0, remote_write_performed=False)


async def change(db, campaign_id, payload: HandoffRequest, actor):
    if not isinstance(actor, str) or not actor or len(actor) > 200:
        raise ReviewError('operator_required', 403)
    request = dict(campaign_id=campaign_id, action='handoff', **payload.model_dump(mode='json'))
    request_hash = controls._digest(request)
    try:
        saved = await controls._replay(db, payload.request_id, request_hash)
        if saved:
            await db.commit()
            return saved
        await campaigns._lock(db)
        if not await db.scalar(text('SELECT pg_try_advisory_xact_lock_shared(:key)'), {'key': controls.REFRESH_LOCK}):
            raise ReviewError('refresh_in_progress')
        if not await db.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key': local.LOCAL_LOCK}):
            raise ReviewError('catalog_source_worker_busy')
        columns = await scope_inventory.schema(db)
        await scope_inventory.lock_inputs(db, columns)
        saved = await controls._replay(db, payload.request_id, request_hash)
        if saved:
            await db.commit()
            return saved
        async with asyncio.timeout(scan.MAX_SCAN_SECONDS):
            data, before, proof = await _capture(db, campaign_id, payload.family_key, columns, lock=True)
        if before['snapshot_hash'] != payload.expected_snapshot_hash or before['scope_codes'] != payload.scope_codes:
            raise ReviewError('catalog_handoff_changed')
        if not before['eligible']:
            raise ReviewError('catalog_handoff_not_eligible')
        stamp = await db.scalar(text('SELECT to_jsonb(clock_timestamp())'))
        old = data['job']
        new = {**old, 'operator_withdrawn': True, 'operator_control_revision': old['operator_control_revision'] + 1,
            'operator_control_id': str(payload.request_id), 'updated_at': stamp,
            'stage': 'excluded', 'source_card_stage': 'blocked'}
        result = dict(schema=VERSION, status='completed', action='handoff', request_id=str(payload.request_id),
            request_hash=request_hash, campaign_id=campaign_id, family_key=payload.family_key,
            actor=actor, reason=payload.reason, scope_codes=before['scope_codes'],
            before_snapshot_hash=before['snapshot_hash'], before_job_hash=controls._digest(old),
            after_job_hash=controls._digest(new), job_revision=new['operator_control_revision'],
            operator_withdrawn=True, stage='excluded', replan_allowed=False,
            current_native_intent='none_under_fenced_complete_scope', historical_native_effects='not_determined',
            identity_migration_completed=False, source_publication_completed=False, content_publication_completed=False,
            local_effects=before['local_effects'], retained_card_ids=before['retained_card_ids'],
            proofs_preserved=True, review_history_preserved=True, original_job_preserved=True,
            campaign_remains_paused=True, remote_write_performed=False, physical_stock_changed=False)
        await engine._exec(db, '''INSERT INTO atomer_catalog_job_controls
            (request_id,campaign,family_key,action,request_hash,actor,request,before_job,after_job,evidence,result)
            VALUES (:id,:c,:f,'handoff',:h,:actor,CAST(:request AS jsonb),CAST(:before AS jsonb),CAST(:after AS jsonb),
                    CAST(:evidence AS jsonb),CAST(:result AS jsonb))''',
            id=str(payload.request_id), c=campaign_id, f=payload.family_key, h=request_hash, actor=actor,
            request=engine._json(request), before=engine._json(old), after=engine._json(new),
            evidence=engine._json(proof), result=engine._json(result))
        await db.execute(text("SELECT set_config('app.atomer_job_control_request',:id,true)"), {'id': str(payload.request_id)})
        await engine._exec(db, '''UPDATE atomer_catalog_jobs SET stage='excluded',source_card_stage='blocked',
            operator_withdrawn=true,operator_control_revision=:revision,operator_control_id=:id,
            updated_at=CAST(:stamp AS timestamptz) WHERE campaign=:c AND family_key=:f''',
            c=campaign_id, f=payload.family_key, revision=new['operator_control_revision'], id=str(payload.request_id),
            stamp=datetime.fromisoformat(stamp))
        await db.commit()
        return result
    except scan.InventoryTimeout:
        # Its rollback released every fence; abort, never continue with the
        # captured proof or try to reacquire only part of the boundary.
        raise ReviewError('catalog_handoff_inventory_incomplete') from None
    except TimeoutError:
        await db.rollback()
        raise ReviewError('catalog_handoff_inventory_incomplete') from None
    except DBAPIError as error:
        await db.rollback()
        if getattr(error.orig, 'sqlstate', None) == '55P03':
            raise ReviewError('catalog_handoff_writers_busy') from None
        raise
    except Exception:
        await db.rollback()
        raise
