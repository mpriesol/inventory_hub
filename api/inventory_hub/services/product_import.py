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
from sqlalchemy import select, text, func, or_, String, cast
from sqlalchemy.dialects.postgresql import insert, JSONPATH

from inventory_hub.ai_content_models import AiContentRevision, AiJob
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
                 'seo_description', 'seo_url', 'category_code', 'parameters', 'metadata', 'ai_enabled', 'ai_category_profile',
                 'target_shops', 'target_category_codes'}
AI_FIELDS = {'name', 'group_name', 'short_description', 'description_html', 'seo_title', 'seo_description', 'parameters', 'metadata', 'category_code'}


def money(value):
    return format(Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP), '.2f') if value is not None else None


def content_hash(document):
    return ai_content.digest([{'id': r['id'], 'values': r['values'],
        **({'ai_policy': r['ai_policy']} if r.get('ai_policy') else {}),
        **({'identity_decision': r['identity_decision']} if r.get('identity_decision') else {})} for r in document['rows']])


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
        document.pop('publications', None)


def link_only(row):
    return row.get('identity_link_only') or (row.get('identity_decision') or {}).get('action') == 'link'


def row_targets(row, primary):
    if link_only(row):
        return []
    selected = row['values'].get('target_shops')
    return [primary] if selected is None else selected


def publication_previews(draft):
    previews = draft.document.get('publications')
    if previews is not None:
        return previews
    preview = draft.document.get('publication')
    return {draft.shop: preview} if preview else {}


def target_result(shop, preview):
    try:
        return catalog_import.import_result(shop, preview['preview_id'])
    except CatalogError as error:
        if error.code == 'import_not_started':
            return None
        raise


def source_preview_retryable(draft):
    """Only a proved zero-product-attempt expiry can replace publication IDs."""
    if not getattr(draft, 'document', None) or not getattr(draft, 'shop', None):
        return False
    found_expiry = False
    for shop, preview in publication_previews(draft).items():
        result = target_result(shop, preview)
        if result is None:
            continue
        if result['status'] in ('queued', 'running') or any(
                item.get('product_write_attempted') is not False or item['status'] not in ('ready', 'failed', 'exists')
                for item in result['items']):
            return False
        errors = result.get('errors', []) + [error for item in result['items'] for error in item.get('errors', [])]
        if any(error != 'import_source_availability_expired' for error in errors):
            return False
        found_expiry = found_expiry or bool(errors)
    return found_expiry


def publication_result(draft):
    previews = publication_previews(draft)
    if not previews:
        return None
    results = {shop: target_result(shop, preview) for shop, preview in previews.items()}
    if not any(results.values()):
        return None
    if len(previews) == 1:
        shop = next(iter(previews))
        result = results[shop]
        return {**result, 'items': [{**item, 'shop': shop} for item in result['items']]}
    items = [{**item, 'shop': shop} for shop, result in results.items()
             for item in (result or {'items': [{**item, 'status': 'pending'} for item in previews[shop]['items']]})['items']]
    states = {result['status'] if result else 'pending' for result in results.values()}
    status = 'running' if 'running' in states else 'queued' if 'queued' in states else 'completed' if states == {'completed'} else 'failed'
    return {'preview_id': draft.document['publication']['preview_id'], 'shop': draft.shop,
            'status': status, 'items': items,
            'errors': [shop + ':' + error for shop, result in results.items() if result for error in result.get('errors', [])],
            'updated_at': max(result.get('updated_at', '') for result in results.values() if result)}


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
    from inventory_hub.services.product_import_source import assert_ready
    assert_ready(draft)
    result = publication_result(draft)
    if publication_state(result) == 'completed':
        raise CatalogError('import_publication_finished', 'This import is complete; use the product editor for further changes', 409)
    if result:
        raise CatalogError('import_publication_pending', 'Reconcile the existing publication before changing this draft', 409)


async def assert_ai_applied(db, draft):
    rows = [r for r in draft.document['rows'] if r['values']['ai_enabled'] and not link_only(r)]
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
        row['values']['target_shops'] = row_targets(row, draft.shop)
        row['values'].setdefault('target_category_codes', {})
    ids = {r.get('ai_job_id') for r in rows if r.get('ai_job_id')}
    result = publication_result(draft)
    if result:
        # Historical journals did not retain URLs. Their already verified local
        # shop content can fill links without new network requests or writes.
        from inventory_hub.services.catalog import attach_shop_links
        result = deepcopy(result)
        for shop in {item.get('shop', draft.shop) for item in result['items']}:
            pending = [item for item in result['items'] if item.get('shop', draft.shop) == shop
                and item['status'] in ('created', 'exists') and not (item.get('shop_url') and item.get('shop_admin_url'))]
            link_ids = {id for item in pending for id in item['product_ids']}
            sources = [to_source(row).model_copy(update={'listed': True}) for row in document['rows'] if row['id'] in link_ids]
            if sources:
                await attach_shop_links(db, shop, sources)
                by_id = {source.id: source for source in sources}
                for item in pending:
                    source = next((by_id[id] for id in item['product_ids'] if id in by_id), None)
                    if source:
                        item['shop_url'] = item.get('shop_url') or source.shop_url
                        item['shop_admin_url'] = item.get('shop_admin_url') or source.shop_admin_url
    jobs = {j.id: {**ai_content.summary(j), 'staging': ai_job_import_status(j, draft, result)}
            for j in (await db.scalars(select(AiJob).where(AiJob.id.in_(ids)))).all()} if ids else {}
    for row in rows:
        from inventory_hub.services.catalog import _http_url
        row['source_url'] = _http_url((row.get('source') or {}).get('url'))
        row.pop('source', None)
        row.pop('ai_baseline', None)
        row.pop('ai_applied_snapshot', None)
        if row.get('identity_decision'):
            row['identity_decision'] = {key: row['identity_decision'][key] for key in ('action', 'product_id')
                                        if key in row['identity_decision']}
        row['ai_job'] = jobs.get(row.pop('ai_job_id', None))
        outcomes = [item['status'] for item in (result or {}).get('items', []) if row['id'] in item['product_ids']]
        row['publication_status'] = next((status for status in ('uncertain', 'failed', 'invalid', 'pending', 'ready', 'created', 'exists')
                                           if status in outcomes), None)
        row['publication_links'] = [{'shop': item.get('shop', draft.shop), 'status': item['status'],
            'shop_url': item.get('shop_url'), 'shop_admin_url': item.get('shop_admin_url')}
            for item in (result or document.get('publication') or {}).get('items', [])
            if row['id'] in item['product_ids'] and item['status'] in ('created', 'exists', 'failed', 'uncertain', 'pending')]
    publications = {shop: {'preview': preview, 'result': outcome, 'state': publication_state(outcome)}
        for shop, preview in publication_previews(draft).items() for outcome in [target_result(shop, preview)]}
    from inventory_hub.services.product_import_identity import summary as identity_summary
    return {'id': draft.id, 'revision': draft.revision, 'status': draft.status, 'supplier': draft.supplier,
            'supplier_id': document.get('supplier_id'),
            'shop': draft.shop, 'feed_key': document['feed_key'], 'options': document['options'],
            'shops': document.get('shops', [{'code': draft.shop, 'name': draft.shop, 'categories': document['categories']}]),
            'default_shop': document.get('default_shop', draft.shop), 'publications': publications,
            'categories': document['categories'], 'rows': rows, 'publication': document.get('publication'),
            'publication_result': result, 'publication_state': publication_state(result), 'created_at': draft.created_at,
            'updated_at': draft.updated_at,
            'publication_preview_retryable': bool(result) and source_preview_retryable(draft),
            'source_lookup': source_lookup_public(draft),
            'receiving_origin': document.get('receiving_origin'),
            **({'identity_review_required': False, 'identity_review_count': 0} if result else await identity_summary(db, draft))}


