"""Versioned, read-only identity scope for the known catalog writers.

This is deliberately a parser, not a search for arbitrary words or numbers.
Every accepted container has a known writer schema. Unknown fields, incomplete
lineage and bounded reads fail closed; original documents are hashed elsewhere.
"""
from dataclasses import dataclass, field
from collections import Counter
import hashlib
import json
import re

from pydantic import ValidationError

from inventory_hub.catalog_types import CatalogProduct, CatalogPrices, CatalogParameter, CatalogShopMatch
from inventory_hub.product_import_types import ImportValues
from inventory_hub.services.product_identity import barcode_keys, verified_barcodes


VERSION = 'typed-catalog-scope-v5'
MAX_ITEMS = 20000
MAX_NODES = 400000
MAX_MATCHES = 100
UUID32 = re.compile(r'^[a-f0-9]{32}$')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


class Unverified(ValueError):
    def __init__(self, reason, path=''):
        self.reason, self.path = reason, path
        super().__init__(reason)


def obj(value, allowed=None, required=(), path=''):
    if not isinstance(value, dict) or not set(required) <= value.keys():
        raise Unverified('incomplete_object', path)
    if allowed is not None and value.keys() - set(allowed):
        # Do not expose unknown keys: a raw key can itself contain private text.
        raise Unverified('unknown_schema_field', path)
    return value


def array(value, path='', *, nonempty=False, limit=MAX_ITEMS):
    if not isinstance(value, list) or len(value) > limit or nonempty and not value:
        raise Unverified('incomplete_or_unbounded_array', path)
    return value


def positive(value, path='', *, nullable=False):
    if nullable and value is None:
        return None
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit() and 0 < int(value) <= 9007199254740991:
        return int(value)
    raise Unverified('invalid_identity_number', path)


def code(value, path='', *, blank=False):
    if not isinstance(value, str) or len(value) > 260 or (not blank and not value.strip()):
        raise Unverified('invalid_identity_code', path)
    return value


def shop(value, path=''):
    value = code(value, path)
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,50}', value):
        raise Unverified('invalid_shop_namespace', path)
    return value


def model(model_type, value, path):
    obj(value, model_type.model_fields, path=path)
    try:
        result = model_type.model_validate(value)
    except (ValidationError, ValueError, TypeError):
        raise Unverified('invalid_writer_model', path) from None
    # CatalogProduct and its nested public models ignore extra fields by
    # default. Inspect their raw keys before using any normalized projection.
    for name in ('parameters', 'variant_attributes') if model_type in (CatalogProduct, ImportValues) else ():
        for index, item in enumerate(value.get(name, [])):
            obj(item, CatalogParameter.model_fields, ('name', 'value'), f'{path}/{name}/{index}')
    if model_type is CatalogProduct:
        obj(value.get('prices'), CatalogPrices.model_fields, ('currency',), path + '/prices')
        for index, item in enumerate(value.get('shop_matches', [])):
            obj(item, CatalogShopMatch.model_fields, path=f'{path}/shop_matches/{index}')
    return result


def bounded(value):
    count = 0
    def visit(item, depth):
        nonlocal count
        count += 1
        if count > MAX_NODES or depth > 64:
            raise Unverified('document_node_or_depth_limit')
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise Unverified('invalid_dictionary_key')
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
    visit(value, 0)


