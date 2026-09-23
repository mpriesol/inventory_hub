"""Deterministic category and availability rules; never writes stock quantities."""
from inventory_hub.services.catalog import CatalogError
from inventory_hub.config_normalize import normalize_supplier_availability


def _system_category_root(category):
    # Upgates documents explicit parent_id: null as a system menu root. A
    # missing field in an older cached row is not evidence of a system root.
    # https://docs.upgates.com/api-reference/kategorie
    return (category.get('category_id') is not None and 'parent_id' in category
            and category['parent_id'] is None and not category.get('parent_code'))


def system_category_codes(rows):
    """Only roots identified by Upgates metadata, for historical readback."""
    return {row['code'] for row in rows if row.get('code')
            and (row.get('system_root') is True or _system_category_root(row))}


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
        system_root = _system_category_root(c)
        result.append({'code': c['code'], 'category_id': c.get('category_id'), 'parent_code': parent,
                       'system_root': system_root, 'assignable': not system_root,
                       'active': c.get('active_yn', True),
                       'names': {d['language']: d.get('name', c['code']) for d in c.get('descriptions', [])}})
    return result


def category_chain(rows, code):
    if not code:
        return []
    index = {r['code']: r for r in rows}
    system_roots = system_category_codes(rows)
    if code in system_roots:
        raise CatalogError('category_not_assignable', 'A system menu cannot be assigned to a product', 422)
    chain, seen = [], set()
    current = code
    while current:
        if current in seen or current not in index:
            raise CatalogError('category_tree_invalid', 'Category ancestry is incomplete or cyclic', 422)
        seen.add(current)
        if current not in system_roots:
            chain.append({'code': current, 'main_yn': current == code})
        current = index[current].get('parent_code')
    return list(reversed(chain))


def availability_policy(supplier, config=None):
    """Only supplier configuration owns availability; AI import rules cannot override it."""
    try:
        labels = normalize_supplier_availability((config or {}).get('adapter_settings', {}).get('availability'))
    except (ValueError, AttributeError) as error:
        raise CatalogError('supplier_availability_invalid', 'Check supplier availability configuration', 422) from error
    return {'orderable': labels['orderable'], 'unknown': labels['unknown'], 'hide_zero_stock': False,
            'supplier_name': {'paul-lange': 'Paul Lange', 'northfinder': 'Northfinder'}.get(supplier, supplier)}


def apply_availability(payload, products, policy):
    def apply(obj, product):
        positive = (product.supplier_stock is not None and product.supplier_stock > 0) or bool(product.supplier_stock_min and product.supplier_stock_min > 0)
        orderable = positive or product.supplier_external_available is True
        obj['availability'] = policy['orderable'] if orderable else policy['unknown']
        # Unknown/zero supplier stock is orderable; visibility remains governed
        # by the separate import review/approval contract.
        obj['can_add_to_basket_yn'] = True
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
        payload['availability'] = policy['unknown']
        payload['can_add_to_basket_yn'] = True
    else:
        apply(payload, products[0])
        if not payload.get('active_yn'):
            for d in payload.get('descriptions', []):
                d['active_yn'] = False
