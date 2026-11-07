"""Reviewed local sale factors, preserving all physical and order history.

Native reads precede database locks. Apply revalidates exact IDs, ownership,
units and operation fences, then saves the complete mapping group and recovery
result atomically. This workflow never writes shop content, prices or stock.
"""
from datetime import timedelta
from types import SimpleNamespace
from sqlalchemy import select, text
from inventory_hub.db_models import Product
from inventory_hub.db_models_ext import ShopOrder, ShopOrderItem, ShopProduct
from inventory_hub.shared_stock_sale_models import SharedStockSalePreview
from inventory_hub.services import product_links as links, sale_units
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK, load_identity_index, RemoteIdentity


def _key(sale):
    return sale['is_variant'], str(sale['native_variant_id'] if sale['is_variant'] else sale['native_product_id'])


def _mapping(row):
    return {**links._mapping(row), 'product_id': row.product_id, 'shop_id': row.shop_id,
            'shared_stock': row.shared_stock,
            'sale_to_stock_factor': str(int(sale_units.factor(row.sale_to_stock_factor))),
            'sale_barcodes': row.sale_barcodes}


def _replacement_snapshot(row):
    """Preserve obsolete shop observations before the native binding changes."""
    stamp = lambda value: value.isoformat() if value is not None else None
    amount = lambda value: str(value) if value is not None else None
    return {**_mapping(row), 'shop_stock': amount(row.shop_stock), 'shop_price': amount(row.shop_price),
            'shop_availability': row.shop_availability, 'push_pending': row.push_pending,
            'last_push_at': stamp(row.last_push_at), 'last_push_status': row.last_push_status,
            '_last_push_error': row.last_push_error, 'last_pull_at': stamp(row.last_pull_at),
            'created_at': stamp(row.created_at), 'updated_at': stamp(row.updated_at)}


def _public_mapping(mapping):
    # Retain old diagnostic text in the durable audit, never echo it through
    # a new API response where third-party error text may contain private data.
    return {key: value for key, value in mapping.items() if not key.startswith('_')}


def _selected(sales, observed):
    leaves = links._unique(observed['leaves'])
    selected = []
    for sale in sales:
        rows = [row for row in leaves if links.remote_key(row) == _key(sale)
                and row['code'] == sale['code'] and row['parent_code'] == sale['parent_code']
                and row['parent_id'] == str(sale['native_product_id'])]
        if len(rows) != 1 or rows[0].get('invalid_barcodes'):
            raise links.LinkError('shared_stock_sale_native_identity_changed')
        selected.append({'sale': sale, 'remote': rows[0]})
    return selected


def _replacements(current, selected):
    """Only an explicitly pinned ordinary mapping can change native identity."""
    by_id = {row.id: row for row in current}
    result = {}
    desired_keys = {links.remote_key(row['remote']) for row in selected}
    for entry in selected:
        pin = entry['sale'].get('replace_deleted_mapping')
        if not pin:
            continue
        old = by_id.get(pin['mapping_id'])
        variant = pin['native_variant_id'] is not None
        if (old is None or old.shared_stock or not old.is_listed or old.is_variant != variant
                or old.external_product_id != str(pin['native_product_id'])
                or old.external_variant_id != (str(pin['native_variant_id']) if variant else None)
                or (old.is_variant, old.native_id) in desired_keys):
            raise links.LinkError('shared_stock_sale_replacement_changed')
        result[links.remote_key(entry['remote'])] = old
    return result


async def _absence(shop_code, local, fingerprint, used_requests):
    previous = local.get('replaced_mappings', [])
    if not previous:
        return None
    proof = await links._verify_replacement_absence(shop_code, previous, fingerprint,
        max_requests=links.MAX_REQUESTS-used_requests)
    if proof['existing_mapping_ids']:
        raise links.LinkError('shared_stock_sale_original_still_exists')
    return {'mapping_ids': [row['id'] for row in previous], 'fingerprint': fingerprint,
            'active_and_archived': True, 'checked_at': links.now().isoformat(), 'requests': proof['requests']}


