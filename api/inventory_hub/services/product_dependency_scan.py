"""Bounded full-row catalog scope inventory used by preview and final apply."""
import json

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from inventory_hub.services import product_dependency_scope as s
from inventory_hub.services import product_dependency_ai_scope as ai_scope
from inventory_hub.services import product_dependency_atomer_scope as atomer_scope
from inventory_hub.services import product_dependency_journals as journals
from inventory_hub.services import product_links as links


TABLES = {'product_import_drafts', 'ai_content_jobs', 'atomer_catalog_jobs', 'product_consolidations'}
JSON_COLUMNS = {'product_import_drafts': {'document'},
    'ai_content_jobs': {'context', 'output', 'checks', 'events', 'usage'},
    'atomer_catalog_jobs': {'guards', 'document', 'receipt'},
    'product_consolidations': {'document', 'result'}}
MAX_ROWS = 1000
MAX_ROW_BYTES = 16000000
MAX_TOTAL_BYTES = 64000000
MAX_EXTRA_ROWS = 20000
MAX_IDENTITY_QUERIES = 20000
# All known writers can insert new rows, not only change a currently selected
# row. Table SHARE fences phantoms. NOWAIT avoids inversion with writers that
# first hold draft/job row locks and then ask for the shared identity lock.
LOCK_TABLES = sorted(TABLES | {'atomer_catalog_campaigns', 'atomer_progress_items', 'atomer_review_work',
    'suppliers', 'supplier_products', 'supplier_feeds', 'shops', 'sku_brands', 'sku_brand_aliases'})


async def lock_inputs(db):
    try:
        await db.execute(text('LOCK TABLE ' + ','.join(LOCK_TABLES) + ' IN SHARE MODE NOWAIT'))
    except DBAPIError as error:
        if getattr(error.orig, 'sqlstate', None) == '55P03':
            raise links.LinkError('consolidation_dependency_writer_busy') from None
        raise


async def _rows(db, table, condition, params, limit=MAX_ROWS):
    # Table and condition are exclusively server-owned identifiers/expressions.
    sizes = (await db.execute(text(f'''SELECT count(*) AS count,
        coalesce(sum(octet_length(to_jsonb(t)::text)),0) AS bytes,
        coalesce(max(octet_length(to_jsonb(t)::text)),0) AS largest
        FROM {table} t WHERE {condition}'''), params)).mappings().one()
    if sizes['count'] > limit or sizes['bytes'] > MAX_TOTAL_BYTES or sizes['largest'] > MAX_ROW_BYTES:
        raise s.Unverified('database_document_limit', table)
    rows = (await db.execute(text(f'''WITH selected AS (
        SELECT to_jsonb(t) AS document, octet_length(to_jsonb(t)::text) AS bytes,
            row_number() OVER(ORDER BY to_jsonb(t)::text) AS position
        FROM {table} t WHERE {condition} ORDER BY to_jsonb(t)::text LIMIT :scope_limit
        ), sized AS (SELECT *,sum(bytes) OVER(ORDER BY position) AS total_bytes FROM selected)
        SELECT CASE WHEN bytes<=:max_row AND total_bytes<=:max_total THEN document ELSE NULL END AS document,
            md5(document::text) AS hash,bytes FROM sized ORDER BY position'''),
        {**params, 'scope_limit': limit + 1, 'max_row': MAX_ROW_BYTES, 'max_total': MAX_TOTAL_BYTES})).mappings().all()
    if len(rows) != sizes['count'] or sum(row['bytes'] for row in rows) != sizes['bytes']:
        raise s.Unverified('database_scope_changed_during_read', table)
    if any(row['document'] is None for row in rows):
        raise s.Unverified('database_document_limit', table)
    return rows, int(sizes['bytes'])


def _identity(table, row):
    return ({'campaign': row['campaign'], 'family_key': row['family_key']} if table == 'atomer_catalog_jobs'
            else {'id': row['id']})


def _catch(proof, function, *args):
    try:
        function(proof, *args)
    except s.Unverified as error:
        proof.errors.append({'code': error.reason, 'path': error.path})
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        proof.errors.append({'code': 'writer_shape_unverified', 'path': ''})


