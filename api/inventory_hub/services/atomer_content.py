"""Atomer source preparation for the existing selected-field publication journal.

This workflow updates three parent description fields on explicitly linked
xTrek cards. It does not allocate products, link cards, or publish inventory.
"""
from copy import deepcopy
from datetime import timedelta

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from inventory_hub.adapters.atomer_content import AtomerSourceError, source_values
from inventory_hub.db_models import Product, Shop
from inventory_hub.db_models_ext import ShopProduct, ShopProductContent
from inventory_hub.product_editor_models import ProductEditorOverride, ProductEditorPublication
from inventory_hub.services import atomer_content_source as transport
from inventory_hub.services import product_editor as editor, product_publication as publications
from inventory_hub.services.product_identity import RemoteIdentity, load_identity_index


WORKFLOW = 'atomer_content'
ERROR = editor.EditorError


async def history(db):
    records = list(await db.scalars(select(ProductEditorPublication)
        .where(ProductEditorPublication.document['workflow'].astext == WORKFLOW)
        .order_by(ProductEditorPublication.created_at.desc()).limit(100)))
    return {'items': [publications.summary(record) for record in records]}


async def _replay(db, identifier, input_hash):
    record = await db.scalar(select(ProductEditorPublication)
        .where(ProductEditorPublication.id == identifier).execution_options(populate_existing=True))
    if record is None:
        return None
    if record.document.get('workflow') != WORKFLOW or record.document.get('input_hash') != input_hash:
        raise ERROR('atomer_content_request_changed')
    return publications.summary(record)


async def family_facts(db, shop_id, remote, anchor_id):
    """The anchor is not a substitute for every affected physical identity."""
    parent = remote['parent']
    variants = parent.get('variants') or []
    leaves = variants or [parent]
    identities = [RemoteIdentity(shop_id=shop_id, code=leaf['code'], is_variant=bool(variants),
        external_id=str(leaf['variant_id'] if variants else parent['product_id']),
        external_product_id=str(parent['product_id']), parent_code=parent['code'],
        barcodes=(leaf.get('ean') or '',)) for leaf in leaves]
    index = await load_identity_index(db, shop_id, identities)
    facts, seen = [], set()
    for identity in identities:
        resolved = index.resolve(identity)
        if resolved.status != 'mapped' or resolved.product_id in seen:
            raise ERROR('atomer_content_family_mapping_required', reasons=resolved.reasons)
        seen.add(resolved.product_id)
        product = index.products[resolved.product_id]
        mappings = index.by_product[product.id]
        if len(mappings) != 1:
            raise ERROR('atomer_content_family_mapping_changed')
        mapping = mappings[0]
        if (not mapping.is_listed or mapping.external_code != identity.code
                or mapping.external_product_id != identity.external_product_id
                or mapping.native_id != identity.external_id
                or mapping.is_variant != identity.is_variant
                or (mapping.parent_code or mapping.external_code) != parent['code']):
            raise ERROR('atomer_content_family_mapping_changed')
        override = await db.scalar(select(ProductEditorOverride)
            .where(ProductEditorOverride.product_id == product.id).execution_options(populate_existing=True))
        effective_brand = ((override.data.get('common') or {}).get('brand') if override else None) or product.brand
        brand = ' '.join(str(effective_brand or '').casefold().split())
        remote_brand = ' '.join(str(parent.get('manufacturer') or '').casefold().split())
        if not brand or not remote_brand or brand != remote_brand:
            raise ERROR('atomer_content_hub_brand_conflict')
        facts.append({'product_id': product.id, 'sku': product.sku, 'brand': effective_brand,
            'active': product.is_active, 'eans': sorted(index.product_barcodes.get(product.id, ())),
            'mapping_id': mapping.id, 'code': mapping.external_code, 'parent_code': mapping.parent_code,
            'product_native_id': mapping.external_product_id, 'variant_native_id': mapping.external_variant_id,
            'revision': override.revision if override else 0})
    if anchor_id not in seen:
        raise ERROR('atomer_content_anchor_changed')
    # An additional stale mapping to this family must not be silently omitted.
    mapped_ids = set(await db.scalars(select(ShopProduct.product_id).where(ShopProduct.shop_id == shop_id,
        func.lower(func.coalesce(func.nullif(ShopProduct.parent_code, ''), ShopProduct.external_code)) == parent['code'].lower(),
        ShopProduct.is_listed.is_(True))))
    if mapped_ids != seen:
        raise ERROR('atomer_content_family_mapping_changed')
    return sorted(facts, key=lambda value: value['product_id'])


async def validate_local(db, record, document):
    facts = await family_facts(db, record.shop_id, document['remote_before'], record.product_id)
    if editor._digest(facts) != document['family_signature']:
        raise ERROR('atomer_content_local_changed')


