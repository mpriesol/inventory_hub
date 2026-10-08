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
from inventory_hub.product_import_types import DraftRevision, ImportValues
from inventory_hub.services import ai_content, catalog_import
from inventory_hub.services.catalog import CatalogError, selected_products, supplier_config
from inventory_hub.services.catalog_identity import parent_shop_code
from inventory_hub.services.catalog_merchandising import apply_availability, availability_policy, category_chain
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK

COMMON_FIELDS = {'group_name', 'brand', 'description_html', 'short_description', 'seo_title',
                 'seo_description', 'seo_url', 'category_code', 'parameters', 'metadata', 'ai_enabled', 'ai_category_profile'}
AI_FIELDS = {'name', 'group_name', 'short_description', 'description_html', 'seo_title', 'seo_description', 'parameters', 'metadata', 'category_code'}


def money(value):
    return format(Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP), '.2f') if value is not None else None


def content_hash(document):
    return ai_content.digest([{'id': r['id'], 'values': r['values'],
        **({'ai_policy': r['ai_policy']} if r.get('ai_policy') else {})} for r in document['rows']])


def applied_ai(job):
    if getattr(job, 'context', {}).get('staging_automation_version') == 1:
        return job.status in ('completed', 'import_queued', 'importing', 'import_failed', 'import_blocked') \
            and bool(job.output) and job.context.get('staging_applied_digest') == ai_content.digest(job.output)
    return job.status == 'completed'


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


def publication_state(result):
    """Read delivery from the durable importer journal, never from AI readiness."""
    if not result:
        return None
    statuses = {item['status'] for item in result['items']}
    if 'uncertain' in statuses:
        return 'uncertain'
    if result['status'] in ('queued', 'running'):
        return result['status']
    if result['status'] == 'completed' and statuses and statuses <= {'created', 'exists'}:
        return 'completed'
    return 'partial' if statuses & {'created', 'exists'} else 'failed'


def ai_job_import_status(job, draft, result):
    rows = [r for r in draft.document['rows'] if r.get('ai_job_id') == job.id]
    selected = {r['id'] for r in rows}
    items = [item for item in (result or {}).get('items', []) if selected.intersection(item['product_ids'])]
    status = publication_state({**result, 'items': items}) if result and items else None
    covered = {id for item in items for id in item['product_ids']}
    if items and selected <= covered and all(item['status'] in ('created', 'exists') for item in items):
        status = 'completed'
    profile_matches = all(r['values'].get('ai_category_profile', 'auto')
        == r.get('ai_baseline', {}).get('ai_category_profile', 'auto') for r in rows)
    automation = draft.document.get('automation', {})
    auto_apply = bool(rows) and getattr(job, 'context', {}).get('staging_automation_version') == 1 and automation.get('version') == 1 \
        and job.id in automation.get('job_ids', []) and not automation.get('paused')
    return {'draft_id': draft.id, 'revision': draft.revision, 'linked': bool(rows),
            'ai_applied': bool(rows) and applied_ai(job) and profile_matches,
            'profile_matches': profile_matches, 'auto_apply': auto_apply,
            'auto_publish': auto_apply and automation.get('auto_publish_requested', False)
                and not job.context.get('resolved', {}).get('policy', {}).get('confirm_import', True),
            'automation_paused': bool(automation.get('paused')),
            'publication_finished': publication_state(result) == 'completed',
            'publication_status': status}


async def ai_job_statuses(db, jobs):
    ids = {j.context.get('staging_id') for j in jobs if j.context.get('staging_id')}
    if not ids:
        return {}
    drafts = (await db.scalars(select(ProductImportDraft).where(ProductImportDraft.id.in_(ids)))).all()
    by_id = {draft.id: draft for draft in drafts}
    results, unavailable = {}, set()
    for draft in drafts:
        try:
            results[draft.id] = publication_result(draft)
        except CatalogError:
            # A damaged/unavailable journal must not hide every unrelated AI job.
            results[draft.id] = None
            unavailable.add(draft.id)
    output = {}
    for job in jobs:
        id = job.context.get('staging_id')
        if id in by_id:
            output[job.id] = ai_job_import_status(job, by_id[id], results[id])
            if id in unavailable:
                output[job.id]['publication_status'] = 'unavailable'
    return output


