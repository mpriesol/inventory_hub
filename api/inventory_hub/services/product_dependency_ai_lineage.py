"""Finite proofs for replaced draft jobs and reviewed historical AI writers.

No status/archive flag grants clearance. The callers still inspect every frozen
fact, complete linked draft, native family identity and publication journal.
"""
from decimal import Decimal, InvalidOperation
import json

from pydantic import ValidationError

from inventory_hub.services import product_dependency_scope as s
from inventory_hub.services.product_dependency_rule_scope import JOB, COUNTERS


PRODUCT_STATES = set('estimate queued generating review blocked ready preparing_import import_queued '
    'importing import_blocked import_failed failed uncertain exists completed cancelled'.split())
LOOKUP_STATES = {'lookup_running', 'lookup_completed', 'lookup_failed', 'lookup_uncertain'}
AUTOMATION = {'version', 'job_ids', 'paused', 'pause_reason', 'auto_publish_requested'}


def _text(value, path, maximum, *, blank=False):
    if not isinstance(value, str) or len(value) > maximum or not blank and not value.strip():
        raise s.Unverified('ai_lineage_text_unverified', path)


def _job(raw, kind, states):
    s.obj(raw, JOB, JOB, '/')
    if raw['kind'] != kind or raw['status'] not in states:
        raise s.Unverified('ai_lineage_lifecycle_unverified', '/status')
    for key in ('id', 'batch_id'):
        if not isinstance(raw[key], str) or not s.UUID32.fullmatch(raw[key]):
            raise s.Unverified('ai_lineage_job_identity_unverified', '/' + key)
    if type(raw['revision']) is not int or raw['revision'] <= 0:
        raise s.Unverified('ai_lineage_job_identity_unverified', '/revision')


def superseded_rows(proof, raw, draft, rows, ids):
    """Require explicit replacement bindings, never infer an absent backlink.

    prepare_ai replaces row bindings and the complete automation job list.
    apply_ai selects only current row bindings; advance_ai additionally requires
    automation membership. Existing or uncertain publication intent is excluded
    from this proof, while the caller checks all original baseline identities.
    """
    _job(raw, 'product', PRODUCT_STATES)
    ctx = raw['context']
    if (any(row.get('ai_job_id') == raw['id'] for row in rows)
            or type(ctx.get('staging_automation_version')) is not int
            or ctx['staging_automation_version'] != 1 or raw['preview_id'] is not None
            or any(key in ctx for key in ('staging_auto_publication', 'update_preview', 'update_result',
                                         'update_only', 'retry_import'))):
        raise s.Unverified('ai_draft_selection_changed', '/context/product_ids')
    automation = s.obj(draft['document'].get('automation'), AUTOMATION, AUTOMATION,
                       '/linked_draft/document/automation')
    jobs = s.array(automation['job_ids'], '/linked_draft/document/automation/job_ids', nonempty=True, limit=500)
    if (type(automation['version']) is not int or automation['version'] != 1
            or type(automation['paused']) is not bool or type(automation['auto_publish_requested']) is not bool
            or automation['pause_reason'] is not None and not isinstance(automation['pause_reason'], str)
            or any(not isinstance(job, str) or not s.UUID32.fullmatch(job) for job in jobs)
            or len(jobs) != len(set(jobs)) or raw['id'] in jobs):
        raise s.Unverified('ai_draft_replacement_unverified', '/linked_draft/document/automation')
    selected = [row for row in rows if row['id'] in ids]
    if (set(ids) != {row['id'] for row in selected}
            or any(row.get('ai_job_id') not in jobs for row in selected)):
        raise s.Unverified('ai_draft_replacement_unverified', '/linked_draft/document/rows')
    proof.context('/context/staging_id', 'validated_replaced_job_current_draft_and_baselines_checked')
    return selected


def legacy_shop_estimate(proof, raw, snapshot, facts):
    """The old estimate lacks parameters, but must retain full native scope.

    start and assert_source reject this source version. Existing update intents
    are never accepted by this branch; the caller still marks its complete
    parent/variant identity as a possible dependency, including sibling writes.
    """
    _job(raw, 'product', {'estimate'})
    ctx = raw['context']
    if (raw['output'] is not None or raw['checks'] != {} or raw['preview_id'] is not None
            or ctx.get('feed_key') != 'shop' or ctx.get('product_ids') != []
            or any(key in ctx for key in ('source_parameters_loaded', 'update_preview', 'update_result',
                'update_source_digest', 'staging_id', 'staging_automation_version', 'staging_auto_publication',
                'staging_applied_digest', 'retry_import', 'source_job_id', 'approval'))
            or 'archived' in ctx and type(ctx['archived']) is not bool):
        raise s.Unverified('shop_ai_source_incomplete', '/context')
    from inventory_hub.services.ai_content import digest
    if (ctx.get('source_digest') != digest(snapshot) or len(facts) != 1
            or set(facts) != {snapshot['product_id']}):
        raise s.Unverified('shop_ai_source_incomplete', '/context/source_snapshot')
    fact = facts[snapshot['product_id']]
    if (fact['code'] != snapshot['code'] or fact['eans'] != ([snapshot['ean']] if snapshot.get('ean') else [])
            or 'variant_identity' in fact and fact['variant_identity'] != snapshot['variants']):
        raise s.Unverified('shop_ai_source_incomplete', '/context/facts')
    proof.context('/context/source_snapshot', 'legacy_estimate_native_scope_preserved_source_refresh_required')


