"""Metadata-only census of the exact typed scanner scope, without loading JSON.

This helper is called only by the existing read-only diagnostics endpoint. It
does not classify dependencies, alter limits, construct an apply proof or run
native requests. Unknown lifecycle text and document bodies stay in SQL.
"""
from sqlalchemy import text

from inventory_hub.services import product_dependency_scan as scan


ATOMER_STAGES = ('pending', 'prepared', 'sending', 'observe', 'cards', 'related',
                 'fidelity', 'updating', 'verified', 'partial', 'blocked', 'excluded')
AI_KINDS = ('product', 'rules')
AI_STATUSES = ('estimate', 'queued', 'generating', 'review', 'blocked', 'ready',
               'preparing_import', 'import_queued', 'importing', 'import_blocked',
               'import_failed', 'failed', 'uncertain', 'exists', 'completed', 'cancelled')
PUBLICATION_STATES = ('ready', 'prepared', 'sending', 'uncertain', 'rejected',
                      'verified', 'completed', 'expired', 'cancelled', 'failed')
JSON_TYPES = ('object', 'array', 'string', 'number', 'boolean', 'null')
REQUIRED = {'ai_content_jobs': {'context', 'kind', 'status'},
            'atomer_catalog_jobs': {'stage', 'guards', 'document', 'campaign', 'family_key'},
            'product_import_drafts': {'document'},
            'product_consolidations': {'id', 'status', 'expires_at'}}


def _limits(row, max_rows):
    values = {key: int(row[key]) for key in ('count', 'bytes', 'largest')}
    limits = {'count': max_rows, 'bytes': scan.MAX_TOTAL_BYTES, 'largest': scan.MAX_ROW_BYTES}
    return {**values, 'limits': limits,
            'exceeded_limits': [key for key in limits if values[key] > limits[key]]}


async def _buckets(db, table, condition, params, expression, known):
    # All SQL fragments and recognized labels come from server constants. The
    # ELSE branch never returns an unknown value from a raw document or column.
    rows = (await db.execute(text(f'''SELECT CASE
        WHEN ({expression}) IS NULL THEN 'missing_or_sql_null'
        WHEN ({expression})=ANY(CAST(:census_known AS text[])) THEN ({expression})
        ELSE 'other' END AS bucket,count(*) AS count
        FROM {table} t WHERE {condition} GROUP BY 1 ORDER BY 1'''),
        {**params, 'census_known': list(known)})).mappings().all()
    return {row['bucket']: int(row['count']) for row in rows}