@dataclass
class Scope:
    hubs: set
    mapping_ids: set
    skus: set
    barcodes: set
    aliases: set
    sales: set
    parents: set
    native: set
    native_parents: set

    @classmethod
    def build(cls, ids, products, identifiers, mappings, targets):
        skus = {r['sku'].casefold() for r in products}
        skus.update(r['value'].casefold() for r in identifiers if r['type'] == 'sku' and r['value'])
        aliases = {r['value'].casefold() for r in identifiers if r['value']}
        barcodes = barcode_keys([r['value'] for r in identifiers if r['type'] in ('ean', 'upc')])
        sales, parents, native, native_parents = set(), set(), set(), set()
        for row in mappings + targets:
            target = 'native_product_id' in row
            namespace = row['shop_code']
            leaf = row.get('code') if target else row.get('external_code')
            if leaf:
                sales.add((namespace, leaf.casefold()))
            if row.get('parent_code'):
                parents.add((namespace, row['parent_code'].casefold()))
            pid = row.get('native_product_id') if target else row.get('external_product_id')
            vid = row.get('native_variant_id') if target else row.get('external_variant_id')
            if not pid and not row.get('is_variant'):
                pid = row.get('legacy_external_id')
            if pid and str(pid).isdigit():
                native_parents.add((namespace, int(pid)))
                if not row.get('is_variant') or vid and str(vid).isdigit():
                    native.add((namespace, int(pid), int(vid) if row.get('is_variant') else None))
        return cls(set(ids), {r['id'] for r in mappings}, skus, barcodes, aliases,
                   sales, parents, native, native_parents)


@dataclass
class Proof:
    scope: Scope
    matches: list = field(default_factory=list)
    contexts: list = field(default_factory=list)
    queries: list = field(default_factory=list)
    publications: set = field(default_factory=set)
    linked_drafts: set = field(default_factory=set)
    errors: list = field(default_factory=list)
    resolutions: list = field(default_factory=list)
    supplier_pins: list = field(default_factory=list)
    match_count: int = 0
    context_count: int = 0
    match_namespaces: Counter = field(default_factory=Counter)

    def hit(self, path, namespace, value):
        self.match_count += 1
        self.match_namespaces[namespace] += 1
        if len(self.matches) < MAX_MATCHES:
            self.matches.append({'path': path, 'namespace': namespace, 'value': value})

    def context(self, path, reason):
        self.context_count += 1
        if len(self.contexts) < MAX_MATCHES:
            self.contexts.append({'path': path, 'reason': reason})

    def hub(self, value, path):
        value = positive(value, path, nullable=True)
        if value in self.scope.hubs:
            self.hit(path, 'hub_product_id', value)

    def mapping(self, value, path):
        value = positive(value, path, nullable=True)
        if value in self.scope.mapping_ids:
            self.hit(path, 'mapping_id', value)

    def leaf(self, value, namespace, path, *, canonical=False):
        value = code(value, path, blank=True)
        if value and ((namespace, value.casefold()) in self.scope.sales
                      or canonical and value.casefold() in self.scope.skus):
            self.hit(path, 'sale_code' if namespace else 'canonical_sku', value)

    def parent_write(self, namespace, parent, pid, path):
        namespace = shop(namespace, path)
        if parent:
            parent = code(parent, path)
            if (namespace, parent.casefold()) in self.scope.parents or (namespace, parent.casefold()) in self.scope.sales:
                self.hit(path, 'native_parent_write_code', parent)
        if pid is not None:
            pid = positive(pid, path)
            if (namespace, pid) in self.scope.native_parents:
                self.hit(path, 'native_parent_write_id', pid)

    def native_leaf(self, namespace, pid, vid, path):
        namespace = shop(namespace, path)
        pid, vid = positive(pid, path), positive(vid, path, nullable=True)
        if (namespace, pid, vid) in self.scope.native:
            self.hit(path, 'native_variant' if vid else 'native_product', [namespace, pid, vid])

    def eans(self, values, path):
        values = array(values, path, limit=50)
        for value in values:
            code(value, path, blank=True)
            if value and (barcode_keys([value]) & self.scope.barcodes or value.casefold() in self.scope.aliases):
                self.hit(path, 'barcode', value)
            if value and value not in verified_barcodes([value]):
                raise Unverified('invalid_or_compound_barcode', path)

    def importer(self, source, path, *, supplier, targets=(), expected=None, sku_identity=None, item_type='stock',
                 refresh_sku_from_feed=False, mapped_sku_identity=None):
        self.hub(expected, path + '/expected_product_id')
        self.eans(source.eans, path + '/eans')
        self.leaf(source.shop_code, None, path + '/shop_code', canonical=True)
        for target in targets:
            self.leaf(source.shop_code, target, path + '/shop_code')
        self.queries.append({'path': path, 'supplier': supplier, 'code': source.shop_code,
            'supplier_sku': source.code, 'barcodes': list(source.eans), 'expected': expected,
            'sku_identity': sku_identity, 'item_type': item_type,
            'refresh_sku_from_feed': refresh_sku_from_feed, 'mapped_sku_identity': mapped_sku_identity})

    def result(self):
        return {'parser_version': VERSION,
            'classification': 'unknown' if self.errors else 'reference' if self.matches else 'context_only' if self.contexts else 'disjoint',
            'matched_paths': self.matches, 'context_paths': self.contexts, 'problems': self.errors,
            'matched_path_count': self.match_count, 'context_path_count': self.context_count,
            'reference_reason_counts': dict(sorted(self.match_namespaces.items())),
            'projection_truncated': self.match_count > len(self.matches) or self.context_count > len(self.contexts),
            'resolved_identity_hash': digest(self.resolutions),
            'linked_drafts': sorted(self.linked_drafts),
            'publication_journals': [list(x) for x in sorted(self.publications)]}


