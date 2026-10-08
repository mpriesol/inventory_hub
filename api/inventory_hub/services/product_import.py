"""One editable, durable staging workflow before Hub save and explicit shop create.

The supplier snapshot is immutable. Feed refresh and AI cannot overwrite manual
cells. Remote delivery uses catalog_import's existing create-only journal.
"""
import asyncio
from collections import Counter, defaultdict
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from inventory_hub.ai_content_models import AiJob
from inventory_hub.ai_content_types import BatchRequest, Content, Policy, Target
from inventory_hub.catalog_types import CatalogProduct, ShopImportOptions, ShopImportPreviewRequest
from inventory_hub.db_models import IdentifierType, Product, ProductGroup, ProductIdentifier, Shop, Supplier
from inventory_hub.db_models_ext import ProductVariantAttribute, ShopProduct
from inventory_hub.product_editor_models import ProductEditorOverride
from inventory_hub.product_import_models import ProductImportDraft
from inventory_hub.product_import_types import ImportValues
from inventory_hub.services import ai_content, catalog_import
from inventory_hub.services.catalog import CatalogError, selected_products, supplier_config
from inventory_hub.services.catalog_identity import parent_shop_code
from inventory_hub.services.catalog_merchandising import apply_availability, availability_policy, category_chain
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK

COMMON_FIELDS = {'group_name', 'brand', 'description_html', 'short_description', 'seo_title',
                 'seo_description', 'seo_url', 'category_code', 'parameters', 'metadata', 'ai_enabled'}
AI_FIELDS = {'name', 'group_name', 'short_description', 'description_html', 'seo_title', 'seo_description', 'parameters', 'metadata', 'category_code'}


def money(value):
    return format(Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP), '.2f') if value is not None else None


def content_hash(document):
    return ai_content.digest([{'id': r['id'], 'values': r['values']} for r in document['rows']])


async def get_draft(db, id, lock=False):
    statement = select(ProductImportDraft).where(ProductImportDraft.id == id)
    if lock:
        statement = statement.with_for_update()
    draft = await db.scalar(statement)
    if not draft:
        raise CatalogError('import_draft_not_found', 'Import draft was not found', 404)
    return draft


def expect(draft, revision):
    if draft.revision != revision:
        raise CatalogError('import_draft_changed', 'This draft changed; reload before editing', 409)


def changed(draft, document, note, *, dirty=True):
    draft.revision += 1
    draft.updated_at = ai_content.now()
    document['events'] = [*document.get('events', []), {'at': draft.updated_at.isoformat(), 'revision': draft.revision, 'note': note}]
    draft.document = document
    if dirty:
        draft.status = 'draft'
        document['publication'] = None


def publication_result(draft):
    preview = draft.document.get('publication')
    if not preview:
        return None
    try:
        return catalog_import.import_result(draft.shop, preview['preview_id'])
    except CatalogError as error:
        if error.code == 'import_not_started':
            return None
        raise


def assert_editable(draft):
    result = publication_result(draft)
    if result and (result['status'] in ('queued', 'running') or any(i['status'] == 'uncertain' for i in result['items'])):
        raise CatalogError('import_publication_pending', 'Reconcile the existing publication before changing this draft', 409)


async def public(db, draft):
    document = draft.document
    rows = deepcopy(document['rows'])
    ids = {r.get('ai_job_id') for r in rows if r.get('ai_job_id')}
    jobs = {j.id: ai_content.summary(j) for j in (await db.scalars(select(AiJob).where(AiJob.id.in_(ids)))).all()} if ids else {}
    for row in rows:
        row.pop('source', None)
        row.pop('ai_baseline', None)
        row['ai_job'] = jobs.get(row.pop('ai_job_id', None))
    return {'id': draft.id, 'revision': draft.revision, 'status': draft.status, 'supplier': draft.supplier,
            'shop': draft.shop, 'feed_key': document['feed_key'], 'options': document['options'],
            'categories': document['categories'], 'rows': rows, 'publication': document.get('publication'),
            'publication_result': publication_result(draft), 'created_at': draft.created_at,
            'updated_at': draft.updated_at}


