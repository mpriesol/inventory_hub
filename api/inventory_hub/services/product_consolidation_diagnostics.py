"""Read-only explanation of the existing conservative dependency matcher.

This is observation, never a cancellation, exemption, approval or new apply
token. JSON documents stay on the server; only identity paths/values and intent
states are returned. In particular, catalog_import.import_result is not used:
that normal recovery read can settle an interrupted filesystem journal.
"""
import hashlib
import json
from datetime import datetime, timezone
from sqlalchemy import text
from inventory_hub import config_io
from inventory_hub.product_consolidation_models import ProductConsolidation
from inventory_hub.services import product_consolidation as consolidation
from inventory_hub.services import product_consolidation_dependencies as dependencies
from inventory_hub.services import product_links as links


TABLE_KEYS = {'ai_content_jobs': ('id',), 'product_import_drafts': ('id',),
              'atomer_catalog_jobs': ('campaign', 'family_key'), 'product_consolidations': ('id',)}
MAX_ROWS = 100
MAX_DOCUMENT_BYTES = 4_000_000
MAX_TOTAL_BYTES = 30_000_000
MAX_NODES = 150_000
MAX_MATCHES = 500
MAX_IDENTITY_FIELDS = 2_000
HUB_KEYS = {'product_id', 'hub_id', 'hub_product_id', 'expected_hub_product_id',
            'expected_product_id', 'keeper_product_id', 'canonical_product_id'}
HUB_LISTS = {'product_ids', 'retired_product_ids'}
MAPPING_KEYS = {'mapping_id', 'sale_mapping_id', 'link_id'}
NATIVE_KEYS = {'native_product_id', 'native_variant_id', 'external_product_id', 'external_variant_id'}
IDENTITY_KEYS = HUB_KEYS | HUB_LISTS | MAPPING_KEYS | NATIVE_KEYS | {
    'code', 'parent_code', 'variant_code', 'sku', 'supplier_sku', 'supplier_code',
    'manufacturer_sku', 'manufacturer_code', 'ean', 'eans', 'shop', 'shop_code',
    'source_shop', 'target_shop', 'target_shops', 'source_kind', 'source_family_codes',
    'existing_product_ids', 'selected_fields', 'action', 'is_variant',
    'sale_to_stock_factor', 'source_code', 'target_code', 'target_ean'}
STATE_KEYS = {'status', 'state', 'stage', 'product_write_attempted', 'dispatched',
              'update_only', 'source_parameters_loaded', 'new_parent_only', 'native_write_performed'}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _variables(raw, local, tokens):
    """Use inspect's exact tokens and the same existing typed-ID sets.

    Unqualified product_ids and pooled native IDs are intentionally reproduced
    here, not corrected. The response exposes their ambiguity for review.
    """
    ids = sorted(consolidation._ids(raw))
    mappings, targets = local['mappings'], consolidation._targets(raw)
    mapping_ids = [row['id'] for row in mappings]
    native = sorted({str(value) for row in mappings for value in
        (row.get('external_product_id'), row.get('external_variant_id')) if value} |
        {str(value) for row in targets for value in (row['native_product_id'], row.get('native_variant_id')) if value})
    return {'ids': ids, 'id_strings': list(map(str, ids)), 'tokens': tokens,
            'mapping_ids': mapping_ids, 'mapping_strings': list(map(str, mapping_ids)),
            'native_strings': native, 'native_ids': [int(value) for value in native if value.isdigit()]}


def _path(parts):
    # RFC6901 pointers, including exact escaped dictionary keys and array indexes.
    return '/' + '/'.join(str(part).replace('~', '~0').replace('/', '~1') for part in parts)


def _scalar(value):
    return value is None or type(value) in (str, int, bool, float)


def _role(table, parts):
    if (table == 'product_import_drafts' and len(parts) == 4
            and parts[:2] == ('document', 'shops') and parts[-1] == 'name'):
        return 'shop_display_label'
    if parts and parts[-1] == 'parent_code':
        return 'native_parent_context_not_necessarily_selected_leaf'
    if 'payload' in parts:
        return 'frozen_write_payload_identity'
    if 'values' in parts and 'rows' in parts:
        return 'draft_selected_row_identity'
    if parts and parts[-1] in {'hub_id', 'hub_product_id', 'expected_hub_product_id',
                              'expected_product_id', 'keeper_product_id', 'canonical_product_id'}:
        return 'explicit_hub_identity_reference'
    if 'source' in parts or 'sources' in parts or 'facts' in parts:
        return 'source_evidence_not_automatically_write_scope'
    return 'identity_or_context_reference_requires_review'


