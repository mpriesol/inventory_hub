"""Link existing Hub cards to verified existing shop leaves, without publication.

Network observations finish before the identity lock. Each apply rechecks the
frozen observations and local identity, then commits only the selected mappings
and its recovery result in one transaction. No canonical or stock writer is used.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import unicodedata

from sqlalchemy import or_, select, text
from sqlalchemy.dialects.postgresql import insert

from inventory_hub.db_models import Product, ProductIdentifier, Shop, SupplierProduct
from inventory_hub.db_models_ext import ProductSupplySource, ProductVariantAttribute, ShopProduct, ShopProductContent
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
    return {'id': row.id, 'external_id': row.native_id, 'external_code': row.external_code,
            'variant_code': row.external_code if row.is_variant else None, 'parent_code': row.parent_code,
            'is_variant': row.is_variant, 'is_listed': row.is_listed,
            'external_product_id': row.external_product_id, 'external_variant_id': row.external_variant_id}


async def _shop(db, shop_code):
    row = await db.scalar(select(Shop).where(Shop.code == shop_code)
                          .execution_options(populate_existing=True))
    if row is None or not row.is_active or row.platform != 'upgates':
        raise LinkError('product_links_shop_unavailable', 404)
    return row


async def _facts(db, product_ids, *, lock=False, exclude_supplier_product_id=None):
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
          AND leaf.data->>'code'=sp.external_code
        """), {'ids': list(product_ids)})).mappings():
        fallback[row['product_id']].extend(variant_attributes({'parameters': row['attributes']}))
    supplier_attributes = (await db.execute(select(Product.id, SupplierProduct.attributes['catalog']['variant_attributes'])
        .join(SupplierProduct, SupplierProduct.id.in_(select(ProductSupplySource.supplier_product_id)
                .where(ProductSupplySource.product_id == Product.id, ProductSupplySource.is_active.is_(True))))
        .where(Product.id.in_(product_ids), SupplierProduct.is_active.is_(True),
               SupplierProduct.id != exclude_supplier_product_id if exclude_supplier_product_id is not None else True))).all()
    for pid, values in supplier_attributes:
        if isinstance(values, list):
            fallback[pid].extend(variant_attributes({'parameters': values}))
    # The same operator-corrected axes displayed by the editor take precedence.
    overrides = {row.product_id: row.data for row in await db.scalars(select(ProductEditorOverride)
        .where(ProductEditorOverride.product_id.in_(product_ids)).execution_options(populate_existing=True))}
    result = {}
    for product in products:
        saved = overrides.get(product.id, {})
        override = saved.get('variant', {})
        display_name = saved.get('common', {}).get('name') or product.name
        attrs = override.get('attributes', attributes[product.id] or fallback[product.id])
        result[product.id] = {'product_id': product.id, 'sku': product.sku, 'name': display_name,
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


async def _replacement_mappings(db, shop, manual, choices, *, expected=None, errors=None):
    selected = {pid for pid, item in manual.items()
                if item.get('replace_deleted_variant') or item.get('replace_deleted_product')}
    if not selected:
        return {}
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
    rows = list(await db.scalars(select(ShopProduct).where(ShopProduct.shop_id == shop.id,
        ShopProduct.product_id.in_(selected)).with_for_update().execution_options(populate_existing=True)))
    by_product = {row.product_id: row for row in rows}
    mappings = {}
    from inventory_hub.db_models_ext import ShopOrder, ShopOrderItem
    from inventory_hub.stock_native_models import StockNativeOrder, StockNativeEvent
    for pid in sorted(selected):
        row, target = by_product.get(pid), manual[pid]
        remote = choices[pid][0]
        # A fresh request after a successful replacement is a normal exact
        # identity check. Do not try to prove that the new live target is gone.
        if (row is not None and remote is not None and row.is_listed and row.is_variant == target['is_variant']
                and row.external_code == target['code']
                and row.parent_code == (target['parent_code'] if target['is_variant'] else None)
                and row.external_product_id == remote['parent_id'] and row.native_id == remote['external_id']):
            continue
        try:
            if (row is None or not row.is_listed or not target.get(
                    'replace_deleted_variant' if row.is_variant else 'replace_deleted_product')):
                raise LinkError('product_links_replacement_not_allowed')
            try:
                source._native_id(row.external_product_id)
                exact_code(row.external_code)
                if row.is_variant:
                    source._native_id(row.external_variant_id)
                    exact_code(row.parent_code)
                elif row.external_variant_id is not None or row.parent_code is not None:
                    raise ValueError('A standalone mapping has no variant or parent code')
            except (ValueError, TypeError, source.SourceError):
                raise LinkError('product_links_replacement_identity_missing') from None
            native = await db.scalar(select(StockNativeOrder.order_id).join(StockNativeEvent,
                StockNativeEvent.id == StockNativeOrder.event_id).where(StockNativeOrder.shop_id == shop.id,
                StockNativeEvent.facts.contains({'lines': [{'product_id': pid, 'native': True}]})).limit(1))
            active = await db.scalar(select(ShopOrderItem.id).join(ShopOrder, ShopOrder.id == ShopOrderItem.order_id)
                .where(ShopOrder.shop_id == shop.id, ShopOrderItem.product_id == pid,
                    ShopOrderItem.stock_managed.is_(True), ShopOrder.stock_state.in_(('pending', 'reserved'))).limit(1))
            if native is not None or active is not None:
                raise LinkError('product_links_replacement_in_use')
            await _require_mapping_write_available(db, shop, row,
                {row.parent_code if row.is_variant else row.external_code, target['parent_code'], target['code']},
                'product_links_replacement_in_use')
            mappings[pid] = _mapping(row)
        except LinkError as error:
            if errors is None:
                raise
            errors[pid] = error.code
    if expected is not None and mappings != expected:
        raise LinkError('product_links_identity_changed')
    return mappings


async def _verify_replacement_absence(shop_code, mappings, fingerprint, *, max_requests, collect_conflicts=False):
    """Keep both original identity namespaces under one remote-read budget."""
    requests, existing = 0, []
    for is_variant, verify in ((True, source.verify_deleted_variants), (False, source.verify_deleted_products)):
        selected = [mapping for mapping in mappings if mapping['is_variant'] is is_variant]
        if not selected:
            continue
        result = await _source_call(verify(shop_code, selected, fingerprint,
            max_requests=max_requests - requests, collect_conflicts=collect_conflicts))
        requests += result['requests']
        existing.extend(result.get('existing_mapping_ids', []))
    return {'requests': requests, 'existing_mapping_ids': existing}


async def _require_mapping_write_available(db, shop, mapping, parents, error_code):
    """Keep unresolved remote intents attached to their original local identity."""
    from inventory_hub.stock_sync_models import StockSyncItem
    from inventory_hub.stock_publication_models import StockPublicationBatch, StockPublicationItem
    from inventory_hub.services.merchandising_write_guard import require_target_available
    from inventory_hub.services.catalog import CatalogError
    sku = await db.scalar(select(Product.sku).where(Product.id == mapping.product_id))
    pending = await db.scalar(select(StockSyncItem.id).where(StockSyncItem.shop_id == shop.id,
        StockSyncItem.sku == sku, StockSyncItem.status.in_(('sending', 'uncertain'))).limit(1))
    maintenance = await db.scalar(select(StockPublicationItem.id).join(StockPublicationBatch,
        StockPublicationBatch.id == StockPublicationItem.batch_id).where(StockPublicationBatch.shop_id == shop.id,
        StockPublicationItem.sku == sku, StockPublicationItem.status.in_(('sending', 'uncertain'))).limit(1))
    if pending is not None or maintenance is not None:
        raise LinkError(error_code)
    try:
        for parent in parents:
            await require_target_available(db, shop.code, parent)
    except CatalogError:
        raise LinkError(error_code) from None


async def _rows(db, shop, facts, choices, replacements=None):
    identities = [RemoteIdentity(shop.id, remote['code'], remote['is_variant'], remote['external_id'],
        remote['parent_code'], tuple(remote['eans']), expected_product_id=pid, external_product_id=remote['parent_id'])
        for pid, (remote, mode, count) in choices.items() if remote is not None]
    index = await load_identity_index(db, shop.id, identities)
    replacements = replacements or {}
    if replacements:
        index = index.without_mappings({mapping['id'] for mapping in replacements.values()})
    duplicate_targets = Counter(remote_key(remote) for remote, mode, count in choices.values() if remote is not None)
    rows = []
    for pid, item in facts.items():
        remote, mode, count = choices[pid]
        row = {'product_id': pid, 'sku': item['sku'], 'name': item['name'], 'remote': remote,
               'local_eans': sorted(verified_barcodes(item['manual_eans'] if item['manual_eans'] is not None else item['eans'])),
               'match_by': mode if remote else None, 'status': 'ready', 'warnings': [], 'errors': [],
               'local_hash': _facts_hash(item)}
        if pid in replacements:
            row['previous_mapping'] = replacements[pid]
            row['warnings'].append('replaces_deleted_variant' if replacements[pid]['is_variant']
                                   else 'replaces_deleted_product')
            if remote is None:
                row['errors'].append('product_links_replacement_not_allowed')
            elif (not row['local_eans'] or set(row['local_eans']) != set(remote['eans'])
                    or remote.get('invalid_barcodes')):
                row['errors'].append('product_links_replacement_ean_mismatch')
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
            remote['parent_code'], tuple(remote['eans']), expected_product_id=pid, external_product_id=remote['parent_id'])
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
                    or mapping_code(mapping) != remote['code']
                    or mapping.native_id is not None and str(mapping.native_id) != remote['external_id']
                    or mapping.external_product_id is not None and mapping.external_product_id != remote['parent_id']
                    or (remote['is_variant'] and mapping.parent_code != expected_parent)):
                row['errors'].append('existing_mapping_differs')
            elif mapping.native_id is None or mapping.external_product_id is None:
                # Completing an existing row is an explicit reviewed link, never
                # an implicit code-only publication or a new competing mapping.
                try:
                    await _require_mapping_write_available(db, shop, mapping,
                        {remote['parent_code']}, 'product_links_identity_in_use')
                except LinkError as error:
                    row['errors'].append(error.code)
                row['unverified_mapping'] = _mapping(mapping)
                row['warnings'].append('native_identity_verification')
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
    result = ({key: value for key, value in row.result.items() if key != '_content_archives'}
              if row.result is not None else None)
    return {'id': row.id, 'shop_code': row.document['shop_code'], 'status': row.status,
        'preview_hash': row.preview_hash, 'expires_at': row.expires_at.isoformat(),
        'rows': [{key: value for key, value in item.items() if key != 'local_hash'} for item in row.document['rows']],
        'remote_requests': row.document.get('remote_requests', 0), 'result': result}