DRAFT_KEYS = set('feed_key run_id options categories rows publication publications previous_publications events shops targets default_shop target receiving_origin receiving_rows saved_hash automation source_lookup source_checked_at supplier_availability_expires_at supplier_availability_observed_at'.split())
DRAFT_KEYS.add('supplier_id')
ROW_KEYS = set('id group_key group_name is_variant source_category mapping_revision mapping_provenance source values manual_fields provenance hub_product_id canonical_code source_kind receiving_line_ids catalog_product_id expected_hub_product_id sku_preview identity_decision identity_link_only created_in_draft enriched_in_draft editor_revision errors hub_errors warnings ai_job_id ai_baseline ai_policy ai_applied_snapshot source_status source_error'.split())
ROW_KEYS.add('categories')


def draft(proof, raw, path='/document'):
    namespace, supplier = shop(raw.get('shop')), shop(raw.get('supplier'))
    document = obj(raw.get('document'), DRAFT_KEYS, ('rows', 'options'), path)
    if 'supplier_id' in document:
        proof.supplier_pins.append({'supplier': supplier, 'id': positive(document['supplier_id']),
                                    'path': path + '/supplier_id'})
    rows = array(document['rows'], path + '/rows', nonempty=True, limit=500)
    from inventory_hub.catalog_types import ShopImportOptions
    model(ShopImportOptions, document['options'], path + '/options')
    selectors = document.get('shops', [])
    for i, selector in enumerate(array(selectors, path + '/shops', limit=10)):
        obj(selector, {'code', 'name', 'categories', 'error'}, ('code', 'name', 'categories'), path + '/shops')
        shop(selector['code'])
        if selector['code'] not in ('biketrek', 'xtrek') or not isinstance(selector['name'], str):
            raise Unverified('invalid_shop_selector', path + '/shops')
        array(selector['categories'], path + '/shops/categories')
        proof.context(f'{path}/shops/{i}/name', 'known_shop_display_label')
    selected_ids = set()
    for i, row in enumerate(rows):
        at = f'{path}/rows/{i}'
        obj(row, ROW_KEYS, ('id', 'source', 'values', 'group_key', 'is_variant'), at)
        for category in array(row.get('categories', []), at + '/categories'):
            obj(category, {'code', 'main_yn'}, ('code', 'main_yn'), at + '/categories')
            code(category['code'], at + '/categories/code')
            if type(category['main_yn']) is not bool:
                raise Unverified('invalid_taxonomy_projection', at + '/categories')
        source_id = positive(row['id'], at + '/id')
        if source_id in selected_ids:
            raise Unverified('duplicate_source_id', at)
        selected_ids.add(source_id)
        source = model(CatalogProduct, row['source'], at + '/source')
        positive(row['source'].get('id'), at + '/source/id')
        values = model(ImportValues, row['values'], at + '/values')
        if source.id != source_id or source.supplier != supplier or type(row['is_variant']) is not bool:
            raise Unverified('draft_source_lineage_changed', at)
        for key in ('hub_product_id', 'expected_hub_product_id'):
            proof.hub(row.get(key), at + '/' + key)
        for key in ('canonical_code',):
            if row.get(key):
                proof.leaf(row[key], None, at + '/' + key, canonical=True)
        decision = row.get('identity_decision')
        if decision:
            obj(decision, {'action', 'product_id', 'source_hash', 'state_hash', 'link_hash'}, ('action',), at + '/identity_decision')
            if decision['action'] not in ('link', 'create'):
                raise Unverified('unknown_identity_decision', at)
            proof.hub(decision.get('product_id'), at + '/identity_decision/product_id')
        sku = row.get('sku_preview')
        if sku:
            obj(sku, {'status', 'sku', 'product_id', 'brand_id', 'reasons', 'candidate_product_ids'}, ('status',), at + '/sku_preview')
            proof.hub(sku.get('product_id'), at + '/sku_preview/product_id')
            for pid in array(sku.get('candidate_product_ids', []), at + '/sku_preview/candidate_product_ids'):
                proof.hub(pid, at + '/sku_preview/candidate_product_ids')
            if sku.get('sku'):
                proof.leaf(sku['sku'], None, at + '/sku_preview/sku', canonical=True)
        targets = values.target_shops if values.target_shops is not None else [namespace]
        if row.get('identity_link_only') or decision and decision['action'] == 'link':
            targets = []
        # Both the frozen source and the effective importer identity matter.
        # The source numeric ID is never interpreted as a Hub ID.
        proof.importer(source, at + '/source', supplier=supplier, targets=targets)
        from inventory_hub.services.product_import import to_source, mapped_manufacturer_identity
        effective = to_source(row)
        proof.importer(effective, at + '/values', supplier=supplier, targets=targets,
            expected=row.get('hub_product_id') or row.get('expected_hub_product_id'),
            sku_identity=values.sku_identity.model_dump() if values.sku_identity else None, item_type=values.item_type,
            refresh_sku_from_feed=not values.sku_identity or row.get('provenance', {}).get('sku_identity') == 'feed_mapping',
            mapped_sku_identity=mapped_manufacturer_identity(row, supplier))
        if row['is_variant']:
            proof.context(at + '/group_key', 'create_only_family_grouping')
            if source.canonical_parent_code:
                proof.context(at + '/source/canonical_parent_code', 'create_only_family_grouping')
        else:
            for target in targets:
                proof.leaf(row['group_key'], target, at + '/group_key', canonical=True)
        if row.get('ai_baseline'):
            baseline = model(ImportValues, row['ai_baseline'], at + '/ai_baseline')
            proof.importer(to_source({**row, 'values': row['ai_baseline']}), at + '/ai_baseline',
                supplier=supplier, targets=targets, sku_identity=baseline.sku_identity.model_dump() if baseline.sku_identity else None)
    if document.get('receiving_origin') or document.get('receiving_rows'):
        # Receiving has its own complete raw+enriched proof. An import draft is
        # active executable intent, never an invoice-only preservation exception.
        origin = obj(document.get('receiving_origin'), path=path + '/receiving_origin')
        lineage = obj(document.get('receiving_rows'), path=path + '/receiving_rows')
        if set(lineage) != {str(i) for i in selected_ids} or not origin:
            raise Unverified('receiving_lineage_unverified', path)
        for key, value in lineage.items():
            obj(value, {'line_ids', 'catalog_product_id', 'existing_product_id'}, ('line_ids',), path + '/receiving_rows')
            proof.hub(value.get('existing_product_id'), path + '/receiving_rows/' + key)
    lookup = document.get('source_lookup')
    if lookup and obj(lookup).get('state') not in ('completed', 'skipped'):
        raise Unverified('draft_source_still_executable', path + '/source_lookup')
    publications = document.get('publications')
    if publications is None:
        publications = {namespace: document['publication']} if document.get('publication') else {}
    obj(publications, path=path + '/publications')
    for target, preview in publications.items():
        publication(proof, preview, shop(target), path + '/publications/' + target)
    # Earlier importer intents retain their own durable journal even after a
    # fresh preview replaces them; the global journal scanner also sees them.
    for old in array(document.get('previous_publications', []), path + '/previous_publications'):
        obj(old, {'publication', 'publications', 'previews', 'reason', 'at'}, path=path + '/previous_publications')
        for target, identifier in obj(old.get('previews', {})).items():
            if not isinstance(identifier, str) or not UUID32.fullmatch(identifier):
                raise Unverified('previous_publication_id_invalid', path)
            proof.publications.add((shop(target), identifier))
        past = old.get('publications')
        if past is None:
            past = {namespace: old['publication']} if old.get('publication') else {}
        for target, preview in obj(past).items():
            publication(proof, preview, shop(target), path + '/previous_publications')
    return rows