def _namespace(parts, document):
    if 'product_ids' in parts:
        context = document.get('context')
        if parts[:2] == ('context', 'product_ids') and not isinstance(context, dict):
            return 'unknown_context_shape_not_assumed_hub'
        if parts[:2] == ('context', 'product_ids') and context.get('source_kind') == 'shop':
            return 'native_shop_product_ids'
        if parts[:2] == ('context', 'product_ids') or 'preview' in parts or 'price_lines' in parts:
            return 'supplier_catalog_ids_or_staging_source_ids'
        return 'unqualified_product_ids_not_assumed_hub'
    if any(part in NATIVE_KEYS for part in parts):
        return 'native_ids_existing_matcher_pools_product_and_variant_namespaces'
    if any(part in MAPPING_KEYS for part in parts):
        return 'hub_mapping'
    if parts and parts[-1] in HUB_KEYS - {'product_id'}:
        return 'hub_product'
    if parts and parts[-1] == 'product_id':
        return 'unqualified_product_id_context_required'
    return 'code_or_metadata_context_required'


def explain(document, variables, table, *, strings_only=False):
    """Locate existing predicate matches without exposing unrelated text.

    Python paths explain SQL's boolean predicate; this never replaces the SQL
    predicate as an eligibility decision. Bounds make omissions explicit.
    """
    matches, identities, states = [], [], []
    incomplete = []
    nodes = 0
    tokens = set(variables['tokens'])
    id_values = variables['ids'] + variables['id_strings']
    mapping_values = variables['mapping_ids'] + variables['mapping_strings']
    native_values = variables['native_ids'] + variables['native_strings']

    def append(target, value, limit, reason):
        if len(target) < limit:
            target.append(value)
        elif reason not in incomplete:
            incomplete.append(reason)

    def visit(value, parts=(), key=None):
        nonlocal nodes
        nodes += 1
        if nodes > MAX_NODES or len(parts) > 64:
            if 'node_or_depth_limit' not in incomplete:
                incomplete.append('node_or_depth_limit')
            return
        if isinstance(value, dict):
            for name, child in value.items():
                visit(child, (*parts, name), name)
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, (*parts, index), key)
            return
        if not _scalar(value):
            return
        reasons = []
        if isinstance(value, str) and value in tokens:
            reasons.append('exact_string_token')
        if not strings_only and type(value) in (int, str):
            if key in HUB_KEYS | HUB_LISTS and value in id_values:
                reasons.append('unqualified_product_id_field')
            if key in MAPPING_KEYS and value in mapping_values:
                reasons.append('mapping_id_field')
            if key in NATIVE_KEYS and value in native_values:
                reasons.append('pooled_native_id_field')
        pointer = _path(parts)
        if reasons:
            append(matches, {'path': pointer, 'value': value, 'matched_by': reasons,
                             'role': _role(table, parts), 'namespace': _namespace(parts, document)},
                   MAX_MATCHES, 'match_limit')
        if key in IDENTITY_KEYS:
            # Identity strings are bounded; never echo arbitrary descriptions,
            # credentials, provider prompts, invoice text, prices or diagnostics.
            if isinstance(value, str) and len(value) > 260:
                if 'identity_value_limit' not in incomplete:
                    incomplete.append('identity_value_limit')
            else:
                append(identities, {'path': pointer, 'value': value, 'role': _role(table, parts),
                                    'namespace': _namespace(parts, document)}, MAX_IDENTITY_FIELDS, 'identity_field_limit')
        if key in STATE_KEYS and (not isinstance(value, str) or len(value) <= 64):
            append(states, {'path': pointer, 'value': value}, MAX_IDENTITY_FIELDS, 'state_field_limit')

    visit(document)
    if table == 'ai_content_jobs' and not isinstance(document.get('context'), dict):
        incomplete.append('ai_context_shape_unverified')
    selected = None
    if table == 'atomer_catalog_jobs':
        guards = document.get('guards')
        if isinstance(guards, dict) and all(isinstance(guards.get(k), dict) for k in ('rows', 'excluded', 'blocked', 'scope')):
            # Exact selection used by engine._selected; exclusions/reasons stay
            # private, and no assumption is made that paused means settled.
            codes = sorted(k for k in guards['rows'] if k not in guards['excluded'] and k not in guards['blocked'])
            selected = []
            for code in codes[:MAX_IDENTITY_FIELDS]:
                scope = guards['scope'].get(code)
                if not isinstance(code, str) or len(code) > 260:
                    incomplete.append('selected_code_shape_unverified')
                    continue
                valid = scope is None or isinstance(scope, str) and len(scope) <= 100
                if not valid:
                    incomplete.append('selected_scope_shape_unverified')
                selected.append({'code': code, 'scope': scope if valid else None,
                                 **({'scope_unverified': True} if not valid else {})})
            if len(codes) > MAX_IDENTITY_FIELDS:
                incomplete.append('selected_code_limit')
        else:
            incomplete.append('atomer_selection_shape_unverified')
    return {'matched_paths': matches, 'identity_fields': identities, 'intent_states': states,
            'selected_atomer_leaves': selected, 'complete': not incomplete, 'limits': sorted(set(incomplete)),
            'classification': 'diagnostic_only_no_guard_exemption'}