async def _inspect(db, shop, product_id, selected, *, lock=False):
    query = select(Product).where(Product.id == product_id)
    product = await db.scalar((query.with_for_update(read=True) if lock else query)
                              .execution_options(populate_existing=True))
    if product is None or not product.is_active or product.item_type != 'stock' or product.stock_unit != 'pcs':
        raise links.LinkError('shared_stock_sale_piece_card_required')
    identities = [RemoteIdentity(shop.id, code=row['remote']['code'], is_variant=row['remote']['is_variant'],
                  external_id=row['remote']['external_id'], external_product_id=row['remote']['parent_id'])
                  for row in selected]
    index = await load_identity_index(db, shop.id, identities)
    current = index.by_product.get(product_id, [])
    if lock and current:
        current = list(await db.scalars(select(ShopProduct).where(ShopProduct.id.in_([row.id for row in current]))
            .order_by(ShopProduct.id).with_for_update().execution_options(populate_existing=True)))
    desired_keys = {links.remote_key(row['remote']) for row in selected}
    replacements = _replacements(current, selected)
    replaced_ids = {row.id for row in replacements.values()}
    if any((row.is_variant, row.native_id) not in desired_keys and row.id not in replaced_ids for row in current):
        # Every current mapping is retained or explicitly replaced after proof
        # that its exact previous native identity was deleted.
        raise links.LinkError('shared_stock_sale_existing_mapping_omitted')
    current_by_key = {(row.is_variant, row.native_id): row for row in current}
    if len(current_by_key) != len(current):
        raise links.LinkError('shared_stock_sale_ownership_conflict')
    changes = []
    for entry in selected:
        sale, remote = entry['sale'], entry['remote']
        owners = {row.id: row for row in [*index.by_code.get(remote['code'].casefold(), []),
                                         *index.by_external_id.get(links.remote_key(remote), [])]}
        replacement = replacements.get(links.remote_key(remote))
        existing = replacement or current_by_key.get(links.remote_key(remote))
        sku_owners = index.by_sku.get(remote['code'].casefold(), set()) | index.by_internal_sku.get(remote['code'].casefold(), set())
        if sku_owners and sku_owners != {product_id}:
            raise links.LinkError('shared_stock_sale_ownership_conflict')
        if owners and (existing is None or set(owners) != {existing.id}):
            raise links.LinkError('shared_stock_sale_ownership_conflict')
        if existing is not None and replacement is None and (existing.external_code != remote['code']
                or existing.external_product_id != remote['parent_id']
                or existing.parent_code != (remote['parent_code'] if remote['is_variant'] else None)):
            raise links.LinkError('shared_stock_sale_native_identity_changed')
        changes.append(existing is None or not existing.shared_stock
                       or sale_units.factor(existing.sale_to_stock_factor) != sale_units.factor(sale['sale_to_stock_factor'])
                       or existing.sale_barcodes != remote['eans'] or not existing.is_listed)
    reinterprets = bool(replacements) or not current or any(row is not None and
        (not row.shared_stock or sale_units.factor(row.sale_to_stock_factor) != sale_units.factor(entry['sale']['sale_to_stock_factor'])
         or row.sale_barcodes != entry['remote']['eans'])
        for entry in selected for row in [current_by_key.get(links.remote_key(entry['remote']))])
    if any(changes):
        parents = {row['remote']['parent_code'] for row in selected}
        parents.update(row.parent_code if row.is_variant else row.external_code for row in replacements.values())
        await links._require_mapping_write_available(db, shop, SimpleNamespace(product_id=product_id),
                                                    parents, 'shared_stock_sale_operation_in_use')
        # A pending price for any sibling must retain its original factor too.
        from inventory_hub.fifo_cost_models import FifoCostPublication
        if await db.scalar(select(FifoCostPublication.id).where(FifoCostPublication.shop_id == shop.id,
                ((FifoCostPublication.target_key == f'product:{product_id}') |
                 FifoCostPublication.target_key.startswith(f'product:{product_id}:')),
                FifoCostPublication.status.in_(('sending', 'uncertain'))).limit(1)) is not None:
            raise links.LinkError('shared_stock_sale_operation_in_use')
    if reinterprets:
        managed = await db.scalar(select(ShopOrderItem.id).join(ShopOrder, ShopOrder.id == ShopOrderItem.order_id)
            .where(ShopOrder.shop_id == shop.id, ShopOrderItem.product_id == product_id,
                   ShopOrderItem.stock_managed.is_(True)).limit(1))
        from inventory_hub.stock_native_models import StockNativeEvent, StockNativeOrder
        native = await db.scalar(select(StockNativeOrder.order_id).join(StockNativeEvent,
            StockNativeEvent.id == StockNativeOrder.event_id).where(StockNativeOrder.shop_id == shop.id,
            StockNativeEvent.facts.contains({'lines': [{'product_id': product_id, 'native': True}]})).limit(1))
        if managed is not None or native is not None:
            raise links.LinkError('shared_stock_sale_history_requires_review')
    return {'product': {'id': product.id, 'sku': product.sku, 'name': product.name,
                        'item_type': product.item_type, 'stock_unit': product.stock_unit},
            'mappings': [_mapping(row) for row in sorted(current, key=lambda row: row.id)],
            'replaced_mappings': [_replacement_snapshot(row) for row in sorted(replacements.values(), key=lambda row: row.id)]}