def publication(proof, preview, namespace, path):
    obj(preview, path=path)
    preview_id = preview.get('preview_id')
    if not isinstance(preview_id, str) or not UUID32.fullmatch(preview_id) or preview.get('shop') != namespace:
        raise Unverified('publication_lineage_unverified', path)
    proof.publications.add((namespace, preview_id))


def payload(proof, value, namespace, path, *, parent_update=False, pid=None):
    # This allowlist contains fields emitted by the reviewed create/content
    # writers. Financial values remain opaque and are never returned or changed.
    fields = set('code product_id variant_id product_code parent_code ean code_supplier descriptions variants manufacturer categories images parameters parameters_new prices vats metas active_yn archived_yn main_yn unit availability availability_id weight related products_related related_products accessory_products alternative_products labels flags position can_add_to_basket_yn adult_yn stock text option_id'.split())
    value = obj(value, fields, ('code',), path)
    parent = code(value['code'], path + '/code')
    children = array(value.get('variants', []), path + '/variants')
    if parent_update:
        proof.parent_write(namespace, parent, pid if pid is not None else value.get('product_id'), path)
    elif children:
        proof.context(path + '/code', 'create_only_parent_container')
    else:
        proof.leaf(parent, namespace, path + '/code')
    if value.get('ean'):
        proof.eans([value['ean']], path + '/ean')
    seen = set()
    for i, child in enumerate(children):
        at = f'{path}/variants/{i}'
        obj(child, fields | {'image'}, ('code',), at)
        if 'image' in child:
            # catalog_import.build_item emits one image reference per variant.
            # It cannot carry arbitrary nested identity or update instructions.
            image = obj(child['image'], {'url'}, ('url',), at + '/image')
            if not isinstance(image['url'], str) or not image['url'].strip():
                raise Unverified('invalid_variant_image_reference', at + '/image/url')
        leaf_code = code(child['code'], at + '/code')
        if leaf_code in seen:
            raise Unverified('duplicate_payload_leaf', at)
        seen.add(leaf_code)
        proof.leaf(leaf_code, namespace, at + '/code')
        if child.get('ean'):
            proof.eans([child['ean']], at + '/ean')
        if child.get('variant_id'):
            proof.native_leaf(namespace, pid or value.get('product_id'), child['variant_id'], at)
    return seen or {parent}