async def _documents(db, summary, variables, request_id):
    output, problems, total = [], [], 0
    for blocker in summary['blockers']:
        if blocker['code'] != 'consolidation_pending_document':
            continue
        table, count = blocker['table'], blocker['count']
        if table not in TABLE_KEYS:
            output.append({'table': table, 'matched_count': count, 'records': [], 'complete': False,
                           'reason': 'unsupported_document_schema_no_exemption'})
            problems.append('unsupported_document_schema')
            continue
        columns = set(await db.scalars(text('''SELECT a.attname FROM pg_attribute a
            WHERE a.attrelid=to_regclass(:table) AND a.attnum>0 AND NOT a.attisdropped'''), {'table': table}))
        condition = dependencies._mentions_sql() + ' AND (' + dependencies._open_condition(table, columns) + ')'
        if table == 'product_consolidations':
            condition += ' AND t.id<>:request_id'
        keys = TABLE_KEYS[table]
        key_sql = ','.join("'" + key + "',t." + dependencies.quoted(key) for key in keys)
        order = ','.join('t.' + dependencies.quoted(key) for key in keys)
        rows = (await db.execute(text(f'''WITH selected AS (
            SELECT jsonb_build_object({key_sql}) AS identity, to_jsonb(t) AS document,
                row_number() OVER(ORDER BY {order}) AS position
            FROM {dependencies.quoted(table)} t WHERE {condition} ORDER BY {order} LIMIT :limit
            ), sized AS (
            SELECT *,octet_length(document::text) AS bytes,
                sum(octet_length(document::text)) OVER(ORDER BY position) AS cumulative_bytes FROM selected
            ) SELECT identity,bytes,md5(document::text) AS row_hash,
                CASE WHEN bytes<=:max_bytes AND cumulative_bytes<=:remaining THEN document ELSE NULL END AS document
            FROM sized ORDER BY position'''),
            {'vars': json.dumps(variables), 'request_id': request_id,
             'max_bytes': MAX_DOCUMENT_BYTES, 'remaining': max(0, MAX_TOTAL_BYTES-total),
             'limit': MAX_ROWS})).mappings().all()
        records, complete = [], len(rows) == count
        for row in rows:
            total += row['bytes']
            base = {'row_identity': row['identity'], 'row_md5': row['row_hash'], 'document_bytes': row['bytes']}
            if row['document'] is None or total > MAX_TOTAL_BYTES:
                records.append({**base, 'complete': False, 'reason': 'document_bytes_limit'})
                complete = False
                continue
            detail = explain(row['document'], variables, table)
            if not detail['matched_paths']:
                detail['complete'] = False
                detail['limits'].append('sql_match_not_fully_explained')
            records.append({**base, **detail})
            complete = complete and detail['complete']
        if not complete:
            problems.append('document_explanation_incomplete')
        output.append({'table': table, 'matched_count': count, 'records': records, 'complete': complete,
                       'original_summary': summary['facts'].get('pending:' + table)})
    return output, problems