async def inspect(db, request_id, *, open_condition):
    """Use the caller's read-only repeatable-read request transaction."""
    catalog = (await db.execute(text('''SELECT c.relname AS table_name,a.attname AS column_name,
        format_type(a.atttypid,a.atttypmod) AS data_type FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_attribute a ON a.attrelid=c.oid
        WHERE n.nspname=current_schema() AND c.relkind IN ('r','p') AND a.attnum>0 AND NOT a.attisdropped
        ORDER BY c.relname,a.attnum'''))).mappings().all()
    columns = {}
    for row in catalog:
        columns.setdefault(row['table_name'], {})[row['column_name']] = row['data_type']
    tables, problems = {}, []
    params = {'request_id': str(request_id)}
    for table in sorted(scan.TABLES):
        fields = columns.get(table, {})
        if not REQUIRED[table] <= fields.keys():
            tables[table] = {'available': False, 'problem': 'required_schema_unavailable'}
            problems.append({'table': table, 'code': 'required_schema_unavailable'})
            continue
        condition = 'true' if table == 'product_import_drafts' else open_condition(table, fields)
        if table == 'product_consolidations':
            condition += ' AND t.id<>:request_id'
        row = (await db.execute(text(f'''SELECT count(*) AS count,
            coalesce(sum(octet_length(to_jsonb(t)::text)),0) AS bytes,
            coalesce(max(octet_length(to_jsonb(t)::text)),0) AS largest
            FROM {table} t WHERE {condition}'''), params)).mappings().one()
        entry = {'available': True, **_limits(row, scan.MAX_ROWS)}
        entry['json_schema_recognized'] = ({key for key, kind in fields.items() if kind == 'jsonb'}
                                           == scan.JSON_COLUMNS[table])
        shapes = {}
        if table == 'atomer_catalog_jobs':
            shapes['stage'] = await _buckets(db, table, condition, params, 't.stage', ATOMER_STAGES)
            for field in ('guards', 'document'):
                shapes[field + '_json_type'] = await _buckets(db, table, condition, params,
                    f'jsonb_typeof(t.{field})', JSON_TYPES)
        elif table == 'ai_content_jobs':
            for name, expression, known in (
                ('kind', 't.kind', AI_KINDS), ('status', 't.status', AI_STATUSES),
                ('context_json_type', 'jsonb_typeof(t.context)', JSON_TYPES),
                ('current_publication_state', "t.context->'update_preview'->>'state'", PUBLICATION_STATES)):
                shapes[name] = await _buckets(db, table, condition, params, expression, known)
        if shapes:
            entry['lifecycle_shapes'] = shapes
        tables[table] = entry

    required_join = {'atomer_catalog_jobs': {'campaign', 'family_key', 'stage'},
        'atomer_catalog_campaigns': {'id', 'checked_run_id'},
        'atomer_progress_items': {'run_id', 'family_key', 'code'},
        'atomer_review_work': {'kind', 'key'}}
    if all(fields <= columns.get(table, {}).keys() for table, fields in required_join.items()):
        # Exact row/byte definition from _linked_inputs, without its LIMIT or
        # transfer of item/work/family_work JSON. Every current family leaf is
        # counted, including excluded or blocked siblings.
        row = (await db.execute(text('''SELECT count(*) AS count,
            coalesce(sum(octet_length(to_jsonb(i)::text)+coalesce(octet_length(to_jsonb(w)::text),0)
                +coalesce(octet_length(to_jsonb(g)::text),0)),0) AS bytes,
            coalesce(max(octet_length(to_jsonb(i)::text)+coalesce(octet_length(to_jsonb(w)::text),0)
                +coalesce(octet_length(to_jsonb(g)::text),0)),0) AS largest,
            count(DISTINCT (j.campaign,j.family_key)) AS matched_jobs
            FROM atomer_catalog_jobs j JOIN atomer_catalog_campaigns c ON c.id=j.campaign
            JOIN atomer_progress_items i ON i.run_id=c.checked_run_id AND i.family_key=j.family_key
            LEFT JOIN atomer_review_work w ON w.kind='item' AND w.key=i.code
            LEFT JOIN atomer_review_work g ON g.kind='family' AND g.key=j.family_key
            WHERE j.stage NOT IN ('verified','excluded')'''))).mappings().one()
        families = {'available': True, **_limits(row, scan.MAX_EXTRA_ROWS),
                    'matched_jobs': int(row['matched_jobs'])}
        jobs = tables.get('atomer_catalog_jobs', {})
        if jobs.get('available'):
            families['open_jobs_without_current_family'] = jobs['count'] - families['matched_jobs']
    else:
        families = {'available': False, 'problem': 'required_schema_unavailable'}
        problems.append({'table': 'atomer_current_families', 'code': 'required_schema_unavailable'})

    base_bytes = sum(row['bytes'] for row in tables.values() if row.get('available'))
    with_families = base_bytes + families.get('bytes', 0)
    return {'version': 'dependency-census-v1', 'metadata_only': True,
        'complete': not problems, 'problems': problems, 'tables': tables,
        'atomer_current_families': families,
        'known_tables_total_bytes': base_bytes,
        'known_tables_total_bytes_exceeds_limit': base_bytes > scan.MAX_TOTAL_BYTES,
        'known_and_atomer_bytes_lower_bound': with_families,
        'known_and_atomer_bytes_lower_bound_exceeds_limit': with_families > scan.MAX_TOTAL_BYTES,
        'combined_bytes_limit': scan.MAX_TOTAL_BYTES,
        'interpretation': [
            'Counts use every row selected by the current typed scanner, without a product-token filter.',
            'The current consolidation UUID is excluded; all saved drafts remain in scope.',
            'Lifecycle buckets describe shapes only and never exempt a dependency.',
            'Combined bytes are a lower bound: supplier sources, other linked evidence and filesystem journals are not measured here.',
            'Complete means metadata queries completed, not that parsing or identity verification passed.']}