def source_lookup_public(draft):
    from inventory_hub.services.product_import_source import public_status
    return public_status(draft)


async def history(db, q=''):
    statement = select(ProductImportDraft)
    if q.strip():
        pattern = '%' + q.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        statement = statement.where(or_(*(func.jsonb_path_query_array(ProductImportDraft.document,
            cast('$.rows[*].values.' + field, JSONPATH)).cast(String).ilike(pattern, escape='\\') for field in ('name', 'code', 'group_name'))))
    drafts = (await db.scalars(statement.order_by(ProductImportDraft.updated_at.desc()).limit(100))).all()
    attention = set((await db.scalars(select(AiJob.context['staging_id'].astext).where(
        AiJob.context['staging_id'].astext.in_([d.id for d in drafts]),
        AiJob.status.in_(['blocked', 'failed', 'review', 'import_failed', 'import_blocked'])))).all()) if drafts else set()
    return {'items': [{'id': d.id, 'revision': d.revision, 'status': d.status, 'supplier': d.supplier, 'shop': d.shop,
                       'publication_state': publication_state(publication_result(d)),
                       'needs_attention': d.id in attention or d.status == 'source_failed' or any(r.get('errors') for r in d.document.get('rows', [])),
                       'title': next((r['values'].get('group_name') or r['values'].get('name') or r['values'].get('code') for r in d.document.get('rows', [])), ''),
                       'codes': [r['values'].get('code', '') for r in d.document.get('rows', [])][:3],
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
    importing = [r for r in rows if not link_only(r)]
    importing_ids = {r['id'] for r in importing}
    codes = Counter(r['values']['code'].casefold() for r in importing)
    barcode_key = lambda value: ProductIdentifierService.gtin_key(value) or value
    eans = Counter(e for r in importing for e in {barcode_key(value) for value in r['values']['eans']})
    groups = defaultdict(list)
    options = ShopImportOptions.model_validate(document['options'])
    for row in rows:
        if row['id'] not in importing_ids:
            row['errors'] = []
            row['hub_errors'] = []
            row['warnings'] = sorted(set(row.get('warnings', [])) | {'existing_card_link_only'})
            continue
        source = to_source(row)
        row['errors'] = list(source.import_blockers) + (['stock_feed_only'] if 'stock_feed_only' in source.warnings else [])
        row['warnings'] = list(source.warnings)
        if row['values']['manufacturer_code']:
            row['warnings'].append('manufacturer_code_source_only')
        from inventory_hub.services.ai_content_validation import has_active_html
        if any(has_active_html(row['values'][key]) for key in ('description_html', 'short_description')):
            row['errors'].append('ai_unsafe_html')
        if codes[source.shop_code.casefold()] > 1 or any(eans[barcode_key(e)] > 1 for e in source.eans):
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
    for row in rows:
        # A local card can precede prices, VAT and shop merchandising. Identity,
        # unsafe content and contradictory source facts still block its save.
        if link_only(row):
            row['hub_errors'] = []
            continue
        merchandising = {'missing_price', 'missing_vat'}
        if (row['values'].get('sku_identity') or {}).get('leaf_confirmed'):
            # A documented concrete leaf can exist before variant presentation
            # is complete. Duplicated supplier codes/EANs are never exempted.
            merchandising |= {'stock_feed_only', 'variant_attributes_missing', 'variant_attributes_ambiguous'}
        if all(row['values'].get(key) is None for key in ('purchase_net', 'retail_gross', 'sale_gross')):
            merchandising.add('currency_mismatch')
        row['hub_errors'] = sorted(set(row.get('errors', [])) - merchandising
            | set((row.get('sku_preview') or {}).get('reasons', [])
                if (row.get('sku_preview') or {}).get('status') in ('blocked', 'conflict') else []))


async def prepare_sku_rows(db, draft, document):
    """Preview the same allocation policy for feed and invoice preparations.

    This is read-only. The save repeats it inside IDENTITY_WRITE_LOCK and keeps
    the verified input in the immutable saved draft digest.
    """
    from inventory_hub.sku_policy_types import SkuIdentityInput
    from inventory_hub.services.sku_policy import preview_sku, registered_brand_id, brand_key
    supplier_id = await db.scalar(select(Supplier.id).where(Supplier.code == draft.supplier))
    document['supplier_id'] = supplier_id
    selected = [r for r in document['rows'] if not link_only(r)]
    existing, conflicts = await catalog_import.local_identities(db, [to_source(r) for r in selected])
    for row in selected:
        values = row['values']
        expected = row.get('hub_product_id') or row.get('expected_hub_product_id')
        if row['id'] in conflicts:
            row['sku_preview'] = {'status': 'conflict', 'reasons': ['local_identity_conflict']}
            continue
        product_id = existing.get(row['id'])
        if expected and expected != product_id:
            row['sku_preview'] = {'status': 'conflict', 'reasons': ['assigned_product_changed']}
            continue
        identity = values.get('sku_identity')
        if identity:
            item = SkuIdentityInput.model_validate(identity)
            result = await preview_sku(db, item,
                barcodes=values['eans'], supplier_id=supplier_id,
                supplier_sku=values['supplier_code'], expected_product_id=expected)
            if product_id and result['status'] == 'new':
                # A legacy prefixed-code candidate is not an approved target.
                # Retain its original code for the ordinary explicit review;
                # do not hide it by replacing the candidate with a new SKU.
                result = {**result, 'status': 'blocked', 'reasons': ['import_identity_review_required'],
                          'candidate_product_ids': [product_id]}
            elif product_id and result['status'] == 'existing' and result['product_id'] != product_id:
                result = {**result, 'status': 'conflict', 'reasons': ['local_identity_conflict'],
                          'candidate_product_ids': [product_id, result['product_id']]}
            if values['brand'] and brand_key(values['brand']) != brand_key(item.brand):
                source_brand = await registered_brand_id(db, values['brand'])
                if source_brand is None or source_brand != result.get('brand_id'):
                    result = {**result, 'status': 'conflict', 'reasons': ['sku_brand_conflict']}
        elif product_id:
            product = await db.get(Product, product_id)
            result = {'status': 'existing', 'sku': product.sku, 'product_id': product_id,
                      'reasons': [], 'candidate_product_ids': [product_id]}
        else:
            result = {'status': 'blocked', 'reasons': ['sku_identity_required'], 'candidate_product_ids': []}
        row['sku_preview'] = result
        if result['status'] in ('new', 'existing'):
            # The supplier parent is a grouping code, not a physical SKU. Freeze
            # it before changing the leaf so no prefix is guessed from the MPN.
            if row['is_variant'] and not row['source'].get('canonical_parent_code'):
                row['source'] = {**row['source'], 'canonical_parent_code': row['group_key']}
            values['code'] = result['sku']
            if identity and not values['brand']:
                values['brand'] = item.brand
            row['canonical_code'] = result['sku']
            if not row['is_variant']:
                row['group_key'] = result['sku']


async def _receiving_sources(db, sources, receiving_rows):
    """Reuse canonical SKUs and allow only pristine invoice cards a new parent.

    The selected feed's explicit family is frozen here and attached on Hub save.
    Existing grouped cards retain the shared resolver's confirmed parent checks.
    No products or quantities are changed while preparing the snapshot.
    """
    matches, conflicts = await catalog_import.local_identities(db, sources)
    if conflicts:
        raise CatalogError('local_identity_conflict', 'Local identifiers require reconciliation', 409)
    products = {p.id: p for p in (await db.scalars(select(Product).where(Product.id.in_(set(matches.values()))))).all()} if matches else {}
    family_parents = {p.group_code: parent_shop_code(p) for p in sources if p.group_code and p.variant_relationship == 'explicit'}
    pristine = {}
    prepared = []
    for source in sources:
        expected = receiving_rows[source.id].get('existing_product_id')
        if expected is not None and matches.get(source.id) != expected:
            raise CatalogError('local_identity_conflict', 'The invoice product identity changed; reload the receipt', 409)
        product = products.get(matches.get(source.id))
        if product and product.group_id is None and source.group_code and source.variant_relationship == 'explicit':
            override = await db.get(ProductEditorOverride, product.id)
            if (not _minimal_receiving_card(product, override) or override
                    or await db.scalar(select(ShopProduct.id).where(ShopProduct.product_id == product.id))):
                raise CatalogError('local_group_conflict', 'Confirm the existing card variant parent in the product editor', 409)
            pristine[source.id] = source
            source = source.model_copy(update={'group_code': None, 'group_name': None,
                'variant_relationship': 'not_provided', 'canonical_parent_code': None})
        prepared.append(source)
    canonical = await catalog_import.canonical_products(db, prepared)
    parents = {p.group_code: p.canonical_parent_code for p in canonical if p.group_code and p.canonical_parent_code}
    family_parents.update(parents)
    result = []
    for source in canonical:
        original = pristine.get(source.id)
        if original:
            source = original.model_copy(update={'shop_code': source.shop_code,
                'canonical_parent_code': family_parents[original.group_code]})
        elif source.group_code and source.variant_relationship == 'explicit':
            source = source.model_copy(update={'canonical_parent_code': family_parents[source.group_code]})
        result.append(source)
    return result


def _minimal_receiving_card(product, override):
    return bool(product and (product.created_from_source or '').startswith('invoice:')
        and product.validation_required and not (override and override.data.get('import_fields')))


def _overlay_receiving_manual_values(row, override):
    """Retain existing editor values as manual facts before optional AI runs."""
    if not override:
        return
    values = {}
    common, variant = override.data.get('common', {}), override.data.get('variant', {})
    for field in ('name', 'brand'):
        if field in common:
            values[field] = common[field] or ''
    if 'image_url' in common:
        values['images'] = [common['image_url']] if common['image_url'] else []
    for old, field in (('sale_price_gross', 'sale_gross'), ('vat_rate', 'vat_percent'), ('attributes', 'variant_attributes')):
        if old in variant:
            values[field] = variant[old]
    row['values'] = ImportValues.model_validate({**row['values'], **values}).model_dump(mode='json')
    row['manual_fields'] = sorted(set(row['manual_fields']) | set(values))
    row['provenance'].update({key: 'manual' for key in values})
    row['editor_revision'] = override.revision


async def create(db, request, *, sources=None, receiving_origin=None, receiving_rows=None):
    # Trusted normalized receipt sources are internal only. The public draft
    # request cannot submit arbitrary product snapshots or existing product IDs.
    if (sources is not None) != (receiving_origin is not None) or (sources is not None) != (receiving_rows is not None):
        raise ValueError('Receiving sources, origin and row lineage must be supplied together')
    if sources is not None and (set(request.product_ids) != {p.id for p in sources}
            or len(sources) != len(set(request.product_ids)) or any(p.id <= 0 or p.supplier != request.supplier for p in sources)
            or set(receiving_rows) != set(request.product_ids)):
        raise CatalogError('import_invalid_receiving_sources', 'Invalid receiving selection', 422)
    # Keep creation replay tied to the selected receipt, not later feed refreshes.
    fingerprint = ai_content.digest({**request.model_dump(mode='json'),
        **({'receiving_origin': receiving_origin} if receiving_origin else {})})
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
    source = deepcopy(sources) if sources is not None else await selected_products(db, request.supplier, request.feed_key, request.product_ids, request.run_id)
    from inventory_hub.services.feed_mapping import apply_shop_mapping
    async def mapped(p, shop):
        if receiving_rows is None:
            return await apply_shop_mapping(db, p, shop)
        catalog_id = receiving_rows[p.id].get('catalog_product_id')
        if catalog_id is None:
            return p.model_copy(deep=True)
        product = await apply_shop_mapping(db, p.model_copy(update={'id': catalog_id}), shop)
        return product.model_copy(update={'id': p.id})
    products = [await mapped(p, request.shop) for p in source]
    if receiving_rows is not None:
        products = await _receiving_sources(db, products, receiving_rows)
    else:
        from inventory_hub.services.product_import_identity import canonical_for_review
        products = await canonical_for_review(db, products)
    remote = await asyncio.to_thread(catalog_import.cached_import_options, request.shop)
    cfg = supplier_config(request.supplier)
    default_shop = cfg.get('default_shop') or ('biketrek' if request.supplier == 'sloger' else request.shop)
    shops = [{'code': request.shop, 'name': 'BIKETREK' if request.shop == 'biketrek' else 'xTrek', 'categories': remote['categories']}]
    targets = {request.shop: catalog_import._target(catalog_import.shop_config(request.shop))}
    mapped_targets = {}
    active_shops = (await db.scalars(select(Shop).where(Shop.is_active.is_(True), Shop.code.in_(['biketrek', 'xtrek'])))).all()
    for shop in active_shops:
        if shop.code == request.shop:
            continue
        try:
            settings = await asyncio.to_thread(catalog_import.cached_import_options, shop.code)
            targets[shop.code] = catalog_import._target(catalog_import.shop_config(shop.code))
            shops.append({'code': shop.code, 'name': 'BIKETREK' if shop.code == 'biketrek' else 'xTrek', 'categories': settings['categories']})
            mapped_targets[shop.code] = {p.id: await mapped(p, shop.code) for p in source}
        except CatalogError as error:
            shops.append({'code': shop.code, 'name': shop.name, 'categories': [], 'error': error.code})
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
            availability=availability['availability'], target_shops=[request.shop],
            target_category_codes={shop: getattr(mapped[p.id], 'target_category_code', None) for shop, mapped in mapped_targets.items()})
        source_kind = 'invoice' if receiving_rows is not None and receiving_rows[p.id].get('catalog_product_id') is None else 'feed'
        provenance = {key: source_kind for key in values.model_fields}
        for key in ('sale_gross', 'availability', 'seo_title', 'seo_description'):
            provenance[key] = 'derived'
        if values.category_code:
            provenance['category_code'] = 'manual' if request.options.category_code else 'mapping'
        rows.append({'id': p.id, 'group_key': parent_shop_code(p), 'group_name': p.group_name,
            'is_variant': p.variant_relationship == 'explicit' and bool(p.group_code),
            'source_category': p.category, 'mapping_revision': getattr(p, 'mapping_revision', 0),
            'mapping_provenance': getattr(p, 'mapping_provenance', {}), 'source': p.model_dump(mode='json'),
            'values': values.model_dump(mode='json'), 'manual_fields': ['category_code'] if request.options.category_code else [],
            'provenance': provenance, 'hub_product_id': None,
            'canonical_code': p.shop_code})
        if receiving_rows is not None:
            lineage = receiving_rows[p.id]
            rows[-1].update(source_kind=source_kind, receiving_line_ids=list(lineage['line_ids']),
                catalog_product_id=lineage.get('catalog_product_id'), expected_hub_product_id=lineage.get('existing_product_id'))
            if lineage.get('existing_product_id'):
                product = await db.get(Product, lineage['existing_product_id'])
                if product is None:
                    raise CatalogError('local_identity_conflict', 'The invoice product identity changed; reload the receipt', 409)
                override = await db.get(ProductEditorOverride, product.id)
                if not _minimal_receiving_card(product, override) or await db.scalar(select(ShopProduct.id).where(ShopProduct.product_id == product.id)):
                    raise CatalogError('import_receiving_product_changed', 'This card is already complete; use the product editor', 409)
                _overlay_receiving_manual_values(rows[-1], override)
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
                'shops': shops, 'targets': targets, 'default_shop': default_shop,
                'target': catalog_import._target(catalog_import.shop_config(request.shop))}
    if receiving_origin is not None:
        document['receiving_origin'] = deepcopy(receiving_origin)
        document['receiving_rows'] = {str(key): deepcopy(value) for key, value in receiving_rows.items()}
    if request.supplier == 'sloger':
        from inventory_hub.services.product_import_source import initialize
        initialize(document)
    validate_rows(document)
    draft = ProductImportDraft(id=request.request_id.hex, request_hash=fingerprint, revision=1,
                              supplier=request.supplier, shop=request.shop,
                              status='source_pending' if request.supplier == 'sloger' else 'draft', document=document)
    await prepare_sku_rows(db, draft, document)
    validate_rows(document)
    db.add(draft)
    await db.flush()
    return await public(db, draft)


async def refresh_categories(db, draft, request):
    expect(draft, request.expected_revision)
    assert_editable(draft)
    document = deepcopy(draft.document)
    shops = document.get('shops', [{'code': draft.shop, 'name': draft.shop}])
    for shop in shops:
        if shop.get('error'):
            continue
        expected = document.get('targets', {}).get(shop['code'], document.get('target') if shop['code'] == draft.shop else None)
        if expected != catalog_import._target(catalog_import.shop_config(shop['code'])):
            raise CatalogError('shop_target_changed', 'The target connection changed', 409)
        options = await asyncio.to_thread(catalog_import.cached_import_options, shop['code'], refresh=True)
        shop['categories'] = options['categories']
        if shop['code'] == draft.shop:
            document['categories'] = options['categories']
    document['shops'] = shops
    if document.get('automation', {}).get('version') == 1:
        document['automation'] = {**document['automation'], 'paused': True, 'pause_reason': 'draft_edited'}
    validate_rows(document)
    changed(draft, document, 'Shop categories refreshed; selected categories and product cells preserved')
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
    for patch in request.rows:
        row = rows[patch.id]
        values = deepcopy(patch.values)
        try:
            parsed = ImportValues.model_validate({**row['values'], **values})
        except (ValidationError, ValueError):
            raise CatalogError('import_invalid_cells', 'Check the edited field values', 422) from None
        if 'code' in values and parsed.code != row['values']['code']:
            raise CatalogError('sku_identity_required', 'Use the verified product identity to propose its SKU', 422)
        if (row.get('hub_product_id') or row.get('expected_hub_product_id')) and any(k in values for k in ('code', 'supplier_code', 'eans', 'sku_identity')):
            raise CatalogError('import_identity_saved', 'Edit saved product identifiers in the product editor', 409)
        if 'target_shops' in values:
            available = {shop['code'] for shop in document.get('shops', [{'code': draft.shop}]) if not shop.get('error')}
            if set(parsed.target_shops or []) - available:
                raise CatalogError('shop_not_found', 'Choose a configured active target shop', 422)
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
    await prepare_sku_rows(db, draft, document)
    validate_rows(document)
    changed(draft, document, 'Manual import cells edited')
    await db.flush()
    return await public(db, draft)


async def save(db, draft, request):
    expect(draft, request.expected_revision)
    assert_editable(draft)
    # The AI worker records handled domain failures in its outer transaction.
    # Keep all card/link writes atomic even when that caller catches an error.
    async with db.begin_nested():
        return await _save(db, draft)


async def _save(db, draft):
    document = deepcopy(draft.document)
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
    await prepare_sku_rows(db, draft, document)
    from inventory_hub.services.product_import_identity import guard_and_link
    linked = await guard_and_link(db, draft, document)
    validate_rows(document)
    # Basic receipt cards may precede merchandising. Unknown amounts stay NULL;
    # shop preview still applies every ordinary price/VAT/category validation.
    if any(r['hub_errors'] for r in document['rows']):
        raise CatalogError('import_invalid_rows', 'Resolve invalid rows before saving products', 422)
    importing = [row for row in document['rows'] if row['id'] not in linked]
    sources = [to_source(r) for r in importing]
    products = await _receiving_sources(db, sources, {int(key): value for key, value in document['receiving_rows'].items()}) \
        if document.get('receiving_origin') else await catalog_import.canonical_products(db, sources)
    for row, source in zip(importing, products):
        row['values']['code'] = source.shop_code
        row['source'] = {**row['source'], 'canonical_parent_code': source.canonical_parent_code}
        row['group_key'] = parent_shop_code(source)
    existing, conflicts = await catalog_import.local_identities(db, products)
    if conflicts:
        raise CatalogError('local_identity_conflict', 'Local identifiers require reconciliation', 409)
    supplier_id = await db.scalar(select(Supplier.id).where(Supplier.code == draft.supplier))
    for row in document['rows']:
        if row['id'] in linked:
            product = await db.get(Product, linked[row['id']])
            row['values']['code'] = product.sku
            row['canonical_code'] = product.sku
            row['sku_preview'] = {'status': 'existing', 'sku': product.sku, 'product_id': product.id,
                                  'reasons': [], 'candidate_product_ids': [product.id]}
    for row, source in zip(importing, products):
        v = row['values']
        product_id = existing.get(row['id'])
        expected_id = row.get('hub_product_id') or row.get('expected_hub_product_id')
        if expected_id and product_id != expected_id:
            raise CatalogError('local_identity_conflict', 'The saved card identity changed; reload the receipt', 409)
        owned = bool(row.get('hub_product_id') and product_id == row['hub_product_id'] and row.get('created_in_draft'))
        product = await db.get(Product, product_id) if product_id else None
        override = await db.get(ProductEditorOverride, product_id, with_for_update=True) if product_id else None
        receiving = document.get('receiving_origin')
        enrich = bool(receiving and product_id and (row.get('enriched_in_draft') or (
            _minimal_receiving_card(product, override) and (row.get('expected_hub_product_id') == product_id
                or product.created_from_source == 'invoice:' + receiving['invoice_no']))))
        if receiving and expected_id and not owned and not enrich:
            raise CatalogError('import_receiving_product_changed', 'This card changed; use the product editor', 409)
        if product_id and not owned and not enrich:
            # Identity resolution has proved this additional supplier's alias.
            # Persist it even for Hub-only imports and existing shop listings;
            # supplier-link reconciliation must not depend on a later POST.
            if source.code:
                await db.execute(insert(ProductIdentifier).values(product_id=product_id, supplier_id=supplier_id,
                    identifier_type=IdentifierType.supplier_sku, value=source.code, is_primary=False)
                    .on_conflict_do_nothing(index_elements=[ProductIdentifier.supplier_id, ProductIdentifier.value],
                        index_where=text("identifier_type = 'supplier_sku'")))
            await record_row_identity(db, product, row)
            row['hub_product_id'] = product_id
            row['warnings'] = sorted(set(row['warnings']) | {'already_in_hub_unchanged'})
            continue
        if (owned or enrich) and await db.scalar(select(ShopProduct.id).where(ShopProduct.product_id == product_id)):
            if enrich:
                raise CatalogError('import_receiving_product_changed', 'This card is now listed; use the product editor', 409)
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
        catalog_id = row.get('catalog_product_id') if receiving else source.id
        if product_id is None:
            product = Product(sku=source.shop_code, supplier_id=supplier_id, name=source.name, brand=source.brand,
                group_id=group_id, validation_required=True, validation_reason='Hub import draft; shop publication separate',
                created_from_source='product_import_draft', source_supplier_product_id=catalog_id,
                supplier_feed_data=row['source'], supplier_feed_synced_at=source.fetched_at)
            db.add(product)
            await db.flush()
            product_id = product.id
            row['created_in_draft'] = True
        await record_row_identity(db, product, row)
        if product_id and (row.get('created_in_draft') or enrich):
            identifiers = ProductIdentifierService(db)
            registered = (await db.scalars(select(ProductIdentifier).where(ProductIdentifier.product_id == product_id,
                ProductIdentifier.identifier_type.in_(identifiers.BARCODE_TYPES)))).all()
            known_barcodes = {identifier.value for identifier in registered}
            has_primary = any(identifier.is_primary for identifier in registered)
            for ean in source.eans:
                if ean not in known_barcodes:
                    await identifiers.add_identifier(product_id, ean, is_primary=not has_primary)
                    known_barcodes.add(ean)
                    has_primary = True
            if source.code:
                await db.execute(insert(ProductIdentifier).values(product_id=product_id, supplier_id=supplier_id,
                    identifier_type=IdentifierType.supplier_sku, value=source.code, is_primary=False)
                    .on_conflict_do_nothing(index_elements=[ProductIdentifier.supplier_id, ProductIdentifier.value],
                        index_where=text("identifier_type = 'supplier_sku'")))
            if not await db.scalar(select(ProductVariantAttribute.id).where(ProductVariantAttribute.product_id == product_id)):
                for order, attribute in enumerate(source.variant_attributes):
                    db.add(ProductVariantAttribute(product_id=product_id, attribute_name=attribute.name,
                        attribute_value=attribute.value, display_order=order))
        if enrich:
            if group_id and product.group_id is None:
                # _receiving_sources permitted this only for a pristine card.
                product.group_id = group_id
            product.source_supplier_product_id = catalog_id
            product.supplier_feed_data = deepcopy(row['source'])
            product.supplier_feed_synced_at = source.fetched_at
            row['enriched_in_draft'] = True
        # Do not overwrite changes made in the ordinary product editor after a draft save.
        if override and row.get('editor_revision') != override.revision:
            raise CatalogError('import_hub_product_changed', 'Product was edited elsewhere; use the product editor', 409)
        data = deepcopy(override.data) if override else {}
        data.update(common={**data.get('common', {}), 'name': v['name'], 'brand': v['brand'], 'image_url': v['images'][0] if v['images'] else None},
            variant={**data.get('variant', {}), 'sale_price_gross': v['sale_gross'], 'vat_rate': v['vat_percent'], 'attributes': v['variant_attributes']},
            import_fields=deepcopy(v), import_manual_fields=row['manual_fields'])
        if override:
            override.data, override.revision, override.updated_at = data, override.revision + 1, ai_content.now()
        else:
            override = ProductEditorOverride(product_id=product_id, revision=1, data=data)
            db.add(override)
        row['editor_revision'] = override.revision
        row['hub_product_id'] = product_id
    await db.flush()
    if document.get('receiving_origin'):
        from inventory_hub.services.receiving_products import bind_imported_products
        await bind_imported_products(db, draft, document)
    from inventory_hub.services.supplier_links import reconcile_supplier_links
    ordinary_ids = [r['hub_product_id'] for r in document['rows'] if r['id'] not in linked]
    if ordinary_ids:
        links = await reconcile_supplier_links(db, draft.supplier, product_ids=ordinary_ids)
        if links['conflicts']:
            raise CatalogError('local_identity_conflict', 'Supplier source links require reconciliation', 409)
    document['saved_hash'] = content_hash(document)
    changed(draft, document, 'Products saved in Hub; no shop writes', dirty=False)
    draft.status = 'saved'
    await db.flush()
    return await public(db, draft)


async def record_row_identity(db, product, row):
    if row['values'].get('sku_identity'):
        from inventory_hub.sku_policy_types import SkuIdentityInput
        from inventory_hub.services.sku_policy import record_sku_identity, SkuPolicyError
        try:
            await record_sku_identity(db, product, SkuIdentityInput.model_validate(row['values']['sku_identity']),
                                      actor='product-import')
        except SkuPolicyError as error:
            raise CatalogError(error.code, 'Review the verified identity before saving this product', error.status) from None


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


def approved_visibility(rows, product_ids, *, shop=None, primary_shop=None):
    if shop is not None and primary_shop is not None and shop != primary_shop:
        return False
    flags = {bool((bound_ai_policy(r) or {}).get('active_after_import', False))
             for r in rows if r['id'] in product_ids}
    if len(flags) > 1:
        raise CatalogError('import_family_field_conflict', 'A product family must share its approved visibility', 422)
    return flags == {True}


async def preview(db, draft, request):
    expect(draft, request.expected_revision)
    # Any attempted target retains its original journal, including partial and
    # uncertain multi-shop delivery. A fresh preview must never repeat a POST.
    if publication_result(draft):
        if not source_preview_retryable(draft):
            return await public(db, draft)
        document = deepcopy(draft.document)
        document['previous_publications'] = [*document.get('previous_publications', []), {
            'reason': 'import_source_availability_expired',
            'previews': {shop: value['preview_id'] for shop, value in publication_previews(draft).items()}}]
        document['publication'] = None
        document.pop('publications', None)
        draft.document = document
    assert_editable(draft)
    await assert_ai_applied(db, draft)
    document = deepcopy(draft.document)
    if draft.status != 'saved' or document.get('saved_hash') != content_hash(document):
        raise CatalogError('import_save_required', 'Save current products in Hub before preparing publication', 409)
    selected = sorted({shop for row in document['rows'] for shop in row_targets(row, draft.shop)})
    if not selected:
        return await public(db, draft)
    previews = {}
    for shop in selected:
        if catalog_import._target(catalog_import.shop_config(shop)) != document.get('targets', {draft.shop: document['target']}).get(shop):
            raise CatalogError('shop_target_changed', 'The target connection changed', 409)
        rows = deepcopy([row for row in document['rows'] if shop in row_targets(row, draft.shop)])
        from inventory_hub.services.product_import_source import availability_snapshot
        source_deadline = await availability_snapshot(db, rows, availability_policy(draft.supplier, supplier_config(draft.supplier))) \
            if draft.supplier == 'sloger' else {}
        categories = next((entry['categories'] for entry in document.get('shops', []) if entry['code'] == shop), document['categories'])
        if shop != draft.shop:
            for row in rows:
                row['values']['category_code'] = row['values'].get('target_category_codes', {}).get(shop)
                # AI publication policies are resolved for the original shop.
                # Reusing its content does not authorize visibility elsewhere.
                row.pop('ai_policy', None)
        validation = {**document, 'rows': rows, 'categories': categories}
        validate_rows(validation)
        if any(row['errors'] or not row['values']['category_code'] for row in rows):
            raise CatalogError('import_invalid_rows', f'Every selected product needs a valid product category for {shop}', 422)
        snapshot = {'draft_id': draft.id, 'hash': content_hash(document), 'rows': rows, **source_deadline}
        options = ShopImportOptions.model_validate(document['options']).model_copy(update={'category_code': None})
        prepared = await catalog_import.create_preview(db, shop, ShopImportPreviewRequest(supplier=draft.supplier,
            feed_key=document['feed_key'], product_ids=[r['id'] for r in rows], options=options,
            sale_price_overrides={r['id']: r['values']['sale_gross'] for r in rows}),
            frozen_products=[to_source(r) for r in rows], staging=snapshot)
        previews[shop] = prepared.model_dump(mode='json')
        previews[shop].update(source_deadline)
    document['publications'] = previews
    first = next(iter(previews.values()))
    document['publication'] = {**first, 'preview_id': first['preview_id'] if len(previews) == 1 else uuid4().hex,
        'items': [{**item, 'shop': shop} for shop, value in previews.items() for item in value['items']],
        'errors': [shop + ':' + error for shop, value in previews.items() for error in value['errors']]}
    changed(draft, document, 'Explicit shop publication preview prepared', dirty=False)
    await db.flush()
    return await public(db, draft)


async def assert_snapshot(db, staging):
    from inventory_hub.services.product_import_source import assert_current_availability
    await assert_current_availability(db, staging)
    draft = await get_draft(db, staging['draft_id'])
    if draft.status != 'saved' or content_hash(draft.document) != staging['hash']:
        raise CatalogError('import_draft_changed', 'The saved import draft changed after preview', 409)
    if draft.document.get('receiving_origin'):
        rows = draft.document['rows']
        matches, conflicts = await catalog_import.local_identities(db, [to_source(row) for row in rows])
        if conflicts or any(not row.get('hub_product_id') or matches.get(row['id']) != row['hub_product_id'] for row in rows):
            raise CatalogError('local_identity_conflict', 'The saved Hub card changed after publication preparation', 409)
    return draft


async def publish(db, draft, request):
    expect(draft, request.expected_revision)
    from inventory_hub.services.product_import_source import assert_ready
    assert_ready(draft)
    preview = draft.document.get('publication')
    if not preview or preview['preview_id'] != request.preview_id:
        raise CatalogError('import_preview_changed', 'Confirm the current publication preview', 409)
    result = publication_result(draft)
    if publication_state(result) == 'completed':
        return await public(db, draft), False
    # Recovery belongs to the already confirmed snapshot, even if AI was edited later.
    if not result:
        await assert_ai_applied(db, draft)
        from inventory_hub.services.product_import_source import assert_current_availability
        for target in publication_previews(draft).values():
            await assert_current_availability(db, target)
    await assert_snapshot(db, {'draft_id': draft.id, 'hash': draft.document.get('saved_hash')})
    tasks = queue_publications(draft, request.retry_failed)
    document = deepcopy(draft.document)
    changed(draft, document, 'Shop publication explicitly confirmed', dirty=False)
    await db.flush()
    return await public(db, draft), tasks


def queue_publications(draft, retry_failed=False):
    tasks = []
    previews = publication_previews(draft)
    # Validate every payload before queuing any target. Queue failures after this
    # point remain durable and are reconciled against the same preview IDs.
    if any(preview.get('errors') or any(item['status'] not in ('ready', 'exists') for item in preview['items'])
           for preview in previews.values()):
        raise CatalogError('preview_invalid', 'Resolve every selected shop preview before sending', 422)
    for shop, preview in previews.items():
        _, run = catalog_import.queue_import(shop, preview['preview_id'], retry_failed, staging_id=draft.id)
        if run:
            tasks.append((shop, preview['preview_id']))
    return tasks


async def execute_publications(tasks):
    # Start each confirmed shop immediately. Waiting behind another shop's
    # large batch would make its untouched queue look like an interrupted run
    # to the importer's existing 30-second recovery check.
    outcomes = await asyncio.gather(*(catalog_import.execute_import(shop, preview_id)
                                      for shop, preview_id in tasks), return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome


async def prepare_ai(db, draft, request):
    expect(draft, request.expected_revision)
    assert_editable(draft)
    document = deepcopy(draft.document)
    groups = defaultdict(list)
    for row in document['rows']:
        if row['values']['ai_enabled'] and not link_only(row):
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
            category_profiles={r['id']: r['values'].get('ai_category_profile', 'auto') for r in family}, research='official',
            targets=[Target(shop=draft.shop, options=options, policy=Policy())])
        jobs = await ai_content.create_batch(db, batch, frozen_products=[to_source(r) for r in family],
            staging_id=draft.id, staging_automation_version=1)
        prepared_jobs.extend(jobs)
        for row in family:
            row['ai_job_id'] = jobs[0]['id']
            row['ai_baseline'] = deepcopy(row['values'])
            row.pop('ai_policy', None)
            row.pop('ai_applied_snapshot', None)
    previous_result = publication_result(draft)
    document['automation'] = {'version': 1, 'job_ids': [j['id'] for j in prepared_jobs],
        'paused': bool(previous_result), 'pause_reason': 'publication_recovery' if previous_result else None,
        'auto_publish_requested': all(r['values']['ai_enabled'] for r in document['rows'])
            and all(row_targets(r, draft.shop) == [draft.shop] for r in document['rows'])
            and all(not j.get('policy', {}).get('confirm_import', True) for j in prepared_jobs)}
    changed(draft, document, 'AI estimates prepared from current mapped cells', dirty=False)
    await db.flush()
    return await public(db, draft)


async def start_ai(db, draft, request):
    expect(draft, request.expected_revision)
    assert_editable(draft)
    ids = {r.get('ai_job_id') for r in draft.document['rows'] if r.get('ai_job_id') and r['values']['ai_enabled'] and not link_only(r)}
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


def ai_proposed_values(row, output, context):
    baseline = row.get('ai_baseline', {})
    proposed = {'group_name' if row['is_variant'] else 'name': output.title,
        'short_description': output.short_description, 'description_html': output.long_description,
        'seo_title': output.seo_title, 'seo_description': output.meta_description,
        'metadata': {**row['values']['metadata'], **{key: getattr(output, key) for key in ('h1_descriptor', 'future_name', 'h1_descr_suffix') if getattr(output, key)}}}
    registry = (context.get('resolved', {}).get('category') or {}).get('parameters', [])
    if registry:
        from inventory_hub.services.ai_parameter_mapping import parameter_mapping, canonical_attributes
        mapping, _ = parameter_mapping(output, context)
        proposed['variant_attributes'] = canonical_attributes(baseline.get('variant_attributes', row['values']['variant_attributes']), mapping)
        allowed = {p['name'] for p in registry if p.get('approved', True)}
        generated = [{'name': p.name, 'value': value} for p in output.parameters if p.product_id is None for value in p.values]
        generated_names = {p['name'] for p in generated}
        preserved = [p for p in row['values']['parameters'] if p['name'] in allowed and p['name'] not in generated_names]
        proposed['parameters'] = preserved + generated
    descend_to_leaf = bool(baseline.get('category_code') and context.get('classification_mode') == 'category_and_profile'
        and context.get('category_selection'))
    accept_category = not baseline.get('category_code') or descend_to_leaf
    if accept_category:
        proposed['category_code'] = context.get('options', {}).get('category_code')
    safety = CatalogProduct.model_validate(row['source']).safety_information
    if safety:
        from inventory_hub.services.catalog_html import clean_description
        proposed['description_html'] += '<h2>Bezpečnostné informácie</h2>' + clean_description(safety)
    return proposed, accept_category


def previous_ai_values(row, context, previous_output=None):
    """Prove ownership by the same job and its exact last applied revision."""
    policy = row.get('ai_policy') or {}
    if (context.get('staging_automation_version') != 1 or not row.get('ai_job_id')
            or policy.get('job_id') != row['ai_job_id']):
        return {}
    previous_digest = policy.get('content_digest')
    if not previous_digest or previous_digest != context.get('staging_applied_digest'):
        raise CatalogError('import_ai_identity_changed', 'The previously applied AI revision no longer matches this draft', 409)
    snapshot = row.get('ai_applied_snapshot') or {}
    if snapshot.get('job_id') == row['ai_job_id'] and snapshot.get('content_digest') == previous_digest:
        return snapshot.get('values', {})
    if previous_output is None or ai_content.digest(previous_output.model_dump()) != previous_digest:
        raise CatalogError('import_ai_identity_changed', 'The previously applied AI revision is unavailable; keep the saved cells and prepare a new revision', 409)
    # Upgrade an existing v1 draft only from its exact saved content revision.
    # Reconstruct from the immutable pre-AI cells, never claim arbitrary current
    # cells as model output merely because an old provenance label says AI.
    original = {**row, 'values': deepcopy(row['ai_baseline'])}
    expected, _ = ai_proposed_values(original, previous_output, context)
    return {key: value for key, value in expected.items()
            if row.get('provenance', {}).get(key) == 'ai' and row['values'].get(key) == value}


def merge_ai_values(row, output, context, *, previous_output=None):
    baseline = row.get('ai_baseline', {})
    previous = previous_ai_values(row, context, previous_output)
    if row['values'].get('ai_category_profile', 'auto') != baseline.get('ai_category_profile', 'auto'):
        raise CatalogError('import_ai_identity_changed', 'The category rule profile changed; prepare a new AI job', 409)
    if any(row['values'].get(key) != baseline.get(key) and not (key == 'variant_attributes' and key in previous and row.get('provenance', {}).get(key) == 'ai' and row['values'].get(key) == previous[key]) for key in ('code', 'supplier_code', 'eans', 'manufacturer_code', 'variant_attributes', 'brand')):
        raise CatalogError('import_ai_identity_changed', 'Product identity or category changed; prepare a new AI job', 409)
    current_category = row['values'].get('category_code')
    unchanged_ai_category = (current_category and current_category == previous.get('category_code')
        and row.get('provenance', {}).get('category_code') == 'ai'
        and current_category == context.get('options', {}).get('category_code'))
    if current_category != baseline.get('category_code') and not unchanged_ai_category:
        raise CatalogError('import_ai_identity_changed', 'Product identity or category changed; prepare a new AI job', 409)
    proposed, accept_category = ai_proposed_values(row, output, context)
    applied = {}
    for key, value in proposed.items():
        # An explicitly selected parent constrains automatic classification to
        # its subtree; accepting that result refines it to the validated leaf.
        # Clearing a category before preparation also delegates its selection
        # to AI, even though the clear itself is recorded as a manual edit.
        # A selected leaf or a category changed after preparation stays pinned.
        unchanged = row['values'].get(key) == baseline.get(key) or (key in previous
            and row.get('provenance', {}).get(key) == 'ai' and row['values'].get(key) == previous[key])
        if (key not in row['manual_fields'] or key == 'category_code' and accept_category) and unchanged:
            row['values'][key] = value
            row['provenance'][key] = 'ai'
            applied[key] = deepcopy(value)
    row['values'] = ImportValues.model_validate(row['values']).model_dump(mode='json')
    if context.get('staging_automation_version') == 1 and row.get('ai_job_id'):
        row['ai_applied_snapshot'] = {'job_id': row['ai_job_id'], 'content_digest': ai_content.digest(output.model_dump()),
            'values': applied}


async def apply_ai(db, draft, request, *, job_ids=None):
    expect(draft, request.expected_revision)
    assert_editable(draft)
    document = deepcopy(draft.document)
    ids = {r.get('ai_job_id') for r in document['rows'] if r.get('ai_job_id') and r['values']['ai_enabled'] and not link_only(r)}
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
        previous_output = None
        applied_digest = job.context.get('staging_applied_digest')
        bound_rows = [row for row in document['rows'] if row.get('ai_job_id') == job.id and row['values']['ai_enabled'] and not link_only(row)]
        if applied_digest and any((row.get('ai_policy') or {}).get('job_id') == job.id
                and ((row.get('ai_applied_snapshot') or {}).get('job_id') != job.id
                     or (row.get('ai_applied_snapshot') or {}).get('content_digest') != applied_digest) for row in bound_rows):
            revisions = (await db.scalars(select(AiContentRevision).where(AiContentRevision.job_id == job.id)
                .order_by(AiContentRevision.revision.desc()))).all()
            previous = next((revision.content for revision in revisions if ai_content.digest(revision.content) == applied_digest), None)
            if previous is not None:
                previous_output = Content.model_validate(previous)
        for row in document['rows']:
            if row.get('ai_job_id') == job.id and row['values']['ai_enabled'] and not link_only(row):
                merge_ai_values(row, output, job.context, previous_output=previous_output)
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
    linked = [r for r in draft.document['rows'] if r.get('ai_job_id') == job.id and r['values']['ai_enabled'] and not link_only(r)]
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
    # The frozen AI policy belongs to its original shop. Additional targets
    # need explicit review of their own categories and publication previews.
    if any(row_targets(row, draft.shop) not in ([], [draft.shop]) for row in rows):
        return
    if not automation.get('auto_publish_requested') and any(row_targets(row, draft.shop) for row in rows):
        return
    ids = {r['ai_job_id'] for r in rows}
    jobs = {j.id: j for j in (await db.scalars(select(AiJob).where(AiJob.id.in_(ids)).order_by(AiJob.id).with_for_update())).all()}
    if any(not (current := jobs.get(id)) or current.context.get('staging_automation_version') != 1
           or current.context.get('staging_id') != draft.id or not applied_ai(current)
           or current.context.get('approval') not in ('human', 'policy')
           or current.context['resolved']['policy']['confirm_import'] for id in ids):
        return
    await assert_ai_applied(db, draft)
    from inventory_hub.services.product_import_identity import summary as identity_summary
    if (await identity_summary(db, draft))['identity_review_required']:
        job.error = 'import_identity_review_required'
        ai_content.event(job, 'import_blocked', 'Existing Hub product candidates need an explicit identity review')
        return
    await save(db, draft, DraftRevision(expected_revision=draft.revision))
    if not any(row_targets(row, draft.shop) for row in draft.document['rows']):
        return
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