async def history(db):
    drafts = (await db.scalars(select(ProductImportDraft).order_by(ProductImportDraft.updated_at.desc()).limit(100))).all()
    return {'items': [{'id': d.id, 'revision': d.revision, 'status': d.status, 'supplier': d.supplier, 'shop': d.shop,
                       'updated_at': d.updated_at, 'rows_count': len(d.document.get('rows', []))} for d in drafts]}


def to_source(row):
    product = CatalogProduct.model_validate(row['source'])
    v = ImportValues.model_validate(row['values'])
    prices = product.prices.model_copy(update={'currency': v.currency,
        'vat_percent': Decimal(v.vat_percent) if v.vat_percent else None,
        'purchase_net': Decimal(v.purchase_net) if v.purchase_net else None,
        'purchase_gross': None, 'retail_gross': Decimal(v.retail_gross) if v.retail_gross else None,
        'retail_net': None, 'discount_net': None, 'discount_gross': None})
    if prices.vat_percent is not None:
        factor = 1 + prices.vat_percent / 100
        prices.purchase_gross = prices.purchase_net * factor if prices.purchase_net is not None else None
        prices.retail_net = prices.retail_gross / factor if prices.retail_gross is not None else None
    return product.model_copy(update={'code': v.supplier_code, 'shop_code': v.code, 'name': v.name,
        'group_name': v.group_name or None, 'brand': v.brand or None, 'manufacturer_code': v.manufacturer_code or None,
        'eans': v.eans, 'images': v.images, 'description': v.description_html, 'parameters': v.parameters,
        'variant_attributes': v.variant_attributes, 'prices': prices})


def validate_rows(document):
    rows = document['rows']
    codes = Counter(r['values']['code'].casefold() for r in rows)
    eans = Counter(e for r in rows for e in r['values']['eans'])
    groups = defaultdict(list)
    options = ShopImportOptions.model_validate(document['options'])
    for row in rows:
        source = to_source(row)
        row['errors'] = list(source.import_blockers)
        row['warnings'] = list(source.warnings)
        if row['values']['manufacturer_code']:
            row['warnings'].append('manufacturer_code_source_only')
        from inventory_hub.services.ai_content_validation import has_active_html
        if any(has_active_html(row['values'][key]) for key in ('description_html', 'short_description')):
            row['errors'].append('ai_unsafe_html')
        if codes[source.shop_code.casefold()] > 1 or any(eans[e] > 1 for e in source.eans):
            row['errors'].append('selection_identity_conflict')
        if source.prices.vat_percent is None:
            row['errors'].append('missing_vat')
        if source.prices.currency != options.currency:
            row['errors'].append('currency_mismatch')
        if not row['values']['sale_gross'] or Decimal(row['values']['sale_gross']) <= 0:
            row['errors'].append('missing_price')
        category = row['values']['category_code']
        try:
            row['categories'] = category_chain(document['categories'], category)
            if category and any(c.get('parent_code') == category and c.get('assignable', True) for c in document['categories']):
                row['errors'].append('category_must_be_leaf')
        except CatalogError as error:
            row['categories'] = []
            row['errors'].append(error.code)
        if not category:
            row['warnings'].append('category_missing')
        if source.prices.purchase_net is not None and source.prices.vat_percent is not None and row['values']['sale_gross']:
            if Decimal(row['values']['sale_gross']) < source.prices.purchase_net * (1 + source.prices.vat_percent / 100):
                row['warnings'].append('sale_below_purchase')
        groups[row['group_key']].append(row)
    for family in groups.values():
        sources = [to_source(r) for r in family]
        if family[0]['is_variant']:
            for key in COMMON_FIELDS - {'parameters'}:
                if any(r['values'][key] != family[0]['values'][key] for r in family):
                    for row in family:
                        row['errors'].append('import_family_field_conflict:' + key)
            item = catalog_import.build_item(sources, options, {}, True,
                {r['id']: Decimal(r['values']['sale_gross']) for r in family if r['values']['sale_gross']})
            for row in family:
                row['errors'].extend(item.errors)
                row['errors'] = sorted(set(row['errors']))


