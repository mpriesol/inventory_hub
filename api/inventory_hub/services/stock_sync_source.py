"""Exact leaf reads and single-leaf writes; never batch or retry a stock PUT."""
import asyncio
from datetime import datetime, timezone
from time import monotonic
from urllib.parse import quote, urlencode
from inventory_hub.services import stock_publication_source as transport

SourceError = transport.SourceError
MAX_BATCH_QUERY_BYTES = 6000


def batch_groups(targets):
    """Bound exact filters by both leaf count and encoded query size."""
    if not isinstance(targets, list) or not 1 <= len(targets) <= 100:
        raise transport._invalid_target()
    targets = [transport._target(target) for target in targets]
    if len({target['code'].casefold() for target in targets}) != len(targets):
        raise transport._invalid_target()
    groups = []
    for variant, path, field in ((False, 'products/simple', 'codes'),
                                 (True, 'products/variants', 'variant_codes')):
        selected = []
        for target in targets:
            if (target['variant_code'] is not None) != variant:
                continue
            candidates = [*selected, target]
            query = urlencode({field: ';'.join(row['code'] for row in candidates),
                               'page': 1, 'current_page_items': 100})
            if selected and len(query.encode('ascii')) > MAX_BATCH_QUERY_BYTES:
                groups.append({'path': path, 'filter': field, 'targets': selected})
                selected = [target]
            else:
                selected = candidates
        if selected:
            groups.append({'path': path, 'filter': field, 'targets': selected})
    return groups


def _batch_observations(payload, targets):
    """Only a complete exact response may certify that a leaf is unchanged."""
    key = 'variants' if targets[0]['variant_code'] is not None else 'products'
    if not isinstance(payload, dict) or not transport._messages_ok(payload.get('messages')):
        raise SourceError('stock_publication_invalid_response')
    rows = payload.get(key)
    metadata = [payload.get(field) for field in ('current_page', 'current_page_items', 'number_of_items', 'number_of_pages')]
    if (not isinstance(rows, list) or any(type(value) is not int for value in metadata)
            or metadata != [1, len(targets), len(targets), 1] or len(rows) != len(targets)
            or any(not isinstance(row, dict) or not isinstance(row.get('code'), str) for row in rows)):
        raise SourceError('stock_publication_invalid_response')
    by_code = {row['code']: row for row in rows}
    if len(by_code) != len(rows) or set(by_code) != {target['code'] for target in targets}:
        raise SourceError('stock_publication_identity_changed', 409)
    observed = {}
    for target in targets:
        row = by_code[target['code']]
        # Validate completeness before reusing the existing strict single-leaf
        # identity/quantity parser. No truncated page is turned into a success.
        single = {key: [row], 'current_page': 1, 'current_page_items': 1, 'number_of_items': 1, 'number_of_pages': 1}
        observation = transport._observation(single, target)
        if ('availability' not in row or row['availability'] is not None and not isinstance(row['availability'], str)
                or 'can_add_to_basket_yn' not in row
                or row['can_add_to_basket_yn'] is not None and type(row['can_add_to_basket_yn']) is not bool):
            raise SourceError('stock_publication_invalid_response')
        observed[target['code']] = {'identity': observation['identity'], 'stock': observation['quantity'],
            'availability': row['availability'], 'can_add_to_basket_yn': row['can_add_to_basket_yn']}
    return observed


def _consistent_identities(observations):
    parents, product_ids, variants, simple_ids = {}, {}, set(), set()
    for value in observations.values():
        identity = value['observation']['identity']
        parent, product_id, variant_id = identity['parent_code'], identity['product_id'], identity['variant_id']
        if (parent in parents and parents[parent] != product_id
                or product_id in product_ids and product_ids[product_id] != parent
                or variant_id is not None and variant_id in variants
                or product_id in simple_ids
                or variant_id is None and product_id in product_ids):
            raise SourceError('stock_publication_identity_changed', 409)
        parents[parent], product_ids[product_id] = product_id, parent
        if variant_id is not None:
            variants.add(variant_id)
        else:
            simple_ids.add(product_id)


def _read_many(shop_code, targets, fingerprint):
    groups = batch_groups(targets)
    client = transport._client(shop_code, fingerprint)
    try:
        result = {}
        for group in groups:
            payload = transport._request(client, 'get', group['path'], params={
                group['filter']: ';'.join(target['code'] for target in group['targets']),
                'page': 1, 'current_page_items': 100})
            observed = _batch_observations(payload, group['targets'])
            at, elapsed = datetime.now(timezone.utc), monotonic()
            result.update({code: {'observation': value, 'observed_at': at, 'observed_monotonic': elapsed}
                           for code, value in observed.items()})
        _consistent_identities(result)
        return result
    finally:
        client.session.close()


def _fields(value):
    if (not isinstance(value, dict) or set(value) != {'stock', 'availability', 'can_add_to_basket_yn'}
            or value['can_add_to_basket_yn'] is not True or not isinstance(value['availability'], str)
            or not 1 <= len(value['availability']) <= 100
            or any(ord(c) < 32 for c in value['availability'])):
        raise SourceError('stock_sync_invalid_payload', 422)
    return {**value, 'stock': transport._quantity(value['stock'], outgoing=True)}


def _read(shop_code, target, fingerprint):
    client = transport._client(shop_code, fingerprint)
    try:
        if target['variant_code'] is None:
            payload = transport._request(client, 'get', f'products/{quote(target["code"], safe="")}/simple')
        else:
            payload = transport._request(client, 'get', 'products/variants',
                                         params={'variant_codes': target['code'], 'page': 1, 'current_page_items': 100})
        observation = transport._observation(payload, target)
        leaf = transport._one(payload, 'variants' if target['variant_code'] is not None else 'products')
        # Missing fields must not masquerade as a verified desired state.
        return {'identity': observation['identity'], 'stock': observation['quantity'],
                'availability': leaf.get('availability'), 'can_add_to_basket_yn': leaf.get('can_add_to_basket_yn')}
    finally:
        client.session.close()


def _write(shop_code, target, desired, fingerprint):
    client = transport._client(shop_code, fingerprint)
    try:
        leaf = {'code': target['code'], **desired, 'stock': int(desired['stock'])}
        product = leaf if target['variant_code'] is None else {'code': target['parent_code'], 'variants': [leaf]}
        payload = transport._request(client, 'put', 'products', payload={'products': [product]})
        if not transport._acknowledged(payload, target):
            raise SourceError('stock_publication_write_unconfirmed', uncertain=True)
        return {'acknowledged': True}
    finally:
        client.session.close()


async def read(shop_code, target, fingerprint):
    return await asyncio.to_thread(_read, shop_code, transport._target(target), fingerprint)


async def read_many(shop_code, targets, fingerprint):
    # Read only. An incomplete/error response never fans out into single GETs.
    return await asyncio.to_thread(_read_many, shop_code, targets, fingerprint)


async def write_once(shop_code, target, desired, fingerprint):
    return await asyncio.to_thread(_write, shop_code, transport._target(target, frozen=True), _fields(desired), fingerprint)