def public(row):
    result = {key: value for key, value in row.result.items() if key != '_content_archives'} if row.result is not None else None
    if result is not None:
        result['replaced_mappings'] = [{**item, 'previous_mapping': _public_mapping(item['previous_mapping'])}
                                      for item in result.get('replaced_mappings', [])]
    return {'id': row.id, 'shop_code': row.document['shop_code'], 'product': row.document['local']['product'],
            'status': row.status, 'preview_hash': row.preview_hash, 'expires_at': row.expires_at.isoformat(),
            'sales': row.document['selected'], 'existing_mappings': row.document['local']['mappings'],
            'replaced_mappings': [_public_mapping(item) for item in row.document['local'].get('replaced_mappings', [])],
            'deletion_proof': row.document.get('deletion_proof'),
            'identity_evidence': row.document['request']['identity_evidence'], 'result': result,
            **({'source_read': row.document['source_read']} if row.document.get('source_read') else {})}


async def get(db, identifier):
    row = await db.get(SharedStockSalePreview, str(identifier))
    if row is None:
        raise links.LinkError('shared_stock_sale_not_found', 404)
    return public(row)


def request_document(payload):
    raw = payload.model_dump(mode='json')
    raw['sales'] = sorted(raw['sales'], key=lambda sale: (sale['code'], _key(sale)))
    return raw, links.digest(raw)


