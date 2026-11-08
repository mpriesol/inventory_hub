"""Bounded, redacted diagnostics of the existing one-job control snapshot.

This module has no database or network access and makes no lifecycle decision.
Canonical hashes retain every captured value, including unknown fields. A
typed projection is not proof that historical native writes did not happen.
"""
from hashlib import sha256
import json
import re


VERSION = 'atomer-job-control-evidence-v1'
MAX_LEAVES = 500
MAX_BYTES = 5_000_000
MAX_DEPTH = 24
MAX_NODES = 50_000
MAX_PROBLEMS = 100
CODE = re.compile(r'[^\s\x00-\x1f\x7f@]{1,260}\Z')
HEX = re.compile(r'[a-f0-9]{64}\Z')


def optional(spec):
    return ('optional', spec)


def array(spec):
    return ('array', spec)


def mapping(spec):
    return ('mapping', spec)


def enum(*values):
    return ('enum', values)


def record(spec):
    return ('record', spec)


SCOPE = enum('default', 'both', 'xtrek', 'biketrek')
REVIEW_STATE = enum('open', 'queued', 'working', 'blocked', 'done')
STAGE = enum('pending', 'prepared', 'sending', 'observe', 'cards', 'related',
             'fidelity', 'updating', 'verified', 'partial', 'blocked', 'excluded')
PROOF_STATE = enum('verified', 'partial', 'blocked', 'excluded', 'card_ready')
STATUS = enum('verified', 'partial', 'blocked', 'excluded', 'card_ready', 'ready',
              'conflict', 'pending', 'sent', 'uncertain', 'accepted', 'missing',
              'created', 'existing', 'review', 'failed', 'error', 'ok')
ACTION = enum('created', 'reused')
GUARD = ('tuple', ('count', REVIEW_STATE, SCOPE))
OWNER = ('tuple', (enum('family', 'item'), 'code', 'count'))

