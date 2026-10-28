"""Opt-in supplier/brand/shop availability, reusing the durable editor publisher.

No stock field is sent. A supplier observation is never an own-stock balance.
Every attempt is audited; sending/uncertain intents fence the original target.
"""
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select, exists, func, String, text, case, literal, or_, update
from sqlalchemy.orm import aliased

from inventory_hub import config_io
from inventory_hub.database import get_session_context
from inventory_hub.db_models import Product, Shop, Supplier, SupplierProduct, SupplierFeed
from inventory_hub.db_models_ext import ProductSupplySource, ShopProduct
from inventory_hub.product_editor_models import ProductEditorPublication
from inventory_hub.services import product_publication as publication, product_editor as editor
from inventory_hub.services import product_publication_source as transport
from inventory_hub.services import supplier_availability as availability
from inventory_hub.services.catalog import CatalogError
from inventory_hub.supplier_availability_models import SupplierAvailabilityObservation, SupplierAvailabilitySettings
from inventory_hub.supplier_availability_delivery_models import SupplierAvailabilityDelivery as Delivery

WORKFLOW = 'supplier_availability'
# A new daily feed is not permission to repeat a known-invalid target. Only safe
# pre-write failures get a delayed retry; uncertain writes retain the old fence.
RETRYABLE = frozenset({'stock_publication_source_unavailable', 'stock_publication_rate_limited',
    'product_publication_remote_changed', 'product_editor_changed', 'product_publication_expired',
    'supplier_availability_publication_stale', 'supplier_availability_publication_changed'})


def _effective_available(fact, setting):
    correct_feed = exists(select(SupplierFeed.id).where(SupplierFeed.id == fact.feed_id,
        SupplierFeed.code == setting.feed_key)).correlate(fact, setting)
    fresh = ((fact.observed_at <= editor.now()) & (fact.expires_at > editor.now()) &
             (fact.observed_at + setting.freshness_seconds * text("interval '1 second'") > editor.now()) & correct_feed)
    return case((fresh, fact.available), else_=None)


def _context(policy, cfg, fingerprint):
    """SQL value compared before any remote read; feed timestamps/counts excluded."""
    values = {'supplier': Supplier.code, 'supplier_sku': SupplierProduct.supplier_sku,
        'available': _effective_available(SupplierAvailabilityObservation, SupplierAvailabilitySettings),
        'policy': literal(editor._digest(policy), type_=String),
        'labels': literal(editor._digest((cfg.get('adapter_settings') or {}).get('availability') or {}), type_=String),
        'connection': literal(fingerprint, type_=String), 'mapping_id': ShopProduct.id,
        'code': ShopProduct.external_code, 'parent_code': ShopProduct.parent_code,
        'product_id': ShopProduct.external_product_id, 'variant_id': ShopProduct.external_variant_id,
        'is_variant': ShopProduct.is_variant,
        'cached_own_stock': ShopProduct.shop_stock > 0, 'cached_label': ShopProduct.shop_availability}
    return func.jsonb_build_object(*[part for key, value in values.items()
                                    for part in (literal(key, type_=String), value)])


async def _remember(db, record):
    """Save delivery result without acknowledging a failed or ambiguous intent."""
    context = record.document.get('delivery_context')
    if context is None:
        return
    delivery = await db.get(Delivery, (record.product_id, record.shop_id), with_for_update=True)
    if delivery is None or delivery.publication_id != record.id:
        return
    delivery.state, delivery.updated_at = record.state, editor.now()
    delivery.retry_at = (editor.now() + timedelta(days=1) if record.state == 'rejected'
                         and record.document.get('error') in RETRYABLE else None)
    if record.state == 'resolved':
        # Explicit operator settlement permits a new safe preview, not a replay.
        delivery.retry_at = editor.now()
    if record.state == 'completed':
        delivery.confirmed = deepcopy(context)
        delivery.confirmed_label = record.document.get('after', {}).get('availability')
        delivery.confirmed_at = editor.now()


