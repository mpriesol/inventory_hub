"""Exhaustive paged catalog scope with bounded payloads and complete manifests."""
import asyncio
from collections import Counter

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from inventory_hub.services import product_dependency_scope as s
from inventory_hub.services import product_dependency_ai_scope as ai_scope
from inventory_hub.services import product_dependency_atomer_scope as atomer_scope
from inventory_hub.services import product_dependency_journals as journals
from inventory_hub.services import product_dependency_inventory as inventory
from inventory_hub.services import product_dependency_links as linked
from inventory_hub.services import product_links as links


TABLES = {'product_import_drafts', 'ai_content_jobs', 'atomer_catalog_jobs', 'product_consolidations'}
JSON_COLUMNS = {'product_import_drafts': {'document'},
    'ai_content_jobs': {'context', 'output', 'checks', 'events', 'usage'},
    'atomer_catalog_jobs': {'guards', 'document', 'receipt'},
    'product_consolidations': {'document', 'result'}}
PAGE_ROWS = 64
PAGE_BYTES = 4000000
MAX_ROW_BYTES = 16000000
MAX_TOTAL_BYTES = 64000000  # Aggregate retained serialized payload, not an RSS claim.
MAX_WORK_BYTES = 256000000  # Admitted DB body work, including repeated linked rows.
MAX_SCAN_SECONDS = 120
MAX_EXTRA_ROWS = 20000  # One indivisible linked operation, not the cumulative family inventory.
MAX_IDENTITY_QUERIES = 20000
MAX_RECORDS_PER_TABLE = 25
MAX_SAMPLE_BYTES = 1000000
MAX_JOURNAL_SAMPLES = 100
MAX_REASON_SAMPLES = 100
LOCK_TABLES = sorted(TABLES | {'atomer_catalog_campaigns', 'atomer_progress_items', 'atomer_review_work',
    'suppliers', 'supplier_products', 'supplier_feeds', 'shops', 'sku_brands', 'sku_brand_aliases'})


async def lock_inputs(db):
    try:
        await db.execute(text('LOCK TABLE ' + ','.join(LOCK_TABLES) + ' IN SHARE MODE NOWAIT'))
    except DBAPIError as error:
        if getattr(error.orig, 'sqlstate', None) == '55P03':
            raise links.LinkError('consolidation_dependency_writer_busy') from None
        raise


def _identity(table, raw):
    return ({'campaign': raw['campaign'], 'family_key': raw['family_key']} if table == 'atomer_catalog_jobs'
            else {'id': raw['id']})


def _catch(proof, function, *args):
    try:
        function(proof, *args)
    except s.Unverified as error:
        proof.errors.append({'code': error.reason, 'path': error.path})
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        proof.errors.append({'code': 'writer_shape_unverified', 'path': ''})


class InventoryTimeout(links.LinkError):
    def __init__(self, report):
        super().__init__('consolidation_dependency_inventory_timeout')
        self.known_unapplied = True
        self.report = report


def _reasons(record):
    reasons = {problem['code'] for problem in record['problems']}
    if record['classification'] == 'reference':
        reasons.update('reference:' + key for key in record['reference_reason_counts'])
    return sorted(reasons)


class Samples:
    def __init__(self):
        self.entries, self.counts, self.size = [], Counter(), 0
        self.reason_keys = set()
        self.reason_records = 0
        self.truncated = False

    def add(self, kind, record, limit):
        size = len(inventory.encoded(record))
        lifecycle_kind = record.get('lifecycle', {}).get('kind', '')
        reasons = {(kind, record['classification'], lifecycle_kind, reason) for reason in _reasons(record)}
        representative = bool(reasons - self.reason_keys) and self.reason_records < MAX_REASON_SAMPLES
        if representative and size <= MAX_SAMPLE_BYTES:
            # Keep bounded examples of a late reason even when early disjoint
            # records consumed the projection allowance. Coverage is separate.
            for i in range(len(self.entries) - 1, -1, -1):
                if self.size + size <= MAX_SAMPLE_BYTES:
                    break
                old_kind, _, old_size, old_reason = self.entries[i]
                if not old_reason:
                    self.entries.pop(i)
                    self.size -= old_size
                    self.counts[old_kind] -= 1
                    self.truncated = True
        if (not representative and self.counts[kind] >= limit) or self.size + size > MAX_SAMPLE_BYTES:
            self.truncated = True
            return
        self.entries.append((kind, record, size, representative))
        self.counts[kind] += 1
        self.size += size
        if representative:
            self.reason_keys.update(reasons)
            self.reason_records += 1

    def records(self, *, journals=False):
        return [record for kind, record, _, _ in self.entries if kind.startswith('journal:') == journals]


