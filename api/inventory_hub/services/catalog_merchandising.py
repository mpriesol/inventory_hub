"""Deterministic category and availability rules; never writes stock quantities."""
from datetime import datetime, timedelta, timezone

from inventory_hub.services.catalog import CatalogError, public_product
from inventory_hub.services.supplier_availability import exact_source_evidence
from inventory_hub.supplier_availability_types import DEFAULT_FRESHNESS_SECONDS
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
    return {**({'unavailable': labels['unavailable']} if 'unavailable' in labels else {}),
            'orderable': labels['orderable'], 'unknown': labels['unknown'], 'hide_zero_stock': False,
            'supplier_name': {'paul-lange': 'Paul Lange', 'northfinder': 'Northfinder'}.get(supplier, supplier)}


def _listing_label(product, policy, at):
    positive = any(q is not None and q > 0 for q in (product.supplier_stock, product.supplier_stock_min))
    orderable = positive or product.supplier_available is True or product.supplier_external_available is True
    unavailable = product.supplier_available is False and product.supplier_external_available is not True
    if product.supplier == 'husky':
        expiry = product.supplier_observation_expires_at
        fresh = bool(expiry and expiry > at and 'husky_public_stock_only' not in product.warnings)
        count = product.supplier_stock if product.supplier_stock is not None else product.supplier_stock_min
        orderable = fresh and orderable and count is not None and count >= 2
        unavailable = fresh and unavailable
    if orderable:
        return policy['orderable']
    return policy.get('unavailable', policy['unknown']) if unavailable else policy['unknown']


async def resolve_catalog_availability(db, supplier_code, products, policy, *, at=None):
    """Resolve new listings by exact supplier SKU, independently of the chosen shop code or brand."""
    at = at or datetime.now(timezone.utc)
    by_code = {p.code: p for p in products if p.supplier == supplier_code}
    evidence = await exact_source_evidence(db, supplier_code, by_code.keys(), at, unknown_label=policy['unknown'])
    resolved = {}
    for code, product in by_code.items():
        row = dict(label=policy['unknown'], orderable=True, status='missing_observation', observed_at=None, expires_at=None)
        resolved[code] = row
        if supplier_code == 'sloger' and 'sloger_b2b_feed_fallback' in product.warnings:
            row['status'] = 'skipped'
            continue
        if code not in evidence:
            continue
        stored, projection = evidence[code]
        status = projection['status']
        row.update(status=status, orderable=projection['orderable'])
        if status == 'missing_feed_item':
            row['label'] = projection['label']
        elif projection['fresh']:
            if projection['available'] is True:
                row['label'] = projection['label']
            elif projection['available'] is False:
                row['label'] = policy.get('unavailable', policy['unknown'])
            row['observed_at'] = datetime.fromisoformat(projection['observed_at'])
            row['expires_at'] = datetime.fromisoformat(projection['expires_at'])
        elif status == 'unconfigured' and supplier_code not in ('sloger', 'husky') and stored and (stored.attributes or {}).get('catalog'):
            # Suppliers using only the listing feed still need its latest persisted
            # row. A historical draft or invoice timestamp cannot renew that stock.
            current = public_product(stored)
            observed = current.fetched_at
            if current.supplier != supplier_code or current.code != code or observed is None or observed.tzinfo is None:
                continue
            expiry = observed + timedelta(seconds=DEFAULT_FRESHNESS_SECONDS)
            if observed <= at < expiry:
                # Some listing parsers expose quantities without a corresponding flag.
                if current.supplier_stock_external is not None and current.supplier_stock_external > 0:
                    current.supplier_external_available = True
                if current.supplier_available is None and current.supplier_stock == 0:
                    current.supplier_available = False
                row.update(label=_listing_label(current, policy, at), status='listing', observed_at=observed, expires_at=expiry)
            else:
                row['status'] = 'stale'
    return resolved


def apply_availability(payload, products, policy, resolved=None):
    def apply(obj, product):
        obj['availability'] = (resolved[product.code]['label'] if resolved is not None
                               else _listing_label(product, policy, datetime.now(timezone.utc)))
        # Create-only imports keep their review visibility and basket policy.
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
