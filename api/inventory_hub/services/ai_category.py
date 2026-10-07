"""Bounded category selection before content generation, using a frozen tree."""
from decimal import Decimal

from inventory_hub.ai_content_types import Policy, RuleBook, Scope
from inventory_hub.services import ai_content_provider as provider, ai_content_rules as rules
from inventory_hub.services.catalog import CatalogError
from inventory_hub.services.catalog_merchandising import category_chain


def catalog(rows, book, shop, language, selected=None):
    index = {r['code']: r for r in rows}
    parents = {r.get('parent_code') for r in rows}
    choices = []
    for row in rows:
        if not row.get('assignable', True) or row.get('active') is False:
            continue
        chain = category_chain(rows, row['code'])
        codes = [c['code'] for c in chain]
        if row['code'] in parents or selected and selected not in codes:
            continue
        # Inactive branches must not silently become an automatic target.
        if any(index[c].get('active') is False for c in codes):
            continue
        mapped = []
        for code in reversed(codes):
            mapped = [p.id for p in book.categories if p.shop_categories.get(shop) == code
                      or code in p.shop_category_matches.get(shop, [])]
            if mapped:
                break
        choices.append({'code': row['code'], 'path': ' / '.join(
            index[c].get('names', {}).get(language) or next(iter(index[c].get('names', {}).values()), c)
            for c in codes), 'profile_ids': mapped})
    if not choices:
        raise CatalogError('ai_category_candidates_missing', 'No active leaf categories are available', 422)
    return {'choices': choices, 'profiles': [{'id': p.id, 'name': p.name} for p in book.categories]}


def resolve_context(context, book, profile):
    result = rules.resolve(book, Scope(shop=context['shop'], supplier=context['supplier'],
        brand=context['facts'][0].get('brand') or '', category=profile, product=context['code']),
        Policy.model_validate(context.get('category_policy', {})))
    # Existing-shop source limitations remain in force after classification.
    result['instructions'].extend(i for i in context['resolved']['instructions'] if i['id'] == 'shop-source')
    return result


def prepare(context, book, rows, selected=None):
    context['classification_catalog'] = catalog(rows, book, context['shop'], context['options']['language'], selected)
    # Reserve enough for classification plus the largest possible content prompt.
    estimates, cost_gates = [], []
    for profile in book.categories:
        resolved = resolve_context(context, book, profile.id)
        estimates.append(provider.estimate({**context, 'resolved': resolved}))
        cost_gates.append(resolved['policy']['show_cost_estimate'])
    context['resolved']['policy']['show_cost_estimate'] = any(cost_gates)
    context['estimate_usd'] = str(max(estimates, default=Decimal(0)) + provider.estimate(context, 'classification'))


def apply(context, book, output):
    candidates = context['classification_catalog']
    choice = next((c for c in candidates['choices'] if c['code'] == output['category_code']), None)
    profile = output['profile_id']
    if (not output['confident'] or choice is None
            or profile not in {p['id'] for p in candidates['profiles']}
            or choice['profile_ids'] and profile not in choice['profile_ids']):
        raise CatalogError('ai_category_uncertain', 'AI could not select a consistent leaf category and profile', 422)
    resolved = resolve_context(context, book, profile)
    if context.get('source_kind') == 'shop' and any(p['scope'] == 'variant' and p['required']
            for p in (resolved.get('category') or {}).get('parameters', [])):
        raise CatalogError('ai_existing_variant_registry', 'Existing-product preparation requires a parent-only parameter registry', 422)
    options = {**context['options'], 'category_code': output['category_code']}
    return {**context, 'category_profile': profile, 'resolved': resolved, 'options': options,
            'category_selection': {**output, 'path': choice['path']}}