async def _linked_inputs(db, known, extras):
    """Fetch full rows reached by typed links, without invoking public readers."""
    draft_ids = {row['document'].get('context', {}).get('staging_id') for row in known.get('ai_content_jobs', [])
                 if isinstance(row['document'].get('context'), dict)} - {None, ''}
    # The complete draft table is scanned: a saved draft remains executable and
    # a linked draft may have a different status from its generation job.
    drafts = {row['document']['id']: row['document'] for row in known.get('product_import_drafts', [])}
    if draft_ids - drafts.keys():
        found, size = await _rows(db, 'product_import_drafts', 't.id=ANY(CAST(:ids AS text[]))',
                                  {'ids': sorted(draft_ids - drafts.keys())})
        extras['linked_drafts'] = {'hash': s.digest([row['hash'] for row in found]), 'bytes': size}
        drafts.update({row['document']['id']: row['document'] for row in found})
    catalog_ids = set()
    for row in known.get('ai_content_jobs', []):
        ctx = row['document'].get('context') or {}
        if ctx.get('source_kind', 'catalog') == 'catalog' and not ctx.get('staging_id'):
            for value in ctx.get('product_ids', []) if isinstance(ctx.get('product_ids'), list) else []:
                if type(value) is int and value > 0:
                    catalog_ids.add(value)
    source_rows, size = await _rows(db, 'supplier_products', 't.id=ANY(CAST(:ids AS bigint[]))',
                                    {'ids': sorted(catalog_ids)}, MAX_EXTRA_ROWS)
    extras['supplier_products'] = {'hash': s.digest([row['hash'] for row in source_rows]), 'bytes': size}
    feed_ids = {row['document']['source_feed_id'] for row in source_rows if row['document'].get('source_feed_id')}
    feeds, size = await _rows(db, 'supplier_feeds', 't.id=ANY(CAST(:ids AS bigint[]))', {'ids': sorted(feed_ids)}, MAX_EXTRA_ROWS)
    extras['supplier_feeds'] = {'hash': s.digest([row['hash'] for row in feeds]), 'bytes': size}
    feed_map = {row['document']['id']: row['document'] for row in feeds}
    supplier_ids = {row['document']['supplier_id'] for row in feeds}
    suppliers, size = await _rows(db, 'suppliers', 't.id=ANY(CAST(:ids AS bigint[]))',
                                  {'ids': sorted(supplier_ids)}, MAX_EXTRA_ROWS)
    extras['catalog_suppliers'] = {'hash': s.digest([row['hash'] for row in suppliers]), 'bytes': size}
    supplier_names = {row['document']['id']: row['document']['code'] for row in suppliers}
    catalog = {}
    for row in source_rows:
        raw = row['document']
        data = (raw.get('attributes') or {}).get('catalog')
        feed = feed_map.get(raw.get('source_feed_id'))
        if isinstance(data, dict) and raw.get('is_active') is True and feed is not None:
            supplier = supplier_names.get(feed['supplier_id'])
            if (supplier is None or raw['supplier_id'] != feed['supplier_id']
                    or data.get('supplier') != supplier or data.get('feed_key', 'products') != feed['code']):
                raise s.Unverified('catalog_source_namespace_changed', '/supplier_products/' + str(raw['id']))
            # public_product()'s exact identity projection; keep raw field
            # coverage for the parser instead of letting Pydantic drop extras.
            value = {**data, 'id': raw['id'], 'listed': False}
            catalog[(supplier, feed['code'], raw['id'])] = value
    campaigns, family, claims = {}, {}, {}
    job_rows = [row['document'] for row in known.get('atomer_catalog_jobs', [])]
    if job_rows:
        campaign_rows, size = await _rows(db, 'atomer_catalog_campaigns', 't.id=ANY(CAST(:ids AS text[]))',
                                         {'ids': sorted({row['campaign'] for row in job_rows})})
        extras['atomer_campaigns'] = {'hash': s.digest([row['hash'] for row in campaign_rows]), 'bytes': size}
        campaigns = {row['document']['id']: row['document'] for row in campaign_rows}
        input_rows = (await db.execute(text('''WITH joined AS (SELECT j.campaign,j.family_key,i.code,
            to_jsonb(i) AS item,to_jsonb(w) AS work,to_jsonb(g) AS family_work,
            octet_length(to_jsonb(i)::text)+coalesce(octet_length(to_jsonb(w)::text),0)
                +coalesce(octet_length(to_jsonb(g)::text),0) AS bytes
            FROM atomer_catalog_jobs j JOIN atomer_catalog_campaigns c ON c.id=j.campaign
            JOIN atomer_progress_items i ON i.run_id=c.checked_run_id AND i.family_key=j.family_key
            LEFT JOIN atomer_review_work w ON w.kind='item' AND w.key=i.code
            LEFT JOIN atomer_review_work g ON g.kind='family' AND g.key=j.family_key
            WHERE j.stage NOT IN ('verified','excluded') ORDER BY j.campaign,j.family_key,i.code LIMIT :limit),
            sized AS (SELECT *,sum(bytes) OVER(ORDER BY campaign,family_key,code) AS total_bytes FROM joined)
            SELECT campaign,family_key,bytes,
                CASE WHEN bytes<=:max_row AND total_bytes<=:max_total THEN item ELSE NULL END AS item,
                CASE WHEN bytes<=:max_row AND total_bytes<=:max_total THEN work ELSE NULL END AS work,
                CASE WHEN bytes<=:max_row AND total_bytes<=:max_total THEN family_work ELSE NULL END AS family_work,
                md5(item::text) AS item_hash,md5(work::text) AS work_hash,md5(family_work::text) AS family_work_hash
            FROM sized ORDER BY campaign,family_key,code'''),
            {'limit': MAX_EXTRA_ROWS + 1, 'max_row': MAX_ROW_BYTES, 'max_total': MAX_TOTAL_BYTES})).mappings().all()
        if len(input_rows) > MAX_EXTRA_ROWS or any(row['item'] is None for row in input_rows):
            raise s.Unverified('atomer_family_scope_limit')
        extras['atomer_families'] = {'hash': s.digest([[row['campaign'], row['family_key'], row['item_hash'],
            row['work_hash'], row['family_work_hash']] for row in input_rows]), 'count': len(input_rows),
            'bytes': sum(row['bytes'] for row in input_rows)}
        for row in input_rows:
            item, work, group = row['item'], row['work'] or {}, row['family_work'] or {}
            family.setdefault((row['campaign'], row['family_key']), {})[item['code']] = {
                'detail': item['detail'], 'revision': work.get('revision'), 'status': work.get('status'),
                'scope': work.get('scope'), 'family_guard': [group.get('revision') or 0,
                    group.get('status') or 'open', group.get('scope') or 'default']}
        link_ids = set()
        for job in job_rows:
            for card in ((job.get('receipt') or {}).get('cards') or {}).values():
                for namespace in ('biketrek', 'xtrek'):
                    leaf = (card.get('evidence') or {}).get(namespace)
                    if isinstance(leaf, dict) and type(leaf.get('link_id')) is int and leaf['link_id'] > 0:
                        link_ids.add(leaf['link_id'])
        mapping_rows, size = await _rows(db, 'shop_products', 't.id=ANY(CAST(:ids AS bigint[]))',
                                        {'ids': sorted(link_ids)}, MAX_EXTRA_ROWS)
        extras['atomer_mapping_claims'] = {'hash': s.digest([row['hash'] for row in mapping_rows]), 'bytes': size}
        shops = dict((await db.execute(text('SELECT id,code FROM shops'))).all())
        claims = {row['document']['id']: {**row['document'], 'shop_code': shops.get(row['document']['shop_id'])}
                  for row in mapping_rows}
    return drafts, catalog, family, campaigns, claims


