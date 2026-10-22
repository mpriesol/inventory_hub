"""Explicit supplier links, with optional reviewed filling of an empty barcode.

Inspect/apply helpers participate in the caller's transaction. Public API
operations persist a reviewed preview and its result for lost-response recovery.
The selected supplier-scoped alias/source relation and explicitly reviewed EANs
are written atomically; broad supplier reconciliation is not invoked.
"""
from copy import deepcopy
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError
from sqlalchemy import case, func, or_, select, text

from inventory_hub.db_models import IdentifierType, Product, ProductIdentifier, Supplier, SupplierProduct
from inventory_hub.db_models_ext import ProductSupplySource
from inventory_hub.product_editor_models import ProductEditorAudit, ProductEditorOverride, ProductEditorSave
from inventory_hub.services import catalog, product_links
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK, RemoteIdentity, barcode_keys, load_identity_index, verified_barcodes
from inventory_hub.supplier_card_link_models import SupplierCardLinkPreview


PREVIEW_MINUTES = 30
now, digest, axes = product_links.now, product_links.digest, product_links.axes


class SupplierCardLinkError(Exception):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status, self.known_unapplied = code, status, False


LinkError = SupplierCardLinkError


def _source(row, supplier):
    """Use the accepted catalog identity, never manufacture an offer from a name."""
    try:
        parsed = catalog.public_product(row, detail=True)
    except (ValidationError, ValueError, TypeError):
        raise SupplierCardLinkError('supplier_card_links_catalog_invalid', 409) from None
    if (parsed.supplier != supplier.code or parsed.code != row.supplier_sku
            or parsed.group_code != row.supplier_group_code):
        raise SupplierCardLinkError('supplier_card_links_source_identity_changed')
    record_eans = sorted(verified_barcodes((row.ean,)))
    catalog_eans = sorted(verified_barcodes(parsed.eans))
    if record_eans and catalog_eans and not barcode_keys(record_eans) & barcode_keys(catalog_eans):
        raise SupplierCardLinkError('supplier_card_links_source_ean_conflict')
    raw = [row.ean, *parsed.eans]
    warnings = ['unverified_barcode'] if any(value and not verified_barcodes((value,)) for value in raw) else []
    if parsed.fetched_at is not None:
        warnings.append('supplier_catalog_snapshot')
    attrs = [item.model_dump() for item in parsed.variant_attributes]
    return {'id': row.id, 'supplier_id': supplier.id, 'supplier_code': supplier.code,
        'supplier_name': supplier.name, 'supplier_sku': row.supplier_sku, 'shop_sku': parsed.shop_code,
        'name': parsed.name or row.name, 'brand': parsed.brand or getattr(row, 'brand', None),
        'eans': sorted(set(record_eans + catalog_eans)),
        'attributes': attrs, 'group_code': parsed.group_code,
        'active': bool(supplier.is_active and row.is_active and not row.is_discontinued),
        'last_seen_at': (parsed.fetched_at or row.last_seen_at).isoformat() if parsed.fetched_at or row.last_seen_at else None,
        'warnings': warnings, 'import_blockers': list(parsed.import_blockers)}


async def _product(db, product_id, *, lock=False, exclude_source=None):
    try:
        facts = (await product_links._facts(db, [product_id], lock=lock,
                     exclude_supplier_product_id=exclude_source))[product_id]
    except product_links.LinkError as error:
        raise SupplierCardLinkError('supplier_card_links_product_missing', error.status) from None
    return facts


def _public_product(facts):
    return {'id': facts['product_id'], 'sku': facts['sku'], 'name': facts['name'],
            'eans': facts['eans'], 'attributes': facts['attributes'], 'active': facts['is_active']}


def _policy(row):
    if row is None:
        return {'priority': 100, 'is_primary': False, 'default_lead_time_days': None,
            'orderable': True, 'min_order_qty': 1, 'pack_size': 1, 'negotiated_price': None,
            'negotiated_currency': None, 'is_active': True}
    result = {key: getattr(row, key) for key in _policy(None)}
    if result['negotiated_price'] is not None:
        result['negotiated_price'] = str(result['negotiated_price'])
    return result


