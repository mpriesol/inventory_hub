"""Bounded, read-only duplicate suggestions. Similarity never authorizes a link."""
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
import re
import unicodedata

from sqlalchemy import and_, func, or_, select, tuple_

from inventory_hub.db_models import IdentifierType, Product, ProductIdentifier, SupplierProduct
from inventory_hub.db_models_ext import ProductSupplySource, ProductVariantAttribute
from inventory_hub.product_editor_models import ProductEditorOverride
from inventory_hub.services.product_identity import VERIFIED_BARCODE_TYPES, verified_barcodes
from inventory_hub.services.product_links import axes, LinkError


MAX_QUERIES = 100
FUZZY_POOL_LIMIT = 500
EXACT_POOL_LIMIT = 5000
REASONS = ('exact_ean', 'exact_sku', 'supplier_sku', 'similar_sku', 'similar_name')


@dataclass(frozen=True)
class CandidateQuery:
    key: str
    sku: str = ''
    eans: tuple[str, ...] = ()
    supplier_id: int | None = None
    supplier_sku: str = ''
    name: str = ''
    brand: str = ''
    attributes: tuple[tuple[str, str], ...] = ()
    exclude_product_id: int | None = None


def _fold(value):
    return ''.join(char for char in unicodedata.normalize('NFKD', value.casefold())
                   if not unicodedata.combining(char))


def _sku(value):
    return re.sub(r'[^a-z0-9]', '', _fold(value))


def _name(value):
    return ' '.join(re.findall(r'\w+', _fold(value)))


_LATIN_FOLD = {chr(code): _fold(chr(code)) for code in range(0x00c0, 0x0250)
               if len(_fold(chr(code))) == 1 and 'a' <= _fold(chr(code)) <= 'z'}


def _sql_fold(value):
    return func.translate(func.replace(func.lower(value), 'ß', 'ss'), ''.join(_LATIN_FOLD), ''.join(_LATIN_FOLD.values()))


def _validate(queries, limit):
    if type(limit) is not int or not 1 <= limit <= 10 or len(queries) > MAX_QUERIES:
        raise ValueError('product_candidates_invalid_request')
    keys = set()
    for query in queries:
        if not isinstance(query, CandidateQuery) or not isinstance(query.key, str) or not 1 <= len(query.key) <= 100 or query.key in keys:
            raise ValueError('product_candidates_invalid_request')
        keys.add(query.key)
        for field, maximum in (('sku', 100), ('supplier_sku', 100), ('name', 500), ('brand', 100)):
            value = getattr(query, field)
            if not isinstance(value, str) or len(value) > maximum or any(ord(char) < 32 for char in value):
                raise ValueError('product_candidates_invalid_request')
        if any(value is not None and (type(value) is not int or value < 1)
               for value in (query.supplier_id, query.exclude_product_id)):
            raise ValueError('product_candidates_invalid_request')
        if not isinstance(query.eans, (tuple, list)) or len(query.eans) > 20 or any(
                not isinstance(value, str) or len(value) > 100 for value in query.eans):
            raise ValueError('product_candidates_invalid_request')
        if not isinstance(query.attributes, (tuple, list)) or len(query.attributes) > 100 or any(
                not isinstance(pair, (tuple, list)) or len(pair) != 2 or any(
                    not isinstance(value, str) or len(value) > maximum
                    for value, maximum in zip(pair, (100, 255))) for pair in query.attributes):
            raise ValueError('product_candidates_invalid_request')


def _fuzzy_terms(query):
    # A single broad SQL pool serves the whole batch. Short/generic searches are
    # omitted; no catalog is loaded into Python for each input row.
    code = _sku(query.sku)
    tokens = sorted({word for word in _name(query.name).split() if len(word) >= 4}, key=lambda value: (-len(value), value))[:2]
    return (code[:min(6, len(code))] if len(code) >= 4 else '', tuple(tokens))