async def preview(db, payload, *, actor=None, _observation=None):
    raw, request_hash = request_document(payload)
    existing = await db.get(SharedStockSalePreview, str(payload.request_id))
    if existing is not None:
        if existing.request_hash != request_hash:
            raise links.LinkError('shared_stock_sale_request_changed')
        return public(existing)
    shop = await links._shop(db, payload.shop_code)
    shop_id = shop.id
    await db.commit()
    if _observation is None:
        observed = await links._source_call(links.source.discover(payload.shop_code,
            [sale['code'] for sale in raw['sales']], max_requests=links.MAX_REQUESTS))
    else:
        _observation.require_fresh(payload.shop_code)
        observed = _observation.observed
    selected = _selected(raw['sales'], observed)
    if _observation is not None:
        links.verified_selection(observed, [entry['remote'] for entry in selected])
    preflight = None
    absence = None
    if any(entry['sale'].get('replace_deleted_mapping') for entry in selected):
        shop = await links._shop(db, payload.shop_code)
        preflight = await _inspect(db, shop, payload.product_id, selected)
        await db.commit()
        absence = (_observation.absence(preflight) if _observation is not None else
                   await _absence(payload.shop_code, preflight, observed['fingerprint'], observed['requests']))
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
    existing = await db.get(SharedStockSalePreview, str(payload.request_id), populate_existing=True)
    if existing is not None:
        if existing.request_hash != request_hash:
            raise links.LinkError('shared_stock_sale_request_changed')
        return public(existing)
    if _observation is not None:
        _observation.require_fresh(payload.shop_code)
    shop = await links._shop(db, payload.shop_code)
    if shop.id != shop_id:
        raise links.LinkError('shared_stock_sale_context_changed')
    local = await _inspect(db, shop, payload.product_id, selected, lock=True)
    if preflight is not None and preflight != local:
        raise links.LinkError('shared_stock_sale_context_changed')
    document = {'shop_code': shop.code, 'fingerprint': observed['fingerprint'], 'request': raw,
                'local': local, 'selected': selected, 'deletion_proof': absence,
                'created_by': actor or {'source': 'operator_token'}}
    if _observation is not None:
        if await links._source_call(links.source.current_fingerprint(shop.code)) != observed['fingerprint']:
            raise links.LinkError('source_connection_changed')
        document['source_read'] = _observation.proof()
    row = SharedStockSalePreview(id=str(payload.request_id), shop_id=shop.id, product_id=payload.product_id,
        request_hash=request_hash, preview_hash=links.digest(document), document=document,
        expires_at=links.now()+timedelta(minutes=links.PREVIEW_MINUTES), status='prepared')
    db.add(row)
    await db.flush()
    result = public(row)
    if _observation is not None:
        _observation.require_fresh(payload.shop_code)
    await db.commit()
    return result


def _check(row, payload, *, expired=False):
    if row is None:
        raise links.LinkError('shared_stock_sale_not_found', 404)
    if payload.preview_hash != row.preview_hash or links.digest(row.document) != row.preview_hash:
        raise links.LinkError('shared_stock_sale_preview_changed')
    if row.status == 'completed':
        return False
    if row.status != 'prepared' or (not expired and row.expires_at <= links.now()):
        raise links.LinkError('shared_stock_sale_preview_expired')
    return True


async def apply(db, identifier, payload, *, actor=None, _observation=None):
    row = await db.get(SharedStockSalePreview, str(identifier))
    if not _check(row, payload, expired=True):
        return public(row)
    try:
        return await _apply(db, identifier, payload, row, actor=actor, _observation=_observation)
    except links.LinkError as error:
        if error.code in ('shared_stock_sale_not_found', 'shared_stock_sale_preview_changed'):
            raise
        return await fence_failed_apply(db, identifier, payload, error)


async def fence_failed_apply(db, identifier, payload, error):
    # Settle earlier concurrent applies before saying this request did not
    # write. Unknown DB/commit failures retain their uncertain outcome.
    await db.rollback()
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
    row = await db.scalar(select(SharedStockSalePreview).where(SharedStockSalePreview.id == str(identifier))
        .with_for_update().execution_options(populate_existing=True))
    if row and row.status == 'completed' and row.preview_hash == payload.preview_hash:
        result = public(row)
        await db.commit()
        return result
    if row and row.preview_hash == payload.preview_hash and row.status == 'prepared':
        row.status, row.expires_at = 'expired', min(row.expires_at, links.now())
    await db.commit()
    error.known_unapplied = True
    raise error