# Only exact writer-owned shapes are interpreted. Free text, navigation URLs,
# configuration values and content bodies are represented by typed manifests.
# A content body without a closed schema is explicitly incomplete, never a
# negative proof. In particular a new field cannot silently become harmless.
SCHEMAS = {
    'guards': {
        'group': GUARD, 'rows': mapping(GUARD), 'scope': mapping(SCOPE),
        'own': array(OWNER), 'excluded': mapping('text'), 'blocked': mapping('text'),
    },
    'native': {
        'product_id': optional('native_id'), 'variant_id': optional('native_id'),
        'code': optional('code'), 'parent': optional('blank_code'), 'link_id': optional('mapping_id'),
        'is_variant': 'boolean', 'reason': 'text', 'shop_url': optional('text'),
        'shop_admin_url': optional('text'),
    },
    'cached_target': {
        'product_id': 'native_id', 'variant_id': optional('native_id'), 'sku': 'code',
        'gtins': array('code'), 'source': enum('certified_shop_checkpoint', 'shop_product_content'),
        'observed_at': 'timestamp',
    },
    'barcode_correction': {
        'before': optional('barcode'), 'ean': optional('barcode'), 'parent': optional('code'),
        'manufacturer_or_supplier': 'text', 'evidence': 'text',
        'required_card_id': optional('card_id'), 'required_atomer_id': optional('source_id'),
        'type': 'label', 'native_id': optional('source_id'),
    },
    'biketrek_check': {
        'status': enum('no_exact_catalog_match'), 'skus': array('code'),
        'gtins': array('code'), 'indexed_leaves': 'count', 'checked_at': 'timestamp',
    },
    'card_evidence': {
        'status': STATUS, 'reason': 'text', 'card_id': optional('card_id'),
        'hub_sku': optional('code'), 'xtrek': optional('native'), 'biketrek': optional('native'),
        'ean': optional('barcode'), 'scope': SCOPE, 'source_state': optional('source_state'),
        'verification_scope': enum('source_card_only', 'identity_and_card', 'identity_card_and_mapped_content'),
        'content_pending': 'boolean',
        'content_product_id': optional('native_id'), 'card_action': ACTION,
        'action': ACTION, 'created': 'boolean', 'gtins': array('code'),
        'source_native_id': 'source_id', 'source_ean': optional('barcode'),
        'gtin_added': 'barcode', 'barcode_evidence': 'barcode_correction',
        'cached_native_target': 'cached_target', 'source_card_input': 'digest',
        'biketrek_check': 'biketrek_check', 'source_card': record('card_evidence'),
        'creation_evidence': record('card_evidence'),
    },
    'card': {'state': PROOF_STATE, 'evidence': record('card_evidence')},
    'anchors': {
        'product_id': 'native_id', 'product_code': 'code',
        'variant_ids': mapping('native_id'), 'variant_codes': mapping('code'),
        'allow_parent_write': 'boolean',
    },
    'native_summary': {
        'status': STATUS, 'issues': array('text'), 'deferred': array('label'),
        'identity_verified': 'boolean', 'verified_fields': 'boolean',
        'product_id': optional('native_id'), 'variants': mapping('native_id'),
        'variant_ids': mapping('native_id'), 'inserted': 'boolean',
        'reason_codes': array('label'), 'tranche_verified': 'boolean',
        'remaining_fields': array('text'), 'remaining_content': 'boolean',
    },
    'intent': {
        'status': STATUS, 'issues': array('text'), 'deferred': array('label'),
        'identity_verified': 'boolean', 'verified_fields': 'boolean',
        'product_id': optional('native_id'), 'variants': mapping('native_id'),
        'payload': record('payload'), 'before': record('payload'), 'anchors': 'anchors',
        'gallery_wait': 'boolean', 'reason_codes': array('label'),
    },
    'origin': {
        'kind': enum('created', 'existing', 'unconfirmed_origin'), 'product_id': 'native_id',
        'code': 'code', 'observed_at': 'timestamp', 'current_identity': 'boolean',
        'sample': {'shop_url': 'text', 'title': 'text', 'html_verified': 'boolean', 'variant_count': 'count'},
    },
    'receipt': {
        'dispatched': 'boolean', 'ack': 'native_summary', 'status': STATUS,
        'issues': array('text'), 'deferred': array('label'), 'identity_verified': 'boolean',
        'verified_fields': 'boolean', 'product_id': optional('native_id'),
        'variants': mapping('native_id'), 'cards': mapping(record('card')),
        'content_requested': 'boolean', 'content_result': 'native_summary',
        'content_codes': array('code'), 'content_anchors': 'anchors',
        'content_attempts': array('digest'), 'content_intent': record('intent'),
        'related_intent': 'digest', 'catalog_deferred': mapping('deferred'),
        'rollover': 'boolean', 'previous_stage': STAGE, 'previous_guards': optional('guards'),
        'previous_receipt': record('receipt'), 'previous_document': optional(record('document')),
        'adopt_content': 'boolean', 'reexpand': 'boolean', 'rebuild_after_observation': 'boolean',
        'isolate': 'boolean', 'isolate_phase': STAGE, 'tranche_verified': 'boolean',
        'remaining_content': 'boolean', 'native_origin': optional('origin'),
        'last_recheck': {'previous_stage': STAGE, 'previous_problem': 'text', 'run_id': 'id',
                         'before_revision': 'digest', 'actor': 'text'},
        'last_native_read': 'native_summary', 'added_source_codes': array('code'),
    },
    'deferred': {'reason': 'text', 'ean': optional('barcode'), 'code': 'code'},
    'payload': {
        'code': 'code', 'product_code': 'code', 'parent_code': 'code',
        'product_id': 'native_id', 'variant_id': 'native_id', 'ean': optional('barcode'),
        'code_supplier': optional('blank_code'), 'variants': array(record('payload')),
        'manufacturer': 'text', 'unit': 'text', 'availability': 'text', 'text': 'text',
        'related': array('code'), 'active_yn': 'boolean', 'archived_yn': 'boolean',
        'main_yn': 'boolean', 'set_yn': 'boolean', 'can_add_to_basket_yn': 'boolean', 'adult_yn': 'boolean',
        **dict.fromkeys(('descriptions', 'categories', 'images', 'parameters', 'parameters_new',
                        'prices', 'vats', 'metas', 'weight', 'labels', 'flags', 'position',
                        'stock', 'option_id', 'image', 'availability_id', 'products_related',
                        'related_products', 'accessory_products', 'alternative_products'), 'content'),
    },
    'document': {
        'payload': record('payload'), 'source_family_codes': array('code'),
        'deferred_leaves': mapping('deferred'), 'new_parent_only': 'boolean',
        'vat_create_only': 'boolean', 'source_parent_barcode': 'content',
        'extension': {'product_id': 'native_id', 'existing_report_run': 'id',
                      'existing': mapping({'product_id': 'native_id', 'variant_id': optional('native_id')})},
        'remaining_create_variants': array(record('payload')),
        **dict.fromkeys(('availability', 'flags', 'related', 'missing_related', 'seo_keywords',
                        'ambiguous_source_references', 'missing_source_references', 'variant_axis_origins',
                        'missing_categories', 'admin_price_changes', 'title_from_code', 'barcode_corrections',
                        'source_barcode_placeholders', 'description_sources', 'manufacturer_origin',
                        'source_manufacturer_hint', 'unspecified_weights', 'shipping_weight_rounding',
                        'partial_vat', 'source_bonus_points', 'missing_source_features', 'variant_main_origin',
                        'source_duplicate_ids', 'source_parent_code_alias', 'parent_vat_from_leaves',
                        'managed_label_ids', 'issues', 'extension_plan'), 'content'),
    },
    'detail': {
        'code': 'code', 'family_key': 'code', 'hub_sku': optional('code'),
        **dict.fromkeys(('parent', 'target_code', 'target_parent'), optional('blank_code')),
        **dict.fromkeys(('card_state', 'state', 'match', 'bike_state'), optional('label')),
        'source_state': optional('source_state'),
        **dict.fromkeys(('variant_label', 'family_title', 'family_name', 'name', 'shop_url', 'shop_admin_url'), optional('text')),
        'kind': enum('variant', 'standalone'), 'ean': optional('barcode'), 'target_ean': optional('barcode'),
        'hub_id': optional('card_id'), 'candidates': array('card_id'),
        'product_id': optional('native_id'), 'variant_id': optional('native_id'),
        'reasons': array('text'), 'bike': optional('native'),
        'availability_review': {'atomer': 'text', 'xtrek': optional('text')},
    },
    'source_state': {
        'product': optional('boolean'), 'variant': optional('boolean'),
        'product_origin': enum('admin_csv', 'buyers', 'unknown', 'conflict'),
        'variant_origin': enum('admin_csv', 'buyers', 'unknown', 'conflict', 'na'),
        'product_flags': 'private_string_map', 'variant_flags': 'private_string_map',
    },
    'row': {
        'code': 'code', 'detail': record('detail'), 'revision': optional('count'),
        'status': optional(REVIEW_STATE), 'scope': optional(SCOPE),
        'instruction': optional('text'), 'result': optional('text'), 'command': optional('label'),
    },
    'group': {
        'kind': enum('family'), 'key': 'code', 'revision': 'count', 'status': REVIEW_STATE,
        'scope': SCOPE, 'decision': enum('undecided', 'yes', 'no'), 'note': 'text',
        'instruction': 'text', 'result': 'text', 'signature': 'text', 'updated_by': 'text',
        'updated_at': 'timestamp', 'command': 'label',
    },
    'selection': {
        'run_id': 'id', 'source_sha': 'digest', 'admin_sha': 'digest',
        'scope': enum('catalog', 'families'), 'partial_barcodes': 'boolean',
        'barcode_corrections': mapping('barcode_correction'),
        'zero_barcode_sentinels': mapping('source_id'),
        # These are full campaign configuration manifests, not a silently
        # narrowed per-family selection. Exact family ownership is in guards.
        **dict.fromkeys(('families', 'working', 'categories', 'category_restore', 'category_aliases',
                        'contextual_roots', 'property_names', 'axis_names', 'brands', 'availability',
                        'labels', 'named_axes',
                        'partial_vat_families'), 'configuration'),
    },
    'campaign': {
        'id': 'digest', 'run_id': 'id', 'source_sha': 'digest', 'admin_sha': 'digest',
        'shop_fingerprint': 'text', 'actor': 'text', 'selection': record('selection'),
        'options': 'configuration', 'state': enum('running', 'waiting', 'review', 'done', 'source_changed', 'paused'),
        'next_at': 'timestamp', 'problem': 'text', 'failures': 'count', 'checked_run_id': optional('id'),
        'updated_at': 'timestamp', 'next_category_at': 'timestamp', 'operator_paused': 'boolean',
        'operator_control_revision': 'count',
        'operator_pause_previous_state': optional(enum('running', 'waiting', 'paused')),
    },
    'job': {
        'campaign': 'digest', 'family_key': 'code', 'priority': 'count', 'leaf_count': 'id', 'stage': STAGE,
        'guards': optional(record('guards')), 'document': optional(record('document')), 'receipt': record('receipt'),
        'problem': 'text', 'attempts': 'count', 'next_at': 'timestamp', 'updated_at': 'timestamp',
        'source_card_stage': enum('pending', 'checked', 'blocked'), 'source_card_next_at': 'timestamp',
        'source_card_attempts': 'count', 'source_card_problem': 'text', 'operator_withdrawn': 'boolean',
        'operator_control_revision': 'count', 'operator_control_id': optional('uuid'),
    },
    'proof': {
        'campaign': 'digest', 'family_key': 'code', 'code': 'code', 'state': PROOF_STATE,
        'evidence': record('card_evidence'), 'checked_at': 'timestamp',
    },
    'head': {'id': 'id', 'source_sha256': 'digest', 'admin_export_sha256': 'digest', 'checked_at': 'timestamp'},
    'allocation': {'id': 'card_id', 'sku': 'code', 'created_from_source': enum('atomer')},
    'allocation_audit': {'id': 'id', 'product_id': 'card_id', 'before_sku': optional('code'),
                         'after_sku': optional('code'), 'row_hash': 'md5'},
}
SCHEMAS['source_card'] = {**SCHEMAS['proof'], 'run_id': 'id', 'card_id': optional('card_id'), 'created': optional('boolean')}
REQUIRED = {name: set(SCHEMAS[name]) for name in ('job', 'proof', 'source_card', 'head', 'row', 'guards', 'allocation', 'allocation_audit')}
REQUIRED.update(detail={'code', 'kind'}, card={'state', 'evidence'},
                group={'revision', 'status', 'scope', 'decision', 'note', 'instruction', 'result', 'signature'},
                campaign=set(SCHEMAS['campaign']), document={'payload'},
                selection={'run_id', 'source_sha', 'admin_sha', 'scope'},
                source_state=set(SCHEMAS['source_state']))


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')


