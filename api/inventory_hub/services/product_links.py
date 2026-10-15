"""Link existing Hub cards to verified existing shop leaves, without publication.

Network observations finish before the identity lock. Each apply rechecks the
frozen observations and local identity, then commits only the selected mappings
and its recovery result in one transaction. No canonical or stock writer is used.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import unicodedata

from sqlalchemy import or_, select, text

from inventory_hub.db_models import Product, ProductIdentifier, Shop, SupplierProduct
from inventory_hub.db_models_ext import ProductSupplySource, ProductVariantAttribute, ShopProduct
from inventory_hub.product_editor_models import ProductEditorOverride
from inventory_hub.product_link_models import ProductLinkPreview
from inventory_hub.services import product_link_source as source
from inventory_hub.services.product_identity import (
    IDENTITY_WRITE_LOCK, RemoteIdentity, load_identity_index, mapping_code, verified_barcodes,
)
from inventory_hub.services.upgates import variant_attributes
from inventory_hub.product_link_types import exact_code


MAX_REQUESTS = 8
PREVIEW_MINUTES = 30


class LinkError(Exception):
    def __init__(self, code, status=409, retry_after=None):
        self.code, self.status, self.retry_after = code, status, retry_after
        self.known_unapplied = False
        super().__init__(code)


def now():
    return datetime.now(timezone.utc)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def folded(value):
    return ' '.join(''.join(char for char in unicodedata.normalize('NFKD', str(value))
                            if not unicodedata.combining(char)).casefold().split())


AXIS_NAMES = {'farba': 'color', 'barva': 'color', 'colour': 'color', 'color': 'color',
              'velkost': 'size', 'velikost': 'size', 'size': 'size'}
COLORS = {'modra': 'blue', 'modry': 'blue', 'cierna': 'black', 'cerny': 'black', 'cerna': 'black',
          'cierny': 'black', 'biela': 'white', 'bily': 'white', 'bila': 'white',
          'cervena': 'red', 'cerveny': 'red', 'zelena': 'green', 'zeleny': 'green',
          'zlta': 'yellow', 'zluty': 'yellow', 'zluta': 'yellow', 'siva': 'grey', 'sedy': 'grey',
          'seda': 'grey', 'gray': 'grey', 'hneda': 'brown', 'hnedy': 'brown'}


def axes(values):
    """Compare only named leaf axes; unknown axes remain exact normalized text."""
    result = {}
    for item in values or []:
        if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not isinstance(item.get('value'), str):
            continue
        key = AXIS_NAMES.get(folded(item['name']), folded(item['name']))
        value = folded(item['value'])
        if not key or not value:
            continue
        if key == 'color':
            value = COLORS.get(value, value)
        if key in result and result[key] != value:
            raise LinkError('product_links_axes_ambiguous')
        result[key] = value
    return result


def remote_key(remote):
    return (remote['is_variant'], str(remote['external_id']))


def remote_identity(remote):
    return {key: remote[key] for key in ('code', 'parent_code', 'is_variant', 'external_id', 'parent_id')}


def remote_snapshot(remote):
    return {**remote_identity(remote), 'eans': sorted(verified_barcodes(remote.get('eans'))),
            'axes': axes(remote.get('attributes'))}


def _mapping(row):
    if row is None:
        return None
    return {'id': row.id, 'external_id': row.external_id, 'external_code': row.external_code,
            'variant_code': row.variant_code, 'parent_code': row.parent_code,
            'is_variant': row.is_variant, 'is_listed': row.is_listed}


async def _shop(db, shop_code):
    row = await db.scalar(select(Shop).where(Shop.code == shop_code)
                          .execution_options(populate_existing=True))
    if row is None or not row.is_active or row.platform != 'upgates':
        raise LinkError('product_links_shop_unavailable', 404)
    return row


async def _facts(db, product_ids, *, lock=False):
    query = select(Product).where(Product.id.in_(product_ids)).order_by(Product.id)
    if lock:
        query = query.with_for_update(read=True)
    products = list(await db.scalars(query.execution_options(populate_existing=True)))
    if len(products) != len(product_ids):
        raise LinkError('product_links_product_missing', 404)
    eans, attributes = defaultdict(list), defaultdict(list)
    for row in await db.scalars(select(ProductIdentifier).where(ProductIdentifier.product_id.in_(product_ids))):
        if getattr(row.identifier_type, 'value', row.identifier_type) in ('ean', 'upc'):
            eans[row.product_id].extend(verified_barcodes((row.value,)))
    for row in await db.scalars(select(ProductVariantAttribute).where(ProductVariantAttribute.product_id.in_(product_ids))
                               .order_by(ProductVariantAttribute.attribute_name)):
        attributes[row.product_id].append({'name': row.attribute_name, 'value': row.attribute_value})
    fallback = defaultdict(list)
    # Only the mapped physical leaf can supply fallback axes, never its parent.
    for row in (await db.execute(text("""
        SELECT sp.product_id, coalesce(leaf.data->'parameters_new', leaf.data->'parameters') AS attributes
        FROM shop_products sp JOIN products p ON p.id=sp.product_id
        JOIN shop_product_content c ON c.shop_id=sp.shop_id
          AND c.external_code=coalesce(nullif(sp.parent_code,''),sp.external_code)
        CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(c.data->'variants')='array'
          THEN c.data->'variants' ELSE '[]'::jsonb END) leaf(data)
        WHERE sp.product_id=ANY(CAST(:ids AS bigint[])) AND sp.is_variant AND sp.is_listed
          AND sp.variant_code=p.sku AND leaf.data->>'code'=sp.variant_code
        """), {'ids': list(product_ids)})).mappings():
        fallback[row['product_id']].extend(variant_attributes({'parameters': row['attributes']}))
    supplier_attributes = (await db.execute(select(Product.id, SupplierProduct.attributes['catalog']['variant_attributes'])
        .join(SupplierProduct, or_(SupplierProduct.id == Product.source_supplier_product_id,
            SupplierProduct.id.in_(select(ProductSupplySource.supplier_product_id)
                .where(ProductSupplySource.product_id == Product.id, ProductSupplySource.is_active.is_(True)))))
        .where(Product.id.in_(product_ids), SupplierProduct.is_active.is_(True)))).all()
    for pid, values in supplier_attributes:
        if isinstance(values, list):
            fallback[pid].extend(variant_attributes({'parameters': values}))
    # The same operator-corrected axes displayed by the editor take precedence.
    overrides = {row.product_id: row.data for row in await db.scalars(select(ProductEditorOverride)
        .where(ProductEditorOverride.product_id.in_(product_ids)).execution_options(populate_existing=True))}
    result = {}
    for product in products:
        override = overrides.get(product.id, {}).get('variant', {})
        attrs = override.get('attributes', attributes[product.id] or fallback[product.id])
        result[product.id] = {'product_id': product.id, 'sku': product.sku, 'name': product.name,
            'is_active': product.is_active, 'eans': sorted(set(eans[product.id])),
            'attributes': attrs if isinstance(attrs, list) else attributes[product.id] or fallback[product.id],
            'manual_eans': override.get('eans') if isinstance(override.get('eans'), list) else None}
    return result


def _facts_hash(facts):
    # A title edit does not change physical identity; SKU, axes and EAN do.
    value = {key: facts[key] for key in ('product_id', 'sku', 'is_active', 'eans')}
    value['attributes'] = sorted({(folded(item.get('name', '')), folded(item.get('value', '')))
                                  for item in facts['attributes'] if isinstance(item, dict)})
    value['manual_eans'] = sorted(verified_barcodes(facts['manual_eans'])) if facts['manual_eans'] is not None else None
    return digest(value)


async def _cached(db, shop_id, facts, query=''):
    """Bounded candidate discovery from existing cache; never a whole-shop pull."""
    codes = sorted({item['sku'].lower() for item in facts.values()})
    barcodes = sorted({ean for item in facts.values() for ean in item['eans']})
    rows = (await db.execute(text("""
        SELECT c.external_code AS parent_code, c.data->>'product_id' AS parent_id,
               c.data->'descriptions' AS descriptions, leaf.data,
               CASE WHEN jsonb_typeof(c.data->'variants')='array'
                    THEN jsonb_array_length(c.data->'variants')>0 ELSE false END AS is_variant
        FROM shop_product_content c
        CROSS JOIN LATERAL jsonb_array_elements(CASE
          WHEN jsonb_typeof(c.data->'variants')='array' THEN CASE WHEN jsonb_array_length(c.data->'variants')>0
            THEN c.data->'variants' ELSE jsonb_build_array(c.data) END
          ELSE jsonb_build_array(c.data) END) AS leaf(data)
        WHERE c.shop_id=:shop AND (
          lower(leaf.data->>'code')=ANY(CAST(:codes AS text[]))
          OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(CASE
                     WHEN jsonb_typeof(leaf.data->'ean')='array' THEN leaf.data->'ean'
                     ELSE jsonb_build_array(leaf.data->'ean') END) ean(value)
                     CROSS JOIN LATERAL regexp_split_to_table(coalesce(ean.value,''), '[/,;[:space:]]+') token(value)
                     WHERE token.value=ANY(CAST(:eans AS text[])))
          OR (:query<>'' AND (strpos(lower(coalesce(leaf.data->>'code','')), lower(:query))>0
                            OR c.external_code=:query)))
        ORDER BY c.external_code, leaf.data->>'code' LIMIT 201
        """), {'shop': shop_id, 'codes': codes, 'eans': barcodes, 'query': query})).mappings().all()
    if len(rows) > 200:
        return [], True
    leaves = []
    for row in rows:
        raw = row['data']
        try:
            code = exact_code(raw.get('code', ''))
            parent = exact_code(row['parent_code']) if row['is_variant'] else code
            native = raw.get('variant_id') if row['is_variant'] else raw.get('product_id')
            if not str(native or '').isdigit() or int(native) <= 0 or not str(row['parent_id'] or '').isdigit():
                continue
            descriptions = row['descriptions'] if isinstance(row['descriptions'], list) else []
            name = next((item.get('title') for item in descriptions if isinstance(item, dict)
                         and isinstance(item.get('title'), str)), code)
            raw_eans = raw.get('ean')
            leaves.append({'code': code, 'parent_code': parent, 'is_variant': bool(row['is_variant']),
                'external_id': str(native), 'parent_id': str(row['parent_id']), 'name': name[:500],
                'eans': list(verified_barcodes(raw_eans if isinstance(raw_eans, list) else (raw_eans,))),
                'attributes': variant_attributes(raw, strict=True) if row['is_variant'] else []})
        except (ValueError, TypeError, AttributeError):
            continue
    return leaves, False


def _unique(leaves):
    result = {}
    for leaf in leaves:
        key = (leaf['code'], leaf['parent_code'], *remote_key(leaf))
        if key in result and remote_snapshot(result[key]) != remote_snapshot(leaf):
            raise LinkError('product_links_remote_changed')
        result[key] = leaf
    return list(result.values())


async def candidates(db, payload):
    shop = await _shop(db, payload.shop_code)
    facts = await _facts(db, payload.product_ids)
    cached, limited = await _cached(db, shop.id, facts, payload.query)
    rows = []
    for product_id in payload.product_ids:
        item = facts[product_id]
        matches = []
        for remote in _unique(cached):
            exact = remote['code'] == item['sku']
            barcode = bool(set(remote['eans']) & set(item['eans']))
            if exact or barcode or payload.query:
                matches.append({**remote, 'match_by': 'shared_sku' if exact else 'validated_barcode' if barcode else 'manual'})
        rows.append({'product_id': product_id, 'sku': item['sku'], 'name': item['name'], 'matches': matches[:10],
                     'warnings': ['cached_candidate_limit'] if limited or len(matches) > 10 else []})
    return {'shop_code': shop.code, 'rows': rows}


async def _source_call(operation):
    try:
        return await operation
    except source.SourceError as error:
        raise LinkError(error.code, error.status, getattr(error, 'retry_after', None)) from None


def _choice(facts, leaves, manual=None, cached=()):
    if manual is not None:
        matches = [leaf for leaf in leaves if leaf['code'] == manual['code']
                   and leaf['parent_code'] == manual['parent_code'] and leaf['is_variant'] == manual['is_variant']]
        matched_by = 'manual'
    else:
        matches = [leaf for leaf in leaves if leaf['code'] == facts['sku']]
        matched_by = 'shared_sku'
        if not matches:
            matches = [leaf for leaf in leaves if set(leaf['eans']) & set(facts['eans'])]
            matched_by = 'validated_barcode'
            # A second cached owner remains ambiguous even if only the first
            # appeared in another selected row's fresh lookup.
            identities = {(leaf['code'], leaf['parent_code'], *remote_key(leaf)) for leaf in [*cached, *leaves]
                          if set(leaf['eans']) & set(facts['eans'])}
            if len(identities) > 1:
                return None, matched_by, len(identities)
    matches = _unique(matches)
    return (matches[0] if len(matches) == 1 else None), matched_by, len(matches)


async def _rows(db, shop, facts, choices):
    identities = [RemoteIdentity(shop.id, remote['code'], remote['is_variant'], remote['external_id'],
        remote['parent_code'], tuple(remote['eans']), expected_product_id=pid)
        for pid, (remote, mode, count) in choices.items() if remote is not None]
    index = await load_identity_index(db, shop.id, identities)
    duplicate_targets = Counter(remote_key(remote) for remote, mode, count in choices.values() if remote is not None)
    rows = []
    for pid, item in facts.items():
        remote, mode, count = choices[pid]
        row = {'product_id': pid, 'sku': item['sku'], 'name': item['name'], 'remote': remote,
               'match_by': mode if remote else None, 'status': 'ready', 'warnings': [], 'errors': [],
               'local_hash': _facts_hash(item)}
        if not item['is_active']:
            row['errors'].append('inactive_product')
        if remote is None:
            row['status'] = 'conflict' if count > 1 or row['errors'] else 'not_found'
            row['errors'].append('ambiguous_remote_match' if count > 1 else 'remote_product_not_found')
            rows.append(row)
            continue
        if duplicate_targets[remote_key(remote)] > 1:
            row['errors'].append('remote_target_selected_twice')
        identity = RemoteIdentity(shop.id, remote['code'], remote['is_variant'], remote['external_id'],
            remote['parent_code'], tuple(remote['eans']), expected_product_id=pid)
        resolution = index.resolve(identity)
        if resolution.status == 'conflict' or resolution.product_id != pid:
            row['errors'].extend(resolution.reasons or ['identity_unresolved'])
        mappings = index.by_product.get(pid, [])
        if len(mappings) > 1:
            row['errors'].append('ambiguous_shop_mapping')
        elif mappings:
            mapping = mappings[0]
            expected_parent = remote['parent_code'] if remote['is_variant'] else None
            if (not mapping.is_listed or mapping.is_variant != remote['is_variant']
                    or mapping_code(mapping) != remote['code'] or str(mapping.external_id) != remote['external_id']
                    or (remote['is_variant'] and mapping.parent_code != expected_parent)):
                row['errors'].append('existing_mapping_differs')
            else:
                row['status'] = 'already_linked'
        try:
            local_axes, remote_axes = axes(item['attributes']), axes(remote.get('attributes'))
            if any(local_axes[key] != remote_axes[key] for key in local_axes.keys() & remote_axes.keys()):
                row['errors'].append('variant_axes_conflict')
            if (remote['is_variant'] or local_axes) and (not local_axes or not remote_axes or local_axes.keys() != remote_axes.keys()):
                row['warnings'].append('variant_axes_missing')
        except LinkError:
            row['errors'].append('variant_axes_ambiguous')
        if not item['eans'] or not remote['eans']:
            row['warnings'].append('barcode_missing')
        manual_eans = verified_barcodes(item['manual_eans']) if item['manual_eans'] is not None else None
        if manual_eans and remote['eans'] and not set(manual_eans) & set(remote['eans']):
            row['errors'].append('manual_barcode_conflict')
        if row['errors']:
            row['status'] = 'conflict'
        row['warnings'] = sorted(set(row['warnings'] + list(remote.get('warnings', []))))
        row['errors'] = sorted(set(row['errors']))
        rows.append(row)
    return rows


def public(row):
    return {'id': row.id, 'shop_code': row.document['shop_code'], 'status': row.status,
        'preview_hash': row.preview_hash, 'expires_at': row.expires_at.isoformat(),
        'rows': [{key: value for key, value in item.items() if key != 'local_hash'} for item in row.document['rows']],
        'remote_requests': row.document.get('remote_requests', 0), 'result': row.result}


async def get(db, identifier):
    row = await db.get(ProductLinkPreview, str(identifier))
    if row is None:
        raise LinkError('product_links_not_found', 404)
    return public(row)


async def preview(db, payload, *, actor=None):
    raw = payload.model_dump(mode='json')
    request_hash = digest(raw)
    existing = await db.get(ProductLinkPreview, str(payload.request_id))
    if existing is not None:
        if existing.request_hash != request_hash:
            raise LinkError('product_links_request_changed')
        return public(existing)
    shop = await _shop(db, payload.shop_code)
    shop_id = shop.id
    facts = await _facts(db, payload.product_ids)
    cached, cache_limited = await _cached(db, shop_id, facts)
    manual = {item.product_id: item.model_dump() for item in payload.manual_matches}
    # Explicit rows substitute their chosen leaf for the automatic SKU lookup.
    try:
        codes = sorted({manual[pid]['code'] if pid in manual else exact_code(item['sku']) for pid, item in facts.items()})
    except ValueError:
        raise LinkError('product_links_unsupported_code', 422) from None
    await db.commit()  # No database locks or transaction spans external I/O.
    observed = await _source_call(source.discover(payload.shop_code, codes, max_requests=MAX_REQUESTS))
    leaves = list(observed['leaves'])
    remaining = MAX_REQUESTS - observed['requests']
    fallback = []
    for pid, item in facts.items():
        if pid in manual or any(leaf['code'] == item['sku'] for leaf in leaves):
            continue
        candidates_by_ean = _unique([leaf for leaf in cached if set(leaf['eans']) & set(item['eans'])])
        if len(candidates_by_ean) == 1:
            fallback.append(candidates_by_ean[0]['code'])
    fallback_skipped = bool(fallback and remaining < 2)
    if fallback and remaining >= 2:
        extra = await _source_call(source.discover(payload.shop_code, sorted(set(fallback)),
            expected_fingerprint=observed['fingerprint'], max_requests=remaining))
        leaves.extend(extra['leaves'])
        observed['requests'] += extra['requests']
    leaves = _unique(leaves)
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
    # Concurrent equal UUID requests share the first committed observation.
    existing = await db.get(ProductLinkPreview, str(payload.request_id), populate_existing=True)
    if existing is not None:
        if existing.request_hash != request_hash:
            raise LinkError('product_links_request_changed')
        return public(existing)
    shop = await _shop(db, payload.shop_code)
    if shop.id != shop_id:
        raise LinkError('product_links_identity_changed')
    facts = await _facts(db, payload.product_ids, lock=True)
    choices = {pid: _choice(item, leaves, manual.get(pid), cached) for pid, item in facts.items()}
    rows = await _rows(db, shop, facts, choices)
    for row in rows:
        if fallback_skipped and row['status'] == 'not_found':
            row['warnings'].append('fallback_request_limit')
        if cache_limited:
            row['warnings'].append('cached_candidate_limit')
            if row['match_by'] == 'validated_barcode':
                row['status'] = 'conflict'
                row['errors'].append('cached_candidate_limit')
    at = now()
    document = {'shop_code': shop.code, 'fingerprint': observed['fingerprint'], 'rows': rows,
                'remote_requests': observed['requests'], 'created_by': actor or {'source': 'operator_token'}}
    row = ProductLinkPreview(id=str(payload.request_id), shop_id=shop.id, request_hash=request_hash,
        preview_hash=digest(document), document=document, status='prepared', created_at=at,
        expires_at=at + timedelta(minutes=PREVIEW_MINUTES))
    db.add(row)
    await db.flush()
    result = public(row)
    await db.commit()
    return result


def _apply_selection(row, payload, *, allow_expired=False):
    if payload.preview_hash != row.preview_hash or digest(row.document) != row.preview_hash:
        raise LinkError('product_links_preview_changed')
    chosen = sorted(payload.product_ids)
    if row.status == 'completed':
        if chosen != row.result['product_ids']:
            raise LinkError('product_links_apply_selection_changed')
        return None
    rows = {item['product_id']: item for item in row.document['rows']}
    if any(pid not in rows or rows[pid]['status'] not in ('ready', 'already_linked') for pid in chosen):
        raise LinkError('product_links_invalid_selection', 422)
    if not allow_expired and now() >= row.expires_at:
        raise LinkError('product_links_preview_expired')
    return [rows[pid] for pid in chosen]


async def apply(db, identifier, payload, *, actor=None):
    row = await db.get(ProductLinkPreview, str(identifier))
    if row is None:
        raise LinkError('product_links_not_found', 404)
    # Validate the frozen hash and eligible subset before any failure can expire
    # this operation. An already expired valid request still needs the fence to
    # settle an earlier request whose HTTP response was lost.
    selected = _apply_selection(row, payload, allow_expired=True)
    if selected is None:
        return public(row)
    try:
        return await _apply_prepared(db, identifier, payload, row, actor=actor)
    except LinkError as error:
        if error.code in ('product_links_preview_changed', 'product_links_invalid_selection',
                          'product_links_apply_selection_changed', 'product_links_not_found'):
            raise
        return await _fence_failed_apply(db, identifier, payload, error)


async def _fence_failed_apply(db, identifier, payload, error):
    """Settle all earlier applies before reporting that none can still commit.

    Explicit source/identity failures happen before business writes. The shared
    lock orders this expiry with every in-flight apply: a completed apply wins;
    otherwise every delayed apply must observe the committed expiry. Unknown
    database/commit failures are deliberately not converted into this guarantee.
    """
    await db.rollback()
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
    row = await db.scalar(select(ProductLinkPreview).where(ProductLinkPreview.id == str(identifier))
                          .with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise LinkError('product_links_not_found', 404)
    selected = _apply_selection(row, payload, allow_expired=True)
    if selected is None:
        result = public(row)
        await db.commit()
        return result
    row.expires_at = min(row.expires_at, now())
    await db.commit()
    error.known_unapplied = True
    raise error


async def _apply_prepared(db, identifier, payload, row, *, actor=None):
    selected = _apply_selection(row, payload)
    shop_code, fingerprint = row.document['shop_code'], row.document['fingerprint']
    frozen = [item['remote'] for item in selected]
    await db.commit()
    observed = await _source_call(source.verify(shop_code, frozen, fingerprint, max_requests=MAX_REQUESTS))
    current = {remote_key(leaf): leaf for leaf in _unique(observed['leaves'])}
    if observed['fingerprint'] != fingerprint or any(remote_key(leaf) not in current or
        remote_snapshot(leaf) != remote_snapshot(current[remote_key(leaf)]) for leaf in frozen):
        raise LinkError('product_links_remote_changed')
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
    row = await db.scalar(select(ProductLinkPreview).where(ProductLinkPreview.id == str(identifier))
                          .with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise LinkError('product_links_not_found', 404)
    selected = _apply_selection(row, payload)
    if selected is None:
        return public(row)
    shop = await _shop(db, shop_code)
    if shop.id != row.shop_id:
        raise LinkError('product_links_identity_changed')
    facts = await _facts(db, payload.product_ids, lock=True)
    if any(_facts_hash(facts[item['product_id']]) != item['local_hash'] for item in selected):
        raise LinkError('product_links_identity_changed')
    choices = {item['product_id']: (current[remote_key(item['remote'])], item['match_by'], 1) for item in selected}
    checked = await _rows(db, shop, facts, choices)
    if any(item['status'] not in ('ready', 'already_linked') for item in checked):
        raise LinkError('product_links_identity_changed')
    linked, existing = [], []
    for item in checked:
        pid, remote = item['product_id'], item['remote']
        if item['status'] == 'already_linked':
            existing.append(pid)
            continue
        db.add(ShopProduct(shop_id=shop.id, product_id=pid, external_id=remote['external_id'],
            external_code=remote['code'], variant_code=remote['code'] if remote['is_variant'] else None,
            parent_code=remote['parent_code'] if remote['is_variant'] else None,
            is_variant=remote['is_variant'], is_listed=True, shop_stock=None, shop_price=None,
            push_pending=False, last_pull_at=now()))
        linked.append(pid)
    row.status, row.completed_at = 'completed', now()
    row.result = {'product_ids': sorted(payload.product_ids), 'linked_product_ids': sorted(linked),
        'already_linked_product_ids': sorted(existing), 'completed_at': row.completed_at.isoformat(),
        'applied_by': actor or {'source': 'operator_token'}, 'remote_requests': observed['requests']}
    await db.flush()
    result = public(row)
    await db.commit()
    return result