def consolidation(proof, raw, path='/document'):
    document = obj(raw.get('document'), {'request', 'local', 'native', 'identity', 'dependencies', 'plan', 'created_by'},
                   ('request', 'local', 'plan'), path)
    from inventory_hub.product_consolidation_types import ProductConsolidationPreviewRequest
    request = model(ProductConsolidationPreviewRequest, document['request'], path + '/request')
    proof.hub(request.keeper_product_id, path + '/request/keeper_product_id')
    for pid in request.retired_product_ids:
        proof.hub(pid, path + '/request/retired_product_ids')
    from inventory_hub.services import product_consolidation_supplier_identity as supplier_identity
    enabled = request.use_keeper_supplier_gtin
    allowed_local = {'products', 'product_hash', 'identifiers', 'mappings', 'shops'}
    if enabled:
        allowed_local.add(supplier_identity.LOCAL_KEY)
    local = obj(document['local'], allowed_local, ('products', 'mappings', 'shops'), path + '/local')
    if enabled:
        identity = obj(document.get('identity'), {'verified_gtin_keys', 'physical_axes',
            'same_complete_retail_unit_factor', supplier_identity.PROOF_KEY},
            ('verified_gtin_keys', 'physical_axes', 'same_complete_retail_unit_factor', supplier_identity.PROOF_KEY),
            path + '/identity')
        try:
            supplier_proof = supplier_identity.validate_frozen(document['request'], local, document['native'], identity)
        except (KeyError, TypeError, ValueError, ValidationError, supplier_identity.links.LinkError):
            raise Unverified('consolidation_supplier_proof_unverified', path + '/identity/' + supplier_identity.PROOF_KEY) from None
        source = supplier_proof['source']
        proof.supplier_pins.append({'supplier': source['supplier_code'], 'id': source['supplier_id'],
                                   'path': path + '/identity/' + supplier_identity.PROOF_KEY + '/source'})
        proof.eans(source['eans'], path + '/identity/' + supplier_identity.PROOF_KEY + '/source/eans')
        if source['supplier_sku'].casefold() in proof.scope.aliases:
            proof.hit(path + '/identity/' + supplier_identity.PROOF_KEY + '/source/supplier_sku',
                      'supplier_sku', source['supplier_sku'])
        for item in supplier_proof['registered_identifiers']:
            proof.hub(item['product_id'], path + '/identity/' + supplier_identity.PROOF_KEY + '/registered_identifiers/product_id')
            proof.eans([item['value']], path + '/identity/' + supplier_identity.PROOF_KEY + '/registered_identifiers/value')
    elif supplier_identity.PROOF_KEY in (document.get('identity') or {}):
        raise Unverified('consolidation_supplier_proof_without_optin', path + '/identity')
    from inventory_hub.services.product_consolidation import _plan
    try:
        if _plan(document['request'], local) != document['plan']:
            raise Unverified('consolidation_plan_lineage_changed', path + '/plan')
    except (KeyError, ValueError):
        raise Unverified('consolidation_plan_lineage_changed', path + '/plan') from None
    shops = {r['id']: r['code'] for r in array(local['shops'])}
    for item in array(local['products']):
        proof.hub(item.get('id'), path + '/local/products/id')
        proof.leaf(item.get('sku', ''), None, path + '/local/products/sku', canonical=True)
    for mapping in array(local['mappings']):
        proof.mapping(mapping.get('id'), path + '/local/mappings/id')
        proof.hub(mapping.get('product_id'), path + '/local/mappings/product_id')
        namespace = mapping.get('shop_code') or shops.get(mapping.get('shop_id'))
        proof.leaf(mapping.get('external_code') or '', shop(namespace), path + '/local/mappings/external_code')
        pid = mapping.get('external_product_id') or (mapping.get('legacy_external_id') if not mapping.get('is_variant') else None)
        if pid and (not mapping.get('is_variant') or mapping.get('external_variant_id')):
            proof.native_leaf(namespace, pid, mapping.get('external_variant_id') if mapping.get('is_variant') else None, path + '/local/mappings')
    for action in request.mapping_actions:
        proof.mapping(action.mapping_id, path + '/request/mapping_actions/mapping_id')
    targets = [a.target for a in request.mapping_actions if a.target] + request.additional_sales
    for target in targets:
        proof.leaf(target.code, target.shop_code, path + '/request/targets/code')
        proof.native_leaf(target.shop_code, target.native_product_id, target.native_variant_id, path + '/request/targets')
    # All targets/old absence claims originate in this complete frozen request
    # and local snapshot. A parent container alone is not a sibling's owner.
    proof.context(path + '/plan', 'frozen_mapping_plan_same_request_and_local_scope')