async def _resolve(db, proofs, extras):
    suppliers = dict((await db.execute(text('SELECT code,id FROM suppliers ORDER BY id'))).all())
    extras['supplier_namespaces'] = s.digest(suppliers)
    from inventory_hub.services.product_identity import RemoteIdentity, load_identity_index
    from inventory_hub.services.sku_policy import preview_sku
    from inventory_hub.sku_policy_types import SkuIdentityInput
    unique = {}
    for proof in proofs:
        for query in proof.queries:
            identity = {key: value for key, value in query.items() if key != 'path'}
            unique.setdefault(s.digest(identity), identity)
    if len(unique) > MAX_IDENTITY_QUERIES:
        raise s.Unverified('import_identity_query_limit')
    identities = {key: RemoteIdentity(0, code=value['code'], supplier_id=suppliers.get(value['supplier']),
        supplier_sku=value['supplier_sku'], barcodes=tuple(value['barcodes']), expected_product_id=value['expected'])
        for key, value in unique.items()}
    index = await load_identity_index(db, 0, list(identities.values()), local=True) if identities else None
    resolved = {}
    for key, value in unique.items():
        if value['supplier'] not in suppliers:
            resolved[key] = {'unverified': 'import_supplier_namespace_missing'}
            continue
        result = index.resolve(identities[key])
        owners = set(result.candidate_product_ids)
        if result.product_id:
            owners.add(result.product_id)
        record = {'status': result.status, 'owners': sorted(owners), 'matched_by': result.matched_by,
                  'reasons': sorted(result.reasons)}
        item_identity = value['sku_identity']
        # Replay prepare_sku_rows' read-only manufacturer proof selection. An
        # unsaved draft need not yet carry the auto-derived values.sku_identity.
        if (result.status != 'conflict' and not result.product_id and not value['expected']
                and value['refresh_sku_from_feed']):
            item_identity = value['mapped_sku_identity']
        if item_identity:
            item = SkuIdentityInput.model_validate(item_identity)
            sku = await preview_sku(db, item, sku=value['code'], barcodes=value['barcodes'],
                supplier_id=suppliers[value['supplier']], supplier_sku=value['supplier_sku'],
                expected_product_id=value['expected'], item_type=value['item_type'])
            record['sku_proof'] = sku
            owners.update(sku.get('candidate_product_ids', []))
            if sku.get('product_id'):
                owners.add(sku['product_id'])
            record['owners'] = sorted(owners)
        resolved[key] = record
    for proof in proofs:
        for pin in proof.supplier_pins:
            if suppliers.get(pin['supplier']) != pin['id']:
                proof.errors.append({'code': 'draft_supplier_namespace_changed', 'path': pin['path']})
        for query in proof.queries:
            key = s.digest({name: value for name, value in query.items() if name != 'path'})
            result = resolved[key]
            proof.resolutions.append({'path': query['path'], **result})
            if result.get('unverified'):
                proof.errors.append({'code': result['unverified'], 'path': query['path']})
            for owner in set(result.get('owners', [])) & proof.scope.hubs:
                proof.hit(query['path'], 'resolved_import_hub_owner', owner)


