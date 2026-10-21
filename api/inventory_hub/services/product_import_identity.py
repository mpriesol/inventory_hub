"""Review possible existing Hub cards before a draft creates or links identities.

Names only suggest candidates. Decisions are bound to the current source and
candidate evidence and are checked again inside the common identity write lock.
"""
from collections import Counter
from copy import deepcopy

from sqlalchemy import select

from inventory_hub.db_models import IdentifierType, ProductIdentifier, Supplier
from inventory_hub.services.catalog import CatalogError
from inventory_hub.services.product_candidates import CandidateQuery, find_candidates, _load_cards
from inventory_hub.services.product_identity import RemoteIdentity, load_identity_index, verified_barcodes, barcode_keys
from inventory_hub.services.product_links import axes, digest, LinkError


def source_id(row, document):
    return row.get('catalog_product_id') if document.get('receiving_origin') else row['id']


def _source(row):
    values = row['values']
    return {'sku': values['code'], 'supplier_sku': values['supplier_code'], 'name': values['name'],
            'eans': list(values['eans']), 'attributes': deepcopy(values['variant_attributes']), 'brand': values['brand'],
            'sku_identity': deepcopy(values.get('sku_identity'))}


def _identity(source, supplier_id, expected=None):
    return RemoteIdentity(0, source['sku'], barcodes=tuple(source['eans']), supplier_id=supplier_id,
                          supplier_sku=source['supplier_sku'], expected_product_id=expected)


def _candidate_errors(index, source, supplier_id, candidate):
    resolution = index.resolve(_identity(source, supplier_id, candidate['product_id']))
    errors = list(resolution.reasons) if resolution.status == 'conflict' else []
    try:
        incoming, current = axes(source['attributes']), axes(candidate['attributes'])
        if any(incoming[key] != current[key] for key in incoming.keys() & current.keys()):
            errors.append('variant_axes_conflict')
    except LinkError:
        errors.append('variant_axes_ambiguous')
    manual = verified_barcodes(candidate.get('manual_eans'))
    incoming_barcodes = verified_barcodes(source['eans'])
    if manual and incoming_barcodes and not barcode_keys(manual) & barcode_keys(incoming_barcodes):
        errors.append('manual_barcode_conflict')
    return errors


async def canonical_for_review(db, products):
    """Only draft preparation defers conflicts to its explicit review screen."""
    from inventory_hub.services import catalog_import
    try:
        return await catalog_import.canonical_products(db, products)
    except CatalogError as error:
        if error.code not in ('local_identity_conflict', 'local_group_conflict'):
            raise
        return products