async def create(db, request):
    fingerprint = ai_content.digest(request.model_dump(mode='json'))
    await db.execute(text('SELECT pg_advisory_xact_lock(691432122)'))
    old = await db.get(ProductImportDraft, request.request_id.hex)
    if old:
        if old.request_hash != fingerprint:
            raise CatalogError('import_request_reused', 'Use a new request ID for a changed selection', 409)
        return await public(db, old)
    if not await db.scalar(select(Shop.id).where(Shop.code == request.shop, Shop.is_active.is_(True))):
        raise CatalogError('shop_not_found', 'Choose an active Hub shop', 404)
    # Mapping saves and catalog refresh use the same supplier lock. Keep the
    # complete selected snapshot on one base/shop mapping revision.
    from inventory_hub.services.catalog import _lock_key
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': _lock_key(request.supplier)})
    source = await selected_products(db, request.supplier, request.feed_key, request.product_ids, request.run_id)
    from inventory_hub.services.feed_mapping import apply_shop_mapping
    products = [await apply_shop_mapping(db, p, request.shop) for p in source]
    remote = await asyncio.to_thread(catalog_import.cached_import_options, request.shop)
    cfg = supplier_config(request.supplier)
    policy = availability_policy(request.supplier, cfg)
    rows = []
    for p in products:
        availability = {}
        apply_availability(availability, [p], policy)
        try:
            sale = money(catalog_import._prices(p, request.options, cfg, True)[0]['pricelists'][0]['price_original'])
        except (ValueError, ArithmeticError):
            sale = None
        values = ImportValues(code=p.shop_code, supplier_code=p.code, name=p.name, group_name=p.group_name or '',
            brand=p.brand or '', manufacturer_code=p.manufacturer_code or '', eans=p.eans, images=p.images,
            description_html='\n'.join(filter(None, [p.description, p.safety_information])),
            short_description=getattr(p, 'short_description', ''), seo_title=getattr(p, 'seo_title', '') or p.name,
            seo_description=getattr(p, 'seo_description', '') or ai_content.text_of(p.description)[:160],
            seo_url=getattr(p, 'seo_url', ''), metadata=getattr(p, 'metadata', {}),
            category_code=request.options.category_code or getattr(p, 'target_category_code', None),
            parameters=p.parameters, variant_attributes=p.variant_attributes,
            purchase_net=money(p.prices.purchase_net), retail_gross=money(p.prices.retail_gross), sale_gross=sale,
            vat_percent=money(p.prices.vat_percent), currency=p.prices.currency,
            availability=availability['availability'])
        provenance = {key: 'feed' for key in values.model_fields}
        for key in ('sale_gross', 'availability', 'seo_title', 'seo_description'):
            provenance[key] = 'derived'
        if values.category_code:
            provenance['category_code'] = 'manual' if request.options.category_code else 'mapping'
        rows.append({'id': p.id, 'group_key': parent_shop_code(p), 'group_name': p.group_name,
            'is_variant': p.variant_relationship == 'explicit' and bool(p.group_code),
            'source_category': p.category, 'mapping_revision': getattr(p, 'mapping_revision', 0),
            'mapping_provenance': getattr(p, 'mapping_provenance', {}), 'source': p.model_dump(mode='json'),
            'values': values.model_dump(mode='json'), 'manual_fields': ['category_code'] if request.options.category_code else [],
            'provenance': provenance, 'hub_product_id': None})
    families = defaultdict(list)
    for row in rows:
        if row['is_variant']:
            families[row['group_key']].append(row)
    for family in families.values():
        axes = {a['name'].casefold() for r in family for a in r['values']['variant_attributes']}
        shared = set.intersection(*({(p['name'], p['value']) for p in r['values']['parameters']} for r in family))
        common_parameters = [p for p in family[0]['values']['parameters'] if (p['name'], p['value']) in shared and p['name'].casefold() not in axes]
        for row in family:
            row['values']['parameters'] = deepcopy(common_parameters)
        for key in ('seo_title', 'seo_description'):
            if all(r['mapping_provenance'].get('fields', {}).get(key, {}).get('fallback') or not r['source'].get(key) for r in family):
                shared_seo = (family[0]['values']['group_name'] or family[0]['values']['name'])[:80] if key == 'seo_title' else family[0]['values'][key]
                for row in family:
                    row['values'][key] = shared_seo
                    row['provenance'][key] = 'derived'
    document = {'feed_key': request.feed_key, 'run_id': request.run_id, 'options': request.options.model_dump(mode='json'),
                'categories': remote['categories'], 'rows': rows, 'publication': None, 'events': [],
                'target': catalog_import._target(catalog_import.shop_config(request.shop))}
    validate_rows(document)
    draft = ProductImportDraft(id=request.request_id.hex, request_hash=fingerprint, revision=1,
                              supplier=request.supplier, shop=request.shop, status='draft', document=document)
    db.add(draft)
    await db.flush()
    return await public(db, draft)


