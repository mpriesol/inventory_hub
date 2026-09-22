"""Explicit field-selected content updates, with stale-data checks and durable intent."""
import asyncio
import copy
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4
from urllib.parse import quote

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from inventory_hub.ai_content_types import Content
from inventory_hub.catalog_types import ImportItem
from inventory_hub.db_models import Shop
from inventory_hub.db_models_ext import ShopProductContent
from inventory_hub.services import ai_content as service, catalog_import as imports
from inventory_hub.services.ai_content_upgates import content_fields
from inventory_hub.services.ai_content_validation import overlay, validate_content, text_of
from inventory_hub.services.catalog import CatalogError, selected_products
from inventory_hub.services.catalog_merchandising import category_chain, availability_policy, apply_availability
from inventory_hub.services.upgates import UpgatesClient, UpgatesError

TEXT_FIELDS = {'title', 'short_description', 'long_description', 'seo_title', 'seo_description'}


def parameter_rows(product):
    """The parameter detail endpoint offers canonical and legacy shapes."""
    if isinstance(product.get('parameters_new'), list):
        rows = product['parameters_new']
        if any(not isinstance(p, dict) or not isinstance(p.get('descriptions'), list)
               or not isinstance(p.get('values'), list) or any(not isinstance(v, dict)
                   or not isinstance(v.get('descriptions'), list) for v in p['values']) for p in rows):
            raise CatalogError('ai_update_parameters_unavailable', 'The shop returned invalid parameter details', 502)
        return [{'descriptions':copy.deepcopy(p['descriptions']),
                 'values':[{'descriptions':copy.deepcopy(v['descriptions'])} for v in p['values']]} for p in rows]
    rows = product.get('parameters')
    if not isinstance(rows, list):
        raise CatalogError('ai_update_parameters_unavailable', 'The shop did not return parameter details', 502)
    normalized = []
    for parameter in rows:
        if not isinstance(parameter, dict) or not isinstance(parameter.get('name'), dict) or not isinstance(parameter.get('values'), list) or any(not isinstance(v, dict) for v in parameter['values']):
            raise CatalogError('ai_update_parameters_unavailable', 'The shop returned invalid parameter details', 502)
        normalized.append({'descriptions':[{'language':language,'name':name} for language,name in parameter['name'].items()],
                           'values':[{'descriptions':[{'language':language,'value':value} for language,value in row.items()]}
                                     for row in parameter['values']]})
    return normalized