async def build(db, draft, *, summary_only=False, document=None):
    supplier = await db.scalar(select(Supplier).where(Supplier.code == draft.supplier))
    if supplier is None:
        raise CatalogError('supplier_not_found', 'Supplier was not found', 404)
    current = document if document is not None else draft.document
    raw_rows = current['rows']
    sources = {row['id']: _source(row) for row in raw_rows}
    queries = [CandidateQuery(str(row['id']), sku=sources[row['id']]['sku'],
        eans=tuple(sources[row['id']]['eans']), supplier_id=supplier.id,
        supplier_sku=sources[row['id']]['supplier_sku'], name=' '.join(sources[row['id']]['name'].split()),
        brand=' '.join(sources[row['id']]['brand'].split()), attributes=tuple((a['name'], a['value']) for a in sources[row['id']]['attributes']))
        for row in raw_rows if not row.get('hub_product_id') and not row.get('expected_hub_product_id')]
    found = {}
    for start in range(0, len(queries), 100):
        found.update(await find_candidates(db, queries[start:start + 100], limit=5))
    identities = [_identity(sources[row['id']], supplier.id,
        row.get('hub_product_id') or row.get('expected_hub_product_id')) for row in raw_rows]
    for row in raw_rows:
        identities.extend(_identity(sources[row['id']], supplier.id, candidate['product_id'])
            for candidate in found.get(str(row['id']), {}).get('candidates', []))
    index = await load_identity_index(db, 0, identities, local=True)
    # Resolver targets need full current facts even when absent from a bounded
    # name suggestion list, or when a saved/receipt row skipped that search.
    matched_ids = {index.resolve(value).product_id for value in identities[:len(raw_rows)]}
    matched_cards = await _load_cards(db, matched_ids - {None})
    rows, internal = [], {}
    for raw, identity in zip(raw_rows, identities[:len(raw_rows)]):
        source = sources[raw['id']]
        resolution = index.resolve(identity)
        matched = resolution.product_id
        errors = list(resolution.reasons) if resolution.status == 'conflict' else []
        expected = raw.get('hub_product_id') or raw.get('expected_hub_product_id')
        candidates = []
        search = found.get(str(raw['id']), {'candidates': [], 'truncated': False})
        suggested = list(search['candidates'])
        if matched in matched_cards and not any(item['product_id'] == matched for item in suggested):
            suggested.append({**matched_cards[matched], 'reasons': [resolution.matched_by], 'warnings': []})
        for item in suggested:
            candidate = deepcopy(item)
            failures = _candidate_errors(index, source, supplier.id, candidate)
            if source['brand'] and candidate.get('brand'):
                from inventory_hub.services.sku_policy import registered_brand_id, brand_key
                if brand_key(source['brand']) != brand_key(candidate['brand']):
                    left = await registered_brand_id(db, source['brand'])
                    right = await registered_brand_id(db, candidate['brand'])
                    if left is None or left != right:
                        failures.append('sku_brand_conflict')
            candidate['errors'] = sorted(set(failures))
            candidate['can_link'] = not candidate['errors']
            candidates.append(candidate)
        known = bool(matched and (index.by_supplier_sku.get((supplier.id, source['supplier_sku'])) == {matched}
            or any(c['product_id'] == matched and 'supplier_sku' in c['reasons'] for c in candidates)))
        # A corroborated exact barcode/approved MPN can reuse a card without a
        # name-choice dialog. Contradictory variant evidence remains a conflict.
        matched_candidate = next((c for c in candidates if c['product_id'] == matched), None)
        if matched_candidate and matched_candidate['errors']:
            errors.extend(matched_candidate['errors'])
        automatic = bool(matched and not errors and matched_candidate
            and barcode_keys(source['eans']) & barcode_keys(matched_candidate.get('eans', [])))
        policy_match = (raw.get('sku_preview') or {}).get('status') == 'existing' and bool(source['sku_identity'])
        automatic = automatic or bool(policy_match and matched and not errors)
        status = 'conflict' if errors else 'saved' if raw.get('hub_product_id') else 'linked' if expected or known \
            or automatic else 'review' if candidates or matched or search['truncated'] else 'new'
        exact_candidate = any(set(candidate['reasons']) & {'exact_ean', 'exact_sku', 'supplier_sku'} for candidate in candidates)
        row = {'row_id': raw['id'], 'source': source, 'status': status, 'matched_product_id': matched,
            'can_create': not errors and matched is None and not expected and not exact_candidate,
            'candidates': candidates, 'truncated': search['truncated'], 'decision': None, 'errors': sorted(set(errors))}
        state_hash = digest({'row': row, 'source_id': source_id(raw, current)})
        decision = raw.get('identity_decision')
        if decision and (decision.get('state_hash') == state_hash or
                         (summary_only and decision.get('source_hash') == digest(source) and not errors)):
            row['decision'] = {key: decision[key] for key in ('action', 'product_id') if key in decision}
        elif decision and status != 'saved':
            row['status'] = 'review' if not errors else 'conflict'
            row['errors'].append('import_identity_review_changed')
        internal[raw['id']] = {'state_hash': state_hash}
        rows.append(row)
    # Separate selected source leaves may not silently become one Hub card.
    targets = {row['row_id']: (row['decision'] or {}).get('product_id') or row['matched_product_id'] for row in rows}
    resolved = Counter(pid for pid in targets.values() if pid)
    for row in rows:
        if targets[row['row_id']] and resolved[targets[row['row_id']]] > 1:
            row['status'], row['can_create'] = 'conflict', False
            row['errors'].append('selection_identity_conflict')
            row['decision'] = None
    review = {'draft_id': draft.id, 'revision': draft.revision,
              'review_hash': digest({'draft_id': draft.id, 'revision': draft.revision, 'rows': rows,
                  'states': {str(key): value['state_hash'] for key, value in internal.items()}}), 'rows': rows}
    return review, internal