async def _apply(db, identifier, payload, row, *, actor=None, _observation=None):
    _check(row, payload)
    document = row.document
    shop_code, fingerprint = document['shop_code'], document['fingerprint']
    await db.commit()
    frozen = [entry['remote'] for entry in document['selected']]
    if _observation is None:
        observed = await links._source_call(links.source.verify(shop_code, frozen, fingerprint, max_requests=links.MAX_REQUESTS))
    else:
        _observation.require_fresh(shop_code)
        observed = links.verified_selection(_observation.observed, frozen)
    selected = _selected(document['request']['sales'], observed)
    absence = (_observation.absence(document['local']) if _observation is not None else
               await _absence(shop_code, document['local'], fingerprint, observed['requests']))
    if (observed['fingerprint'] != fingerprint or
        [links.remote_snapshot(row['remote']) for row in selected] !=
        [links.remote_snapshot(row['remote']) for row in document['selected']]):
        raise links.LinkError('shared_stock_sale_native_identity_changed')
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
    row = await db.scalar(select(SharedStockSalePreview).where(SharedStockSalePreview.id == str(identifier))
        .with_for_update().execution_options(populate_existing=True))
    if not _check(row, payload):
        return public(row)
    if _observation is not None:
        _observation.require_fresh(shop_code)
    shop = await links._shop(db, shop_code)
    if shop.id != row.shop_id:
        raise links.LinkError('shared_stock_sale_context_changed')
    fresh = await _inspect(db, shop, row.product_id, selected, lock=True)
    if fresh != document['local']:
        raise links.LinkError('shared_stock_sale_context_changed')
    if await links._source_call(links.source.current_fingerprint(shop_code)) != fingerprint:
        raise links.LinkError('source_connection_changed')
    previous = {(item['is_variant'], item['external_id']): item for item in fresh['mappings']}
    by_id = {item['id']: item for item in fresh['replaced_mappings']}
    applied, replaced, metadata_replacements = [], [], {}
    for entry in selected:
        remote, sale = entry['remote'], entry['sale']
        pin = sale.get('replace_deleted_mapping')
        original = by_id[pin['mapping_id']] if pin else previous.get(links.remote_key(remote))
        mapping = await db.get(ShopProduct, original['id']) if original else ShopProduct(
            shop_id=shop.id, product_id=row.product_id, external_product_id=remote['parent_id'],
            external_variant_id=remote['external_id'] if remote['is_variant'] else None,
            external_code=remote['code'], parent_code=remote['parent_code'] if remote['is_variant'] else None,
            is_variant=remote['is_variant'], is_listed=True, push_pending=False)
        if pin:
            mapping.external_product_id = remote['parent_id']
            mapping.external_variant_id = remote['external_id'] if remote['is_variant'] else None
            mapping.external_code = remote['code']
            mapping.parent_code = remote['parent_code'] if remote['is_variant'] else None
            mapping.is_variant = remote['is_variant']
            # These are obsolete shop observations, not physical stock. Their
            # native meaning must not carry over to the replacement leaf.
            mapping.shop_stock = mapping.shop_price = mapping.shop_availability = None
            mapping.last_push_at = mapping.last_push_status = mapping.last_push_error = None
            mapping.push_pending, mapping.last_pull_at = False, links.now()
            metadata_replacements[links._metadata_key(remote)] = original
        mapping.shared_stock = True
        mapping.sale_to_stock_factor = sale_units.factor(sale['sale_to_stock_factor'])
        mapping.sale_barcodes = remote['eans']
        mapping.is_listed = True
        db.add(mapping)
        applied.append(mapping)
        if pin:
            replaced.append({'previous_mapping': original, 'mapping': _mapping(mapping)})
    await db.flush()
    archives = await links._cache_metadata(db, shop.id, [entry['remote'] for entry in selected],
                                           replacements=metadata_replacements)
    row.status, row.completed_at = 'completed', links.now()
    row.result = {'product_id': row.product_id, 'mappings': [_mapping(item) for item in applied],
                  'stock_unit': 'pcs', 'stock_changed': False, 'remote_writes': 0,
                  'replaced_mappings': replaced, 'deletion_proof': absence,
                  'completed_at': row.completed_at.isoformat(), 'applied_by': actor or {'source': 'operator_token'}}
    if archives:
        row.result['_content_archives'] = archives
    if _observation is not None:
        row.result['source_read'] = _observation.proof()
    await db.flush()
    result = public(row)
    if _observation is not None:
        _observation.require_fresh(shop_code)
    await db.commit()
    return result