async def _load_cards(db, ids):
    if not ids:
        return {}
    override = ProductEditorOverride.data
    query = select(Product.id, Product.sku, Product.is_active,
        func.coalesce(override['common']['name'].astext, Product.name).label('name'),
        func.coalesce(override['common']['brand'].astext, Product.brand).label('brand'),
        override['variant']['attributes'].label('manual_attributes'), override['variant']['eans'].label('manual_eans'))
    rows = (await db.execute(query.outerjoin(ProductEditorOverride, ProductEditorOverride.product_id == Product.id)
                            .where(Product.id.in_(ids)).order_by(Product.id))).mappings().all()
    eans, attributes = defaultdict(set), defaultdict(list)
    for pid, value in (await db.execute(select(ProductIdentifier.product_id, ProductIdentifier.value).where(
            ProductIdentifier.product_id.in_(ids), ProductIdentifier.identifier_type.in_(VERIFIED_BARCODE_TYPES)))).all():
        eans[pid].update(verified_barcodes((value,)))
    for pid, name, value in (await db.execute(select(ProductVariantAttribute.product_id,
            ProductVariantAttribute.attribute_name, ProductVariantAttribute.attribute_value)
            .where(ProductVariantAttribute.product_id.in_(ids))
            .order_by(ProductVariantAttribute.product_id, ProductVariantAttribute.display_order, ProductVariantAttribute.attribute_name))).all():
        attributes[pid].append({'name': name, 'value': value})
    cards = {}
    for row in rows:
        manual = row['manual_attributes']
        attrs = manual if isinstance(manual, list) else attributes[row['id']]
        cards[row['id']] = {'product_id': row['id'], 'sku': row['sku'], 'name': row['name'],
            'brand': row['brand'], 'is_active': row['is_active'], 'eans': sorted(eans[row['id']]),
            'manual_eans': list(verified_barcodes(row['manual_eans'])) if isinstance(row['manual_eans'], list) else None,
            'attributes': [{'name': item['name'], 'value': item['value']} for item in attrs
                           if isinstance(item, dict) and isinstance(item.get('name'), str) and isinstance(item.get('value'), str)]}
    return cards


def _rank(query, cards, exact, limit, truncated, normalized=None):
    incoming = set(verified_barcodes(query.eans))
    code, name = _sku(query.sku), _name(query.name)
    prefix, tokens = _fuzzy_terms(query)
    normalized = normalized or {pid: (_sku(card['sku']), _name(card['name'])) for pid, card in cards.items()}
    ranked = []
    for pid, card in cards.items():
        if pid == query.exclude_product_id:
            continue
        reasons = set(exact.get(pid, ()))
        score = 0.0
        other, other_name = normalized[pid]
        if not reasons and not (prefix and other.startswith(prefix) or tokens and all(token in other_name for token in tokens)):
            continue
        if len(code) >= 4 and len(other) >= 4:
            ratio = SequenceMatcher(None, code, other, autojunk=False).ratio()
            if ratio >= .8 and 'exact_sku' not in reasons:
                reasons.add('similar_sku'); score = max(score, ratio)
        if len(name) >= 6 and len(other_name) >= 6:
            ratio = SequenceMatcher(None, name, other_name, autojunk=False).ratio()
            if ratio >= .75:
                reasons.add('similar_name'); score = max(score, ratio)
        if not reasons:
            continue
        warnings = []
        if card['is_active'] is False:
            warnings.append('inactive_product')
        if incoming and card['eans'] and not incoming.intersection(card['eans']):
            warnings.append('ean_conflict')
        if incoming and card['manual_eans'] and not incoming.intersection(card['manual_eans']):
            warnings.append('manual_ean_conflict')
        if query.brand and card['brand'] and _fold(query.brand) != _fold(card['brand']):
            warnings.append('brand_difference')
        try:
            expected = axes([{'name': name, 'value': value} for name, value in query.attributes])
            actual = axes(card['attributes'])
            if expected and not actual:
                warnings.append('variant_axes_unknown')
            if any(expected[name] != actual[name] for name in expected.keys() & actual.keys()):
                warnings.append('variant_axes_conflict')
        except LinkError:
            warnings.append('variant_axes_ambiguous')
        ordered = [reason for reason in REASONS if reason in reasons]
        ranked.append(((REASONS.index(ordered[0]), -len(reasons & set(REASONS[:3])), -score, card['sku'], pid),
                       {**card, 'reasons': ordered, 'warnings': warnings}))
    ranked.sort(key=lambda item: item[0])
    return {'candidates': [item[1] for item in ranked[:limit]], 'truncated': truncated or len(ranked) > limit}


async def find_candidates(db, queries: list[CandidateQuery], limit=5):
    """Return advisory candidates keyed by input key; never flush or call a shop.

    Exact evidence has its own bounded pool and always ranks before similarity.
    ``truncated`` also flags an incomplete shared search pool, even if fewer than
    ``limit`` candidates survived ranking. Callers must never read absence as
    proof that a new card is unique. Definitive linking uses the identity guards.
    """
    _validate(queries, limit)
    if not queries:
        return {}
    # A read helper must not implicitly flush unrelated pending ORM changes.
    with db.no_autoflush:
        return await _find_candidates(db, queries, limit)