def _amount(value, path, *, nullable=False):
    if nullable and value is None:
        return
    try:
        if type(value) not in (int, float, str, Decimal):
            raise ValueError
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise ValueError
    except (InvalidOperation, ValueError):
        raise s.Unverified('supplier_lookup_cost_unverified', path) from None


def _model(model, value, path):
    try:
        # JSON strict mode accepts the writer's ISO timestamp, not coercions of
        # booleans, counters, strings or unknown nested observation fields.
        return model.model_validate_json(json.dumps(value), strict=True)
    except (ValidationError, ValueError, TypeError):
        raise s.Unverified('supplier_lookup_writer_shape_unverified', path) from None


def supplier_lookup(proof, raw):
    """Historical Sloger probe: a read-only audit, incompatible with Content."""
    from inventory_hub.sloger_probe_types import SlogerDiagnostic, SlogerObservation
    _job(raw, 'supplier_lookup', LOOKUP_STATES)
    ctx = s.obj(raw['context'], {'supplier', 'sku', 'model', 'estimate_usd', 'supplier_diagnostic'},
                ('supplier', 'sku', 'model', 'estimate_usd'), '/context')
    if (ctx['supplier'] != 'sloger' or raw['id'] != raw['batch_id']
            or raw['preview_id'] is not None or raw['checks'] != {}):
        raise s.Unverified('supplier_lookup_product_intent_mixed', '/context')
    _text(ctx['sku'], '/context/sku', 100)
    _text(ctx['model'], '/context/model', 100)
    _amount(ctx['estimate_usd'], '/context/estimate_usd')
    _amount(raw['reserved_usd'], '/reserved_usd')
    _amount(raw['actual_usd'], '/actual_usd', nullable=True)
    if 'supplier_diagnostic' in ctx:
        _model(SlogerDiagnostic, ctx['supplier_diagnostic'], '/context/supplier_diagnostic')
    if raw['status'] == 'lookup_completed':
        output = _model(SlogerObservation, raw['output'], '/output')
        if output.sku != ctx['sku'] or raw['error'] is not None:
            raise s.Unverified('supplier_lookup_observation_changed', '/output/sku')
    elif raw['output'] is not None:
        raise s.Unverified('supplier_lookup_output_state_unverified', '/output')
    usage = s.obj(raw['usage'], {'calls', 'input_tokens', 'output_tokens'}, path='/usage')
    if usage:
        s.obj(usage, required=('calls', 'input_tokens', 'output_tokens'), path='/usage')
        calls = s.array(usage['calls'], '/usage/calls', limit=2)
        for index, call in enumerate(calls):
            path = f'/usage/calls/{index}'
            s.obj(call, COUNTERS | {'long_context', 'response_id', 'model'},
                  ('input_tokens', 'output_tokens', 'model'), path)
            for key in COUNTERS & call.keys():
                if type(call[key]) is not int or call[key] < 0:
                    raise s.Unverified('supplier_lookup_usage_unverified', path + '/' + key)
            if 'long_context' in call and type(call['long_context']) is not bool:
                raise s.Unverified('supplier_lookup_usage_unverified', path + '/long_context')
            for key in ('response_id', 'model'):
                if call.get(key) is not None:
                    _text(call[key], path + '/' + key, 260)
        for key in ('input_tokens', 'output_tokens'):
            if type(usage[key]) is not int or usage[key] != sum(call[key] for call in calls):
                raise s.Unverified('supplier_lookup_usage_unverified', '/usage/' + key)
    for index, event in enumerate(s.array(raw['events'], '/events')):
        path = f'/events/{index}'
        s.obj(event, {'at', 'status', 'note'}, ('at', 'status', 'note'), path)
        if event['status'] not in LOOKUP_STATES:
            raise s.Unverified('supplier_lookup_lifecycle_unverified', path + '/status')
        _text(event['at'], path + '/at', 64)
        _text(event['note'], path + '/note', 20000, blank=True)
    for key in ('created_at', 'updated_at'):
        _text(raw[key], '/' + key, 64)
    if raw['error'] is not None:
        _text(raw['error'], '/error', 2000)
    # The historical probe only read supplier facts; its current service only
    # exposes read(). Product content entry points reject this kind, and this
    # strict observation cannot pass product_import.apply_ai's Content model.
    proof.context('/context', 'validated_historical_supplier_lookup_read_only')
