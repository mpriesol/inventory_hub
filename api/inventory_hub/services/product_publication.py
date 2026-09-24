"""Durable operator-selected merchandising publications, independent of stock.

A committed sending intent is a fence even across a restart. An uncertain PUT
never becomes retryable merely because a subsequent read happens to match.
"""
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import and_, case, func, or_, select, text

from inventory_hub.db_models import Product, Shop
from inventory_hub.db_models_ext import ShopProduct
from inventory_hub.product_editor_models import ProductEditorOverride, ProductEditorPublication
from inventory_hub.services import product_editor as editor, product_publication_source as transport
from inventory_hub.services.catalog import CatalogError
from inventory_hub.services.catalog_import import shop_config
from inventory_hub.services.upgates import connection_fingerprint


ERROR = editor.EditorError


def fingerprint(shop):
    try:
        config = shop_config(shop)
        value = connection_fingerprint(config.get('upgates_api_base_url'), config.get('upgates_login'))
    except (CatalogError, OSError, ValueError, TypeError):
        value = None
    if value is None:
        raise ERROR('product_publication_connection_missing', 422)
    return value


def target(row, shop_code):
    shop = next((item for item in row['shops'] if item['shop_code'] == shop_code), None)
    mapping = shop.get('mapping') if shop else None
    if not shop or not shop['mapped'] or not mapping:
        raise ERROR('product_publication_mapping_missing', 422)
    code = mapping['variant_code'] if mapping['is_variant'] else mapping['code']
    if code != row['sku']:
        raise ERROR('product_publication_identity_changed')
    identity = {'code': code, 'parent_code': mapping['parent_code'] if mapping['is_variant'] else code,
                'variant_code': code if mapping['is_variant'] else None}
    try:
        transport.source._target(identity)
    except transport.source.SourceError as exc:
        raise ERROR(exc.code, exc.status) from None
    return shop, identity


def source_signature(row, shop_code):
    shop, identity = target(row, shop_code)
    return editor._digest({'revision': row['revision'], 'sku': row['sku'], 'eans': row['eans'],
        'attributes': row['attributes'], 'image_url': row['image_url'], 'variant': row['variant'],
        'effective': shop['effective'], 'mapping': shop['mapping'], 'identity': identity})


def summary(record):
    document = record.document
    return {'id': record.id, 'product_id': record.product_id, 'shop_code': document['shop_code'],
        'state': record.state, 'fields': document['fields'], 'revision': document['revision'],
        'before': document['before'], 'after': document['after'], 'payload': document['payload'],
        'expires_at': document['expires_at'], 'error': document.get('error'),
        'observed': document.get('observed'), 'resolution_note': document.get('resolution_note'),
        'price_basis': 'base_list_price_incl_vat', 'keeps_discounts': True}


async def _lock(db, product_id, shop_id):
    key = int(editor._digest(['product-publication', product_id, shop_id])[:15], 16)
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})


async def _inflight(db, product_id, shop_id, except_id=None):
    query = select(ProductEditorPublication.id).where(ProductEditorPublication.product_id == product_id,
        ProductEditorPublication.shop_id == shop_id, ProductEditorPublication.state.in_(('sending', 'uncertain')))
    if except_id:
        query = query.where(ProductEditorPublication.id != except_id)
    if await db.scalar(query):
        raise ERROR('product_publication_inflight')


async def _get(db, identifier, lock=False):
    statement = select(ProductEditorPublication).where(ProductEditorPublication.id == identifier)
    if lock:
        statement = statement.with_for_update()
    record = await db.scalar(statement.execution_options(populate_existing=True))
    if record is None:
        raise ERROR('product_publication_missing', 404)
    return record


async def get(db, identifier):
    return summary(await _get(db, identifier))


async def history(db, product_id):
    items = (await db.scalars(select(ProductEditorPublication).where(ProductEditorPublication.product_id == product_id)
        .order_by(ProductEditorPublication.created_at.desc()).limit(20))).all()
    return {'items': [summary(item) for item in items]}


