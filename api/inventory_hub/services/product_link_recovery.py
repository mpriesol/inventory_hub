"""Explicit recovery evidence for ordinary links; never a general identity override.

The caller holds the shared identity lock while preparing/revalidating local
proofs. Remote observations are completed before that lock. These helpers do
not change canonical identifiers, quantities, history or remote products.
"""
from collections import defaultdict
from copy import deepcopy

from sqlalchemy import or_, select

from inventory_hub.db_models import IdentifierType, ProductIdentifier, Shop, SupplierProduct
from inventory_hub.db_models_ext import ProductSupplySource, ShopProduct, ShopProductContent
from inventory_hub.services import product_links as links, sale_units


def pin_target(review, remote, mapping):
    if (mapping is None or mapping.id != review['mapping_id'] or remote is None
            or remote['parent_id'] != str(review['native_product_id'])
            or remote['is_variant'] != (review['native_variant_id'] is not None)
            or remote['external_id'] != str(review['native_variant_id'] if remote['is_variant']
                                           else review['native_product_id'])):
        raise links.LinkError('product_links_recovery_pin_changed')


def ordinary(mapping):
    try:
        return mapping.shared_stock is False and not sale_units.mapping_fields(mapping)
    except sale_units.SaleUnitError:
        return False


def replacement_identity_error(facts, remote, previous, review):
    """Only the explicit reviewed mode changes the default exact-set rule."""
    canonical = set(links.verified_barcodes(facts['eans']))
    effective = set(links.verified_barcodes(facts['manual_eans'] if facts['manual_eans'] is not None
                                          else facts['eans']))
    incoming = set(links.verified_barcodes(remote['eans']))
    if remote.get('invalid_barcodes'):
        return 'product_links_replacement_ean_mismatch'
    mode = review['request']['mode']
    if mode == 'verified_barcode_subset':
        if incoming and incoming <= canonical and incoming <= effective:
            return None
        return 'product_links_replacement_ean_mismatch'
    if (mode == 'exact_sku_without_barcode' and not canonical and not effective and not incoming
            and previous['external_code'] == remote['code'] == facts['sku'] and review.get('source')):
        return None
    return 'product_links_replacement_identity_unproven'


async def _content(db, shop_id, code):
    return await db.scalar(select(ShopProductContent).where(ShopProductContent.shop_id == shop_id,
        ShopProductContent.external_code == code).with_for_update().execution_options(populate_existing=True))


def content_snapshot(row):
    if row is None:
        return None
    if not isinstance(row.data, dict):
        raise links.LinkError('product_links_recovery_content_conflict')
    return {'id': row.id, 'external_code': row.external_code, 'data_hash': links.digest(row.data),
            'pulled_at': row.pulled_at.isoformat()}