def protected_changes(remote, document):
    observed = transport.protected_observation(remote, document['fields'])
    baseline = document['protected_before']
    return {key: {'before': baseline[key], 'after': observed[key]}
            for key in ('content', 'operations') if observed[key] != baseline[key]}


async def preview(db, item, snapshot):
    identifier = str(item.request_id)
    inputs = {'source_sha256': snapshot['sha256'],
        'hub_sku': item.hub_sku, 'source_code': item.source_code, 'fields': sorted(item.fields)}
    if 'parameters' in item.fields:
        inputs['parameter_names'] = snapshot.get('parameter_names', {})
    if snapshot.get('formatting', 'original') != 'original':
        inputs['formatting'] = snapshot['formatting']
    input_hash = editor._digest(inputs)
    prior = await _replay(db, identifier, input_hash)
    if prior is not None:
        return prior
    group = snapshot['groups'].get(item.source_code)
    if group is None:
        raise ERROR('atomer_content_source_missing', 422)
    try:
        text_fields = [key for key in item.fields if key in transport.TEXT_FIELDS]
        values = source_values(group, text_fields) if text_fields else {}
        if snapshot.get('formatting') == 'upgates' and 'long_description' in values:
            from inventory_hub.services.atomer_parameters import format_for_upgates
            values['long_description'] = format_for_upgates(values['long_description'])
    except AtomerSourceError as exc:
        raise ERROR(exc.code, 422) from None
    products = list(await db.scalars(select(Product).where(func.lower(Product.sku) == item.hub_sku.lower())))
    if len(products) != 1 or products[0].sku != item.hub_sku:
        raise ERROR('atomer_content_hub_identity_required', 422)
    product_id = products[0].id
    row = await editor.detail(db, product_id)
    shop, identity = publications.target(row, 'xtrek')
    if 'parameters' in item.fields:
        identity['parameters_yn'] = True
    shop_id = await db.scalar(select(Shop.id).where(Shop.code == 'xtrek', Shop.is_active.is_(True)))
    if shop_id is None:
        raise ERROR('atomer_content_shop_missing', 422)
    await publications._assert_mapping(db, row, shop, shop_id)
    await publications._inflight(db, product_id, shop_id)
    frozen_fingerprint = publications.fingerprint('xtrek')
    await db.rollback()
    try:
        remote = await transport.read_remote('xtrek', identity, frozen_fingerprint)
        evidence = transport.verify_source(group, remote)
        if 'parameters' in item.fields:
            from inventory_hub.services.atomer_parameters import source_parameters
            values['parameters'] = source_parameters(group, snapshot.get('parameter_names', {}),
                                                     remote['parent']['parameters'])
        patch = transport.build_patch(values, remote, item.fields)
        # The first pilot requires an observed, restorable original value for
        # every selected field. Unknown/null/empty originals need a separate
        # explicit clearing policy; do not invent one during migration.
        transport.build_patch(patch['before'], remote, item.fields)
    except transport.source.SourceError as exc:
        raise ERROR(exc.code, exc.status) from None
    except AtomerSourceError as exc:
        raise ERROR(exc.code, 422) from None
    facts = await family_facts(db, shop_id, remote, product_id)
    signature = editor._digest(facts)
    await db.rollback()
    # Replay and source facts are checked under the same identity lock as linking.
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': editor.IDENTITY_WRITE_LOCK})
    prior = await _replay(db, identifier, input_hash)
    if prior is not None:
        return prior
    if editor._digest(await family_facts(db, shop_id, remote, product_id)) != signature:
        raise ERROR('atomer_content_local_changed')
    from inventory_hub.services.merchandising_write_guard import require_target_available
    from inventory_hub.services.catalog import CatalogError
    try:
        await require_target_available(db, 'xtrek', remote['identity']['parent_code'])
    except CatalogError as exc:
        raise ERROR('product_publication_inflight', exc.status) from None
    no_op = transport.compare_values(patch['before'], patch['after'])
    document = {'workflow': WORKFLOW, 'shop_code': 'xtrek', 'fields': list(item.fields),
        'revision': row['revision'], 'identity': remote['identity'], 'fingerprint': frozen_fingerprint,
        'options': None, 'expires_at': (editor.now() + timedelta(minutes=30)).isoformat(),
        'input_hash': input_hash, 'family_signature': signature, 'family_facts': facts,
        'source_code': item.source_code, 'hub_sku': item.hub_sku, 'source_sha256': snapshot['sha256'],
        'source': {'size_bytes': snapshot['size_bytes'], 'total_rows': snapshot['total_rows'],
                   'group': deepcopy(group), 'identity_evidence': evidence,
                   'parameter_names': deepcopy(snapshot.get('parameter_names', {})),
                   'formatting': snapshot.get('formatting', 'original')},
        'remote_before': remote, 'protected_before': transport.protected_observation(remote, item.fields),
        'no_op': no_op, 'observed': patch['before'] if no_op else None, **patch}
    record = ProductEditorPublication(id=identifier, product_id=product_id, shop_id=shop_id,
        state='completed' if no_op else 'ready', document=document)
    db.add(record)
    await db.commit()
    return publications.summary(record)