async def _ean_adoption(db, facts, source, *, selected=False):
    """Only fill a truly empty card, retaining exact verified leaf identifiers."""
    existing = list(await db.scalars(select(ProductIdentifier.value).where(
        ProductIdentifier.product_id == facts['product_id'],
        ProductIdentifier.identifier_type.in_(ProductIdentifierService.BARCODE_TYPES)).order_by(ProductIdentifier.id)))
    eans = sorted(verified_barcodes(source['eans']))
    comparable_eans = sorted({value for raw in eans for value in ProductIdentifierService.gtin_equivalents(raw)})
    reason = 'ean_present' if existing or facts['manual_eans'] else 'source_ean_missing' if not eans else None
    if reason is None and await db.scalar(select(ProductIdentifier.id).where(
            ProductIdentifier.value.in_(comparable_eans), ProductIdentifier.product_id != facts['product_id']).limit(1)):
        reason = 'ean_conflict'
    return {'available': reason is None, 'selected': selected, 'eans': eans, 'reason': reason,
            '_existing': existing}


async def inspect_link(db, product_id, source_id, *, lock=False, copy_supplier_eans=False):
    """No commits or writes; callers can freeze this hash in another workflow."""
    if lock:
        await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
    facts = await _product(db, product_id, lock=lock, exclude_source=source_id)
    query = select(SupplierProduct).where(SupplierProduct.id == source_id).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update(read=True)
    source_row = await db.scalar(query)
    if source_row is None:
        raise SupplierCardLinkError('supplier_card_links_source_missing', 404)
    supplier = (await db.scalar(select(Supplier).where(Supplier.id == source_row.supplier_id)
        .with_for_update(read=True).execution_options(populate_existing=True))) if lock else \
        await db.get(Supplier, source_row.supplier_id, populate_existing=True)
    if supplier is None:
        raise SupplierCardLinkError('supplier_card_links_supplier_missing', 404)
    source = _source(source_row, supplier)
    errors, warnings = list(source['import_blockers']), list(source['warnings'])
    if not facts['is_active']:
        errors.append('inactive_product')
    if not source['active']:
        errors.append('supplier_source_inactive')
    aliases = list(await db.scalars(select(ProductIdentifier).where(
        ProductIdentifier.identifier_type == IdentifierType.supplier_sku,
        ProductIdentifier.supplier_id == supplier.id, ProductIdentifier.value == source['supplier_sku'])))
    owners = {row.product_id for row in aliases}
    links = list(await db.scalars(select(ProductSupplySource).where(ProductSupplySource.supplier_product_id == source_id)))
    owners.update(row.product_id for row in links)
    if owners - {product_id}:
        errors.append('supplier_source_multiple_products')
    other_sources = list(await db.scalars(select(ProductSupplySource.supplier_product_id)
        .join(SupplierProduct, SupplierProduct.id == ProductSupplySource.supplier_product_id)
        .where(ProductSupplySource.product_id == product_id, SupplierProduct.supplier_id == supplier.id,
               ProductSupplySource.supplier_product_id != source_id)))
    if other_sources:
        errors.append('supplier_link_already_configured')
    identity = RemoteIdentity(0, '', barcodes=tuple(source['eans']),
        supplier_id=supplier.id, supplier_sku=source['supplier_sku'], expected_product_id=product_id)
    resolution = (await load_identity_index(db, 0, [identity], local=True)).resolve(identity)
    if resolution.status == 'conflict' or resolution.product_id != product_id:
        errors.extend(resolution.reasons or ['identity_unresolved'])
    try:
        local_axes, source_axes = axes(facts['attributes']), axes(source['attributes'])
        if any(local_axes[key] != source_axes[key] for key in local_axes.keys() & source_axes.keys()):
            errors.append('variant_axes_conflict')
        if not local_axes or not source_axes or local_axes.keys() != source_axes.keys():
            warnings.append('variant_axes_missing')
    except product_links.LinkError:
        local_axes, source_axes = {}, {}
        errors.append('variant_axes_ambiguous')
    local_brand = None
    if source['brand']:
        local_brand = await db.scalar(select(func.coalesce(
            ProductEditorOverride.data['common']['brand'].astext, Product.brand))
            .select_from(Product).outerjoin(ProductEditorOverride, ProductEditorOverride.product_id == Product.id)
            .where(Product.id == product_id))
        if local_brand:
            from inventory_hub.services.sku_policy import brand_key, registered_brand_id
            if brand_key(local_brand) != brand_key(source['brand']):
                left, right = await registered_brand_id(db, local_brand), await registered_brand_id(db, source['brand'])
                if left is None or left != right:
                    errors.append('sku_brand_conflict')
    manual_eans = verified_barcodes(facts['manual_eans']) if facts['manual_eans'] is not None else ()
    if manual_eans and source['eans'] and not barcode_keys(manual_eans) & barcode_keys(source['eans']):
        errors.append('manual_barcode_conflict')
    if not facts['eans'] or not source['eans']:
        warnings.append('barcode_missing')
    own_alias = any(row.product_id == product_id for row in aliases)
    own_links = [row for row in links if row.product_id == product_id]
    if len(own_links) > 1:
        errors.append('supplier_source_multiple_links')
    link = own_links[0] if own_links else None
    policy = _policy(link)
    if link is not None and (not link.is_active or not link.orderable):
        warnings.append('supplier_link_disabled')
    planned = ([] if own_alias else ['supplier_alias']) + ([] if link is not None else ['supplier_source'])
    adoption = await _ean_adoption(db, facts, source, selected=copy_supplier_eans)
    if copy_supplier_eans:
        if not adoption['available']:
            errors.append('supplier_card_links_ean_unavailable')
        else:
            planned.append('supplier_eans')
    # Adding these exact relations must not itself invalidate integration replay.
    # Independent source policy, all existing opposing owners and actual identity
    # evidence remain frozen. Stock, availability and prices are not identity.
    frozen = {'product_hash': product_links._facts_hash(facts), 'source': {
        key: source[key] for key in ('id', 'supplier_id', 'supplier_code', 'supplier_sku', 'eans', 'group_code', 'active')},
        'source_axes': source_axes, 'brands': {'product': local_brand, 'source': source['brand']},
        'owners_other_than_target': sorted(owners - {product_id}),
        'other_source_ids': sorted(set(other_sources)), 'source_policy': policy,
        'source_import_blockers': sorted(source['import_blockers'])}
    if copy_supplier_eans:
        frozen['ean_adoption'] = adoption
    return {'preview_hash': digest(frozen), 'product': _public_product(facts), 'source': source,
        'link_status': 'conflict' if errors else 'ready' if planned else 'already_linked',
        'warnings': sorted(set(warnings)), 'errors': sorted(set(errors)),
        'planned_changes': planned, 'identity': frozen,
        'ean_adoption': {key: value for key, value in adoption.items() if not key.startswith('_')}}