def assert_editable(draft):
    result = publication_result(draft)
    if result and (result['status'] in ('queued', 'running') or any(i['status'] == 'uncertain' for i in result['items'])):
        raise CatalogError('import_publication_pending', 'Reconcile the existing publication before changing this draft', 409)
    if publication_state(result) == 'completed':
        raise CatalogError('import_publication_finished', 'This import is complete; use the product editor for further changes', 409)


async def assert_ai_applied(db, draft):
    rows = [r for r in draft.document['rows'] if r['values']['ai_enabled']]
    if not rows:
        return
    ids = {r.get('ai_job_id') for r in rows if r.get('ai_job_id')}
    jobs = {j.id: j for j in (await db.scalars(select(AiJob).where(AiJob.id.in_(ids)).with_for_update())).all()}
    if any(not (job := jobs.get(r.get('ai_job_id'))) or job.context.get('staging_id') != draft.id
           or not applied_ai(job)
           or r['values'].get('ai_category_profile', 'auto') != r.get('ai_baseline', {}).get('ai_category_profile', 'auto') for r in rows):
        raise CatalogError('import_ai_not_applied', 'Apply the selected AI results to the table or explicitly turn AI off before publication', 409)


async def public(db, draft):
    document = draft.document
    rows = deepcopy(document['rows'])
    for row in rows:
        row['values'].setdefault('ai_category_profile', 'auto')
    ids = {r.get('ai_job_id') for r in rows if r.get('ai_job_id')}
    result = publication_result(draft)
    jobs = {j.id: {**ai_content.summary(j), 'staging': ai_job_import_status(j, draft, result)}
            for j in (await db.scalars(select(AiJob).where(AiJob.id.in_(ids)))).all()} if ids else {}
    for row in rows:
        row.pop('source', None)
        row.pop('ai_baseline', None)
        row['ai_job'] = jobs.get(row.pop('ai_job_id', None))
        row['publication_status'] = next((item['status'] for item in (result or {}).get('items', [])
                                          if row['id'] in item['product_ids']), None)
    return {'id': draft.id, 'revision': draft.revision, 'status': draft.status, 'supplier': draft.supplier,
            'shop': draft.shop, 'feed_key': document['feed_key'], 'options': document['options'],
            'categories': document['categories'], 'rows': rows, 'publication': document.get('publication'),
            'publication_result': result, 'publication_state': publication_state(result), 'created_at': draft.created_at,
            'updated_at': draft.updated_at}