async def get(db, identifier):
    row = await db.get(ProductLinkPreview, str(identifier))
    if row is None:
        raise LinkError('product_links_not_found', 404)
    return public(row)


def _metadata_key(remote):
    return ('variant:' if remote['is_variant'] else 'product:') + remote['external_id']


async def _cache_metadata(db, shop_id, leaves, *, replacements=None):
    """Store scoped display observations without replacing a complete vault.

    A newly created row has only this sidecar: it cannot impersonate a complete
    family for the legacy export guard. Existing prices, stock, descriptions,
    siblings and full-pull freshness remain untouched for the same parent.
    An explicitly verified parent replacement archives the old complete vault
    in the atomic apply result; it cannot masquerade as the recreated family.
    """
    grouped = defaultdict(list)
    for leaf in leaves:
        grouped[leaf['parent_code']].append(leaf)
    replacements, archives = replacements or {}, []
    for parent_code in sorted(grouped):
        await db.execute(insert(ShopProductContent).values(shop_id=shop_id, external_code=parent_code,
            data={}, pulled_at=now()).on_conflict_do_nothing(
                index_elements=[ShopProductContent.shop_id, ShopProductContent.external_code]))
        row = await db.scalar(select(ShopProductContent).where(ShopProductContent.shop_id == shop_id,
            ShopProductContent.external_code == parent_code).with_for_update().execution_options(populate_existing=True))
        if not isinstance(row.data, dict):
            raise LinkError('product_links_identity_changed')
        data = deepcopy(row.data)
        metadata = data.get('_hub_link_metadata')
        metadata = metadata if isinstance(metadata, dict) else {}
        previous_parent_id = str(data['product_id']) if data.get('product_id') is not None else None
        parent_ids = {leaf['parent_id'] for leaf in grouped[parent_code]}
        if previous_parent_id is not None and parent_ids != {previous_parent_id}:
            # Only selected, deletion-verified replacements can retire this
            # exact earlier parent. An unrelated vault or ordinary refresh
            # remains a conflict, even when the product codes are identical.
            if len(parent_ids) != 1 or data.get('code', parent_code) != parent_code:
                raise LinkError('product_links_identity_changed')
            for leaf in grouped[parent_code]:
                previous = replacements.get(_metadata_key(leaf))
                if (not previous or not previous.get('is_listed')
                        or previous.get('external_product_id') != previous_parent_id):
                    raise LinkError('product_links_identity_changed')
                if previous.get('is_variant') is True:
                    previous_code = previous.get('parent_code')
                elif (previous.get('is_variant') is False and previous.get('parent_code') is None
                      and previous.get('external_variant_id') is None):
                    previous_code = previous.get('external_code')
                else:
                    raise LinkError('product_links_identity_changed')
                if previous_code != parent_code:
                    raise LinkError('product_links_identity_changed')
            if any(isinstance(item, dict) and item.get('parent_id') != previous_parent_id
                   for item in metadata.values()):
                raise LinkError('product_links_identity_changed')
            archives.append({'content_id': row.id, 'external_code': parent_code,
                'previous_parent_id': previous_parent_id, 'parent_id': next(iter(parent_ids)),
                'data': data, 'pulled_at': row.pulled_at.isoformat()})
            data, metadata = {}, {}
        for leaf in grouped[parent_code]:
            # A full earlier observation can prove a parent was recreated.
            if data.get('product_id') is not None and str(data['product_id']) != leaf['parent_id']:
                raise LinkError('product_links_identity_changed')
            previous = metadata.get(_metadata_key(leaf))
            if isinstance(previous, dict) and previous.get('parent_id') != leaf['parent_id']:
                raise LinkError('product_links_identity_changed')
            metadata[_metadata_key(leaf)] = {**remote_identity(leaf), 'name': leaf['name'],
                'shop_url': leaf.get('shop_url'), 'shop_admin_url': leaf.get('shop_admin_url'),
                'observed_at': now().isoformat()}
        data['_hub_link_metadata'] = metadata
        row.data = data
    return archives