async def _record_ean_copy(db, inspected, request_id, actor):
    """Reuse editor revision/audit records without calling its committing save API."""
    product_id = inspected['product']['id']
    eans = inspected['ean_adoption']['eans']
    override = await db.scalar(select(ProductEditorOverride).where(ProductEditorOverride.product_id == product_id)
                               .with_for_update().execution_options(populate_existing=True))
    before = deepcopy(override.data if override else {})
    after = deepcopy(before)
    after.setdefault('variant', {})['eans'] = list(eans)
    revision = override.revision + 1 if override else 1
    audit_id = str(uuid5(NAMESPACE_URL, 'inventory-hub:supplier-eans:' + str(request_id)))
    payload = {'operation': 'supplier_ean_copy', 'product_id': product_id,
        'supplier_product_id': inspected['source']['id'], 'eans': eans, 'actor': actor or {'source': 'operator_token'}}
    saved = ProductEditorSave(request_id=audit_id, input_hash=digest(payload), result={
        'request_id': audit_id, 'status': 'completed', 'external_write_enabled': False,
        'results': [{'product_id': product_id, 'status': 'saved', 'errors': []}], **payload})
    db.add(saved)
    await db.flush()
    for index, value in enumerate(eans):
        db.add(ProductIdentifier(product_id=product_id, value=value,
            identifier_type=ProductIdentifierService.classify_barcode(value), is_primary=index == 0,
            notes='Explicit verified EAN copy from supplier product ' + str(inspected['source']['id'])))
    if override:
        override.data, override.revision, override.updated_at = after, revision, now()
    else:
        db.add(ProductEditorOverride(product_id=product_id, revision=revision, data=after, updated_at=now()))
    # Audit before/after are a field patch; the editor displays their EAN diff.
    db.add(ProductEditorAudit(request_id=audit_id, product_id=product_id, revision=revision,
        before_data={'eans': [], 'overrides': before},
        after_data={'eans': list(eans), 'overrides': after, 'source': payload}))


async def apply_link(db, product_id, source_id, *, expected_hash, actor=None,
                     copy_supplier_eans=False, request_id=None):
    """Atomic within the caller's transaction, including import-draft saves."""
    inspected = await inspect_link(db, product_id, source_id, lock=True, copy_supplier_eans=copy_supplier_eans)
    if inspected['preview_hash'] != expected_hash:
        raise SupplierCardLinkError('supplier_card_links_identity_changed')
    if inspected['errors']:
        raise SupplierCardLinkError('supplier_card_links_identity_conflict')
    if copy_supplier_eans and not request_id:
        raise SupplierCardLinkError('supplier_card_links_invalid_request', 422)
    source = inspected['source']
    changes = list(inspected['planned_changes'])
    if 'supplier_alias' in changes:
        await ProductIdentifierService(db).add_identifier(product_id, source['supplier_sku'],
            IdentifierType.supplier_sku, supplier_id=source['supplier_id'],
            notes='Explicit supplier-card association; canonical SKU retained')
    if 'supplier_source' in changes:
        db.add(ProductSupplySource(product_id=product_id, supplier_product_id=source_id))
    if 'supplier_eans' in changes:
        await _record_ean_copy(db, inspected, request_id, actor)
    await db.flush()
    return {'status': 'linked' if changes else 'already_linked', 'product_id': product_id,
            'supplier_product_id': source_id, 'changes': changes,
            'copied_eans': inspected['ean_adoption']['eans'] if 'supplier_eans' in changes else []}