async def history(db):
    drafts = (await db.scalars(select(ProductImportDraft).order_by(ProductImportDraft.updated_at.desc()).limit(100))).all()
    return {'items': [{'id': d.id, 'revision': d.revision, 'status': d.status, 'supplier': d.supplier, 'shop': d.shop,
                       'publication_state': publication_state(publication_result(d)),
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
                if any(r['values'].get(key, 'auto' if key == 'ai_category_profile' else None)
                       != family[0]['values'].get(key, 'auto' if key == 'ai_category_profile' else None) for r in family):
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
    replaced_ai = set()
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
                if key == 'ai_category_profile' and target['values'].get(key, 'auto') != value and target.get('ai_job_id'):
                    replaced_ai.add(target['ai_job_id'])
                target['values'][key] = value
                target['manual_fields'] = sorted(set(target['manual_fields']) | {key})
                target['provenance'][key] = 'manual'
    if replaced_ai:
        jobs = (await db.scalars(select(AiJob).where(AiJob.id.in_(replaced_ai)).with_for_update())).all()
        for job in jobs:
            if job.status in ('estimate', 'queued'):
                job.reserved_usd = 0
                ai_content.event(job, 'cancelled', 'Category rule profile changed before provider call; prepare a new estimate')
    if document.get('automation', {}).get('version') == 1:
        document['automation'] = {**document['automation'], 'paused': True, 'pause_reason': 'draft_edited'}
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
    if all(bound_ai_policy(row) for row in family):
        active = approved_visibility(rows, item.product_ids)
        payload['active_yn'] = active
        for value in payload['descriptions']:
            value['active_yn'] = active
        for obj in [payload, *payload.get('variants', [])]:
            for meta in obj.get('metas', []):
                if meta.get('key') == 'validation_required':
                    meta['value'] = '0'
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


def bound_ai_policy(row):
    policy = row.get('ai_policy') or {}
    if (row['values']['ai_enabled'] and policy.get('job_id') == row.get('ai_job_id')
            and policy.get('content_digest')
            and row['values'].get('ai_category_profile', 'auto') == row.get('ai_baseline', {}).get('ai_category_profile', 'auto')):
        return policy
    return None


def approved_visibility(rows, product_ids):
    flags = {bool((bound_ai_policy(r) or {}).get('active_after_import', False))
             for r in rows if r['id'] in product_ids}
    if len(flags) > 1:
        raise CatalogError('import_family_field_conflict', 'A product family must share its approved visibility', 422)
    return flags == {True}


async def preview(db, draft, request):
    expect(draft, request.expected_revision)
    if publication_state(publication_result(draft)) == 'completed':
        return await public(db, draft)
    assert_editable(draft)
    await assert_ai_applied(db, draft)
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
    return draft


async def publish(db, draft, request):
    expect(draft, request.expected_revision)
    preview = draft.document.get('publication')
    if not preview or preview['preview_id'] != request.preview_id:
        raise CatalogError('import_preview_changed', 'Confirm the current publication preview', 409)
    result = publication_result(draft)
    if publication_state(result) == 'completed':
        return await public(db, draft), False
    # Recovery belongs to the already confirmed snapshot, even if AI was edited later.
    if not result:
        await assert_ai_applied(db, draft)
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
    prepared_jobs = []
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
            category_profiles={r['id']: r['values'].get('ai_category_profile', 'auto') for r in family}, research=request.research,
            targets=[Target(shop=draft.shop, options=options, policy=Policy())])
        jobs = await ai_content.create_batch(db, batch, frozen_products=[to_source(r) for r in family],
            staging_id=draft.id, staging_automation_version=1)
        prepared_jobs.extend(jobs)
        for row in family:
            row['ai_job_id'] = jobs[0]['id']
            row['ai_baseline'] = deepcopy(row['values'])
            row.pop('ai_policy', None)
    previous_result = publication_result(draft)
    document['automation'] = {'version': 1, 'job_ids': [j['id'] for j in prepared_jobs],
        'paused': bool(previous_result), 'pause_reason': 'publication_recovery' if previous_result else None,
        'auto_publish_requested': all(r['values']['ai_enabled'] for r in document['rows'])
            and all(not j.get('policy', {}).get('confirm_import', True) for j in prepared_jobs)}
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
        if any(r['values'].get('ai_category_profile', 'auto') != r.get('ai_baseline', {}).get('ai_category_profile', 'auto')
               for r in draft.document['rows'] if r.get('ai_job_id') == job.id):
            raise CatalogError('import_ai_identity_changed', 'The category rule profile changed; prepare a new AI estimate', 409)
        await ai_content.start(db, job)
    changed(draft, deepcopy(draft.document), 'AI cost estimates explicitly confirmed', dirty=False)
    await db.flush()
    return await public(db, draft)


def merge_ai_values(row, output, context):
    baseline = row.get('ai_baseline', {})
    if row['values'].get('ai_category_profile', 'auto') != baseline.get('ai_category_profile', 'auto'):
        raise CatalogError('import_ai_identity_changed', 'The category rule profile changed; prepare a new AI job', 409)
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
    descend_to_leaf = bool(baseline.get('category_code') and context.get('classification_mode') == 'category_and_profile'
        and context.get('category_selection'))
    if not baseline.get('category_code') or descend_to_leaf:
        proposed['category_code'] = context.get('options', {}).get('category_code')
    safety = CatalogProduct.model_validate(row['source']).safety_information
    if safety:
        from inventory_hub.services.catalog_html import clean_description
        proposed['description_html'] += '<h2>Bezpečnostné informácie</h2>' + clean_description(safety)
    for key, value in proposed.items():
        # An explicitly selected parent constrains automatic classification to
        # its subtree; accepting that result refines it to the validated leaf.
        # A selected leaf or a category changed after preparation stays pinned.
        if (key not in row['manual_fields'] or key == 'category_code' and descend_to_leaf) and row['values'].get(key) == baseline.get(key):
            row['values'][key] = value
            row['provenance'][key] = 'ai'
    row['values'] = ImportValues.model_validate(row['values']).model_dump(mode='json')


async def apply_ai(db, draft, request, *, job_ids=None):
    expect(draft, request.expected_revision)
    assert_editable(draft)
    document = deepcopy(draft.document)
    ids = {r.get('ai_job_id') for r in document['rows'] if r.get('ai_job_id') and r['values']['ai_enabled']}
    if job_ids is not None:
        ids &= set(job_ids)
    jobs = {j.id: j for j in (await db.scalars(select(AiJob).where(AiJob.id.in_(ids)).with_for_update())).all()}
    used = []
    from inventory_hub.services.ai_content_validation import validate_content
    for job in jobs.values():
        if job.status not in ('review', 'ready') or not job.output:
            continue
        output = Content.model_validate(job.output)
        checks = validate_content(output, job.context, (job.usage or {}).get('opened_sources', []),
                                  human_approved=job.context.get('approval') == 'human')
        if checks['errors']:
            raise CatalogError('ai_validation_failed', '; '.join(checks['errors']), 422)
        for row in document['rows']:
            if row.get('ai_job_id') == job.id and row['values']['ai_enabled']:
                merge_ai_values(row, output, job.context)
                if job.context.get('staging_automation_version') == 1:
                    row['ai_policy'] = {'job_id': job.id, 'rules_version': job.context['rules_version'],
                        'active_after_import': bool(job.context['resolved']['policy']['active_after_import']),
                        'content_digest': ai_content.digest(job.output)}
        used.append(job)
    if not used:
        raise CatalogError('import_ai_not_ready', 'No completed valid AI result is ready', 409)
    for job in used:
        if job.context.get('staging_automation_version') == 1:
            job.context = {**job.context, 'staging_applied_digest': ai_content.digest(job.output)}
        ai_content.event(job, 'completed', 'AI enrichment applied to import draft; no shop write')
    validate_rows(document)
    changed(draft, document, 'AI enriched untouched cells; manual cells preserved')
    await db.flush()
    return await public(db, draft)


async def advance_ai(db, job):
    """Complete only a freshly authorized staged workflow using frozen policies.

    The worker acquires draft then job locks before entering here. Old jobs,
    edited preparations and previously sent publications never gain automation.
    """
    draft = await get_draft(db, job.context['staging_id'], lock=True)
    automation = draft.document.get('automation', {})
    linked = [r for r in draft.document['rows'] if r.get('ai_job_id') == job.id and r['values']['ai_enabled']]
    enabled = (job.context.get('staging_automation_version') == 1 and automation.get('version') == 1
        and job.id in automation.get('job_ids', []) and not automation.get('paused') and linked)
    if not enabled or job.context.get('approval') not in ('human', 'policy'):
        ai_content.event(job, 'ready', 'AI content ready; automatic draft completion is not authorized')
        return
    if publication_result(draft):
        ai_content.event(job, 'ready', 'An existing publication requires manual reconciliation')
        return
    ai_content.event(job, 'ready', 'Approved AI content ready for its authorized draft')
    await apply_ai(db, draft, DraftRevision(expected_revision=draft.revision), job_ids={job.id})
    rows = draft.document['rows']
    if not all(r['values']['ai_enabled'] and r.get('ai_job_id') in automation.get('job_ids', []) for r in rows):
        return
    ids = {r['ai_job_id'] for r in rows}
    jobs = {j.id: j for j in (await db.scalars(select(AiJob).where(AiJob.id.in_(ids)).order_by(AiJob.id).with_for_update())).all()}
    if any(not (current := jobs.get(id)) or current.context.get('staging_automation_version') != 1
           or current.context.get('staging_id') != draft.id or not applied_ai(current)
           or current.context.get('approval') not in ('human', 'policy')
           or current.context['resolved']['policy']['confirm_import'] for id in ids):
        return
    await assert_ai_applied(db, draft)
    await save(db, draft, DraftRevision(expected_revision=draft.revision))
    await preview(db, draft, DraftRevision(expected_revision=draft.revision))
    prepared = draft.document['publication']
    if prepared['errors'] or any(i['status'] not in ('ready', 'exists') for i in prepared['items']):
        job.error = 'preview_invalid'
        ai_content.event(job, 'import_blocked', 'Automatic publication stopped because its complete preview is invalid')
        return
    job.preview_id = prepared['preview_id']
    job.context = {**job.context, 'staging_auto_publication': {'draft_id': draft.id,
        'preview_id': job.preview_id, 'hash': content_hash(draft.document)}}
    ai_content.event(job, 'import_queued', 'Complete draft publication authorized by every frozen import policy')
    changed(draft, deepcopy(draft.document), 'Complete draft queued by inherited import policy', dirty=False)
    await db.flush()