async def _refresh_mappings(db, shop_id, product_ids, *, lock=False):
    query = select(ShopProduct).where(ShopProduct.shop_id == shop_id,
        ShopProduct.product_id.in_(product_ids)).order_by(ShopProduct.product_id, ShopProduct.id)
    if lock:
        query = query.with_for_update()
    rows = list(await db.scalars(query.execution_options(populate_existing=True)))
    counts = Counter(row.product_id for row in rows)
    if any(counts[pid] == 0 for pid in product_ids):
        raise LinkError('product_links_mapping_missing', 404)
    if any(counts[pid] != 1 for pid in product_ids) or any(not row.is_listed for row in rows):
        raise LinkError('product_links_identity_changed')
    try:
        for row in rows:
            exact_code(mapping_code(row))
            exact_code(row.parent_code if row.is_variant else row.external_code)
            source._native_id(row.native_id)
    except (ValueError, TypeError, source.SourceError):
        raise LinkError('product_links_identity_changed') from None
    return {row.product_id: row for row in rows}


async def refresh(db, payload):
    """Refresh display links and a verified missing parent ID for the same leaf."""
    shop = await _shop(db, payload.shop_code)
    shop_id = shop.id
    mappings = await _refresh_mappings(db, shop_id, payload.product_ids)
    snapshots = {pid: _mapping(row) for pid, row in mappings.items()}
    codes = sorted({mapping_code(row) for row in mappings.values()})
    await db.commit()
    fingerprint = await _source_call(source.current_fingerprint(payload.shop_code))
    observed = await _source_call(source.discover(payload.shop_code, codes, fingerprint, max_requests=MAX_REQUESTS))
    if observed['fingerprint'] != fingerprint:
        raise LinkError('source_connection_changed')
    leaves = _unique(observed['leaves'])
    selected = {}
    for pid, mapping in snapshots.items():
        code = mapping['variant_code'] if mapping['is_variant'] else mapping['external_code']
        parent_code = mapping['parent_code'] if mapping['is_variant'] else mapping['external_code']
        matches = [leaf for leaf in leaves if leaf['code'] == code and leaf['is_variant'] == mapping['is_variant']
                   and leaf['external_id'] == str(mapping['external_id']) and leaf['parent_code'] == parent_code
                   and mapping['external_product_id'] in (None, leaf['parent_id'])]
        if len(matches) != 1:
            raise LinkError('product_links_identity_changed')
        selected[pid] = matches[0]
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
    shop = await _shop(db, payload.shop_code)
    current = await _refresh_mappings(db, shop.id, payload.product_ids, lock=True)
    if shop.id != shop_id or {pid: _mapping(row) for pid, row in current.items()} != snapshots:
        raise LinkError('product_links_identity_changed')
    if await _source_call(source.current_fingerprint(payload.shop_code)) != fingerprint:
        raise LinkError('source_connection_changed')
    for pid, mapping in current.items():
        if mapping.external_product_id is None:
            mapping.external_product_id = selected[pid]['parent_id']
    await _cache_metadata(db, shop_id, selected.values())
    result = {'shop_code': payload.shop_code, 'remote_requests': observed['requests'], 'rows': [
        {'product_id': pid, 'status': 'refreshed' if leaf.get('shop_url') and leaf.get('shop_admin_url') else 'incomplete',
         'shop_url': leaf.get('shop_url'), 'shop_admin_url': leaf.get('shop_admin_url')}
        for pid, leaf in sorted(selected.items())]}
    await db.commit()
    return result