async def preview(db, product_id, request):
    row = await editor.detail(db, product_id)
    if request.expected_revision != row['revision']:
        raise ERROR('product_editor_changed')
    shop, identity = target(row, request.shop_code)
    shop_id = await db.scalar(select(Shop.id).where(Shop.code == request.shop_code, Shop.is_active.is_(True)))
    await _inflight(db, product_id, shop_id)
    await _assert_mapping(db, row, shop, shop_id)
    frozen_fingerprint = fingerprint(request.shop_code)
    signature = source_signature(row, request.shop_code)
    # No open DB transaction is held across external calls.
    await db.rollback()
    try:
        remote = await transport.read_remote(request.shop_code, identity, frozen_fingerprint,
            prices='sale_price_gross' in request.fields)
        expected_id = shop['mapping']['external_id']
        actual_id = remote['identity']['variant_id'] if identity['variant_code'] else remote['identity']['product_id']
        if expected_id and str(expected_id) != str(actual_id):
            raise ERROR('product_publication_identity_changed')
        patch = transport.build_patch(row, shop, remote, request.fields)
    except transport.source.SourceError as exc:
        raise ERROR(exc.code, exc.status) from None
    current = await editor.detail(db, product_id)
    if source_signature(current, request.shop_code) != signature:
        raise ERROR('product_editor_changed')
    identifier = str(uuid4())
    record = ProductEditorPublication(id=identifier, product_id=product_id, shop_id=shop_id, state='ready',
        document={'shop_code': request.shop_code, 'fields': request.fields, 'revision': row['revision'],
            'signature': signature, 'identity': remote['identity'], 'fingerprint': frozen_fingerprint,
            'options': remote['options'], 'desired_values': editor.publication_values(row['common'], row['variant'], shop['effective']), 'expires_at': (editor.now() + timedelta(minutes=30)).isoformat(), **patch})
    db.add(record)
    await db.commit()
    return summary(record)


async def send(db, identifier):
    record = await _get(db, identifier)
    product_id, shop_id, document = record.product_id, record.shop_id, deepcopy(record.document)
    if record.state != 'ready':
        return summary(record)  # repeated browser requests never send another PUT
    await db.rollback()
    if editor.now().isoformat() > document['expires_at']:
        raise ERROR('product_publication_expired')
    if fingerprint(document['shop_code']) != document['fingerprint']:
        raise ERROR('product_publication_target_changed')
    try:
        remote = await transport.read_remote(document['shop_code'], document['identity'], document['fingerprint'],
            prices='sale_price_gross' in document['fields'])
        if remote['options'] != document['options'] or transport.field_values(remote, document['fields'], remote['options']) != document['before']:
            raise ERROR('product_publication_remote_changed')
    except transport.source.SourceError as exc:
        raise ERROR(exc.code, exc.status) from None
    await _lock(db, product_id, shop_id)
    record = await _get(db, identifier, lock=True)
    if record.state != 'ready':
        return summary(record)
    await _inflight(db, product_id, shop_id, identifier)
    # Freeze source state under the same identity lock used by import/save.
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': editor.IDENTITY_WRITE_LOCK})
    current = await editor.detail(db, product_id)
    current_shop, _ = target(current, document['shop_code'])
    await _assert_mapping(db, current, current_shop, shop_id)
    if source_signature(current, document['shop_code']) != document['signature']:
        raise ERROR('product_editor_changed')
    from inventory_hub.services.merchandising_write_guard import require_target_available
    try:
        await require_target_available(db, document['shop_code'], document['identity']['parent_code'], publication_id=record.id)
    except CatalogError as exc:
        raise ERROR('product_publication_inflight', exc.status) from None
    record.state, record.updated_at = 'sending', editor.now()
    await db.commit()  # durable intent before one possible remote side effect
    error, state, observed = None, 'uncertain', None
    try:
        # Re-check after the durable fence closes the cross-request stale-read race.
        remote = await transport.read_remote(document['shop_code'], document['identity'], document['fingerprint'],
            prices='sale_price_gross' in document['fields'])
        if remote['options'] != document['options'] or transport.field_values(remote, document['fields'], remote['options']) != document['before']:
            state, error = 'rejected', 'product_publication_remote_changed'
        else:
            await transport.write_once(document['shop_code'], document['identity'], document['fingerprint'], document['payload'])
            remote = await transport.read_remote(document['shop_code'], document['identity'], document['fingerprint'],
                prices='sale_price_gross' in document['fields'])
            observed = transport.field_values(remote, document['fields'], document['options'])
            if observed == document['after'] and remote['options'] == document['options']:
                state = 'completed'
            else:
                error = 'product_publication_readback_mismatch'
    except transport.source.SourceError as exc:
        error = exc.code
        # Even GET/readback errors after an acknowledged write remain uncertain.
        # Conservative fence is intentional; no automatic repeat follows.
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': editor.IDENTITY_WRITE_LOCK})
    record = await _get(db, identifier, lock=True)
    if record.state != 'sending':
        return summary(record)
    if state == 'completed':
        await _record_receipt(db, product_id, identifier, document)
    record.state, record.updated_at = state, editor.now()
    record.document = {**document, 'error': error, 'observed': observed}
    await db.commit()
    return summary(record)


