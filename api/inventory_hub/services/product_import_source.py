"""Bounded supplier reads before editable import preparation; no AI or shop writes.

The existing draft owns progress and recovery. Network calls run between short
transactions; a lease/token rejects duplicate tasks and late results after retry.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import time
from uuid import uuid4

from inventory_hub.catalog_types import CatalogProduct, ShopImportOptions
from inventory_hub.database import get_session_context
from inventory_hub.product_import_types import ImportValues
from inventory_hub.services import catalog_import, sloger_data
from inventory_hub.services.catalog import CatalogError, supplier_config
from inventory_hub.services.catalog_identity import parent_shop_code
from inventory_hub.services.catalog_merchandising import apply_availability, availability_policy
from inventory_hub.sloger_probe_types import SlogerObservation

BATCH_SIZE = 10
LEASE_SECONDS = 180


def initialize(document):
    document['source_lookup'] = {'state': 'queued', 'error': None, 'lease_expires_at': _lease()}
    for row in document['rows']:
        row.update(source_status='pending', source_error=None)


def _expired(progress):
    deadline = progress.get('lease_expires_at')
    return not deadline or datetime.fromisoformat(deadline) <= datetime.now(timezone.utc)


def public_status(draft):
    progress = draft.document.get('source_lookup')
    if not progress:
        return None
    state, error = progress['state'], progress.get('error')
    if state in ('queued', 'running') and _expired(progress):
        state, error = 'failed', 'import_source_interrupted'
    rows = draft.document['rows']
    can_skip = (state == 'failed' and draft.status in ('source_pending', 'source_running', 'source_failed')
        and not any(draft.document.get(key) for key in ('saved_hash', 'publication', 'publications', 'previous_publications'))
        and not any(row.get('hub_product_id') for row in rows))
    return {'state': state, 'total': len(rows),
        'completed': sum(r.get('source_status') == 'completed' for r in rows),
        'failed': sum(r.get('source_status') == 'failed' for r in rows),
        'skipped': sum(r.get('source_status') == 'skipped' for r in rows),
        'error': error, 'retryable': state == 'failed', 'skippable': can_skip}


def assert_ready(draft):
    progress = public_status(draft)
    if progress and progress['state'] not in ('completed', 'skipped'):
        code = 'import_source_failed' if progress['state'] == 'failed' else 'import_source_pending'
        raise CatalogError(code, 'Wait for supplier data before editing or importing this preparation', 409)


async def retry(db, draft, request):
    from inventory_hub.services import product_import as imports
    imports.expect(draft, request.expected_revision)
    progress = public_status(draft)
    if not progress or not progress['retryable']:
        raise CatalogError('import_source_not_retryable', 'Supplier data is complete or still being read', 409)
    document = deepcopy(draft.document)
    document['source_lookup'] = {'state': 'queued', 'error': None, 'lease_expires_at': _lease()}
    for row in document['rows']:
        if row.get('source_status') != 'completed':
            row.update(source_status='pending', source_error=None)
    imports.changed(draft, document, 'Supplier data retry requested', dirty=False)
    draft.status = 'source_pending'
    await db.flush()
    return await imports.public(db, draft)


async def skip(db, draft, request):
    """Explicitly accept listing data after a failed read, under the draft lock."""
    from inventory_hub.services import product_import as imports
    imports.expect(draft, request.expected_revision)
    progress = public_status(draft)
    if not progress or not progress['skippable']:
        raise CatalogError('import_source_not_skippable', 'Supplier data must fail or be interrupted before continuing with the feed', 409)
    document = deepcopy(draft.document)
    policy = availability_policy(draft.supplier, supplier_config(draft.supplier))
    for row in document['rows']:
        if row.get('source_status') == 'completed':
            continue
        evidence = row['source']
        if 'availability' not in row.get('manual_fields', []):
            row['values']['availability'] = policy['unknown']
            row['provenance']['availability'] = 'derived'
        if evidence.get('sloger_observation') and 'purchase_net' not in row.get('manual_fields', []):
            row['values']['purchase_net'] = None
            row['provenance']['purchase_net'] = 'derived'
        # A failed current read cannot endorse old cached B2B stock or quotes.
        # Completed rows retain their original evidence and freshness checks.
        for key in ('availability', 'supplier_available', 'supplier_stock', 'supplier_stock_min',
                    'supplier_stock_raw', 'supplier_stock_external', 'supplier_stock_external_raw',
                    'supplier_external_available', 'sloger_observation', 'supplier_observation_expires_at'):
            evidence[key] = None
        row.update(source_status='skipped', source_error=row.get('source_error') or progress['error'] or 'import_source_failed')
        evidence['warnings'] = sorted(set([*evidence.get('warnings', []), 'sloger_b2b_feed_fallback']))
    # Reconcile all available identifiers just as successful ingestion does.
    # The terminal state drops the old worker token, fencing any late response.
    await _complete(db, draft, document, skipped=True, error=progress['error'])
    await db.flush()
    return await imports.public(db, draft)


def enrich_product(product: CatalogProduct, observation: SlogerObservation) -> CatalogProduct:
    """Retain listing DMOC, add factual identifiers and current supplier quote."""
    if observation.sku != product.code or product.supplier != 'sloger':
        raise CatalogError('sloger_product_identity_mismatch', 'Supplier evidence does not match the selected product', 409)
    prices = product.prices.model_copy(deep=True)
    warnings = list(product.warnings)
    factor = 1 + prices.vat_percent / 100 if prices.vat_percent is not None else None
    same_currency = observation.currency == prices.currency
    if observation.currency and not same_currency:
        warnings.append('sloger_currency_mismatch')
    if same_currency and observation.purchase_price is not None:
        quote = Decimal(observation.purchase_price)
        if observation.price_basis == 'net':
            prices.purchase_net = quote
            prices.purchase_gross = quote * factor if factor is not None else None
        elif observation.price_basis == 'gross' and factor is not None:
            prices.purchase_gross, prices.purchase_net = quote, quote / factor
    # A listing feed's DMOC remains authoritative, including configured brand
    # coefficients. The current web retail is only a missing-price fallback.
    if (prices.retail_gross is None or prices.retail_gross <= 0) and same_currency and observation.retail_price is not None:
        retail = Decimal(observation.retail_price)
        if observation.retail_price_basis == 'gross':
            prices.retail_gross = retail
            prices.retail_net = retail / factor if factor is not None else None
        elif observation.retail_price_basis == 'net' and factor is not None:
            prices.retail_net, prices.retail_gross = retail, retail * factor
    if prices.purchase_net is None:
        warnings.append('sloger_purchase_price_unknown')
    if observation.available is None:
        warnings.append('sloger_availability_unknown')
    quantity = Decimal(observation.quantity) if observation.quantity is not None else None
    eans = list(dict.fromkeys([*product.eans, *observation.eans]))
    if eans:
        warnings = [warning for warning in warnings if warning != 'missing_ean']
    if prices.retail_gross is not None and prices.retail_gross > 0:
        warnings = [warning for warning in warnings if warning != 'missing_retail_price']
    return product.model_copy(update={
        'eans': eans,
        'manufacturer_code': observation.manufacturer_code or product.manufacturer_code,
        'url': observation.source_url, 'prices': prices, 'sloger_observation': observation,
        'availability': observation.availability, 'supplier_available': observation.available,
        'supplier_stock': quantity if observation.quantity_kind == 'exact' else None,
        'supplier_stock_min': quantity if observation.quantity_kind == 'minimum' else None,
        'supplier_stock_raw': observation.availability,
        'supplier_stock_external': None, 'supplier_stock_external_raw': None,
        'supplier_external_available': None,
        'warnings': sorted(set(warnings)),
    })


def apply_observation(row, observation, options, cfg):
    from inventory_hub.services import product_import as imports
    before = CatalogProduct.model_validate(row['source'])
    product = enrich_product(before, observation)
    prices = product.prices
    availability = {}
    apply_availability(availability, [product], availability_policy(product.supplier, cfg))
    values = {**row['values'], 'eans': product.eans,
        'manufacturer_code': product.manufacturer_code or '',
        'purchase_net': imports.money(prices.purchase_net), 'retail_gross': imports.money(prices.retail_gross),
        'availability': availability['availability']}
    if prices.retail_gross != before.prices.retail_gross:
        try:
            values['sale_gross'] = imports.money(catalog_import._prices(product, options, cfg, True)[0]['pricelists'][0]['price_original'])
        except (ValueError, ArithmeticError):
            values['sale_gross'] = None
    manual = set(row.get('manual_fields', []))
    for field in manual:
        values[field] = row['values'][field]
    # Apply through the same editable-cell validation before retaining a result.
    row['values'] = ImportValues.model_validate(values).model_dump(mode='json')
    row['source'] = product.model_dump(mode='json')
    for field in ('eans', 'manufacturer_code', 'purchase_net', 'availability'):
        if field not in manual:
            row['provenance'][field] = 'supplier'
    if prices.retail_gross != before.prices.retail_gross:
        if 'retail_gross' not in manual:
            row['provenance']['retail_gross'] = 'supplier'
        if 'sale_gross' not in manual:
            row['provenance']['sale_gross'] = 'derived'
    row.update(source_status='completed', source_error=None)


def _owns(draft, token):
    progress = draft.document.get('source_lookup') or {}
    return progress.get('state') == 'running' and progress.get('token') == token


def _lease():
    return (datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)).isoformat()


def prepare_availability(rows, policy, freshness_seconds=None):
    """Expired automatic lead times become unknown in this preview only."""
    deadlines = []
    for row in rows:
        if 'availability' in row.get('manual_fields', []):
            continue
        evidence = CatalogProduct.model_validate(row['source'])
        expiry = evidence.supplier_observation_expires_at
        if evidence.sloger_observation is None:
            continue
        if expiry and freshness_seconds is not None:
            expiry = min(expiry, evidence.sloger_observation.observed_at + timedelta(seconds=freshness_seconds))
        if expiry is None or expiry <= datetime.now(timezone.utc):
            row['values']['availability'] = policy['unknown']
        elif evidence.supplier_available is True and row['values']['availability'] != policy['unknown']:
            deadlines.append(expiry)
    return min(deadlines).isoformat() if deadlines else None


async def _freshness_seconds(db):
    from sqlalchemy import select
    from inventory_hub.db_models import Supplier
    from inventory_hub.supplier_availability_models import SupplierAvailabilitySettings
    return await db.scalar(select(SupplierAvailabilitySettings.freshness_seconds)
        .join(Supplier, Supplier.id == SupplierAvailabilitySettings.supplier_id).where(Supplier.code == 'sloger'))


async def availability_snapshot(db, rows, policy):
    if not any(row['source'].get('sloger_observation') for row in rows):
        return {}
    deadline = prepare_availability(rows, policy, await _freshness_seconds(db) or 0)
    if not deadline:
        return {}
    observed = [CatalogProduct.model_validate(row['source']).sloger_observation.observed_at
        for row in rows if row['source'].get('sloger_observation') and row['source'].get('supplier_available') is True
        and 'availability' not in row.get('manual_fields', []) and row['values']['availability'] != policy['unknown']]
    return {'supplier_availability_expires_at': deadline,
            'supplier_availability_observed_at': min(observed).isoformat()}


def assert_availability_fresh(staging):
    deadline = (staging or {}).get('supplier_availability_expires_at')
    if deadline and datetime.fromisoformat(deadline) <= datetime.now(timezone.utc):
        raise CatalogError('import_source_availability_expired', 'Supplier availability expired; prepare a new publication preview', 409)


async def assert_current_availability(db, staging):
    assert_availability_fresh(staging)
    observed = (staging or {}).get('supplier_availability_observed_at')
    if observed:
        cap = datetime.fromisoformat(observed) + timedelta(seconds=await _freshness_seconds(db) or 0)
        assert_availability_fresh({'supplier_availability_expires_at': cap.isoformat()})


async def _lookup(codes):
    # Contention is raised before source access. Defer only that unsent batch;
    # authentication, transport and supplier errors are never retried here.
    deadline = time.monotonic() + 60
    while True:
        try:
            return await sloger_data.lookup_products(codes)
        except CatalogError as error:
            if error.code != 'sloger_b2b_busy' or time.monotonic() >= deadline:
                raise
            await asyncio.sleep(2)


async def _claim(id):
    from inventory_hub.services import product_import as imports
    async with get_session_context() as db:
        draft = await imports.get_draft(db, id, lock=True)
        if (draft.document.get('source_lookup') or {}).get('state') != 'queued':
            return None
        token = uuid4().hex
        document = deepcopy(draft.document)
        document['source_lookup'] = {'state': 'running', 'token': token, 'lease_expires_at': _lease(), 'error': None}
        imports.changed(draft, document, 'Reading supplier product data', dirty=False)
        draft.status = 'source_running'
        return token


async def _fail(id, token, code):
    from inventory_hub.services import product_import as imports
    async with get_session_context() as db:
        draft = await imports.get_draft(db, id, lock=True)
        if not _owns(draft, token):
            return
        document = deepcopy(draft.document)
        document['source_lookup'] = {'state': 'failed', 'error': code}
        imports.changed(draft, document, 'Supplier data preparation needs attention', dirty=False)
        draft.status = 'source_failed'


async def _complete(db, draft, document, *, skipped=False, error=None):
    from inventory_hub.services import product_import as imports
    from inventory_hub.services.product_import_identity import canonical_for_review
    sources = [imports.to_source(row) for row in document['rows']]
    products = await imports._receiving_sources(db, sources,
        {int(key): value for key, value in document['receiving_rows'].items()}) if document.get('receiving_origin') \
        else await canonical_for_review(db, sources)
    for row, product in zip(document['rows'], products):
        row['values']['code'] = product.shop_code
        row['canonical_code'] = product.shop_code
        row['group_key'] = parent_shop_code(product)
        row['source'] = product.model_dump(mode='json')
    await imports.prepare_sku_rows(db, draft, document)
    imports.validate_rows(document)
    document['source_lookup'] = {'state': 'skipped' if skipped else 'completed', 'error': error}
    note = 'Continue with feed data for unread supplier products' if skipped else 'Supplier data ready for import preparation'
    imports.changed(draft, document, note, dirty=False)
    draft.status = 'draft'


async def process(id):
    """One explicitly queued draft. A restart requires a visible manual retry."""
    from inventory_hub.services import product_import as imports, supplier_availability
    token = await _claim(id)
    if token is None:
        return
    try:
        while True:
            async with get_session_context() as db:
                draft = await imports.get_draft(db, id, lock=True)
                if not _owns(draft, token):
                    return
                document = deepcopy(draft.document)
                pending = [row for row in document['rows'] if row.get('source_status') == 'pending'][:BATCH_SIZE]
                if not pending:
                    if any(row.get('source_status') == 'failed' for row in document['rows']):
                        code = next(row['source_error'] for row in document['rows'] if row.get('source_status') == 'failed')
                        document['source_lookup'] = {'state': 'failed', 'error': code}
                        imports.changed(draft, document, 'Some supplier products could not be read', dirty=False)
                        draft.status = 'source_failed'
                    else:
                        await _complete(db, draft, document)
                    return
                document['source_lookup']['lease_expires_at'] = _lease()
                draft.document = document
                codes = [row['values']['supplier_code'] for row in pending]
            # No draft/supplier/identity transaction remains open here.
            result = await _lookup(codes)
            by_code = {item.sku: item for item in result.items}
            async with get_session_context() as db:
                draft = await imports.get_draft(db, id, lock=True)
                if not _owns(draft, token):
                    return
                document = deepcopy(draft.document)
                options = ShopImportOptions.model_validate(document['options'])
                cfg = supplier_config(draft.supplier)
                observations = []
                for row in document['rows']:
                    if row['id'] not in {item['id'] for item in pending}:
                        continue
                    item = by_code.get(row['values']['supplier_code'])
                    if not item or item.observation is None:
                        row.update(source_status='failed', source_error=item.error if item else 'sloger_invalid_result')
                        continue
                    try:
                        apply_observation(row, item.observation, options, cfg)
                        observations.append(item.observation)
                    except (ValueError, ArithmeticError, CatalogError):
                        row.update(source_status='failed', source_error='sloger_product_data_invalid')
                if observations:
                    expiries = await supplier_availability.record_sloger_import(db, observations, id)
                    for row in document['rows']:
                        if row['id'] in {item['id'] for item in pending} and row.get('source_status') == 'completed':
                            expiry = expiries[row['values']['supplier_code']]
                            row['source']['supplier_observation_expires_at'] = expiry.isoformat()
                imports.validate_rows(document)
                document['source_lookup']['lease_expires_at'] = _lease()
                if result.aborted:
                    document['source_lookup'] = {'state': 'failed', 'error': next(
                        (item.error for item in result.items if item.error), 'sloger_lookup_failed')}
                    draft.status = 'source_failed'
                imports.changed(draft, document, 'Supplier product data received', dirty=False)
                if result.aborted:
                    return
    except asyncio.CancelledError:
        await _fail(id, token, 'import_source_interrupted')
        raise
    except Exception as error:
        # Adapter messages/HTML and credential validation details never enter
        # draft events, responses or logs. Only application error codes survive.
        await _fail(id, token, error.code if isinstance(error, CatalogError) else 'import_source_failed')