async def patch_rows(db, draft, request):
    expect(draft, request.expected_revision)
    assert_editable(draft)
    document = deepcopy(draft.document)
    rows = {r['id']: r for r in document['rows']}
    if len({p.id for p in request.rows}) != len(request.rows) or any(p.id not in rows for p in request.rows):
        raise CatalogError('import_row_not_selected', 'Edits must identify distinct selected products', 422)
    # Conflicting edits to a shared family field are rejected rather than choosing the last row.
    shared = {}
    from inventory_hub.supplier_prefix import canonical_supplier_sku, get_supplier_prefix
    prefix = get_supplier_prefix(supplier_config(draft.supplier))
    for patch in request.rows:
        row = rows[patch.id]
        values = deepcopy(patch.values)
        if 'supplier_code' in values and 'code' not in values:
            if not isinstance(values['supplier_code'], str) or not values['supplier_code'].strip():
                raise CatalogError('import_invalid_cells', 'Check the supplier code', 422)
            values['code'] = canonical_supplier_sku(prefix, values['supplier_code'])
        try:
            parsed = ImportValues.model_validate({**row['values'], **values})
        except (ValidationError, ValueError):
            raise CatalogError('import_invalid_cells', 'Check the edited field values', 422) from None
        if parsed.code != canonical_supplier_sku(prefix, parsed.supplier_code):
            raise CatalogError('import_code_prefix', 'The product code must use the configured supplier prefix', 422)
        if row.get('hub_product_id') and any(k in values for k in ('code', 'supplier_code', 'eans')):
            raise CatalogError('import_identity_saved', 'Edit saved product identifiers in the product editor', 409)
        for key in values:
            if key not in ImportValues.model_fields:
                raise CatalogError('import_invalid_cells', 'Unknown import field', 422)
            value = parsed.model_dump(mode='json')[key]
            family_key = (row['group_key'], key)
            if row['is_variant'] and key in COMMON_FIELDS:
                if family_key in shared and shared[family_key] != value:
                    raise CatalogError('import_family_edit_conflict', 'Shared family fields must have the same value', 422)
                shared[family_key] = value
                targets = [r for r in rows.values() if r['group_key'] == row['group_key']]
            else:
                targets = [row]
            for target in targets:
                target['values'][key] = value
                target['manual_fields'] = sorted(set(target['manual_fields']) | {key})
                target['provenance'][key] = 'manual'
    validate_rows(document)
    changed(draft, document, 'Manual import cells edited')
    await db.flush()
    return await public(db, draft)