async def _reviewed_source(db, shop, product_id, facts, reference, *, without_barcode):
    if reference['kind'] == 'shop_mapping':
        mapping = await db.scalar(select(ShopProduct).where(ShopProduct.id == reference['id'])
            .with_for_update().execution_options(populate_existing=True))
        if (mapping is None or mapping.shop_id == shop.id or mapping.product_id != product_id
                or not mapping.is_listed or not ordinary(mapping) or mapping.external_code != facts['sku']):
            raise links.LinkError('product_links_recovery_evidence_changed')
        other_shop = await db.get(Shop, mapping.shop_id, populate_existing=True)
        try:
            links.source._native_id(mapping.external_product_id)
            if mapping.is_variant:
                links.source._native_id(mapping.external_variant_id)
                links.exact_code(mapping.parent_code)
            elif mapping.external_variant_id is not None or mapping.parent_code is not None:
                raise ValueError('Standalone evidence must retain its exact native kind')
        except (ValueError, TypeError, links.source.SourceError):
            raise links.LinkError('product_links_recovery_evidence_changed') from None
        if other_shop is None or not other_shop.is_active or other_shop.platform != 'upgates':
            raise links.LinkError('product_links_recovery_evidence_changed')
        parent_code = mapping.parent_code if mapping.is_variant else mapping.external_code
        content = await _content(db, mapping.shop_id, parent_code)
        remote = {'code': mapping.external_code, 'parent_code': parent_code, 'is_variant': mapping.is_variant,
                  'parent_id': mapping.external_product_id, 'external_id': mapping.native_id}
        _check_parent_content(content, mapping.external_product_id, parent_code, [remote])
        if content is not None:
            known_kind = content.data.get('variants_exists_yn')
            metadata = content.data.get('_hub_link_metadata', {}).get(links._metadata_key(remote))
            if ((type(known_kind) is bool and known_kind != mapping.is_variant) or content.data.get('set_yn') is True
                    or metadata is not None and any(metadata.get(key) != value
                                                   for key, value in links.remote_identity(remote).items())):
                raise links.LinkError('product_links_recovery_evidence_changed')
        proof = {'kind': 'shop_mapping', 'product_id': product_id, 'shop_id': mapping.shop_id,
                 'mapping': links._mapping(mapping), 'shared_stock': False, 'sale_to_stock_factor': '1',
                 'content': content_snapshot(content)}
    else:
        row = await db.scalar(select(SupplierProduct).where(SupplierProduct.id == reference['id'])
            .with_for_update(read=True).execution_options(populate_existing=True))
        owners = list(await db.scalars(select(ProductSupplySource).where(
            ProductSupplySource.supplier_product_id == reference['id']).with_for_update()
            .execution_options(populate_existing=True)))
        if (row is None or not row.is_active or len(owners) != 1 or not owners[0].is_active
                or owners[0].product_id != product_id or without_barcode and links.verified_barcodes((row.ean,))):
            raise links.LinkError('product_links_recovery_evidence_changed')
        aliases = list(await db.scalars(select(ProductIdentifier).where(
            ProductIdentifier.identifier_type == IdentifierType.supplier_sku,
            ProductIdentifier.supplier_id == row.supplier_id, ProductIdentifier.value == row.supplier_sku)
            .with_for_update(read=True).execution_options(populate_existing=True)))
        if not aliases or {item.product_id for item in aliases} != {product_id}:
            raise links.LinkError('product_links_recovery_evidence_changed')
        proof = {'kind': 'supplier_product', 'product_id': product_id, 'id': row.id,
                 'supplier_id': row.supplier_id, 'supplier_sku': row.supplier_sku,
                 'manufacturer_sku': row.manufacturer_sku, 'ean': row.ean, 'name': row.name,
                 'brand': row.brand, 'group_code': row.supplier_group_code,
                 'attributes_hash': links.digest(row.attributes), 'source_id': owners[0].id,
                 'source_pack_size': owners[0].pack_size,
                 'identifier_ids': sorted(item.id for item in aliases)}
    return {**proof, 'source_hash': links.digest(proof)}


async def reviewed_replacements(db, shop, facts, manual, choices, replacements, *, expected=None, errors=None):
    result = {}
    for pid, previous in replacements.items():
        request = manual.get(pid, {}).get('reviewed_deleted_replacement')
        if request is None:
            continue
        try:
            # The pinned map and fresh native target were checked before the
            # normal deletion/history guards. Evidence is also frozen locally.
            reference = request.get('evidence_source')
            proof = await _reviewed_source(db, shop, pid, facts[pid], reference,
                without_barcode=request['mode'] == 'exact_sku_without_barcode') if reference else None
            result[pid] = {'request': request, 'source': proof}
        except links.LinkError as error:
            if errors is None:
                raise
            errors[pid] = error.code
    if expected is not None and result != expected:
        raise links.LinkError('product_links_recovery_evidence_changed')
    return result


def _parent_occupancy(requests, choices, observed):
    for pid, request in requests.items():
        remote = choices[pid][0]
        if remote is None:
            raise links.LinkError('product_links_recovery_pin_changed')
        code = remote['parent_code']
        matches = [row for row in observed.get('occupied_codes', []) if row['code'].casefold() == code.casefold()]
        if (len(matches) != 1 or matches[0]['code'] != code or matches[0]['kind'] != 'parent'
                or matches[0]['native_id'] != str(request['native_product_id'])):
            raise links.LinkError('product_links_parent_code_conflict')


def _check_parent_content(row, parent_id, parent_code, remotes, *, legacy=False):
    snapshot = content_snapshot(row)
    if row is None:
        return snapshot
    data = row.data
    if (data.get('product_id') is not None and str(data['product_id']) != parent_id
            or data.get('code') not in ((None, '') if legacy else (None, '', parent_code))):
        raise links.LinkError('product_links_recovery_content_conflict')
    variants = data.get('variants', [])
    if variants is not None and not isinstance(variants, list):
        raise links.LinkError('product_links_recovery_content_conflict')
    for remote in remotes:
        found = [item for item in variants or [] if isinstance(item, dict) and item.get('code') == remote['code']]
        if len(found) > 1 or any(item.get('variant_id') is not None
                               and str(item['variant_id']) != remote['external_id'] for item in found):
            raise links.LinkError('product_links_recovery_content_conflict')
    metadata = data.get('_hub_link_metadata', {})
    if not isinstance(metadata, dict) or any(not isinstance(item, dict) or item.get('parent_id') != parent_id
                                           for item in metadata.values()):
        raise links.LinkError('product_links_recovery_content_conflict')
    return snapshot


