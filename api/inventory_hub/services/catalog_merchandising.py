"""Deterministic category and availability rules; never writes stock quantities."""
from inventory_hub.services.catalog import CatalogError


def category_rows(rows):
    by_id = {c['category_id']: c['code'] for c in rows if c.get('category_id') is not None and c.get('code')}
    result = []
    for c in rows:
        if not c.get('code'):
            continue
        parent_id = c.get('parent_id')
        parent = c.get('parent_code') or by_id.get(parent_id)
        if not parent and parent_id not in (None, 0, ''):
            raise CatalogError('category_tree_invalid', 'A parent category is missing from the shop response', 422)
        result.append({'code': c['code'], 'category_id': c.get('category_id'), 'parent_code': parent,
                       'active': c.get('active_yn', True),
                       'names': {d['language']: d.get('name', c['code']) for d in c.get('descriptions', [])}})
    return result


def category_chain(rows, code):
    if not code:
        return []
    index = {r['code']: r for r in rows}
    chain, seen = [], set()
    current = code
    while current:
        if current in seen or current not in index:
            raise CatalogError('category_tree_invalid', 'Category ancestry is incomplete or cyclic', 422)
        seen.add(current)
        chain.append({'code': current, 'main_yn': current == code})
        current = index[current].get('parent_code')
    return list(reversed(chain))


def availability_policy(supplier):
    if supplier == 'paul-lange':
        return {'orderable': 'do 5 dní', 'unknown': 'Overíme', 'hide_zero_stock': False, 'supplier_name': 'Paul Lange'}
    if supplier == 'northfinder':
        return {'orderable': 'do 5 dní', 'unknown': 'Overíme', 'hide_zero_stock': True, 'supplier_name': 'Northfinder'}
    return {'unknown': 'Overíme'}


def apply_availability(payload, products, policy):
    def apply(obj, product):
        positive = (product.supplier_stock is not None and product.supplier_stock > 0) or bool(product.supplier_stock_min and product.supplier_stock_min > 0)
        orderable = positive or product.supplier_external_available is True
        obj['availability'] = policy.get('orderable', 'Overíme') if orderable else policy.get('unknown', 'Overíme')
        if policy.get('hide_zero_stock') and product.supplier_stock == 0:
            obj.update(active_yn=False, can_add_to_basket_yn=False, availability=policy.get('unknown', 'Overíme'))
    variants = payload.get('variants')
    if variants:
        by_code = {p.shop_code: p for p in products}
        for variant in variants:
            apply(variant, by_code[variant['code']])
        # Parent remains visible only if at least one selected variant is enabled.
        if not any(v.get('active_yn') for v in variants):
            payload['active_yn'] = False
            for d in payload.get('descriptions', []):
                d['active_yn'] = False
        payload['availability'] = policy.get('unknown', 'Overíme')
    else:
        apply(payload, products[0])
        if not payload.get('active_yn'):
            for d in payload.get('descriptions', []):
                d['active_yn'] = False