async def save(db, draft, request):
    expect(draft, request.expected_revision)
    assert_editable(draft)
    document = deepcopy(draft.document)
    validate_rows(document)
    if any(r['errors'] for r in document['rows']):
        raise CatalogError('import_invalid_rows', 'Resolve invalid rows before saving products', 422)
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
    products = [to_source(r) for r in document['rows']]
    existing, conflicts = await catalog_import.local_identities(db, products)
    if conflicts:
        raise CatalogError('local_identity_conflict', 'Local identifiers require reconciliation', 409)
    catalog_import._claim_product_prefix(products)
    supplier_id = await db.scalar(select(Supplier.id).where(Supplier.code == draft.supplier))
    for row, source in zip(document['rows'], products):
        v = row['values']
        product_id = existing.get(row['id'])
        owned = bool(row.get('hub_product_id') and product_id == row['hub_product_id'] and row.get('created_in_draft'))
        if product_id and not owned:
            row['hub_product_id'] = product_id
            row['warnings'] = sorted(set(row['warnings']) | {'already_in_hub_unchanged'})
            continue
        if owned and await db.scalar(select(ShopProduct.id).where(ShopProduct.product_id == product_id)):
            row['warnings'] = sorted(set(row['warnings']) | {'already_in_shop_hub_unchanged'})
            continue
        group_id = None
        if row['is_variant']:
            group = await db.scalar(select(ProductGroup).where(ProductGroup.code == row['group_key']))
            if group is None:
                group = ProductGroup(code=row['group_key'], name=v['group_name'] or v['name'], brand=v['brand'] or None,
                                     main_image_url=v['images'][0] if v['images'] else None)
                db.add(group)
                await db.flush()
            group_id = group.id
        if product_id is None:
            product = Product(sku=source.shop_code, supplier_id=supplier_id, name=source.name, brand=source.brand,
                group_id=group_id, validation_required=True, validation_reason='Hub import draft; shop publication separate',
                created_from_source='product_import_draft', source_supplier_product_id=source.id,
                supplier_feed_data=row['source'], supplier_feed_synced_at=source.fetched_at)
            db.add(product)
            await db.flush()
            product_id = product.id
            row['created_in_draft'] = True
            identifiers = ProductIdentifierService(db)
            for i, ean in enumerate(source.eans):
                await identifiers.add_identifier(product_id, ean, is_primary=i == 0)
            await db.execute(insert(ProductIdentifier).values(product_id=product_id, supplier_id=supplier_id,
                identifier_type=IdentifierType.supplier_sku, value=source.code, is_primary=False)
                .on_conflict_do_nothing(index_elements=[ProductIdentifier.supplier_id, ProductIdentifier.value],
                    index_where=text("identifier_type = 'supplier_sku'")))
            for order, attribute in enumerate(source.variant_attributes):
                db.add(ProductVariantAttribute(product_id=product_id, attribute_name=attribute.name,
                    attribute_value=attribute.value, display_order=order))
        override = await db.get(ProductEditorOverride, product_id, with_for_update=True)
        # Do not overwrite changes made in the ordinary product editor after a draft save.
        if override and row.get('editor_revision') != override.revision:
            raise CatalogError('import_hub_product_changed', 'Product was edited elsewhere; use the product editor', 409)
        data = deepcopy(override.data) if override else {}
        data.update(common={'name': v['name'], 'brand': v['brand'], 'image_url': v['images'][0] if v['images'] else None},
            variant={'sale_price_gross': v['sale_gross'], 'vat_rate': v['vat_percent'], 'attributes': v['variant_attributes']},
            import_fields=deepcopy(v), import_manual_fields=row['manual_fields'])
        if override:
            override.data, override.revision, override.updated_at = data, override.revision + 1, ai_content.now()
        else:
            override = ProductEditorOverride(product_id=product_id, revision=1, data=data)
            db.add(override)
        row['editor_revision'] = override.revision
        row['hub_product_id'] = product_id
    await db.flush()
    from inventory_hub.services.supplier_links import reconcile_supplier_links
    links = await reconcile_supplier_links(db, draft.supplier, product_ids=[r['hub_product_id'] for r in document['rows']])
    if links['conflicts']:
        raise CatalogError('local_identity_conflict', 'Supplier source links require reconciliation', 409)
    document['saved_hash'] = content_hash(document)
    changed(draft, document, 'Products saved in Hub; no shop writes', dirty=False)
    draft.status = 'saved'
    await db.flush()
    return await public(db, draft)


def overlay_payload(item, rows, language, categories):
    """Apply typed cells only, retaining importer visibility and no-stock guards."""
    family = [r for r in rows if r['id'] in item.product_ids]
    first = family[0]['values']
    payload = item.payload
    description = payload['descriptions'][0]
    description.update(title=first['group_name'] if item.variants_count and first['group_name'] else first['name'],
        long_description=first['description_html'], short_description=first['short_description'],
        seo_title=first['seo_title'], seo_description=first['seo_description'])
    if first['seo_url']:
        description['seo_url'] = first['seo_url']
    payload['categories'] = category_chain(categories, first['category_code'])
    payload['metas'] = [m for m in payload.get('metas', []) if m['key'] == 'validation_required'] + [
        {'key': key, 'value': value} for key, value in first['metadata'].items()]
    parent_parameters = to_source(family[0]).parameters
    if item.variants_count:
        axes = {p['name'].casefold() for row in family for p in row['values']['variant_attributes']}
        shared = set.intersection(*({(p['name'], p['value']) for p in row['values']['parameters']} for row in family))
        parent_parameters = [p for p in parent_parameters if (p.name, p.value) in shared and p.name.casefold() not in axes]
    payload['parameters'] = catalog_import._parameters(parent_parameters, language)
    if item.variants_count:
        by_code = {r['values']['code']: r['values'] for r in family}
        for variant in payload['variants']:
            value = by_code[variant['code']]
            variant['availability'] = value['availability']
    else:
        payload['availability'] = first['availability']
    item.name = description['title']
    return item