def manifest(value):
    raw = _encoded(value)
    kind = 'null' if value is None else {dict: 'object', list: 'array', str: 'string', bool: 'boolean', int: 'integer', float: 'number'}[type(value)]
    return dict(sha256=sha256(raw).hexdigest(), bytes=len(raw), type=kind)


class Projection:
    def __init__(self):
        self.problems = []
        self.references = []
        self.guard_rows = []
        self.nodes = 0

    def problem(self, code, path, value, **extra):
        if len(self.problems) < MAX_PROBLEMS:
            self.problems.append(dict(code=code, path=path, observed=manifest(value), **extra))
        elif len(self.problems) == MAX_PROBLEMS:
            self.problems.append(dict(code='problem_limit', path='/'))

    def hidden(self, value, reason):
        return dict(redacted=True, reason=reason, canonical=manifest(value))

    def segment(self, key):
        # Unknown object keys can themselves contain a credential or contact.
        return '@' + manifest(key)['sha256']

    def project(self, value, spec, path, depth=0):
        self.nodes += 1
        if depth > MAX_DEPTH or self.nodes > MAX_NODES:
            self.problem('structure_limit', path, value)
            return self.hidden(value, 'structure_limit')
        if isinstance(spec, tuple):
            kind, child = spec
            if kind == 'optional':
                return None if value is None else self.project(value, child, path, depth + 1)
            if kind == 'record':
                return dict(canonical=manifest(value), fields=self.project(value, child, path, depth + 1))
            if kind == 'enum':
                if isinstance(value, str) and value in child:
                    return value
            elif kind == 'tuple':
                if isinstance(value, list) and len(value) == len(child):
                    return [self.project(v, s, path + '/' + str(i), depth + 1) for i, (v, s) in enumerate(zip(value, child))]
            elif kind in ('array', 'mapping') and isinstance(value, list if kind == 'array' else dict):
                if len(value) > MAX_LEAVES:
                    self.problem('collection_limit', path, value, count=len(value))
                    return self.hidden(value, 'collection_limit')
                if kind == 'array':
                    return [self.project(v, child, path + '/' + str(i), depth + 1) for i, v in enumerate(value)]
                result = {}
                for key, item in value.items():
                    safe = self.project(key, 'code', path + '/@key', depth + 1)
                    segment = key if isinstance(safe, str) else self.segment(key)
                    result[segment] = self.project(item, child, path + '/' + segment.replace('~', '~0').replace('/', '~1'), depth + 1)
                return result
            self.problem('type_or_value_unverified', path, value)
            return self.hidden(value, 'type_or_value_unverified')
        schema = SCHEMAS.get(spec) if isinstance(spec, str) else spec
        if isinstance(schema, dict):
            if not isinstance(value, dict):
                self.problem('object_required', path, value)
                return self.hidden(value, 'object_required')
            if len(value) > MAX_LEAVES:
                self.problem('object_width_limit', path, value, count=len(value))
                return self.hidden(value, 'object_width_limit')
            if spec == 'guards' and isinstance(value.get('rows'), dict):
                self.guard_rows.append((path, sorted(value['rows'])))
                if (not all(isinstance(value.get(k), dict) for k in ('scope', 'excluded', 'blocked'))
                        or set(value.get('scope') or {}) != set(value['rows'])
                        or not set(value.get('excluded') or {}) <= set(value['rows'])
                        or not set(value.get('blocked') or {}) <= set(value['rows'])):
                    self.problem('guard_scope_shape_unverified', path, value)
            if spec == 'card_evidence' and not (set(value) & {'status', 'reason'}):
                self.problem('card_evidence_status_missing', path, value)
            for key in sorted(REQUIRED.get(spec, set()) - set(value)) if isinstance(spec, str) else ():
                self.problem('required_field_missing', path + '/' + key, None)
            result = {}
            for key, item in value.items():
                if self.nodes >= MAX_NODES:
                    self.problem('structure_limit', path, value)
                    return self.hidden(value, 'structure_limit')
                part = key if key in schema else self.segment(key)
                at = path + '/' + part
                if key not in schema:
                    self.nodes += 1
                    self.problem('unknown_field', at, item, key=manifest(key))
                    result[part] = self.hidden(item, 'unknown_field')
                else:
                    result[key] = self.project(item, schema[key], at, depth + 1)
            return result
        if spec == 'private_string_map':
            if not isinstance(value, dict) or len(value) > MAX_LEAVES or any(
                    not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
                self.problem('private_string_map_unverified', path, value)
            return self.hidden(value, 'private_string_map')
        if spec in ('text', 'content', 'configuration'):
            if spec == 'text' and not isinstance(value, str):
                self.problem('text_required', path, value)
            if spec == 'content':
                self.problem('content_schema_not_interpreted', path, value)
            return self.hidden(value, spec)
        valid = False
        if spec in ('code', 'blank_code', 'barcode'):
            valid = isinstance(value, str) and (spec == 'blank_code' and value == '' or bool(CODE.fullmatch(value))) and '://' not in value
            if spec == 'barcode':
                valid = isinstance(value, str) and len(value) <= 260 and not any(ord(c) < 32 or ord(c) == 127 for c in value) and '@' not in value and '://' not in value
        elif spec == 'label':
            valid = isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z][A-Za-z0-9_:-]{0,99}', value))
        elif spec in ('id', 'native_id', 'card_id', 'mapping_id', 'source_id'):
            valid = type(value) is int and value > 0 or (spec in ('native_id', 'source_id') and isinstance(value, str)
                and bool(re.fullmatch(r'[1-9][0-9]{0,23}', value)))
        elif spec == 'count':
            valid = type(value) is int and value >= 0
        elif spec == 'boolean':
            valid = type(value) is bool
        elif spec == 'digest':
            valid = isinstance(value, str) and bool(HEX.fullmatch(value))
        elif spec == 'md5':
            valid = isinstance(value, str) and bool(re.fullmatch(r'[a-f0-9]{32}', value))
        elif spec == 'uuid':
            valid = isinstance(value, str) and bool(re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', value))
        elif spec == 'timestamp':
            valid = isinstance(value, str) and bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})', value))
        if not valid:
            self.problem('type_or_value_unverified', path, value)
            return self.hidden(value, 'type_or_value_unverified')
        if spec in ('native_id', 'card_id', 'mapping_id', 'source_id'):
            self.references.append(dict(path=path, kind=spec, value=value))
        return value