async def _find_candidates(db, queries, limit):
    exact = {query.key: defaultdict(set) for query in queries}
    barcode_queries, sku_queries, supplier_queries = defaultdict(list), defaultdict(list), defaultdict(list)
    for query in queries:
        for value in verified_barcodes(query.eans):
            barcode_queries[value].append(query.key)
        if query.sku:
            sku_queries[query.sku].append(query.key)
        if query.supplier_id and query.supplier_sku:
            supplier_queries[(query.supplier_id, query.supplier_sku)].append(query.key)
    identifiers = []
    if barcode_queries:
        identifiers.append(and_(ProductIdentifier.identifier_type.in_(VERIFIED_BARCODE_TYPES), ProductIdentifier.value.in_(barcode_queries)))
    if sku_queries:
        identifiers.append(and_(ProductIdentifier.identifier_type == IdentifierType.internal_sku, ProductIdentifier.value.in_(sku_queries)))
    if supplier_queries:
        identifiers.append(and_(ProductIdentifier.identifier_type == IdentifierType.supplier_sku,
            tuple_(ProductIdentifier.supplier_id, ProductIdentifier.value).in_(supplier_queries)))
    exact_truncated = False
    if identifiers:
        matches = (await db.execute(select(ProductIdentifier.product_id, ProductIdentifier.identifier_type,
            ProductIdentifier.value, ProductIdentifier.supplier_id).where(or_(*identifiers))
            .order_by(ProductIdentifier.id).limit(EXACT_POOL_LIMIT + 1))).all()
        exact_truncated = len(matches) > EXACT_POOL_LIMIT
        for pid, kind, value, supplier in matches[:EXACT_POOL_LIMIT]:
            if kind in VERIFIED_BARCODE_TYPES:
                keys, reason = barcode_queries[value], 'exact_ean'
            elif kind == IdentifierType.internal_sku:
                keys, reason = sku_queries[value], 'exact_sku'
            else:
                keys, reason = supplier_queries[(supplier, value)], 'supplier_sku'
            for key in keys:
                exact[key][pid].add(reason)
    if sku_queries:
        for pid, code in (await db.execute(select(Product.id, Product.sku).where(Product.sku.in_(sku_queries)))).all():
            for key in sku_queries[code]:
                exact[key][pid].add('exact_sku')
    if supplier_queries:
        scoped_sources = tuple_(SupplierProduct.supplier_id, SupplierProduct.supplier_sku).in_(supplier_queries)
        source_columns = (Product.id.label('product_id'), SupplierProduct.supplier_id, SupplierProduct.supplier_sku)
        legacy = select(*source_columns).select_from(SupplierProduct).join(Product,
            Product.source_supplier_product_id == SupplierProduct.id).where(scoped_sources)
        linked = select(*source_columns).select_from(SupplierProduct).join(ProductSupplySource,
            ProductSupplySource.supplier_product_id == SupplierProduct.id).join(Product,
            Product.id == ProductSupplySource.product_id).where(scoped_sources)
        source_rows = (await db.execute(legacy.union(linked)
            .order_by('product_id', 'supplier_id', 'supplier_sku').limit(EXACT_POOL_LIMIT + 1))).all()
        exact_truncated |= len(source_rows) > EXACT_POOL_LIMIT
        for pid, supplier, code in source_rows[:EXACT_POOL_LIMIT]:
            for key in supplier_queries[(supplier, code)]:
                exact[key][pid].add('supplier_sku')
    exact_ids = {pid for values in exact.values() for pid in values}
    effective_name = func.coalesce(ProductEditorOverride.data['common']['name'].astext, Product.name)
    compact_sku = func.regexp_replace(_sql_fold(Product.sku), '[^a-z0-9]', '', 'g')
    terms = sorted(set(_fuzzy_terms(query) for query in queries))
    predicates = []
    for prefix, tokens in terms:
        if prefix:
            predicates.append(compact_sku.startswith(prefix, autoescape=True))
        if tokens:
            predicates.append(and_(*[_sql_fold(effective_name).contains(token, autoescape=True) for token in tokens]))
    names = sorted({query.name.lower() for query in queries if len(_name(query.name)) >= 6})
    if names:
        # Preserve literal matches for scripts outside the Latin folding table.
        predicates.append(func.lower(effective_name).in_(names))
    fuzzy_ids = []
    if predicates:
        fuzzy_ids = list(await db.scalars(select(Product.id).outerjoin(ProductEditorOverride,
            ProductEditorOverride.product_id == Product.id).where(or_(*predicates), Product.id.not_in(exact_ids))
            .order_by(Product.id).limit(FUZZY_POOL_LIMIT + 1)))
    fuzzy_truncated = len(fuzzy_ids) > FUZZY_POOL_LIMIT
    cards = await _load_cards(db, sorted(exact_ids | set(fuzzy_ids[:FUZZY_POOL_LIMIT])))
    normalized = {pid: (_sku(card['sku']), _name(card['name'])) for pid, card in cards.items()}
    return {query.key: _rank(query, cards, exact[query.key], limit,
            exact_truncated or (fuzzy_truncated and any(_fuzzy_terms(query))), normalized) for query in queries}