def _lifecycle(table, raw):
    if table != 'ai_content_jobs':
        return {}
    from inventory_hub.services.product_dependency_census import AI_STATUSES
    context = raw.get('context') if isinstance(raw.get('context'), dict) else {}
    recognized = lambda value, allowed: value if isinstance(value, str) and value in allowed else 'other'
    return {'kind': recognized(raw.get('kind'), {'product', 'rules', 'supplier_lookup'}),
        'source_kind': recognized(context.get('source_kind', 'catalog'), {'catalog', 'shop', 'import_draft'}),
        'status': recognized(raw.get('status'), AI_STATUSES),
        'has_staging': bool(context.get('staging_id')),
        'has_preview': context.get('update_preview') is not None,
        'has_update': context.get('update_only') is True}


async def _namespaces(db):
    # Namespace projections are bounded metadata, never full document bodies.
    suppliers = (await db.execute(text('SELECT code,id FROM suppliers ORDER BY id LIMIT :limit'),
        {'limit': MAX_EXTRA_ROWS+1})).all()
    shops = (await db.execute(text('SELECT id,code,platform FROM shops ORDER BY id LIMIT :limit'),
        {'limit': MAX_EXTRA_ROWS+1})).all()
    if len(suppliers) > MAX_EXTRA_ROWS or len(shops) > MAX_EXTRA_ROWS:
        raise s.Unverified('identity_namespace_limit')
    return dict(suppliers), [row.code for row in shops if row.platform == 'upgates'], {
        'suppliers': s.digest([list(row) for row in suppliers]),
        'shops': s.digest([list(row) for row in shops])}


async def _resolve(db, proofs, extras, *, suppliers, budget):
    extras['supplier_namespaces'] = s.digest(suppliers)
    from inventory_hub.services.product_identity import RemoteIdentity, load_identity_index
    from inventory_hub.services.sku_policy import preview_sku
    from inventory_hub.sku_policy_types import SkuIdentityInput
    unique = {}
    for proof in proofs:
        for query in proof.queries:
            identity = {key: value for key, value in query.items() if key != 'path'}
            unique.setdefault(s.digest(identity), identity)
    budget.query_keys.update(unique)
    if len(budget.query_keys) > MAX_IDENTITY_QUERIES:
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


async def _record(db, table, row, *, scope, budget, suppliers, available_journals):
    raw, proof = row['document'], s.Proof(scope)
    closure = inventory.Closure(db, budget=budget, page_rows=PAGE_ROWS, page_bytes=PAGE_BYTES,
        retained=row['bytes'], max_linked_rows=MAX_EXTRA_ROWS)
    _catch(proof, lambda p, value: s.bounded(value), raw)
    if table == 'product_import_drafts':
        _catch(proof, s.draft, raw)
    elif table == 'ai_content_jobs':
        drafts, products = await linked.ai(closure, raw)
        _catch(proof, ai_scope.inspect, raw, drafts, products)
    elif table == 'atomer_catalog_jobs':
        family, campaign, claims = await linked.atomer(closure, raw)
        _catch(proof, atomer_scope.inspect, raw, family, campaign, claims)
    else:
        _catch(proof, s.consolidation, raw)
    await _resolve(db, [proof], {}, suppliers=suppliers, budget=budget)
    for reference in sorted(proof.publications):
        if reference not in available_journals:
            proof.errors.append({'code': 'linked_publication_journal_missing', 'path': '/publications'})
    budget.check_time()
    edge = closure.result()
    record = {'table': table, 'row_identity': _identity(table, raw), 'row_hash': row['hash'],
              'linked_input_manifest': edge, **proof.result()}
    if table == 'ai_content_jobs':
        record['lifecycle'] = _lifecycle(table, raw)
    return record, edge