def required(row):
    return row['status'] == 'conflict' or (row['status'] == 'review' and row['decision'] is None)


async def summary(db, draft):
    if all(row.get('hub_product_id') for row in draft.document['rows']):
        return {'identity_review_required': False, 'identity_review_count': 0}
    review, _ = await build(db, draft, summary_only=True)
    count = sum(required(row) for row in review['rows'])
    return {'identity_review_required': bool(count), 'identity_review_count': count}


async def review(db, draft, request):
    from inventory_hub.services import product_import as imports
    imports.expect(draft, request.expected_revision)
    imports.assert_editable(draft)
    return (await build(db, draft))[0]


async def decisions(db, draft, request):
    from inventory_hub.services import product_import as imports
    imports.expect(draft, request.expected_revision)
    imports.assert_editable(draft)
    review, state = await build(db, draft)
    if review['review_hash'] != request.review_hash:
        raise CatalogError('import_identity_review_changed', 'Product identity changed; review the current candidates', 409)
    by_id = {row['row_id']: row for row in review['rows']}
    document = deepcopy(draft.document)
    raw_rows = {row['id']: row for row in document['rows']}
    for choice in request.decisions:
        row = by_id.get(choice.row_id)
        if row is None or row['status'] in ('saved', 'linked', 'conflict'):
            raise CatalogError('import_identity_decision_invalid', 'Choose an eligible unsaved product', 422)
        if choice.action == 'create':
            if not row['can_create']:
                raise CatalogError('import_identity_decision_invalid', 'An identified product cannot be duplicated', 422)
        elif not any(c['product_id'] == choice.product_id and c['can_link'] for c in row['candidates']):
            raise CatalogError('import_identity_decision_invalid', 'Choose a verified candidate from this review', 422)
        frozen = {**choice.model_dump(exclude_none=True, exclude={'row_id'}),
                  'state_hash': state[choice.row_id]['state_hash'], 'source_hash': digest(row['source'])}
        catalog_id = source_id(raw_rows[choice.row_id], document)
        if choice.action == 'link' and catalog_id is not None:
            inspected = await _inspect_choice(db, draft, row['source'], choice.product_id, catalog_id)
            frozen['link_hash'] = inspected['preview_hash']
        raw_rows[choice.row_id]['identity_decision'] = frozen
    chosen = [(r.get('identity_decision') or {}).get('product_id') or by_id[r['id']]['matched_product_id']
              for r in document['rows']]
    chosen = [pid for pid in chosen if pid]
    if len(chosen) != len(set(chosen)):
        raise CatalogError('import_identity_decision_invalid', 'Two source rows cannot select the same Hub product', 422)
    if document.get('automation', {}).get('version') == 1:
        document['automation'] = {**document['automation'], 'paused': True, 'pause_reason': 'identity_review'}
    imports.changed(draft, document, 'Existing product identities explicitly reviewed')
    await db.flush()
    return await imports.public(db, draft)