async def preview(db, draft, request):
    expect(draft, request.expected_revision)
    assert_editable(draft)
    document = deepcopy(draft.document)
    if draft.status != 'saved' or document.get('saved_hash') != content_hash(document):
        raise CatalogError('import_save_required', 'Save current products in Hub before preparing publication', 409)
    if any(r['errors'] or not r['values']['category_code'] for r in document['rows']):
        raise CatalogError('import_invalid_rows', 'Every product needs a valid leaf category before publication', 422)
    if catalog_import._target(catalog_import.shop_config(draft.shop)) != document['target']:
        raise CatalogError('shop_target_changed', 'The target connection changed', 409)
    snapshot = {'draft_id': draft.id, 'hash': content_hash(document), 'rows': document['rows']}
    options = ShopImportOptions.model_validate(document['options']).model_copy(update={'category_code': None})
    prepared = await catalog_import.create_preview(db, draft.shop, ShopImportPreviewRequest(supplier=draft.supplier,
        feed_key=document['feed_key'], product_ids=[r['id'] for r in document['rows']], options=options,
        sale_price_overrides={r['id']: r['values']['sale_gross'] for r in document['rows']}),
        frozen_products=[to_source(r) for r in document['rows']], staging=snapshot)
    document['publication'] = prepared.model_dump(mode='json')
    changed(draft, document, 'Explicit shop publication preview prepared', dirty=False)
    await db.flush()
    return await public(db, draft)


async def assert_snapshot(db, staging):
    draft = await get_draft(db, staging['draft_id'])
    if draft.status != 'saved' or content_hash(draft.document) != staging['hash']:
        raise CatalogError('import_draft_changed', 'The saved import draft changed after preview', 409)


async def publish(db, draft, request):
    expect(draft, request.expected_revision)
    preview = draft.document.get('publication')
    if not preview or preview['preview_id'] != request.preview_id:
        raise CatalogError('import_preview_changed', 'Confirm the current publication preview', 409)
    await assert_snapshot(db, {'draft_id': draft.id, 'hash': draft.document.get('saved_hash')})
    result, run = catalog_import.queue_import(draft.shop, request.preview_id, request.retry_failed, staging_id=draft.id)
    document = deepcopy(draft.document)
    changed(draft, document, 'Shop publication explicitly confirmed', dirty=False)
    await db.flush()
    return await public(db, draft), run


async def prepare_ai(db, draft, request):
    expect(draft, request.expected_revision)
    assert_editable(draft)
    document = deepcopy(draft.document)
    groups = defaultdict(list)
    for row in document['rows']:
        if row['values']['ai_enabled']:
            groups[row['group_key']].append(row)
    if not groups:
        raise CatalogError('import_ai_selection_empty', 'Choose products for AI enrichment', 422)
    for family in groups.values():
        active_ids = {r.get('ai_job_id') for r in family if r.get('ai_job_id')}
        if active_ids:
            active = (await db.scalars(select(AiJob).where(AiJob.id.in_(active_ids)).with_for_update())).all()
            if any(j.status in ('generating', 'preparing_import') for j in active):
                raise CatalogError('import_ai_already_prepared', 'Finish the running AI preparation first', 409)
            for old in active:
                if old.status in ('estimate', 'queued'):
                    old.reserved_usd = 0
                    ai_content.event(old, 'cancelled', 'Replaced by an explicit new import draft estimate before provider call')
        category = family[0]['values']['category_code']
        options = ShopImportOptions.model_validate(document['options']).model_copy(update={'category_code': category})
        batch = BatchRequest(request_id=uuid4(), supplier=draft.supplier, feed_key=document['feed_key'],
            product_ids=[r['id'] for r in family], ai_product_ids=[r['id'] for r in family],
            category_profiles={r['id']: 'general' if category else 'auto' for r in family}, research=request.research,
            targets=[Target(shop=draft.shop, options=options, policy=Policy(show_cost_estimate=True, confirm_import=True))])
        jobs = await ai_content.create_batch(db, batch, frozen_products=[to_source(r) for r in family], staging_id=draft.id)
        for row in family:
            row['ai_job_id'] = jobs[0]['id']
            row['ai_baseline'] = deepcopy(row['values'])
    changed(draft, document, 'AI estimates prepared from current mapped cells', dirty=False)
    await db.flush()
    return await public(db, draft)