async def parent_completions(db, shop, facts, manual, choices, observed, *, expected=None, errors=None):
    requests = {pid: item['complete_unverified_parent'] for pid, item in manual.items()
                if item.get('complete_unverified_parent')}
    if not requests:
        return {}
    parents = {item['expected_parent_code'] for item in requests.values()}
    rows = list(await db.scalars(select(ShopProduct).where(ShopProduct.shop_id == shop.id,
        or_(ShopProduct.parent_code.in_(parents),
            ShopProduct.is_variant.is_(False) & ShopProduct.external_code.in_(parents)))
        .order_by(ShopProduct.id).with_for_update().execution_options(populate_existing=True)))
    families, selected = defaultdict(list), defaultdict(dict)
    for row in rows:
        families[row.parent_code if row.is_variant else row.external_code].append(row)
    for pid, request in requests.items():
        selected[request['expected_parent_code']][pid] = request
    result = {}
    for old_parent, family_requests in selected.items():
        try:
            family = families[old_parent]
            if not family or {row.product_id for row in family} != set(family_requests) or len(family) != len(family_requests):
                raise links.LinkError('product_links_parent_family_incomplete')
            remotes = [choices[row.product_id][0] for row in family]
            _parent_occupancy(family_requests, choices, observed)
            if len({(remote['parent_id'], remote['parent_code']) for remote in remotes}) != 1:
                raise links.LinkError('product_links_parent_family_changed')
            parent_id, parent_code = remotes[0]['parent_id'], remotes[0]['parent_code']
            if old_parent not in {row.external_code for row in family} or parent_code in {row.external_code for row in family}:
                raise links.LinkError('product_links_parent_fallback_unproven')
            for mapping, remote in zip(family, remotes):
                pid, request = mapping.product_id, family_requests[mapping.product_id]
                pin_target(request, remote, mapping)
                canonical = set(links.verified_barcodes(facts[pid]['eans']))
                effective = set(links.verified_barcodes(facts[pid]['manual_eans'])) if facts[pid]['manual_eans'] is not None else canonical
                if (not mapping.is_listed or not mapping.is_variant or not ordinary(mapping)
                        or mapping.external_product_id is not None or mapping.external_variant_id is not None
                        or mapping.external_code != remote['code'] or not remote['is_variant']
                        or not canonical or canonical != set(remote['eans']) or effective != canonical
                        or remote.get('invalid_barcodes')):
                    raise links.LinkError('product_links_parent_completion_not_allowed')
                await links._require_recovery_order_idle(db, shop, pid, 'product_links_identity_in_use')
                await links._require_mapping_write_available(db, shop, mapping,
                    {old_parent, parent_code, remote['code']}, 'product_links_identity_in_use')
            old_content = await _content(db, shop.id, old_parent)
            new_content = await _content(db, shop.id, parent_code)
            content = {'previous': _check_parent_content(old_content, parent_id, old_parent, remotes, legacy=True),
                       'current': _check_parent_content(new_content, parent_id, parent_code, remotes)}
            for mapping in family:
                result[mapping.product_id] = {'request': family_requests[mapping.product_id],
                    'previous_mapping': links._mapping(mapping), 'family_product_ids': sorted(family_requests),
                    'content': content}
        except links.LinkError as error:
            if errors is None:
                raise
            errors.update({pid: error.code for pid in family_requests})
    if expected is not None and result != expected:
        raise links.LinkError('product_links_recovery_evidence_changed')
    return result


def require_complete_selection(rows, chosen):
    for row in rows:
        completion = row.get('parent_completion') or row.get('parent_rename')
        if completion and not set(completion['family_product_ids']) <= set(chosen):
            raise links.LinkError('product_links_parent_family_incomplete', 422)


async def archive_parent_content(db, shop_id, completions):
    archives, seen = [], set()
    for completion in completions.values():
        old_code = completion['request']['expected_parent_code']
        if old_code in seen:
            continue
        seen.add(old_code)
        row = await _content(db, shop_id, old_code)
        if content_snapshot(row) != completion['content']['previous']:
            raise links.LinkError('product_links_recovery_evidence_changed')
        if row is not None:
            archives.append({'content_id': row.id, 'external_code': old_code,
                             'previous_parent_id': row.data.get('product_id'),
                             'parent_id': str(completion['request']['native_product_id']),
                             'reason': 'complete_unverified_parent', 'data': deepcopy(row.data),
                             'pulled_at': row.pulled_at.isoformat()})
            # Retain the original bytes only in the durable operation archive;
            # a fallback key must no longer offer a stale complete family.
            row.data = {}
    return archives
