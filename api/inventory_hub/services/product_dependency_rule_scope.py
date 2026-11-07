"""Pure scope proof for proposals that can only save an unpublished rule book."""
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from inventory_hub.ai_content_types import CategoryProfile, ProposedInstructions, Rule
from inventory_hub.services import product_dependency_scope as s
from inventory_hub.services.ai_content_rules import DEFAULT_POLICY


JOB = set('id batch_id kind status revision context output checks events usage reserved_usd actual_usd preview_id error created_at updated_at'.split())
CONTEXT = set('rules_version current proposal_request category use_ai model name resolved estimate_usd prompt_version reasoning_effort max_output_tokens classification_max_output_tokens archived'.split())
REQUIRED = set('rules_version current proposal_request category use_ai model name resolved estimate_usd'.split())
STATES = {'estimate', 'queued', 'generating', 'review', 'completed', 'failed', 'uncertain', 'cancelled'}
COUNTERS = set('input_tokens cached_tokens cache_write_tokens output_tokens reasoning_tokens web_calls billable_search_calls'.split())


def _text(value, path, maximum, *, blank=False):
    if not isinstance(value, str) or len(value) > maximum or not blank and not value.strip():
        raise s.Unverified('ai_rule_text_unverified', path)


def _amount(value, path, *, nullable=False):
    if nullable and value is None:
        return
    try:
        if type(value) not in (int, float, str, Decimal):
            raise ValueError
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError
    except (InvalidOperation, ValueError):
        raise s.Unverified('ai_rule_cost_unverified', path) from None


def _model(model, value, path):
    # All three models forbid extra fields, including nested parameter/scope
    # objects. Never use Content coercion to reinterpret a rule proposal.
    try:
        return model.model_validate(value, strict=True)
    except (ValidationError, ValueError, TypeError):
        raise s.Unverified('ai_rule_writer_shape_unverified', path) from None


def inspect(proof, raw):
    s.obj(raw, JOB, JOB, '/')
    if raw['kind'] != 'rules' or raw['status'] not in STATES:
        raise s.Unverified('ai_rule_lifecycle_unverified', '/status')
    for key in ('id', 'batch_id'):
        if not isinstance(raw[key], str) or not s.UUID32.fullmatch(raw[key]):
            raise s.Unverified('ai_rule_job_identity_unverified', '/' + key)
    if type(raw['revision']) is not int or raw['revision'] <= 0:
        raise s.Unverified('ai_rule_job_identity_unverified', '/revision')
    if raw['preview_id'] is not None or raw['checks'] != {}:
        raise s.Unverified('ai_rule_product_intent_mixed', '/preview_id')
    ctx = s.obj(raw['context'], CONTEXT, REQUIRED, '/context')
    if type(ctx['category']) is not bool or ctx['use_ai'] is not True:
        raise s.Unverified('ai_rule_proposal_kind_unverified', '/context')
    if type(ctx['rules_version']) is not int or ctx['rules_version'] <= 0:
        raise s.Unverified('ai_rule_version_unverified', '/context/rules_version')
    current = _model(CategoryProfile if ctx['category'] else Rule, ctx['current'], '/context/current')
    if ctx['name'] != current.name:
        raise s.Unverified('ai_rule_snapshot_unverified', '/context/name')
    if ctx['category'] and current.reference_products:
        # Native reference snapshots need their own reviewed parser contract;
        # the first rules-only proof does not grant them an implicit exception.
        raise s.Unverified('ai_rule_native_reference_unverified', '/context/current/reference_products')
    _text(ctx['proposal_request'], '/context/proposal_request', 6000)
    _text(ctx['model'], '/context/model', 100)
    resolved = s.obj(ctx['resolved'], {'policy', 'origins'}, ('policy', 'origins'), '/context/resolved')
    policy = s.obj(resolved['policy'], DEFAULT_POLICY, DEFAULT_POLICY, '/context/resolved/policy')
    if (any(type(value) is not bool for value in policy.values()) or policy != DEFAULT_POLICY
            or resolved['origins'] != {}):
        raise s.Unverified('ai_rule_policy_unverified', '/context/resolved')
    if 'archived' in ctx and type(ctx['archived']) is not bool:
        raise s.Unverified('ai_rule_archive_metadata_unverified', '/context/archived')
    if 'reasoning_effort' in ctx and ctx['reasoning_effort'] not in ('low', 'high'):
        raise s.Unverified('ai_rule_generation_settings_unverified', '/context/reasoning_effort')
    for key in ('prompt_version', 'max_output_tokens', 'classification_max_output_tokens'):
        if key in ctx and (type(ctx[key]) is not int or ctx[key] <= 0):
            raise s.Unverified('ai_rule_generation_settings_unverified', '/context/' + key)
    _amount(ctx['estimate_usd'], '/context/estimate_usd')
    _amount(raw['reserved_usd'], '/reserved_usd')
    _amount(raw['actual_usd'], '/actual_usd', nullable=True)
    if raw['output'] is not None:
        _model(ProposedInstructions, raw['output'], '/output')
    if (raw['status'] in ('review', 'completed') and raw['output'] is None
            or raw['status'] in ('estimate', 'queued', 'generating', 'failed', 'uncertain') and raw['output'] is not None):
        raise s.Unverified('ai_rule_output_state_unverified', '/output')
    for index, event in enumerate(s.array(raw['events'], '/events')):
        path = f'/events/{index}'
        s.obj(event, {'at', 'status', 'note'}, ('at', 'status', 'note'), path)
        if event['status'] not in STATES:
            raise s.Unverified('ai_rule_lifecycle_unverified', path + '/status')
        _text(event['at'], path + '/at', 64)
        _text(event['note'], path + '/note', 20000, blank=True)
    usage = s.obj(raw['usage'], COUNTERS | {'long_context', 'response_id', 'model', 'opened_sources'}, path='/usage')
    for key in COUNTERS & usage.keys():
        if type(usage[key]) is not int or usage[key] < 0:
            raise s.Unverified('ai_rule_usage_unverified', '/usage/' + key)
    if 'long_context' in usage and type(usage['long_context']) is not bool:
        raise s.Unverified('ai_rule_usage_unverified', '/usage/long_context')
    for key in ('response_id', 'model'):
        if usage.get(key) is not None:
            _text(usage[key], '/usage/' + key, 260)
    for value in s.array(usage.get('opened_sources', []), '/usage/opened_sources'):
        _text(value, '/usage/opened_sources', 2000)
    for key in ('created_at', 'updated_at'):
        _text(raw[key], '/' + key, 64)
    if raw['error'] is not None:
        _text(raw['error'], '/error', 2000)
    # accept_proposal copies this rule into a NEW, UNPUBLISHED AiRuleVersion.
    # Rule scope and prose are not product write targets. The rules output is
    # incompatible with Content, including product_import.apply_ai's reader.
    # Complete draft/journal inventory and full raw-row hashes remain required.
    proof.context('/context', 'validated_rule_proposal_unpublished_version_only')
