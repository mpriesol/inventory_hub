"""One selected leaf: own stock, availability and orderability only."""
import asyncio
from urllib.parse import quote
from inventory_hub.services import stock_publication_source as transport

SourceError = transport.SourceError


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


async def write_once(shop_code, target, desired, fingerprint):
    return await asyncio.to_thread(_write, shop_code, transport._target(target, frozen=True), _fields(desired), fingerprint)