async def preview(db, payload, *, actor=None):
    raw = payload.model_dump(mode='json')
    for match in raw['manual_matches']:
        # Default-false additions must not change hashes of persisted requests.
        for flag in ('replace_deleted_variant', 'replace_deleted_product'):
            if not match.get(flag):
                match.pop(flag, None)
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
    fallback, fallback_needs_parent = [], False
    for pid, item in facts.items():
        if pid in manual or any(leaf['code'] == item['sku'] for leaf in leaves):
            continue
        candidates_by_ean = _unique([leaf for leaf in cached if set(leaf['eans']) & set(item['eans'])])
        if len(candidates_by_ean) == 1:
            fallback.append(candidates_by_ean[0]['code'])
            fallback_needs_parent = fallback_needs_parent or candidates_by_ean[0]['is_variant']
    minimum_fallback = 3 if fallback_needs_parent else 2
    fallback_skipped = bool(fallback and remaining < minimum_fallback)
    if fallback and remaining >= minimum_fallback:
        extra = await _source_call(source.discover(payload.shop_code, sorted(set(fallback)),
            expected_fingerprint=observed['fingerprint'], max_requests=remaining))
        leaves.extend(extra['leaves'])
        observed['requests'] += extra['requests']
    leaves = _unique(leaves)
    # Recreated products can reuse both codes. Only freshly observed native
    # IDs distinguish an existing link from a deletion-verified replacement.
    choices = {pid: _choice(item, leaves, manual.get(pid), cached) for pid, item in facts.items()}
    replacement_errors = {}
    replacements = await _replacement_mappings(db, shop, manual, choices, errors=replacement_errors)
    await db.commit()  # Release mapping locks before checking remote deletion.
    deletion_errors = {}
    if replacements:
        deleted = await _verify_replacement_absence(payload.shop_code, list(replacements.values()),
            observed['fingerprint'], max_requests=MAX_REQUESTS - observed['requests'], collect_conflicts=True)
        observed['requests'] += deleted['requests']
        deletion_errors = {pid: ('product_links_variant_still_exists' if mapping['is_variant']
                                else 'product_links_product_still_exists') for pid, mapping in replacements.items()
                           if mapping['id'] in deleted.get('existing_mapping_ids', [])}
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
    replacement_errors = {}
    replacements = await _replacement_mappings(db, shop, manual, choices,
        expected=replacements, errors=replacement_errors)
    rows = await _rows(db, shop, facts, choices, replacements)
    for row in rows:
        code = replacement_errors.get(row['product_id']) or deletion_errors.get(row['product_id'])
        if code:
            row['status'] = 'conflict'
            row['errors'] = sorted(set([*row['errors'], code]))
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
    replacements = {item['product_id']: item['previous_mapping'] for item in selected if item.get('previous_mapping')}
    await db.commit()
    observed = await _source_call(source.verify(shop_code, frozen, fingerprint, max_requests=MAX_REQUESTS))
    if replacements:
        deleted = await _verify_replacement_absence(shop_code, list(replacements.values()),
            fingerprint, max_requests=MAX_REQUESTS - observed['requests'])
        observed['requests'] += deleted['requests']
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
    manual = {item['product_id']: {**item['remote'],
              ('replace_deleted_variant' if replacements[item['product_id']]['is_variant']
               else 'replace_deleted_product'): True} for item in selected
              if item['product_id'] in replacements}
    replacements = await _replacement_mappings(db, shop, manual, choices, expected=replacements)
    checked = await _rows(db, shop, facts, choices, replacements)
    if any(item['status'] not in ('ready', 'already_linked') for item in checked):
        raise LinkError('product_links_identity_changed')
    verified_before = {item['product_id']: item.get('unverified_mapping') for item in selected}
    if any(item.get('unverified_mapping') != verified_before[item['product_id']] for item in checked):
        raise LinkError('product_links_identity_changed')
    if await _source_call(source.current_fingerprint(shop_code)) != fingerprint:
        raise LinkError('source_connection_changed')
    content_archives = await _cache_metadata(db, shop.id, [item['remote'] for item in checked],
        replacements={_metadata_key(item['remote']): replacements[item['product_id']]
                      for item in checked if item['product_id'] in replacements})
    linked, existing, replaced = [], [], []
    for item in checked:
        pid, remote = item['product_id'], item['remote']
        if item['status'] == 'already_linked':
            existing.append(pid)
            continue
        if item.get('unverified_mapping'):
            original = item['unverified_mapping']
            mapping = await db.scalar(select(ShopProduct).where(ShopProduct.id == original['id'])
                .with_for_update().execution_options(populate_existing=True))
            if _mapping(mapping) != original:
                raise LinkError('product_links_identity_changed')
            mapping.external_product_id = remote['parent_id']
            mapping.external_variant_id = remote['external_id'] if remote['is_variant'] else None
            linked.append(pid)
            continue
        if pid in replacements:
            mapping = await db.get(ShopProduct, replacements[pid]['id'])
            mapping.external_product_id = remote['parent_id']
            mapping.external_variant_id = remote['external_id'] if remote['is_variant'] else None
            mapping.external_code = remote['code']
            mapping.parent_code = remote['parent_code'] if remote['is_variant'] else None
            mapping.is_variant, mapping.is_listed = remote['is_variant'], True
            mapping.shop_stock, mapping.shop_price, mapping.shop_availability = None, None, None
            mapping.last_push_at, mapping.last_push_status, mapping.last_push_error = None, None, None
            mapping.push_pending, mapping.last_pull_at = False, now()
            replaced.append({'product_id': pid, 'sku': item['sku'], 'eans': item['local_eans'],
                             'previous_mapping': replacements[pid], 'mapping': _mapping(mapping)})
            linked.append(pid)
            continue
        db.add(ShopProduct(shop_id=shop.id, product_id=pid, external_product_id=remote['parent_id'],
            external_variant_id=remote['external_id'] if remote['is_variant'] else None, external_code=remote['code'],
            parent_code=remote['parent_code'] if remote['is_variant'] else None,
            is_variant=remote['is_variant'], is_listed=True, shop_stock=None, shop_price=None,
            push_pending=False, last_pull_at=now()))
        linked.append(pid)
    row.status, row.completed_at = 'completed', now()
    row.result = {'product_ids': sorted(payload.product_ids), 'linked_product_ids': sorted(linked),
        'already_linked_product_ids': sorted(existing), 'completed_at': row.completed_at.isoformat(),
        'applied_by': actor or {'source': 'operator_token'}, 'remote_requests': observed['requests'],
        'replaced_mappings': replaced}
    if content_archives:
        row.result['_content_archives'] = content_archives
    await db.flush()
    result = public(row)
    await db.commit()
    return result