def build(data):
    """Project precisely the already-read snapshot; never fetch missing history."""
    p = Projection()
    raw_manifest = manifest(data)
    coverage = {key: dict(returned=len(data[key]), query_limit=501,
                         complete=len(data[key]) <= MAX_LEAVES) for key in ('proofs', 'source_cards')}
    result = dict(schema=VERSION, encoding='canonical-json-utf8-sorted-keys-compact',
                  snapshot=raw_manifest, complete=False, review_state='hold',
                  historical_native_effects='not_determined',
                  scope_description='captured single-family control snapshot; current retained history only',
                  redaction_policy='free text and campaign configuration are manifests; uninterpreted native content holds',
                  coverage=coverage, remote_requests=0, remote_write_performed=False)
    for key, item in coverage.items():
        if not item['complete']:
            p.problem('query_sentinel_reached', '/' + key, data[key], returned=item['returned'], query_limit=501)
    if raw_manifest['bytes'] > MAX_BYTES:
        p.problem('snapshot_byte_limit', '/', data, limit=MAX_BYTES)
        result['problems'] = p.problems
        return result
    fields = dict(campaign=record('campaign'), job=record('job'), document_is_sql_null='boolean',
                  head=optional(record('head')), rows=mapping(record('row')), group=record('group'),
                  proofs=array(record('proof')), source_cards=array(record('source_card')),
                  source_allocations=array(record('allocation')), source_allocation_audits=array(record('allocation_audit')))
    result['captured'] = p.project(data, fields, '')
    guards = data['job'].get('guards')
    saved = guards.get('rows') if isinstance(guards, dict) else None
    current = data['rows']
    code_sets = dict(saved_rows=set(saved) if isinstance(saved, dict) else set(), current_rows=set(current),
                     historical_guard_rows=set().union(*(set(codes) for _, codes in p.guard_rows)),
                     proofs={row['code'] for row in data['proofs'] if isinstance(row.get('code'), str)},
                     source_cards={row['code'] for row in data['source_cards'] if isinstance(row.get('code'), str)})
    union = set().union(*code_sets.values())
    if len(union) > MAX_LEAVES:
        p.problem('family_union_limit', '/scope', sorted(union), count=len(union), limit=MAX_LEAVES)
    result['scope'] = {key: p.project(sorted(codes), array('code'), '/scope/' + key) for key, codes in code_sets.items()}
    result['scope']['union'] = p.project(sorted(union), array('code'), '/scope/union')
    result['scope']['saved_selection_available'] = isinstance(saved, dict)
    result['scope']['guard_rows'] = [dict(path=path, rows=p.project(codes, array('code'), '/scope/guard_rows'))
                                   for path, codes in p.guard_rows]
    result['source_allocation_query'] = dict(scope='current_family_codes_only',
        codes=p.project(sorted(current), array('code'), '/source_allocation_query/codes'),
        covers_saved_proof_source_union=union <= set(current),
        missing_codes=p.project(sorted(union - set(current)), array('code'), '/source_allocation_query/missing_codes'),
        audit_row_hash_encoding='PostgreSQL md5(to_jsonb(row)::text); original snapshot projection')
    if union - set(current):
        p.problem('allocation_query_does_not_cover_union', '/source_allocation_query', sorted(union - set(current)))
    result['references'] = p.references
    result['problems'] = p.problems
    result['complete'] = not p.problems
    result['review_state'] = 'typed_snapshot' if result['complete'] else 'hold'
    if len(_encoded(result)) > MAX_BYTES:
        # Do not retain a truncated projection under a complete flag. The
        # captured-snapshot manifest is explicitly not a full database export.
        return {key: value for key, value in result.items() if key not in ('captured', 'scope', 'references', 'source_allocation_query')} | dict(
            complete=False, review_state='hold', problems=[dict(code='projection_byte_limit', path='/', limit=MAX_BYTES)])
    return result