async def record_receipt(db, document, remote):
    # Source content is not a manual editor override. The publication is the
    # permanent receipt; refresh only the observed shop cache after verification.
    parent = remote['parent']
    shop_id = await db.scalar(select(Shop.id).where(Shop.code == 'xtrek'))
    await db.execute(insert(ShopProductContent).values(shop_id=shop_id, external_code=parent['code'],
        data=parent, pulled_at=editor.now()).on_conflict_do_nothing(
            index_elements=[ShopProductContent.shop_id, ShopProductContent.external_code]))
    cached = await db.scalar(select(ShopProductContent).where(ShopProductContent.shop_id == shop_id,
        ShopProductContent.external_code == parent['code']).with_for_update().execution_options(populate_existing=True))
    previous = cached.data if isinstance(cached.data, dict) else {}
    if previous.get('product_id') is not None and str(previous['product_id']) != str(parent['product_id']):
        document['cache_refresh_skipped'] = 'cached_native_identity_changed'
        return
    metadata = previous.get('_hub_link_metadata')
    if isinstance(metadata, dict):
        leaves = parent.get('variants') or [parent]
        expected = {(leaf['code'], str(leaf.get('variant_id') or parent['product_id'])) for leaf in leaves}
        if any(not isinstance(value, dict) or str(value.get('parent_id')) != str(parent['product_id'])
               or (value.get('code'), str(value.get('external_id'))) not in expected for value in metadata.values()):
            document['cache_refresh_skipped'] = 'cached_link_identity_changed'
            return
    merged = {**deepcopy(previous), **deepcopy(parent)}
    previous_variants = previous.get('variants')
    old_variants = {(value.get('code'), value.get('variant_id')): value
                    for value in (previous_variants if isinstance(previous_variants, list) else [])
                    if isinstance(value, dict)}
    if isinstance(parent.get('variants'), list):
        merged['variants'] = [{**deepcopy(old_variants.get((value['code'], value['variant_id']), {})), **deepcopy(value)}
                              for value in parent['variants']]
    cached.data, cached.pulled_at = merged, editor.now()


async def rollback_preview(db, original_id, request_id):
    original = await publications._get(db, original_id)
    frozen = deepcopy(original.document)
    if (frozen.get('workflow') != WORKFLOW or original.state != 'completed'
            or frozen.get('no_op') or frozen.get('rollback_of')):
        raise ERROR('atomer_content_rollback_unavailable')
    input_hash = editor._digest({'rollback_of': original_id, 'original_input_hash': frozen['input_hash']})
    prior = await _replay(db, request_id, input_hash)
    if prior is not None:
        return prior
    product_id, shop_id = original.product_id, original.shop_id
    if publications.fingerprint('xtrek') != frozen['fingerprint']:
        raise ERROR('product_publication_target_changed')
    await db.rollback()
    try:
        remote = await transport.read_remote('xtrek', frozen['identity'], frozen['fingerprint'])
        current = transport.field_values(remote, frozen['fields'])
        if not transport.compare_values(current, frozen['after']):
            raise ERROR('atomer_content_rollback_remote_changed')
        patch = transport.build_patch(frozen['before'], remote, frozen['fields'])
    except transport.source.SourceError as exc:
        raise ERROR(exc.code, exc.status) from None
    facts = await family_facts(db, shop_id, remote, product_id)
    signature = editor._digest(facts)
    await db.rollback()
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': editor.IDENTITY_WRITE_LOCK})
    prior = await _replay(db, request_id, input_hash)
    if prior is not None:
        return prior
    if editor._digest(await family_facts(db, shop_id, remote, product_id)) != signature:
        raise ERROR('atomer_content_local_changed')
    from inventory_hub.services.merchandising_write_guard import require_target_available
    from inventory_hub.services.catalog import CatalogError
    try:
        await require_target_available(db, 'xtrek', frozen['identity']['parent_code'])
    except CatalogError as exc:
        raise ERROR('product_publication_inflight', exc.status) from None
    document = {**frozen, **patch, 'rollback_of': original_id, 'input_hash': input_hash,
        'family_signature': signature, 'family_facts': facts, 'remote_before': remote,
        'protected_before': transport.protected_observation(remote, frozen['fields']),
        'protected_changes': None, 'observed': None, 'error': None,
        'expires_at': (editor.now() + timedelta(minutes=30)).isoformat()}
    record = ProductEditorPublication(id=request_id, product_id=product_id, shop_id=shop_id,
        state='ready', document=document)
    db.add(record)
    await db.commit()
    return publications.summary(record)
