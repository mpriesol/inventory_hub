"""Selected merchandising fields on one exact Upgates leaf, with one PUT only."""
import asyncio
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from inventory_hub.services import stock_publication_source as source
from inventory_hub.services.upgates import variant_attributes


def _read(client, target):
    variant = target['variant_code'] is not None
    result = source._request(client, 'get', 'products/variants' if variant else 'products',
        params={'variant_codes' if variant else 'codes': target['code'], 'page': 1, 'current_page_items': 100,
                **({} if variant else {'variants_yn': False})})
    row = source._one(result, 'variants' if variant else 'products')
    # Shared identity validation also rejects sets/parents posing as physical leaves.
    identity_payload = deepcopy(result)
    identity_payload['variants' if variant else 'products'][0]['stock'] = '0'
    identity = source._observation(identity_payload, target)['identity']
    return {'identity': identity, 'leaf': row}


def _options(client):
    config = source._request(client, 'get', 'config').get('config') or {}
    languages = source._request(client, 'get', 'languages').get('languages')
    lists = source._request(client, 'get', 'pricelists').get('pricelists')
    if not isinstance(config.get('prices_with_vat_yn'), bool) or not isinstance(languages, list) or not isinstance(lists, list):
        raise source.SourceError('product_publication_price_settings_unknown', 422)
    language = [v for v in languages if isinstance(v, dict) and v.get('language_id') == 'sk' and v.get('active_yn') is True]
    defaults = [v for v in lists if isinstance(v, dict) and v.get('default_yn') is True and isinstance(v.get('name'), str)]
    if len(language) != 1 or language[0].get('currency_id') != 'EUR' or len(defaults) != 1:
        raise source.SourceError('product_publication_price_settings_unknown', 422)
    return {'prices_with_vat': config['prices_with_vat_yn'], 'language': 'sk', 'currency': 'EUR', 'pricelist': defaults[0]['name']}


def _read_remote(shop, target, fingerprint, prices):
    client = source._client(shop, fingerprint)
    try:
        result = _read(client, source._target(target))
        result['options'] = _options(client) if prices else None
        # JSONB and API responses store exact decimals as strings, never floats.
        return plain(result)
    finally:
        client.session.close()


def plain(value):
    if isinstance(value, Decimal):
        return format(value, 'f')
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    return value


async def read_remote(shop, target, fingerprint, prices=False):
    return await asyncio.to_thread(_read_remote, shop, target, fingerprint, prices)


def money(value):
    try:
        if value is None or isinstance(value, bool):
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result > Decimal('9999999999.99'):
            raise ValueError
        return result
    except (ValueError, InvalidOperation):
        raise source.SourceError('product_publication_price_unknown', 422) from None


def field_values(remote, fields, options):
    leaf = remote['leaf']
    values = {}
    if 'name' in fields:
        values['name'] = next((r.get('title') for r in leaf.get('descriptions', []) if r.get('language') == 'sk'), None)
    if 'visible' in fields:
        values['visible'] = leaf.get('active_yn')
    if 'ean' in fields:
        values['ean'] = leaf.get('ean') or ''
    if 'attributes' in fields:
        values['attributes'] = sorted(variant_attributes(leaf), key=lambda a: a['name'])
    if 'image_url' in fields:
        image = leaf.get('image')
        values['image_url'] = (image.get('url') if isinstance(image, dict) else image) or next((r.get('url')
            for r in leaf.get('images', []) if r.get('main_yn') is True), None)
    if 'sale_price_gross' in fields:
        price = next((p for p in leaf.get('prices', []) if p.get('language') == options['language']), {})
        lists = [p for p in price.get('pricelists', []) if p.get('name') == options['pricelist']]
        if len(lists) != 1:
            raise source.SourceError('product_publication_price_unknown', 422)
        vat = money(price.get('vat'))
        if vat > 100:
            raise source.SourceError('product_publication_price_unknown', 422)
        original = money(lists[0].get('price_original'))
        values['sale_price_gross'] = format((original if options['prices_with_vat'] else original * (1 + vat / 100)).quantize(Decimal('.01')), 'f')
        # Discounts/action prices remain untouched, and participate in stale checks.
        values['price_original'] = format(original.quantize(Decimal('.01')), 'f')
        values['vat'] = format(vat.quantize(Decimal('.01')), 'f')
        values['product_discount'] = format(money(lists[0]['product_discount']).normalize(), 'f') if lists[0].get('product_discount') is not None else None
        values['price_sale'] = format(money(lists[0]['price_sale']).quantize(Decimal('.01')), 'f') if lists[0].get('price_sale') is not None else None
    return values


def build_patch(row, shop, remote, fields):
    options = remote['options']
    identity = remote['identity']
    before = field_values(remote, fields, options)
    after = deepcopy(before)
    leaf = {'code': identity['code']}
    for field in fields:
        if field == 'name':
            if identity['variant_code'] is not None:
                raise source.SourceError('product_publication_variant_name_unsupported', 422)
            after[field] = shop['effective']['name']
            leaf['descriptions'] = [{'language': 'sk', 'title': after[field]}]
        elif field == 'visible':
            value = shop['effective']['visible']
            if type(value) is not bool:
                raise source.SourceError('product_publication_visibility_unknown', 422)
            after[field] = leaf['active_yn'] = value
        elif field == 'ean':
            if len(row['eans']) > 1:
                raise source.SourceError('product_publication_multiple_barcodes', 422)
            after[field] = leaf['ean'] = row['eans'][0] if row['eans'] else ''
        elif field == 'attributes':
            if identity['variant_code'] is None:
                raise source.SourceError('product_publication_parent_attributes_unsupported', 422)
            after[field] = sorted(row['attributes'], key=lambda a: a['name'])
            leaf['parameters'] = [{'descriptions': [{'language': 'sk', 'name': a['name']}],
                'values': [{'descriptions': [{'language': 'sk', 'value': a['value']}]}]} for a in row['attributes']]
        elif field == 'image_url':
            if not row['image_url']:
                raise source.SourceError('product_publication_image_unknown', 422)
            if identity['variant_code'] is None:
                raise source.SourceError('product_publication_parent_image_unsupported', 422)
            after[field] = row['image_url']
            leaf['image'] = {'url': row['image_url']}
        elif field == 'sale_price_gross':
            gross = money(shop['effective']['sale_price_gross'])
            vat = money(before['vat'])
            if row['variant']['vat_rate'] is not None and money(row['variant']['vat_rate']) != vat:
                raise source.SourceError('product_publication_vat_changed', 409)
            original = (gross if options['prices_with_vat'] else gross / (1 + vat / 100)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
            # Float is used only at the JSON-number boundary after decimal calculation.
            leaf['prices'] = [{'language': 'sk', 'pricelists': [{'name': options['pricelist'], 'price_original': float(original)}]}]
            after[field] = format((original if options['prices_with_vat'] else original * (1 + vat / 100)).quantize(Decimal('.01')), 'f')
            after['price_original'] = format(original.quantize(Decimal('.01')), 'f')
    product = leaf if identity['variant_code'] is None else {'code': identity['parent_code'], 'variants': [leaf]}
    return {'payload': {'products': [product]}, 'before': before, 'after': after}


def _write(shop, identity, fingerprint, payload):
    client = source._client(shop, fingerprint)
    try:
        result = source._request(client, 'put', 'products', payload=payload)
        if not source._acknowledged(result, identity):
            raise source.SourceError('product_publication_write_unconfirmed', uncertain=True)
    finally:
        client.session.close()


async def write_once(shop, identity, fingerprint, payload):
    await asyncio.to_thread(_write, shop, identity, fingerprint, payload)