async def invalidate_targets(db, product_ids, shop_id=None):
    """Queue a bounded local recheck; never release sending/uncertain fences."""
    if not product_ids:
        return
    statement = update(Delivery).where(Delivery.product_id.in_(product_ids),
        Delivery.state.not_in(('sending', 'uncertain', 'ready')))
    if shop_id is not None:
        statement = statement.where(Delivery.shop_id == shop_id)
    await db.execute(statement.values(retry_at=editor.now()))


async def request_recheck(db, supplier, payload):
    supplier_row = await db.scalar(select(Supplier).where(Supplier.code == supplier))
    settings = await db.get(SupplierAvailabilitySettings, supplier_row.id, with_for_update=True) if supplier_row else None
    if settings is None or settings.revision != payload.expected_revision:
        raise availability.AvailabilityError('supplier_availability_changed')
    policy = next((policy for code, policy, _cfg in policies() if code == supplier), None)
    if policy is None:
        raise availability.AvailabilityError('supplier_availability_publication_disabled')
    shop_id = await db.scalar(select(Shop.id).where(Shop.code == policy['shop_code'], Shop.is_active.is_(True)))
    # Only exact supplier/target states may be rechecked. In-flight intents cannot
    # be released by this endpoint, even if their supplier setting is changed.
    result = await db.execute(update(Delivery).where(Delivery.product_id.in_(payload.product_ids),
        Delivery.shop_id == shop_id, Delivery.desired['supplier'].astext == supplier,
        Delivery.state.in_(('completed', 'rejected', 'resolved'))).values(retry_at=editor.now()))
    return {'supplier': supplier, 'shop_code': policy['shop_code'], 'queued': result.rowcount}


def policies():
    result = []
    for path in sorted((config_io.DATA_ROOT / 'suppliers').glob('*/config.json')):
        try:
            cfg = config_io.load_supplier(path.parent.name, write_back_on_load=False)
            policy = (cfg.get('adapter_settings') or {}).get('availability_publication') or {}
            if policy.get('enabled') is True:
                result.append((path.parent.name, policy, cfg))
        except (ValueError, OSError):
            continue
    return result


def _selected_source(configured):
    """One effective eligible source per card/shop, using projection priorities."""
    item, supplier = aliased(SupplierProduct), aliased(Supplier)
    link, setting = aliased(ProductSupplySource), aliased(SupplierAvailabilitySettings)
    fact = aliased(SupplierAvailabilityObservation)
    effective = _effective_available(fact, setting)
    scopes = [((supplier.code == code) & (Shop.code == policy['shop_code']) &
               func.upper(item.brand).in_(policy['brands'])) for code, policy, _cfg in configured]
    return (select(item.id).join(supplier, supplier.id == item.supplier_id)
        .join(link, link.supplier_product_id == item.id).join(setting, setting.supplier_id == supplier.id)
        .join(fact, (fact.supplier_id == supplier.id) & (fact.supplier_sku == item.supplier_sku))
        .where(link.product_id == Product.id,
            link.is_active.is_(True), link.orderable.is_(True), item.is_active.is_(True),
            item.is_discontinued.is_(False), supplier.is_active.is_(True), setting.enabled.is_(True),
            or_(*scopes)).order_by(case((effective.is_(True), 0), (effective.is_(False), 1), else_=2),
                link.is_primary.desc(), link.priority, item.id)
        .limit(1).correlate(Product, Shop).scalar_subquery())


def desired_label(remote_stock, available, labels, current_label=None):
    # Preserve own stock priority using the existing shop's exact quantity;
    # this mode deliberately does not assume Hub authority over that quantity.
    stock = transport.source._quantity(remote_stock)
    if int(stock) > 0 or str(current_label or '').strip().casefold() == 'skladom':
        return 'SKLADOM'
    if available is True:
        return labels.get('orderable')
    if available is False:
        return labels.get('unavailable', 'overíme')
    return labels.get('unknown', 'overíme')


async def eligible_source(db, product_id, supplier, brands, sku=None):
    query = select(SupplierProduct).join(Supplier, Supplier.id == SupplierProduct.supplier_id)
    if sku is not None:
        query = query.where(SupplierProduct.supplier_sku == sku)
    return await db.scalar(query
        .where(Supplier.code == supplier, Supplier.is_active.is_(True), SupplierProduct.is_active.is_(True),
            SupplierProduct.is_discontinued.is_(False), func.upper(SupplierProduct.brand).in_(brands),
            exists(select(ProductSupplySource.id).where(ProductSupplySource.product_id == product_id,
                    ProductSupplySource.supplier_product_id == SupplierProduct.id,
                    ProductSupplySource.is_active.is_(True), ProductSupplySource.orderable.is_(True))))
        .order_by(SupplierProduct.id))