async def _inspect_choice(db, draft, source, product_id, catalog_id):
    from inventory_hub.services.supplier_card_links import inspect_link, SupplierCardLinkError
    try:
        inspected = await inspect_link(db, product_id, catalog_id)
    except SupplierCardLinkError as error:
        raise CatalogError(error.code, 'Review this supplier identity before linking', error.status) from None
    actual = inspected['source']
    errors = list(inspected['errors'])
    if actual['supplier_code'] != draft.supplier or actual['supplier_sku'] != source['supplier_sku']:
        errors.append('import_identity_source_changed')
    incoming_barcodes = verified_barcodes(source['eans'])
    if incoming_barcodes and actual['eans'] and not barcode_keys(incoming_barcodes) & barcode_keys(actual['eans']):
        errors.append('import_identity_source_changed')
    try:
        incoming, current = axes(source['attributes']), axes(actual['attributes'])
        if any(incoming[key] != current[key] for key in incoming.keys() & current.keys()):
            errors.append('import_identity_source_changed')
    except LinkError:
        errors.append('variant_axes_ambiguous')
    if errors:
        raise CatalogError(errors[0], 'The selected supplier identity cannot be linked; review its evidence', 409)
    return inspected


async def guard_and_link(db, draft, document):
    """Caller holds draft lock then IDENTITY_WRITE_LOCK; never commits here."""
    review, _ = await build(db, draft, document=document)
    if any(required(row) for row in review['rows']):
        raise CatalogError('import_identity_review_required', 'Review possible existing Hub products before saving', 409)
    by_id = {row['id']: row for row in document['rows']}
    linked = {}
    for row in review['rows']:
        raw = by_id[row['row_id']]
        choice = row['decision']
        if raw.get('hub_product_id') and raw.get('identity_link_only'):
            linked[raw['id']] = raw['hub_product_id']
            continue
        automatic = row['status'] == 'linked' and row['matched_product_id'] and not raw.get('hub_product_id') \
            and not raw.get('expected_hub_product_id')
        if not automatic and (not choice or choice['action'] != 'link'):
            continue
        pid = row['matched_product_id'] if automatic else choice['product_id']
        candidate = next((c for c in row['candidates'] if c['product_id'] == pid and c['can_link']), None)
        if candidate is None:
            raise CatalogError('import_identity_review_required', 'The selected existing product needs a new review', 409)
        catalog_id = source_id(raw, document)
        if catalog_id is not None:
            from inventory_hub.services.supplier_card_links import apply_link, SupplierCardLinkError
            try:
                expected_hash = (await _inspect_choice(db, draft, row['source'], pid, catalog_id))['preview_hash'] \
                    if automatic else raw['identity_decision']['link_hash']
                await apply_link(db, pid, catalog_id, expected_hash=expected_hash)
            except SupplierCardLinkError as error:
                raise CatalogError(error.code, 'Supplier identity changed; review this link again', error.status) from None
        else:
            # Invoice-only identities are aliases, not invented supplier offers.
            supplier_id = await db.scalar(select(Supplier.id).where(Supplier.code == draft.supplier))
            alias = await db.scalar(select(ProductIdentifier).where(ProductIdentifier.supplier_id == supplier_id,
                ProductIdentifier.identifier_type == IdentifierType.supplier_sku,
                ProductIdentifier.value == row['source']['supplier_sku']))
            if alias is not None and alias.product_id != pid:
                raise CatalogError('local_identity_conflict', 'Supplier code belongs to another product', 409)
            if alias is None:
                db.add(ProductIdentifier(product_id=pid, supplier_id=supplier_id,
                    identifier_type=IdentifierType.supplier_sku, value=row['source']['supplier_sku'], is_primary=False))
                await db.flush()
        if automatic:
            raw['warnings'] = sorted(set(raw.get('warnings', [])) | {'existing_card_reused'})
            continue
        raw['hub_product_id'], raw['created_in_draft'], raw['identity_link_only'] = pid, False, True
        raw['warnings'] = sorted(set(raw.get('warnings', [])) | {'existing_card_link_only'})
        linked[raw['id']] = pid
    return linked