async def _journal(db, namespace, identifier, record, *, scope, budget, suppliers, available_journals):
    proof, settled = s.Proof(scope), False
    closure = inventory.Closure(db, budget=budget, page_rows=PAGE_ROWS, page_bytes=PAGE_BYTES,
        retained=record['bytes'], max_linked_rows=MAX_EXTRA_ROWS)
    if record['bytes'] > MAX_TOTAL_BYTES:
        raise s.Unverified('database_payload_limit', 'catalog_journal')
    drafts = {}
    staging = record['document'].get('staging')
    if isinstance(staging, dict) and isinstance(staging.get('draft_id'), str):
        drafts = await linked.drafts(closure, {staging['draft_id']})
    try:
        settled = journals.parse(proof, record['document'], namespace, identifier, drafts)
    except s.Unverified as error:
        proof.errors.append({'code': error.reason, 'path': error.path})
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        proof.errors.append({'code': 'journal_writer_shape_unverified', 'path': ''})
    await _resolve(db, [proof], {}, suppliers=suppliers, budget=budget)
    for reference in sorted(proof.publications):
        if reference not in available_journals:
            proof.errors.append({'code': 'linked_publication_journal_missing', 'path': '/publications'})
    budget.check_time()
    edge = closure.result()
    return {'shop_code': namespace, 'journal_id': identifier, 'hash': record['hash'],
        'file_hashes': record['file_hashes'], 'bytes': record['bytes'], 'settled': settled,
        'linked_input_manifest': edge, **proof.result()}, edge


async def inspect(db, *, scope, columns, open_condition, request_id):
    budget = inventory.Budget(MAX_ROW_BYTES, MAX_TOTAL_BYTES, MAX_WORK_BYTES, MAX_SCAN_SECONDS)
    checkpoint = {}
    try:
        # Cancellation also interrupts a long awaited SQL operation. This
        # boundary contains reads only, never preview persistence or commit.
        async with asyncio.timeout(MAX_SCAN_SECONDS):
            return await _inspect(db, scope=scope, columns=columns, open_condition=open_condition,
                request_id=request_id, budget=budget, checkpoint=checkpoint)
    except TimeoutError:
        await db.rollback()
        report = checkpoint['report']
        report['complete'] = False
        report['blockers'].append({'code': 'consolidation_dependency_scope_unverified',
            'reason': 'database_inventory_time_limit', 'path': ''})
        raise InventoryTimeout(report) from None