async def resolve(db, identifier, request):
    record = await _get(db, identifier)
    if record.state not in ('sending', 'uncertain'):
        return summary(record)
    if record.state == 'sending' and editor.now() - record.updated_at < timedelta(minutes=5):
        raise ERROR('product_publication_still_sending')
    document, product_id = deepcopy(record.document), record.product_id
    await db.rollback()
    try:
        remote = await transport.read_remote(document['shop_code'], document['identity'], document['fingerprint'],
            prices='sale_price_gross' in document['fields'])
        observed = transport.field_values(remote, document['fields'], document['options'])
    except transport.source.SourceError as exc:
        raise ERROR(exc.code, exc.status) from None
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': editor.IDENTITY_WRITE_LOCK})
    record = await _get(db, identifier, lock=True)
    if record.state not in ('sending', 'uncertain'):
        return summary(record)
    matches = observed == document['after'] and remote['options'] == document['options']
    if matches:
        await _record_receipt(db, product_id, identifier, document)
    record.state = 'completed' if matches else 'resolved'
    record.updated_at = editor.now()
    record.document = {**document, 'observed': observed, 'resolution_note': request.note,
        'original_request_settled': True, 'resolved_at': editor.now().isoformat()}
    await db.commit()
    return summary(record)


async def refresh_attributes(db, product_id):
    """Read one mapped leaf, enrich missing canonical axes; never PUT or scan siblings."""
    from inventory_hub.db_models_ext import ProductVariantAttribute
    from inventory_hub.services.upgates import variant_attributes
    row = await editor.detail(db, product_id)
    shops = sorted((s for s in row['shops'] if s['mapped'] and s.get('mapping', {}).get('is_variant')),
                   key=lambda s: (s['shop_code'] != 'xtrek', s['shop_code']))
    if not shops:
        raise ERROR('product_publication_mapping_missing', 422)
    shop_code = shops[0]['shop_code']
    shop, identity = target(row, shop_code)
    frozen_fingerprint = fingerprint(shop_code)
    expected_mapping = deepcopy(shop['mapping'])
    await db.rollback()
    try:
        remote = await transport.read_remote(shop_code, identity, frozen_fingerprint)
    except transport.source.SourceError as exc:
        raise ERROR(exc.code, exc.status) from None
    expected_id = expected_mapping['external_id']
    if expected_id and str(expected_id) != str(remote['identity']['variant_id']):
        raise ERROR('product_publication_identity_changed')
    attrs = variant_attributes(remote['leaf'])
    if not attrs:
        raise ERROR('product_editor_attributes_unavailable', 422)
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': editor.IDENTITY_WRITE_LOCK})
    current = await editor.detail(db, product_id)
    current_shop, _ = target(current, shop_code)
    if current_shop['mapping'] != expected_mapping:
        raise ERROR('product_publication_identity_changed')
    existing = set(await db.scalars(select(ProductVariantAttribute.attribute_name)
        .where(ProductVariantAttribute.product_id == product_id)))
    for position, attribute in enumerate(attrs):
        if attribute['name'] not in existing:
            db.add(ProductVariantAttribute(product_id=product_id, attribute_name=attribute['name'],
                attribute_value=attribute['value'], display_order=position))
    await db.commit()
    return await editor.detail(db, product_id)


async def _record_receipt(db, product_id, identifier, document):
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': editor.IDENTITY_WRITE_LOCK})
    override = await db.scalar(select(ProductEditorOverride).where(ProductEditorOverride.product_id == product_id)
        .with_for_update().execution_options(populate_existing=True))
    if override is None:
        return
    data = deepcopy(override.data)
    receipts = data.setdefault('published', {}).setdefault(document['shop_code'], {})
    for field in document['fields']:
        receipts[field] = {'value': document['desired_values'][field], 'publication_id': identifier,
                           'verified_at': editor.now().isoformat()}
    override.data, override.updated_at = data, editor.now()


async def _assert_mapping(db, row, shop, shop_id):
    """Reject case aliases and multiply owned leaf IDs before any remote write."""
    mapping = shop['mapping']
    code = mapping['variant_code'] if mapping['is_variant'] else mapping['code']
    leaf_code = case((ShopProduct.is_variant.is_(True), ShopProduct.variant_code), else_=ShopProduct.external_code)
    duplicates = [func.lower(leaf_code) == code.lower()]
    if mapping['external_id']:
        duplicates.append(and_(ShopProduct.is_variant == mapping['is_variant'], ShopProduct.external_id == mapping['external_id']))
    duplicate = await db.scalar(select(ShopProduct.id).where(ShopProduct.shop_id == shop_id,
        ShopProduct.product_id != row['id'], or_(*duplicates)).limit(1))
    sku_alias = await db.scalar(select(Product.id).where(Product.id != row['id'], func.lower(Product.sku) == row['sku'].lower()).limit(1))
    if duplicate or sku_alias:
        raise ERROR('product_publication_identity_changed')