def _existing_source(row, supplier):
    """An old link remains visible even when its catalog can no longer validate."""
    try:
        return _source(row, supplier)
    except SupplierCardLinkError:
        return {'id': row.id, 'supplier_id': supplier.id, 'supplier_code': supplier.code,
            'supplier_name': supplier.name, 'supplier_sku': row.supplier_sku, 'shop_sku': '',
            'name': row.name, 'eans': list(verified_barcodes((row.ean,))), 'attributes': [],
            'group_code': row.supplier_group_code,
            'active': bool(supplier.is_active and row.is_active and not row.is_discontinued),
            'last_seen_at': row.last_seen_at.isoformat() if row.last_seen_at else None,
            'warnings': ['supplier_catalog_unavailable'], 'import_blockers': []}


async def options(db, product_id):
    facts = await _product(db, product_id)
    suppliers = list(await db.scalars(select(Supplier).where(Supplier.is_active.is_(True)).order_by(Supplier.name)))
    current = (await db.execute(select(ProductSupplySource, SupplierProduct, Supplier)
        .join(SupplierProduct, SupplierProduct.id == ProductSupplySource.supplier_product_id)
        .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
        .where(ProductSupplySource.product_id == product_id).order_by(Supplier.name, SupplierProduct.supplier_sku))).all()
    return {'product': _public_product(facts),
        'suppliers': [{'id': row.id, 'code': row.code, 'name': row.name} for row in suppliers],
        'current_links': [{**_existing_source(source, supplier), 'link_id': link.id,
            'is_active': link.is_active, 'orderable': link.orderable, 'is_primary': link.is_primary,
            'priority': link.priority} for link, source, supplier in current]}