def read_product(client, code, *, include_parameters=False):
    rows = imports._get(client, 'products', {'codes':code, 'current_page_items':100}).get('products', [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise CatalogError('upgates_read_failed', 'The shop returned an invalid product response', 502)
    exact = [r for r in rows if r.get('code') == code]
    if len(exact) != 1:
        raise CatalogError('ai_update_not_found', 'The exact product code was not found in the shop', 422)
    remote = copy.deepcopy(exact[0])
    if include_parameters:
        rows = imports._get(client, f'products/{quote(code, safe="")}/parameters').get('products')
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise CatalogError('ai_update_parameters_unavailable', 'The shop did not return one parameter detail', 502)
        detail = rows[0]
        if detail.get('code') != code or detail.get('product_id') != remote.get('product_id'):
            raise CatalogError('ai_update_identity', 'Product identity changed while loading parameters', 409)
        remote['parameters'] = parameter_rows(detail)
        variants = {v['code']:v for v in detail.get('variants') or []}
        if set(variants) != {v['code'] for v in remote.get('variants') or []}:
            raise CatalogError('ai_update_identity', 'Variant identities changed while loading parameters', 409)
        for variant in remote.get('variants') or []:
            found = variants[variant['code']]
            if found.get('variant_id') != variant.get('variant_id'):
                raise CatalogError('ai_update_identity', 'Variant identity changed while loading parameters', 409)
            variant['parameters'] = parameter_rows(found)
    return remote


def identity(remote):
    return {'product_id': remote.get('product_id'), 'code': remote.get('code'), 'ean': remote.get('ean'),
            'variants': sorted([v.get('code', ''), v.get('variant_id'), v.get('ean')]
                               for v in remote.get('variants') or [])}


def projection(remote, payload):
    """Compare only selected fields; ignore stock and unrelated simultaneous changes."""
    out = {}
    for key, value in payload.items():
        if key == 'descriptions':
            out[key] = [{k: (next((d for d in remote.get(key, []) if d.get('language') == target['language']), {})).get(k)
                         for k in target} for target in value]
        elif key != 'code':
            out[key] = remote.get(key)
    return copy.deepcopy(out)


def patch_from_content(remote, enriched, fields, language):
    payload = {'code':remote['code']}
    desc = next(d for d in enriched['descriptions'] if d['language'] == language)
    text = {k:desc[k] for k in fields if k in TEXT_FIELDS}
    if text:
        payload['descriptions'] = [{'language':language, **text}]
    for key in ('categories', 'availability'):
        if key in fields:
            payload[key] = enriched.get(key)
    if 'parameters' in fields:
        parameters = copy.deepcopy(remote.get('parameters') or [])
        additions = enriched.get('parameters') or []
        def name(p):
            return next((d.get('name') for d in p.get('descriptions', []) if d.get('language') == language), None)
        names = {name(p) for p in additions}
        parameters = [p for p in parameters if name(p) not in names]
        payload['parameters'] = parameters + additions
    if 'metas' in fields:
        # Only the three content fields, not validation flags or supplier metadata.
        changes = {m['key']:m for m in enriched.get('metas', []) if m['key'] in ('h1_descriptor','future_name','h1_descr_suffix')}
        def writable(m):
            return {k:v for k,v in m.items() if k in ('key','value','values')}
        for previous in remote.get('metas') or []:
            change = changes.get(previous.get('key'))
            if change is None or 'value' in change:
                continue
            # Updating Slovak content must not erase other shop languages.
            existing = previous.get('values') or []
            if isinstance(existing, dict):
                existing = [{'language': key, 'value': value.get('value') if isinstance(value, dict) else value}
                            for key, value in existing.items()]
            changed_languages = {v['language'] for v in change['values']}
            change['values'] = [{'language': v['language'], 'value': v.get('value', '')}
                                for v in existing if v.get('language') not in changed_languages] + change['values']
        payload['metas'] = [writable(m) for m in remote.get('metas', []) if m['key'] not in changes] + list(changes.values())
    return payload


async def prepare(db, job, request):
    service.expect(job, request.expected_revision)
    if job.kind != 'product' or job.status not in ('review','blocked','ready','exists','completed','import_blocked') or not job.output:
        raise CatalogError('ai_update_state', 'Review content before preparing an update', 409)
    prior = job.context.get('update_preview') or {}
    if prior.get('state') in ('sending','uncertain'):
        raise CatalogError('ai_update_uncertain', 'Reconcile the previous update first', 409)
    ctx = job.context
    if ctx['target'] != imports._target(imports.shop_config(ctx['shop'])):
        raise CatalogError('shop_target_changed', 'Shop connection changed; prepare from the current shop again', 409)
    checks = validate_content(Content.model_validate(job.output), ctx, (job.usage or {}).get('opened_sources', []))
    if checks['errors']:
        raise CatalogError('ai_validation_failed', '; '.join(checks['errors']), 422)
    client = UpgatesClient.from_shop(ctx['shop'])
    remote = await asyncio.to_thread(read_product, client, ctx['code'],
        include_parameters='parameters' in request.fields or ctx.get('source_kind') == 'shop')
    if ctx.get('source_kind') == 'shop':
        from inventory_hub.services.ai_content_existing import assert_source, products_from_remote
        assert_source(remote, ctx)
        products = products_from_remote(remote, ctx)
        if 'availability' in request.fields:
            raise CatalogError('ai_update_source_availability', 'Supplier availability is not available for this shop-only preparation', 422)
    else:
        products = await selected_products(db, ctx['supplier'], ctx['feed_key'], ctx['product_ids'])
        if service.source_digest(products) != ctx['source_digest']:
            raise CatalogError('ai_source_changed', 'Supplier facts changed; prepare fresh content', 409)
        if not remote.get('variants') and products[0].eans and remote.get('ean') not in products[0].eans:
            raise CatalogError('ai_update_identity', 'The shop EAN differs from the selected product', 409)
    if remote.get('variants') and ctx.get('source_kind') != 'shop':
        known = {v['code']:v for v in remote['variants']}
        if set(known) != {p.shop_code for p in products} or any(p.eans and known[p.shop_code].get('ean') not in p.eans for p in products):
            raise CatalogError('ai_update_family', 'Select the complete matching family before updating shared content', 409)
    language = ctx['options']['language']
    item = ImportItem(code=ctx['code'], name=ctx['name'], product_ids=ctx['product_ids'] or [p.id for p in products], status='ready',
                      payload={'code':ctx['code'], 'descriptions':[{'language':language}]})
    policy = {**availability_policy(ctx['supplier']), **ctx['resolved'].get('import_policy', {})}
    enriched = overlay(item, {'content':job.output, 'active_after_import':False,
        'registered_parameters':bool((ctx['resolved'].get('category') or {}).get('parameters')),
        'meta_common':await asyncio.to_thread(content_fields, ctx['shop'], client) if 'metas' in request.fields else {},
        'safety':'\n'.join(dict.fromkeys(p.safety_information for p in products if p.safety_information))}, language).payload
    if 'categories' in request.fields:
        options = await asyncio.to_thread(imports.cached_import_options, ctx['shop'], client)
        if not ctx['options'].get('category_code'):
            raise CatalogError('category_not_found', 'Choose a category before updating categories', 422)
        assigned = category_chain(options['categories'], ctx['options']['category_code'])
        by_id = {c['category_id']:c['code'] for c in options['categories'] if c.get('category_id') is not None}
        categories = {}
        for existing in remote.get('categories') or []:
            code = existing.get('code') or by_id.get(existing.get('category_id'))
            if not code:
                raise CatalogError('category_tree_invalid', 'An existing category could not be resolved', 422)
            for category in category_chain(options['categories'], code):
                categories[category['code']] = {'code':category['code'], 'main_yn':False}
        for category in assigned:
            categories[category['code']] = category
        enriched['categories'] = list(categories.values())
    if 'availability' in request.fields:
        if remote.get('variants'):
            raise CatalogError('ai_update_variant_availability', 'Variant availability needs a separate per-variant update', 422)
        try:
            stock = Decimal(str(remote.get('stock')))
            if not stock.is_finite():
                raise InvalidOperation
        except InvalidOperation:
            raise CatalogError('ai_update_stock_unknown', 'Verify shop stock before changing availability', 422) from None
        if stock > 0:
            enriched['availability'] = remote.get('availability')
        else:
            apply_availability(enriched, products, policy)
    if 'parameters' in request.fields and not (ctx['resolved'].get('category') or {}).get('parameters'):
        raise CatalogError('ai_parameter_registry_missing', 'Choose a category parameter registry first', 422)
    payload = patch_from_content(remote, enriched, request.fields, language)
    preview = {'id':uuid4().hex, 'state':'ready', 'target':imports._target(imports.shop_config(ctx['shop'])),
               'expires_at':(service.now()+timedelta(minutes=30)).isoformat(), 'fields':request.fields,
               'identity':identity(remote), 'language':language,
               'before':projection(remote,payload), 'after':projection(payload,payload), 'payload':payload}
    if 'availability' in request.fields:
        preview['shop_stock'] = remote.get('stock')
    if 'categories' in request.fields:
        preview['category_codes_by_id'] = {str(key):value for key,value in by_id.items()}
        from inventory_hub.services.catalog_merchandising import system_category_codes
        preview['system_category_codes'] = sorted(system_category_codes(options['categories']))
    job.context = {**ctx, 'update_preview':preview, 'update_result':None}
    service.event(job, job.status, 'Field-selected update preview prepared; no product changed')
    return service.summary(job, detail=True)


def mismatched_fields(remote, preview):
    actual = projection(remote, preview['payload'])
    expected = preview['after']
    mismatches = []
    # Text HTML may be normalized by Upgates; all other selected data must match.
    for key in actual:
        if key == 'descriptions':
            for description, target in zip(actual[key], expected[key]):
                mismatches.extend(field for field in target if field != 'language'
                    and text_of(str(description.get(field))) != text_of(str(target[field])))
        elif key == 'parameters':
            def params(rows):
                return sorted((d['language'],d.get('name',''),tuple(sorted((t['language'],t.get('value','')) for v in r.get('values',[]) for t in v.get('descriptions',[])))) for r in (rows or []) for d in r.get('descriptions',[]))
            if params(actual[key]) != params(expected[key]): mismatches.append(key)
        elif key == 'categories':
            def category_values(rows):
                return sorted((c.get('code') or preview.get('category_codes_by_id', {}).get(str(c.get('category_id')), ''),
                               bool(c.get('main_yn'))) for c in rows or [])
            ignored = set(preview.get('system_category_codes', []))
            if [c for c in category_values(actual[key]) if c[0] not in ignored or c[1]] != [c for c in category_values(expected[key]) if c[0] not in ignored or c[1]]:
                mismatches.append(key)
        elif key == 'metas':
            from inventory_hub.services.ai_content_upgates import meta_value
            lookup = {m['key']:m for m in actual[key] or []}
            for expected_meta in expected[key] or []:
                found = lookup.get(expected_meta['key'])
                if found is None:
                    mismatches.append(key)
                    break
                values = expected_meta.get('values') or []
                languages = list(values) if isinstance(values, dict) else [v['language'] for v in values]
                if 'value' in expected_meta:
                    languages = [preview.get('language', 'sk')]
                if any(meta_value(found, lang) != meta_value(expected_meta, lang) for lang in languages):
                    mismatches.append(key)
                    break
        elif actual[key] != expected[key]: mismatches.append(key)
    return list(dict.fromkeys(mismatches))


def values_match(remote, preview):
    return not mismatched_fields(remote, preview)


async def cache_confirmed(db, shop, code, remote):
    """Reuse verified readback for Hub details without another remote API call."""
    shop_id = await db.scalar(select(Shop.id).where(Shop.code == shop))
    if shop_id is None:
        raise CatalogError('shop_not_found', 'The target shop is no longer registered', 409)
    row = insert(ShopProductContent).values(shop_id=shop_id, external_code=code, data=remote, pulled_at=imports.now())
    await db.execute(row.on_conflict_do_update(
        index_elements=[ShopProductContent.shop_id, ShopProductContent.external_code],
        set_={'data':remote, 'pulled_at':imports.now()}))


def check_before(remote, preview):
    if projection(remote, preview['payload']) != preview['before']:
        raise CatalogError('ai_shop_content_changed', 'Selected fields changed in the shop; prepare a new comparison', 409)
    if preview.get('identity') != identity(remote):
        raise CatalogError('ai_update_identity', 'Product identity changed; prepare a new comparison', 409)
    if 'availability' in preview['fields'] and remote.get('stock') != preview.get('shop_stock'):
        raise CatalogError('ai_shop_content_changed', 'Shop stock changed; prepare a new availability comparison', 409)


async def confirm(db, job, request):
    service.expect(job, request.expected_revision)
    preview = copy.deepcopy(job.context.get('update_preview') or {})
    if preview.get('id') != request.preview_id or preview.get('state') not in ('ready','sending','uncertain','completed'):
        raise CatalogError('ai_update_state', 'Prepare a valid update preview first', 409)
    if preview['state'] == 'completed':
        return service.summary(job, detail=True)
    if preview['target'] != imports._target(imports.shop_config(job.context['shop'])):
        raise CatalogError('shop_target_changed', 'Shop connection changed', 409)
    client = UpgatesClient.from_shop(job.context['shop'])
    include_parameters = 'parameters' in preview['fields'] or job.context.get('source_kind') == 'shop'
    if 'categories' in preview['fields'] and 'system_category_codes' not in preview:
        from inventory_hub.services.catalog_merchandising import system_category_codes
        options = await asyncio.to_thread(imports.cached_import_options, job.context['shop'], client)
        preview['system_category_codes'] = sorted(system_category_codes(options['categories']))
    error = None
    try:
        remote = await asyncio.to_thread(read_product, client, job.context['code'], include_parameters=include_parameters)
    except CatalogError as exc:
        if preview['state'] == 'ready':
            raise
        remote, error = None, exc.code
    if preview['state'] == 'ready':
        if service.now().isoformat() > preview['expires_at']:
            raise CatalogError('preview_expired', 'Prepare a new update preview', 409)
        check_before(remote, preview)
        preview['state'] = 'sending'
        job.context = {**job.context, 'update_preview':preview}
        service.event(job,job.status,'Update intent recorded; no automatic repeated PUT')
        await db.commit()
        # Different jobs may target the same live product. This transaction keeps
        # their final comparison, PUT and readback serialized across API workers.
        key = int(service.digest(['ai-content-update', job.context['shop'], job.context['code']])[:16], 16) % (1 << 63)
        await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key':key})
        try:
            remote = await asyncio.to_thread(read_product, client, job.context['code'], include_parameters=include_parameters)
            check_before(remote, preview)
        except CatalogError as exc:
            # The recorded intent never reached PUT; a fresh preview is safe.
            preview['state'], error = 'rejected', exc.code
        rejected = False
        if preview['state'] != 'rejected':
            try:
                response = await asyncio.to_thread(client.put, 'products', {'products':[preview['payload']]})
                # HTTP 200 can still carry a per-product rejection.
                rows = response.get('products') or [] if isinstance(response, dict) else []
                rejected = any(row.get('code') == job.context['code'] and row.get('updated_yn') is False
                               for row in rows if isinstance(row, dict))
                if rejected:
                    error = 'upgates_update_rejected'
            except UpgatesError as exc:
                rejected = exc.status_code in (400, 401, 403, 422, 429)
                error = f'upgates_update_http_{exc.status_code}' if rejected else 'ai_update_uncertain'
                # Do not immediately repeat rejected authentication/rate-limited calls.
                if exc.status_code in (401, 403, 429):
                    preview['state'] = 'rejected'
        if preview['state'] != 'rejected':
            try:
                remote = await asyncio.to_thread(read_product, client, job.context['code'], include_parameters=include_parameters)
            except CatalogError as exc:
                remote, error = None, error or exc.code
            if rejected and remote is not None and identity(remote) == preview['identity'] and projection(remote, preview['payload']) == preview['before']:
                preview['state'] = 'rejected'
        # The committed intent released the original row lock. Preserve changes
        # made by another reader/operator while the network request was in flight.
        await db.refresh(job, with_for_update=True)
        current = job.context.get('update_preview') or {}
        if current.get('id') != preview['id']:
            raise CatalogError('ai_job_changed', 'The update preparation changed while sending', 409)
        if current.get('state') == 'completed':
            return service.summary(job, detail=True)
    mismatches = []
    if preview['state'] != 'rejected':
        confirmed_identity = remote is not None and identity(remote) == preview.get('identity')
        mismatches = mismatched_fields(remote, preview) if confirmed_identity else []
        preview['state'] = 'completed' if confirmed_identity and not mismatches else 'uncertain'
        if mismatches:
            error = 'ai_update_readback_mismatch'
        elif remote is not None and not confirmed_identity:
            error = 'ai_update_identity'
    if preview['state'] == 'completed':
        error = None
        await cache_confirmed(db, job.context['shop'], job.context['code'], remote)
        if job.context.get('source_kind') == 'shop':
            from inventory_hub.services.ai_content_existing import source_snapshot
            job.context = {**job.context, 'update_source_digest':service.digest(source_snapshot(remote, job.context['options']['language']))}
    result = {'status':preview['state'], 'fields':preview['fields']}
    if preview['state'] == 'uncertain' and remote is not None and confirmed_identity:
        result['mismatched_fields'] = mismatches
        result['observed'] = projection(remote, preview['payload'])
    if error:
        result['error'] = error
    job.context = {**job.context,'update_preview':preview,'update_result':result}
    notes = {'completed':'Updated fields verified in shop', 'rejected':'Shop rejected the update; prepare a new comparison',
             'uncertain':'Update requires reconciliation; do not resend'}
    service.event(job, job.status, notes[preview['state']])
    return service.summary(job,detail=True)