async def validate(db, document, product_id):
    supplier = document['supplier']
    configured = next(((policy, cfg) for code, policy, cfg in policies() if code == supplier), None)
    if configured is None:
        raise editor.EditorError('supplier_availability_publication_disabled')
    policy, cfg = configured
    if (policy['shop_code'] != document['shop_code'] or editor._digest(policy) != document['policy_hash']
            or document.get('labels_hash', editor._digest((cfg.get('adapter_settings') or {}).get('availability') or {})) != editor._digest((cfg.get('adapter_settings') or {}).get('availability') or {})):
        raise editor.EditorError('supplier_availability_publication_changed')
    source = await eligible_source(db, product_id, supplier, policy['brands'], document['supplier_sku'])
    if source is None or source.supplier_sku != document['supplier_sku'] or (source.brand or '').upper() not in policy['brands']:
        raise editor.EditorError('supplier_availability_publication_changed')
    observed = await availability.source_observation(db, supplier, source.supplier_sku)
    effective = observed['available'] if observed['fresh'] else None
    if effective is not document['supplier_available']:
        raise editor.EditorError('supplier_availability_publication_stale')
    if document.get('delivery_context') is not None:
        winner = await db.scalar(select(_selected_source(policies())).select_from(Product).join(Shop, Shop.code == document['shop_code'])
                                 .where(Product.id == product_id))
        if winner != source.id:
            raise editor.EditorError('supplier_availability_publication_changed')


async def owns_target(db, shop_code, parent_code):
    for supplier, policy, cfg in policies():
        if policy['shop_code'] != shop_code:
            continue
        rows = await db.scalars(select(ShopProduct.product_id).join(Shop, Shop.id == ShopProduct.shop_id)
            .where(Shop.code == shop_code, case((ShopProduct.is_variant, ShopProduct.parent_code),
                else_=ShopProduct.external_code) == parent_code, ShopProduct.is_listed.is_(True)))
        for product_id in rows:
            source = await eligible_source(db, product_id, supplier, policy['brands'])
            if source and (source.brand or '').upper() in policy['brands']:
                return True
    return False