def _journals(shop_codes, variables):
    """Use the same matcher and merged sidecars, without recovery side effects."""
    scan = dependencies.catalog_journals(shop_codes, set(variables['tokens']))
    problems = [item['code'] for item in scan['blockers'] if item['code'] == 'consolidation_catalog_journal_unverified']
    pending = {(item.get('shop_code'), item.get('journal_id')) for item in scan['blockers']}
    result, total = [], 0
    from inventory_hub.services.catalog_import import _load_job
    if len(scan['records']) > MAX_ROWS:
        problems.append('journal_record_limit')
    for entry in scan['records'][:MAX_ROWS]:
        base = {**entry, 'pending_by_existing_guard': (entry['shop_code'], entry['journal_id']) in pending}
        path = config_io.shop_path(entry['shop_code']).parent / 'catalog-imports' / (entry['journal_id'] + '.json')
        try:
            paths = [path, path.with_suffix('.result.json'), path.with_suffix('.events.jsonl')]
            if any(p.is_symlink() for p in paths):
                raise ValueError('symlink')
            size = sum(p.stat().st_size for p in paths if p.exists())
            total += size
            if size > MAX_DOCUMENT_BYTES or total > MAX_TOTAL_BYTES:
                raise ValueError('limit')
            raw = path.read_bytes()
            document = json.loads(raw)
            merged = _load_job(path)
            if _hash(merged) != entry['hash']:
                raise ValueError('changed')
            detail = explain(document, variables, 'catalog_import_journal', strings_only=True)
            current = explain(merged, variables, 'catalog_import_journal', strings_only=True)
            if not detail['matched_paths'] or _hash(_load_job(path)) != entry['hash'] or path.read_bytes() != raw:
                raise ValueError('changed')
            result.append({**base, **detail, 'base_document_sha256': hashlib.sha256(raw).hexdigest(),
                           'current_intent_states': current['intent_states'],
                           'current_intent_identity_fields': current['identity_fields'],
                           'complete': detail['complete'] and current['complete'],
                           'limits': sorted(set(detail['limits'] + current['limits']))})
            if not result[-1]['complete']:
                problems.append('journal_explanation_incomplete')
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            result.append({**base, 'complete': False, 'reason': 'journal_changed_unreadable_or_bounded'})
            problems.append('journal_explanation_incomplete')
    return result, scan['blockers'], problems


async def inspect(db, identifier):
    # This endpoint owns its fresh request session. The database itself rejects
    # DML, even if a future helper accidentally tries to mutate an ORM object.
    await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
    row = await db.get(ProductConsolidation, str(identifier))
    if row is None:
        raise links.LinkError('consolidation_not_found', 404)
    local, raw = row.document['local'], row.document['request']
    summary = await dependencies.inspect(db, ids=consolidation._ids(raw), keeper_id=raw['keeper_product_id'],
        products=local['products'], identifiers=local['identifiers'], mappings=local['mappings'],
        targets=consolidation._targets(raw), request_id=row.id)
    variables = _variables(raw, local, summary.pop('tokens'))
    documents, problems = await _documents(db, summary, variables, row.id)
    journals, journal_blockers, journal_problems = _journals([shop['code'] for shop in local['shops']], variables)
    problems.extend(journal_problems)
    return {'consolidation_id': row.id, 'preview_hash': row.preview_hash, 'preview_status': row.status,
            'observed_at': datetime.now(timezone.utc).isoformat(),
            'database_snapshot_at': (await db.scalar(text('SELECT transaction_timestamp()'))).isoformat(),
            'scope_source': 'original_server_stored_preview', 'scope_hash': _hash(variables),
            'matcher_policy': 'base_dependency_guard_without_optional_exemptions',
            'preserve_keeper_invoice_receiving_requested': raw.get('preserve_keeper_invoice_receiving') is True,
            'read_only': True, 'remote_requests': 0, 'guard_decision_changed': False,
            'complete': not problems, 'problems': sorted(set(problems)),
            'scope': {'hub_product_ids': variables['ids'], 'mapping_ids': variables['mapping_ids'],
                      'exact_string_tokens': variables['tokens'],
                      'pooled_native_ids_in_existing_matcher': variables['native_strings']},
            'frozen_blockers': row.document['dependencies']['blockers'],
            'current_blockers': summary['blockers'] + journal_blockers,
            'documents': documents, 'catalog_journals': journals,
            'limits': {'rows_per_table': MAX_ROWS, 'document_bytes': MAX_DOCUMENT_BYTES,
                       'journal_records': MAX_ROWS,
                       'total_document_bytes': MAX_TOTAL_BYTES, 'matches_per_document': MAX_MATCHES,
                       'identity_fields_per_document': MAX_IDENTITY_FIELDS},
            'interpretation': ['Matched display labels and parent context are evidence to review, never exemptions.',
                'Generic product_ids may be supplier or native IDs; the existing matcher remains unchanged.',
                'This is the base dependency scanner, without optional keeper-only dependency exemptions.',
                'Database rows share a read-only repeatable-read snapshot; filesystem journals have separately checked observations.',
                'A complete explanation does not make a blocked preview eligible. Apply repeats all existing guards.']}