async def start_ai(db, draft, request):
    expect(draft, request.expected_revision)
    assert_editable(draft)
    ids = {r.get('ai_job_id') for r in draft.document['rows'] if r.get('ai_job_id') and r['values']['ai_enabled']}
    jobs = (await db.scalars(select(AiJob).where(AiJob.id.in_(ids)).order_by(AiJob.id).with_for_update())).all()
    estimates = [job for job in jobs if job.status == 'estimate']
    if not estimates:
        raise CatalogError('import_ai_no_estimate', 'No AI estimate is waiting for confirmation', 409)
    for job in estimates:
        await ai_content.start(db, job)
    changed(draft, deepcopy(draft.document), 'AI cost estimates explicitly confirmed', dirty=False)
    await db.flush()
    return await public(db, draft)


def merge_ai_values(row, output, context):
    baseline = row.get('ai_baseline', {})
    if any(row['values'].get(key) != baseline.get(key) for key in ('code', 'supplier_code', 'eans', 'manufacturer_code', 'category_code', 'variant_attributes', 'brand')):
        raise CatalogError('import_ai_identity_changed', 'Product identity or category changed; prepare a new AI job', 409)
    proposed = {'group_name' if row['is_variant'] else 'name': output.title,
        'short_description': output.short_description, 'description_html': output.long_description,
        'seo_title': output.seo_title, 'seo_description': output.meta_description,
        'metadata': {**row['values']['metadata'], **{key: getattr(output, key) for key in ('h1_descriptor', 'future_name', 'h1_descr_suffix') if getattr(output, key)}}}
    registry = (context.get('resolved', {}).get('category') or {}).get('parameters', [])
    if registry:
        allowed = {p['name'] for p in registry if p.get('approved', True)}
        generated = [{'name': p.name, 'value': value} for p in output.parameters if p.product_id is None for value in p.values]
        generated_names = {p['name'] for p in generated}
        preserved = [p for p in row['values']['parameters'] if p['name'] in allowed and p['name'] not in generated_names]
        proposed['parameters'] = preserved + generated
    if not baseline.get('category_code'):
        proposed['category_code'] = context.get('options', {}).get('category_code')
    safety = CatalogProduct.model_validate(row['source']).safety_information
    if safety:
        from inventory_hub.services.catalog_html import clean_description
        proposed['description_html'] += '<h2>Bezpečnostné informácie</h2>' + clean_description(safety)
    for key, value in proposed.items():
        if key not in row['manual_fields'] and row['values'].get(key) == baseline.get(key):
            row['values'][key] = value
            row['provenance'][key] = 'ai'
    row['values'] = ImportValues.model_validate(row['values']).model_dump(mode='json')


async def apply_ai(db, draft, request):
    expect(draft, request.expected_revision)
    assert_editable(draft)
    document = deepcopy(draft.document)
    ids = {r.get('ai_job_id') for r in document['rows'] if r.get('ai_job_id') and r['values']['ai_enabled']}
    jobs = {j.id: j for j in (await db.scalars(select(AiJob).where(AiJob.id.in_(ids)).with_for_update())).all()}
    used = []
    from inventory_hub.services.ai_content_validation import validate_content
    for job in jobs.values():
        if job.status not in ('review', 'ready') or not job.output:
            continue
        output = Content.model_validate(job.output)
        checks = validate_content(output, job.context, (job.usage or {}).get('opened_sources', []))
        if checks['errors']:
            raise CatalogError('ai_validation_failed', '; '.join(checks['errors']), 422)
        for row in document['rows']:
            if row.get('ai_job_id') == job.id and row['values']['ai_enabled']:
                merge_ai_values(row, output, job.context)
        used.append(job)
    if not used:
        raise CatalogError('import_ai_not_ready', 'No completed valid AI result is ready', 409)
    for job in used:
        ai_content.event(job, 'completed', 'AI enrichment applied to import draft; no shop write')
    validate_rows(document)
    changed(draft, document, 'AI enriched untouched cells; manual cells preserved')
    await db.flush()
    return await public(db, draft)