async def cycle():
    configured = policies()
    if not configured:
        return False
    async with get_session_context() as db:
        # Manual uncertain-write settlement goes through the shared publisher.
        # Adopt its result locally; no API call and no claim that a retry is safe
        # until the original intent has actually been settled.
        changed = (await db.scalars(select(ProductEditorPublication).join(Delivery,
            Delivery.publication_id == ProductEditorPublication.id).where(
                Delivery.state != ProductEditorPublication.state).limit(100))).all()
        for record in changed:
            await _remember(db, record)
        await db.commit()
        ready = await db.scalar(select(ProductEditorPublication).where(
            ProductEditorPublication.document['workflow'].astext == WORKFLOW,
            ProductEditorPublication.state == 'ready').order_by(ProductEditorPublication.created_at).limit(1))
        if ready is not None:
            supplier = ready.document['supplier']
            shop_id = ready.shop_id
            identifiers = list(await db.scalars(select(ProductEditorPublication.id).where(
                ProductEditorPublication.document['workflow'].astext == WORKFLOW,
                ProductEditorPublication.document['supplier'].astext == supplier,
                ProductEditorPublication.document['fingerprint'].astext == ready.document['fingerprint'],
                ProductEditorPublication.shop_id == shop_id,
                ProductEditorPublication.state == 'ready').order_by(ProductEditorPublication.created_at).limit(20)))
            await send_batch(db, identifiers)
            return True
    for supplier, policy, cfg in configured:
        async with get_session_context() as db:
            # Process one bounded batch of exact mapped leaves per tick. Prior
            # attempts for this observation, including rejections, are not loops.
            linked = exists(select(ProductSupplySource.id).where(ProductSupplySource.product_id == Product.id,
                    ProductSupplySource.supplier_product_id == SupplierProduct.id,
                    ProductSupplySource.is_active.is_(True), ProductSupplySource.orderable.is_(True)))
            selected_source = _selected_source(configured)
            try:
                fingerprint = publication.fingerprint(policy['shop_code'])
            except editor.EditorError:
                continue
            context = _context(policy, cfg, fingerprint)
            fenced = exists(select(ProductEditorPublication.id).where(
                ProductEditorPublication.product_id == Product.id, ProductEditorPublication.shop_id == Shop.id,
                ProductEditorPublication.state.in_(('sending', 'uncertain'))))
            candidates = (await db.execute(select(Product.id, Shop.id, SupplierProduct.supplier_sku, SupplierProduct.brand,
                    SupplierAvailabilityObservation.run_id, context).select_from(Product)
                .join(ShopProduct, ShopProduct.product_id == Product.id).join(Shop, Shop.id == ShopProduct.shop_id)
                .join(SupplierProduct, linked).join(Supplier, Supplier.id == SupplierProduct.supplier_id)
                .join(SupplierAvailabilitySettings, SupplierAvailabilitySettings.supplier_id == Supplier.id)
                .join(SupplierAvailabilityObservation, (SupplierAvailabilityObservation.supplier_id == Supplier.id) &
                      (SupplierAvailabilityObservation.supplier_sku == SupplierProduct.supplier_sku))
                .outerjoin(Delivery, (Delivery.product_id == Product.id) & (Delivery.shop_id == Shop.id))
                .where(or_(Delivery.product_id.is_(None), Delivery.desired.is_distinct_from(context), Delivery.retry_at <= editor.now()),
                    ~fenced, SupplierProduct.id == selected_source,
                    Supplier.is_active.is_(True), func.upper(SupplierProduct.brand).in_(policy['brands']), Supplier.code == supplier, Shop.code == policy['shop_code'], Shop.is_active.is_(True),
                    ShopProduct.is_listed.is_(True), SupplierAvailabilitySettings.enabled.is_(True),
                    ShopProduct.native_id.op('~')('^[1-9][0-9]{0,19}$'),
                    Product.item_type == "stock", Product.stock_unit != "m",
                    SupplierProduct.is_active.is_(True), SupplierProduct.is_discontinued.is_(False))
                .order_by(Product.id).limit(20))).all()
            contexts = []
            for product_id, shop_id, sku, brand, run_id, desired_context in candidates:
                if (brand or '').upper() not in policy['brands']:
                    continue
                fenced = await db.scalar(select(ProductEditorPublication.id).where(
                    ProductEditorPublication.product_id == product_id, ProductEditorPublication.shop_id == shop_id,
                    ProductEditorPublication.state.in_(('sending', 'uncertain'))).limit(1))
                if fenced:
                    continue
                row = await editor.detail(db, product_id)
                _, identity = publication.target(row, policy['shop_code'])
                contexts.append((product_id, shop_id, sku, run_id, row, identity, desired_context))
            if contexts:
                await db.rollback()
                remotes, remote_error = {}, None
                try:
                    remotes = await read_batch(policy['shop_code'], [{'identity': context[5]} for context in contexts], fingerprint)
                except transport.source.SourceError as failure:
                    remote_error = failure
                identifiers = []
                for product_id, shop_id, sku, run_id, row, identity, desired_context in contexts:
                    identifiers.append(await _prepare_and_send(db, product_id, shop_id, supplier, sku, run_id, policy, cfg,
                        row=row, remote=remotes.get(identity['code']), remote_error=remote_error, send=False,
                        desired_context=desired_context))
                await send_batch(db, identifiers)
                return True
    return False