async def inspect(db, *, scope, columns, open_condition, request_id):
    facts, blockers, known, entries, extras, total = {}, [], {}, [], {}, 0
    try:
        for table in sorted(TABLES):
            fields = columns.get(table)
            if fields is None or {key for key, kind in fields.items() if kind == 'jsonb'} != JSON_COLUMNS[table]:
                raise s.Unverified('known_table_schema_changed', table)
            condition = open_condition(table, fields)
            # Saved/complete drafts can be reopened and referenced by older AI
            # jobs. Parse every draft, not a status-based assumption of no intent.
            if table == 'product_import_drafts':
                condition = 'true'
            if table == 'product_consolidations':
                condition += ' AND t.id<>:request_id'
            rows, size = await _rows(db, table, condition, {'request_id': str(request_id)})
            total += size
            if total > MAX_TOTAL_BYTES:
                raise s.Unverified('database_total_document_limit')
            known[table] = rows
            facts[table] = {'count': len(rows), 'bytes': size, 'hash': s.digest([row['hash'] for row in rows])}
        drafts, catalog, families, campaigns, claims = await _linked_inputs(db, known, extras)
        if total + sum(value.get('bytes', 0) for value in extras.values() if isinstance(value, dict)) > MAX_TOTAL_BYTES:
            raise s.Unverified('database_total_document_limit')
        for table in sorted(known):
            for row in known[table]:
                raw = row['document']
                proof = s.Proof(scope)
                _catch(proof, lambda p, value: s.bounded(value), raw)
                if table == 'product_import_drafts':
                    _catch(proof, s.draft, raw)
                elif table == 'ai_content_jobs':
                    _catch(proof, ai_scope.inspect, raw, drafts, catalog)
                elif table == 'atomer_catalog_jobs':
                    _catch(proof, atomer_scope.inspect, raw, families.get((raw['campaign'], raw['family_key'])),
                           campaigns.get(raw['campaign']), claims)
                else:
                    _catch(proof, s.consolidation, raw)
                entries.append({'table': table, 'row_identity': _identity(table, raw), 'row_hash': row['hash'], 'proof': proof})
        shop_codes = list(await db.scalars(text("SELECT code FROM shops WHERE platform='upgates' ORDER BY code")))
        journal_rows, manifests = journals.observe(shop_codes)
        extras['journal_directories'] = manifests
        journal_entries = []
        for (namespace, identifier), record in journal_rows.items():
            proof, settled = s.Proof(scope), False
            try:
                settled = journals.parse(proof, record['document'], namespace, identifier, drafts)
            except s.Unverified as error:
                proof.errors.append({'code': error.reason, 'path': error.path})
            except (KeyError, TypeError, ValueError, AttributeError):
                proof.errors.append({'code': 'journal_writer_shape_unverified', 'path': ''})
            journal_entries.append({'shop_code': namespace, 'journal_id': identifier, 'hash': record['hash'],
                'file_hashes': record['file_hashes'], 'bytes': record['bytes'], 'settled': settled, 'proof': proof})
        proofs = [entry['proof'] for entry in entries + journal_entries]
        for proof in proofs:
            for namespace, identifier in proof.publications:
                if (namespace, identifier) not in journal_rows:
                    proof.errors.append({'code': 'linked_publication_journal_missing', 'path': '/publications'})
        await _resolve(db, proofs, extras)
        # DB reads above await. Re-read every base/sidecar and directory before
        # returning; final apply calls this again while holding all input fences.
        again, repeated_manifests = journals.observe(shop_codes)
        comparison = lambda values: {key: {name: value[name] for name in ('hash', 'file_hashes', 'bytes')} for key, value in values.items()}
        if comparison(again) != comparison(journal_rows) or repeated_manifests != manifests:
            raise s.Unverified('journal_changed_during_database_proof')
        records = []
        for entry in entries:
            proof = entry.pop('proof').result()
            records.append({**entry, **proof})
        for table in sorted(TABLES):
            count = sum(record['table'] == table and record['classification'] in ('unknown', 'reference') for record in records)
            if count:
                blockers.append({'code': 'consolidation_pending_document', 'table': table, 'count': count})
        journal_records = []
        for entry in journal_entries:
            proof = entry.pop('proof').result()
            record = {**entry, **proof}
            journal_records.append(record)
            if proof['classification'] == 'unknown' or proof['classification'] == 'reference' and not entry['settled']:
                blockers.append({'code': 'consolidation_catalog_journal_pending', 'shop_code': entry['shop_code'],
                                 'journal_id': entry['journal_id']})
        return {'parser_version': s.VERSION, 'tables': facts, 'inputs': extras, 'records': records,
                'catalog_journals': journal_records, 'blockers': blockers}
    except (s.Unverified, OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        return {'parser_version': s.VERSION, 'tables': facts, 'inputs': extras, 'records': [], 'catalog_journals': [],
                'blockers': [{'code': 'consolidation_dependency_scope_unverified',
                    'reason': getattr(error, 'reason', 'scope_input_unreadable_or_malformed'),
                    'path': getattr(error, 'path', '')}]}