async def _inspect(db, *, scope, columns, open_condition, request_id, budget, checkpoint):
    facts, inputs, blockers, conditions = {}, {}, [], {}
    samples = Samples()
    counts = {table: Counter() for table in sorted(TABLES)}
    reasons = {table: {} for table in sorted(TABLES)}
    def count_reasons(table, record):
        group = reasons.setdefault(table, {}).setdefault(record['classification'], Counter())
        group.update(_reasons(record))
    proof_root, closure_root = inventory.Manifest('proofs'), inventory.Manifest('closures')
    closure_edges = closure_bytes = journal_count = 0
    journal_counts, journal_blocked = Counter(), Counter()
    file_bytes = [0, 0]
    def result(complete):
        return {'parser_version': s.VERSION, 'complete': complete,
            'tables': facts, 'inputs': inputs, 'records': samples.records(),
            'record_count': sum(sum(values.values()) for values in counts.values()),
            'classification_counts': {table: dict(sorted(values.items())) for table, values in counts.items()},
            'reason_counts': {table: {kind: dict(sorted(values.items())) for kind, values in sorted(groups.items())}
                              for table, groups in sorted(reasons.items())},
            'record_projection_complete': not samples.truncated,
            'catalog_journals': samples.records(journals=True), 'journal_count': journal_count,
            'journal_classification_counts': dict(journal_counts),
            'journal_projection_complete': not samples.truncated,
            'proof_manifest': proof_root.result()['hash'],
            'linked_manifest': {'hash': closure_root.result()['hash'], 'edges': closure_edges, 'bytes': closure_bytes},
            'work': {'charged_rows': budget.rows, 'charged_bytes': budget.bytes,
                'limit_bytes': MAX_WORK_BYTES, 'row_limit_bytes': MAX_ROW_BYTES,
                'retained_payload_limit_bytes': MAX_TOTAL_BYTES, 'deadline_seconds': MAX_SCAN_SECONDS,
                'peak_retained_payload_bytes': budget.peak_payload,
                'distinct_identity_queries': len(budget.query_keys),
                'journal_observation_bytes': file_bytes, 'journal_limit_per_pass_bytes': journals.MAX_TOTAL_BYTES},
            'blockers': blockers}
    try:
        for table in sorted(TABLES):
            fields = columns.get(table)
            if fields is None or {key for key, kind in fields.items() if kind == 'jsonb'} != JSON_COLUMNS[table]:
                raise s.Unverified('known_table_schema_changed', table)
            condition = 'true' if table == 'product_import_drafts' else open_condition(table, fields)
            if table == 'product_consolidations':
                condition += ' AND t.id<>:request_id'
            conditions[table] = condition
            stats = await inventory.statistics(db, table, condition, {'request_id': str(request_id)})
            facts[table] = {**stats, 'complete': False}
        if sum(value['bytes'] for value in facts.values()) > MAX_WORK_BYTES:
            raise s.Unverified('database_total_document_limit')
        suppliers, shop_codes, inputs['namespaces'] = await _namespaces(db)
        expected_directories, available_journals = journals.inventory(shop_codes)
        inputs['journal_directories'] = expected_directories
        for table in sorted(TABLES):
            root = inventory.Manifest(['table', table])
            async for row in inventory.documents(db, table, conditions[table], {'request_id': str(request_id)},
                    budget=budget, page_rows=PAGE_ROWS, manifest=root, expected=facts[table]):
                record, edge = await _record(db, table, row, scope=scope, budget=budget,
                    suppliers=suppliers, available_journals=available_journals)
                counts[table][record['classification']] += 1
                count_reasons(table, record)
                proof_root.add_frame([table, row['key'], record])
                closure_root.add_frame([table, row['key'], edge])
                closure_edges += edge['edges']; closure_bytes += edge['bytes']
                samples.add(table, record, MAX_RECORDS_PER_TABLE)
            facts[table].update(root.result(), complete=True)
            unsafe = counts[table]['unknown'] + counts[table]['reference']
            if unsafe:
                blockers.append({'code': 'consolidation_pending_document', 'table': table, 'count': unsafe})
        # Base/linked bodies above have operation-local lifetime. File sets are
        # likewise parsed one at a time, never retained as a global body map.
        row = None
        first_files = inventory.Manifest('journal-files')
        observed_directories = {}
        for kind, key, raw in journals.iter_observations(shop_codes):
            budget.check_time()
            if kind == 'directory':
                observed_directories[key] = raw
                continue
            namespace, identifier = key
            file_observation = {name: raw[name] for name in ('hash', 'file_hashes', 'bytes')}
            first_files.add_frame([key, file_observation])
            file_bytes[0] += raw['bytes']
            record, edge = await _journal(db, namespace, identifier, raw, scope=scope, budget=budget,
                suppliers=suppliers, available_journals=available_journals)
            journal_count += 1
            journal_counts[record['classification']] += 1
            count_reasons('catalog_import_journals', record)
            proof_root.add_frame(['journal', key, record])
            closure_root.add_frame(['journal', key, edge])
            closure_edges += edge['edges']; closure_bytes += edge['bytes']
            samples.add('journal:' + namespace, record, MAX_JOURNAL_SAMPLES)
            if record['classification'] == 'unknown' or record['classification'] == 'reference' and not record['settled']:
                journal_blocked[namespace] += 1
        raw = None
        repeated_files, repeated_directories = inventory.Manifest('journal-files'), {}
        for kind, key, raw in journals.iter_observations(shop_codes):
            budget.check_time()
            if kind == 'directory':
                repeated_directories[key] = raw
            else:
                file_bytes[1] += raw['bytes']
                repeated_files.add_frame([key, {name: raw[name] for name in ('hash', 'file_hashes', 'bytes')}])
        if (first_files.result() != repeated_files.result() or observed_directories != expected_directories
                or repeated_directories != expected_directories):
            raise s.Unverified('journal_changed_during_database_proof')
        inputs['journal_files'] = first_files.result()['hash']
        for namespace, count in sorted(journal_blocked.items()):
            blockers.append({'code': 'consolidation_catalog_journal_pending', 'shop_code': namespace, 'count': count})
        budget.check_time()
        return result(True)
    except TimeoutError:
        # Built-in TimeoutError is also an OSError. Elapsed synchronous checks
        # must reach the same rollback boundary as cancelled awaited SQL.
        raise
    except (s.Unverified, OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        blockers.append({'code': 'consolidation_dependency_scope_unverified',
            'reason': getattr(error, 'reason', 'scope_input_unreadable_or_malformed'),
            'path': getattr(error, 'path', '')})
        return result(False)
    finally:
        # Plain bounded values survive a SQL cancellation/rollback; callers may
        # report incomplete progress but must never persist it as a preview.
        checkpoint['report'] = result(False)