async def _prepare_and_send(db, product_id, shop_id, supplier, sku, run_id, policy, cfg, *, row=None, remote=None, remote_error=None, send=True, desired_context=None):
    row = row or await editor.detail(db, product_id)
    shop, identity = publication.target(row, policy['shop_code'])
    fingerprint = publication.fingerprint(policy['shop_code'])
    document = {'workflow': WORKFLOW, 'supplier': supplier, 'supplier_sku': sku, 'source_run_id': run_id,
        'policy_hash': editor._digest(policy), 'labels_hash': editor._digest((cfg.get('adapter_settings') or {}).get('availability') or {}), 'shop_code': policy['shop_code'], 'fields': ['availability'],
        'revision': row['revision'], 'signature': publication.source_signature(row, policy['shop_code']),
        'identity': identity, 'fingerprint': fingerprint, 'options': None,
        'expires_at': (editor.now() + timedelta(minutes=5)).isoformat(), 'before': {}, 'after': {}, 'payload': {},
        'error': 'supplier_availability_preparing'}
    if desired_context is not None:
        document['delivery_context'] = desired_context
    observation = await availability.source_observation(db, supplier, sku)
    document['supplier_available'] = observation['available'] if observation['fresh'] else None
    identifier = str(uuid4())
    record = ProductEditorPublication(id=identifier, product_id=product_id, shop_id=shop_id, state='rejected', document=document)
    # Durable diagnostics for read/validation failures; no sending claim yet.
    db.add(record)
    if desired_context is not None:
        await db.flush()
        delivery = await db.get(Delivery, (product_id, shop_id), with_for_update=True)
        if delivery is None:
            delivery = Delivery(product_id=product_id, shop_id=shop_id, desired=desired_context,
                                publication_id=identifier, state='rejected', retry_at=editor.now() + timedelta(minutes=5))
            db.add(delivery)
        else:
            delivery.desired, delivery.publication_id = desired_context, identifier
            delivery.state, delivery.retry_at, delivery.updated_at = 'rejected', editor.now() + timedelta(minutes=5), editor.now()
    await db.commit()
    try:
        if desired_context is not None and desired_context['available'] is not document['supplier_available']:
            raise editor.EditorError('supplier_availability_publication_stale')
        await validate(db, document, product_id)
        await db.rollback()
        if remote_error is not None:
            raise remote_error
        remote = remote or await transport.read_remote(policy['shop_code'], identity, fingerprint)
        actual_id = remote['identity']['variant_id'] if identity['variant_code'] else remote['identity']['product_id']
        if not shop['mapping']['external_id'] or str(actual_id) != str(shop['mapping']['external_id']):
            raise editor.EditorError('product_publication_identity_changed')
        desired = desired_label(remote['leaf'].get('stock'), document['supplier_available'],
                                (cfg.get('adapter_settings') or {}).get('availability') or {},
                                remote['leaf'].get('availability'))
        if desired is None:
            raise editor.EditorError('supplier_availability_publication_stale')
        leaf = {'code': identity['code'], 'availability': desired}
        payload = leaf if identity['variant_code'] is None else {'code': identity['parent_code'], 'variants': [leaf]}
        before = transport.field_values(remote, ['availability'], None)
        document.update(identity=remote['identity'], before=before, after={**before, 'availability': desired},
                        desired_values={'availability': desired}, payload={'products': [payload]}, error=None)
        record = await publication._get(db, identifier, lock=True)
        record.document = document
        record.state = 'completed' if before == document['after'] else 'ready'
        await _remember(db, record)
        await db.commit()
        if record.state == 'ready' and send:
            await publication.send(db, identifier)
            record = await publication._get(db, identifier)
            await _remember(db, record)
            await db.commit()
    except (CatalogError, transport.source.SourceError, editor.EditorError) as error:
        await db.rollback()
        record = await publication._get(db, identifier, lock=True)
        if record.state in ('ready', 'rejected'):
            record.state = 'rejected'
            record.document = {**deepcopy(record.document), 'error': error.code}
            await _remember(db, record)
            await db.commit()
    return identifier


async def read_batch(shop, documents, fingerprint):
    from inventory_hub.services.stock_sync_source import read_many
    values = await read_many(shop, [document['identity'] for document in documents], fingerprint)
    return {code: {'identity': item['observation']['identity'], 'options': None,
        'leaf': {'code': code, 'stock': item['observation']['stock'], 'availability': item['observation']['availability']}}
        for code, item in values.items()}