async def candidates(db, payload):
    facts = await _product(db, payload.product_id)
    supplier = await db.scalar(select(Supplier).where(Supplier.code == payload.supplier_code))
    if supplier is None or not supplier.is_active:
        raise SupplierCardLinkError('supplier_card_links_supplier_missing', 404)
    aliases = set(await db.scalars(select(ProductIdentifier.value).where(
        ProductIdentifier.product_id == payload.product_id, ProductIdentifier.supplier_id == supplier.id,
        ProductIdentifier.identifier_type == IdentifierType.supplier_sku)))
    codes = {facts['sku'], *aliases}
    conditions = [SupplierProduct.supplier_sku.in_(codes)]
    ean_match = False
    if facts['eans']:
        ean_match = text("""EXISTS (SELECT 1 FROM regexp_split_to_table(
            coalesce(supplier_products.ean,''), '[/,;[:space:]]+') code
            WHERE code=ANY(CAST(:candidate_eans AS text[])))""")
        conditions.append(ean_match)
    params = {'candidate_eans': sorted({value for raw in facts['eans']
                                     for value in ProductIdentifierService.gtin_equivalents(raw)})}
    if payload.query:
        value = payload.query
        conditions = [SupplierProduct.supplier_sku.ilike('%' + value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%', escape='\\'),
                      SupplierProduct.name.icontains(payload.query, autoescape=True),
                      SupplierProduct.ean.icontains(payload.query, autoescape=True)]
    else:
        tokens = [word for word in facts['name'].split() if len(word) >= 4][:3]
        conditions.extend(SupplierProduct.name.icontains(word, autoescape=True) for word in tokens)
    rows = list((await db.scalars(select(SupplierProduct).where(SupplierProduct.supplier_id == supplier.id,
        SupplierProduct.is_active.is_(True), SupplierProduct.is_discontinued.is_(False), or_(*conditions))
        .order_by(SupplierProduct.supplier_sku.in_(codes).desc(), case((ean_match, 0), else_=1),
                  SupplierProduct.supplier_sku).limit(101), params)).all())
    candidates_out, invalid = [], False
    for row in rows[:100]:
        try:
            item = _source(row, supplier)
        except SupplierCardLinkError:
            invalid = True
            continue
        reasons = []
        if item['supplier_sku'] in aliases:
            reasons.append('supplier_sku')
        if item['shop_sku'] == facts['sku'] or item['supplier_sku'] == facts['sku']:
            reasons.append('exact_sku')
        if barcode_keys(item['eans']) & barcode_keys(facts['eans']):
            reasons.append('exact_ean')
        if not reasons:
            reasons.append('search' if payload.query else 'name')
        item['match_reasons'] = reasons
        candidates_out.append(item)
    candidates_out.sort(key=lambda item: (not any(reason in item['match_reasons'] for reason in ('supplier_sku', 'exact_sku', 'exact_ean')), item['supplier_sku']))
    return {'product': _public_product(facts), 'rows': candidates_out[:50],
            'truncated': len(rows) > 100 or len(candidates_out) > 50,
            'warnings': ['supplier_catalog_unavailable'] if invalid else []}


def public(row):
    inspected = row.document['inspection']
    product = inspected['product']
    if row.status == 'completed' and (row.result or {}).get('copied_eans'):
        product = {**product, 'eans': row.result['copied_eans']}
    return {'id': row.id, 'status': row.status, 'preview_hash': row.preview_hash,
        'expires_at': row.expires_at.isoformat(), 'result': row.result,
        **{key: inspected[key] for key in ('source', 'link_status', 'warnings', 'errors', 'planned_changes')},
        'product': product,
        'ean_adoption': inspected.get('ean_adoption')}


async def get(db, identifier):
    row = await db.get(SupplierCardLinkPreview, str(identifier))
    if row is None:
        raise SupplierCardLinkError('supplier_card_links_not_found', 404)
    return public(row)


async def preview(db, payload, *, actor=None):
    # Omitting the new default preserves retries of previews made by older clients.
    request_hash = digest(payload.model_dump(mode='json', exclude_defaults=True))
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
    existing = await db.get(SupplierCardLinkPreview, str(payload.request_id), populate_existing=True)
    if existing is not None:
        if existing.request_hash != request_hash:
            raise SupplierCardLinkError('supplier_card_links_request_changed')
        return public(existing)
    inspected = await inspect_link(db, payload.product_id, payload.supplier_product_id, lock=True,
                                   copy_supplier_eans=payload.copy_supplier_eans)
    at = now()
    document = {'inspection': inspected, 'created_by': actor or {'source': 'operator_token'}}
    row = SupplierCardLinkPreview(id=str(payload.request_id), product_id=payload.product_id,
        supplier_product_id=payload.supplier_product_id, request_hash=request_hash,
        preview_hash=digest(document), document=document, status='prepared', created_at=at,
        expires_at=at + timedelta(minutes=PREVIEW_MINUTES))
    db.add(row)
    await db.flush()
    result = public(row)
    await db.commit()
    return result


def _check_preview(row, payload):
    if payload.preview_hash != row.preview_hash or digest(row.document) != row.preview_hash:
        raise SupplierCardLinkError('supplier_card_links_preview_changed')
    if row.document['inspection']['link_status'] not in ('ready', 'already_linked'):
        raise SupplierCardLinkError('supplier_card_links_invalid_selection', 422)


async def apply(db, identifier, payload, *, actor=None):
    # The identity lock and durable row serialize every attempt before any
    # checks. A committed expiry fences every delayed/lost-response apply.
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
    row = await db.scalar(select(SupplierCardLinkPreview).where(SupplierCardLinkPreview.id == str(identifier))
                          .with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise SupplierCardLinkError('supplier_card_links_not_found', 404)
    _check_preview(row, payload)
    if row.status == 'completed':
        return public(row)
    try:
        if now() >= row.expires_at:
            raise SupplierCardLinkError('supplier_card_links_preview_expired')
        result = await apply_link(db, row.product_id, row.supplier_product_id,
            expected_hash=row.document['inspection']['preview_hash'], actor=actor,
            copy_supplier_eans=bool(row.document['inspection'].get('ean_adoption', {}).get('selected')),
            request_id=row.id)
    except SupplierCardLinkError as error:
        # Domain errors are raised before relation writes. Other exceptions,
        # including an uncertain database commit, never promise known_unapplied.
        row.expires_at = min(row.expires_at, now())
        await db.commit()
        error.known_unapplied = True
        raise
    row.status, row.completed_at = 'completed', now()
    row.result = {**result, 'completed_at': row.completed_at.isoformat(),
                  'applied_by': actor or {'source': 'operator_token'}}
    await db.flush()
    response = public(row)
    await db.commit()
    return response