async def send_batch(db, identifiers):
    """Share reads/one PUT while retaining every existing per-leaf durable fence."""
    if len(identifiers) > 20 or len(set(identifiers)) != len(identifiers):
        raise editor.EditorError('supplier_availability_invalid_batch', 422)
    records = [await publication._get(db, identifier) for identifier in identifiers]
    records = [record for record in records if record.state == 'ready' and record.document.get('workflow') == WORKFLOW]
    if not records:
        return
    documents = {record.id: deepcopy(record.document) for record in records}
    first = next(iter(documents.values()))
    shop, fingerprint = first['shop_code'], first['fingerprint']
    if any(d['shop_code'] != shop or d['fingerprint'] != fingerprint or d['fields'] != ['availability'] for d in documents.values()):
        raise editor.EditorError('supplier_availability_invalid_batch', 422)
    await db.rollback()
    try:
        # Source/config/revisions are checked under the same identity lock as
        # every other publisher. No network call is made while holding it.
        await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': editor.IDENTITY_WRITE_LOCK})
        for identifier, document in documents.items():
            record = await publication._get(db, identifier, lock=True)
            if record.state != 'ready' or editor.now().isoformat() > document['expires_at']:
                raise editor.EditorError('product_publication_expired')
            if publication.fingerprint(shop) != fingerprint:
                raise editor.EditorError('product_publication_target_changed')
            await validate(db, document, record.product_id)
            current = await editor.detail(db, record.product_id)
            current_shop, _ = publication.target(current, shop)
            await publication._assert_mapping(db, current, current_shop, record.shop_id)
            if publication.source_signature(current, shop) != document['signature']:
                raise editor.EditorError('product_editor_changed')
            from inventory_hub.services.merchandising_write_guard import require_target_available
            await require_target_available(db, shop, document['identity']['parent_code'], publication_id=identifier, availability=True)
        # All claims are checked before any is marked sending; sibling variants
        # in this exact envelope do not block one another's shared parent.
        for identifier in documents:
            record = await publication._get(db, identifier, lock=True)
            record.state, record.updated_at = 'sending', editor.now()
            await _remember(db, record)
        await db.commit()
    except (editor.EditorError, CatalogError) as error:
        await db.rollback()
        for identifier in documents:
            record = await publication._get(db, identifier, lock=True)
            if record.state == 'ready':
                record.state = 'rejected'; record.document = {**record.document, 'error': error.code}
                await _remember(db, record)
        await db.commit()
        return
    status, error, observed, write_started = 'uncertain', None, None, False
    try:
        before = await read_batch(shop, list(documents.values()), fingerprint)
        labels_by_supplier = {supplier: (cfg.get('adapter_settings') or {}).get('availability') or {}
                              for supplier, _policy, cfg in policies()}
        if any(transport.field_values(before[d['identity']['code']], ['availability'], None) != d['before']
               or desired_label(before[d['identity']['code']]['leaf'].get('stock'), d['supplier_available'],
                    labels_by_supplier.get(d['supplier'], {}),
                    before[d['identity']['code']]['leaf'].get('availability')) != d['after']['availability']
               for d in documents.values()):
            status, error = 'rejected', 'product_publication_remote_changed'
        else:
            items = [{'identity': d['identity'], 'availability': d['after']['availability']} for d in documents.values()]
            write_started = True
            if len(items) == 1:
                document = first
                await transport.write_once(shop, document['identity'], fingerprint, document['payload'])
            else:
                await transport.write_availability_batch_once(shop, items, fingerprint)
            observed = await read_batch(shop, list(documents.values()), fingerprint)
    except transport.source.SourceError as failure:
        error = failure.code
        if not write_started:
            status = 'rejected'
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': editor.IDENTITY_WRITE_LOCK})
    for identifier, document in documents.items():
        record = await publication._get(db, identifier, lock=True)
        if record.state != 'sending':
            continue
        try:
            value = transport.field_values(observed[document['identity']['code']], ['availability'], None) if observed else None
        except transport.source.SourceError as failure:
            value, error = None, failure.code
        completed = observed is not None and value == document['after']
        if completed:
            await publication._record_receipt(db, record.product_id, identifier, document)
        record.state = 'completed' if completed else status
        record.updated_at = editor.now()
        record.document = {**document, 'observed': value, 'error': None if completed else error or 'product_publication_readback_mismatch'}
        await _remember(db, record)
    await db.commit()
