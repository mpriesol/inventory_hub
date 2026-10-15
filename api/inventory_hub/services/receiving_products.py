"""Invoice product preparation reuses catalog drafts without receiving any stock.

Status reads never change invoice facts. Preparation serializes only the receipt
selection; imported aliases are reconciled into active lines in a separate
session -> identity locked operation, preserving the scanner's lock order.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import or_, select, text

from inventory_hub.catalog_types import CatalogPrices, CatalogProduct, ShopImportOptions
from inventory_hub.db_models import FeedRunStatus, IdentifierType, ProductIdentifier, ReceivingStatus, Shop, SupplierFeed, SupplierProduct
from inventory_hub.db_models_ext import ReceivingLine, ReceivingSession, ShopProduct, StockBalance
from inventory_hub.product_editor_models import ProductEditorOverride
from inventory_hub.product_import_models import ProductImportDraft
from inventory_hub.product_import_types import DraftCreate
from inventory_hub.services.catalog import CatalogError, _source_configured, public_product, supplier_config
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK, RemoteIdentity, load_identity_index, verified_barcodes
from inventory_hub.stock_tracking_models import StockTracking
from inventory_hub.supplier_prefix import canonical_supplier_sku, get_supplier_prefix

STATUSES = ('matched', 'basic_card', 'feed_only', 'not_found', 'ambiguous', 'feed_unverified')
ACTIVE = (ReceivingStatus.new, ReceivingStatus.in_progress, ReceivingStatus.paused)


def is_basic_card(product, *, has_shop=False, import_fields=None):
    return bool(product and (product.created_from_source or '').startswith('invoice:')
                and product.validation_required and not has_shop and not import_fields)


def _barcode_owner(index, eans):
    """All barcodes must already independently identify one existing card."""
    owners = [index.by_barcode.get(ean, set()) for ean in eans]
    if not owners or any(len(ids) != 1 for ids in owners):
        return None
    combined = set().union(*owners)
    return next(iter(combined)) if len(combined) == 1 else None


def _identity(session, line, prefix, source=None, *, expected=None):
    sku = (line.supplier_sku or '').strip()
    return RemoteIdentity(0, prefix + sku if sku else '',
        barcodes=(line.ean or '', *(source.eans if source else [])),
        supplier_id=session.supplier_id, supplier_sku=sku,
        expected_product_id=line.product_id if expected is None else expected)


async def _catalog_candidates(db, session):
    """Only indexed, active listing snapshots count as feed products.

    Minimal SupplierProduct rows created by receiving are not catalog matches.
    Exact supplier SKU and verified invoice barcodes are the only search keys.
    """
    skus = sorted({(line.supplier_sku or '').strip() for line in session.lines} - {''})
    eans = sorted({ean for line in session.lines for ean in verified_barcodes((line.ean,))})
    rows = {}
    for start in range(0, max(len(skus), len(eans)), 200):
        conditions = []
        if skus[start:start + 200]:
            conditions.append(SupplierProduct.supplier_sku.in_(skus[start:start + 200]))
        conditions.extend(SupplierProduct.attributes['catalog']['eans'].contains([ean]) for ean in eans[start:start + 200])
        if not conditions:
            continue
        found = (await db.scalars(select(SupplierProduct).join(SupplierFeed, SupplierFeed.id == SupplierProduct.source_feed_id).where(
            SupplierProduct.supplier_id == session.supplier_id,
            SupplierProduct.is_active.is_(True), SupplierProduct.source_feed_id.is_not(None),
            SupplierFeed.is_active.is_(True), SupplierFeed.feed_type != 'stock', SupplierFeed.code != 'stock',
            SupplierProduct.attributes.has_key('catalog'), or_(*conditions)))).all()
        rows.update((row.id, row) for row in found)
    by_sku, by_ean = defaultdict(list), defaultdict(list)
    malformed = False
    for row in rows.values():
        try:
            source = public_product(row, detail=True)
        except (ValueError, TypeError):
            malformed = True
            continue
        by_sku[row.supplier_sku].append(source)
        for ean in verified_barcodes(source.eans):
            by_ean[ean].append(source)
    from inventory_hub.services.catalog import normalize_stored_groups
    await normalize_stored_groups(db, [source for sources in by_sku.values() for source in sources])
    return by_sku, by_ean, malformed


async def _feed_verified(db, session, cfg):
    feeds = (await db.scalars(select(SupplierFeed).where(
        SupplierFeed.supplier_id == session.supplier_id, SupplierFeed.is_active.is_(True),
        SupplierFeed.code != 'stock', SupplierFeed.feed_type != 'stock'))).all()
    configured = {key for key, source in (cfg.get('feeds', {}).get('sources') or {}).items()
                  if isinstance(source, dict) and key != 'stock' and source.get('type') != 'stock'
                  and _source_configured(source)}
    by_code = {feed.code: feed for feed in feeds}
    expected = configured | set(by_code)
    return bool(expected) and all(key in by_code and by_code[key].last_run_status == FeedRunStatus.completed
               and by_code[key].last_run_id is not None for key in expected)


async def inspect_products(db, session, prefix):
    """Return a read-only projection plus trusted source/product objects."""
    cfg = supplier_config(session.supplier.code)
    verified = await _feed_verified(db, session, cfg)
    by_sku, by_ean, malformed = await _catalog_candidates(db, session)
    verified = verified and not malformed
    candidates, identities = {}, []
    for line in session.lines:
        found = {p.id: p for p in by_sku[(line.supplier_sku or '').strip()]}
        for ean in verified_barcodes((line.ean,)):
            found.update((p.id, p) for p in by_ean[ean])
        candidates[line.id] = list(found.values())
        identities.append(_identity(session, line, prefix))
        if len(found) == 1:
            source = next(iter(found.values()))
            identities.append(_identity(session, line, prefix, source))
            identities.append(RemoteIdentity(0, source.shop_code, barcodes=tuple(source.eans),
                supplier_id=session.supplier_id, supplier_sku=source.code))
    index = await load_identity_index(db, 0, identities, local=True)
    product_ids = list(index.products)
    shops = set((await db.scalars(select(ShopProduct.product_id).where(ShopProduct.product_id.in_(product_ids)))).all()) if product_ids else set()
    overrides = {row.product_id: row.data or {} for row in (await db.scalars(select(ProductEditorOverride).where(
        ProductEditorOverride.product_id.in_(product_ids)))).all()} if product_ids else {}
    stock = dict((await db.execute(select(StockTracking.product_id, StockBalance.qty_on_hand)
        .outerjoin(StockBalance, (StockBalance.product_id == StockTracking.product_id)
                   & (StockBalance.warehouse_id == StockTracking.warehouse_id))
        .where(StockTracking.product_id.in_(product_ids), StockTracking.warehouse_id == session.warehouse_id))).all()) if product_ids else {}
    links = (session.session_data or {}).get('product_drafts') or {}
    items, resolved = [], {}
    invoice_sku_eans = defaultdict(list)
    for line in session.lines:
        eans = set(verified_barcodes((line.ean,)))
        if line.supplier_sku and eans:
            invoice_sku_eans[line.supplier_sku.strip()].append(eans)
    inconsistent_skus = {sku for sku, groups in invoice_sku_eans.items()
                         if len(groups) > 1 and not set.intersection(*groups)
                         and _barcode_owner(index, set().union(*groups)) is None}
    for line in sorted(session.lines, key=lambda item: item.line_number):
        sources = candidates[line.id]
        source = sources[0] if len(sources) == 1 else None
        warnings = []
        resolution = index.resolve(_identity(session, line, prefix, source))
        reasons = list(resolution.reasons)
        candidate_product_ids = set(resolution.candidate_product_ids)
        invoice_eans = set(verified_barcodes((line.ean,)))
        feed_eans = set(verified_barcodes(source.eans)) if source else set()
        conflict = len(sources) > 1 or resolution.status == 'conflict'
        if (line.supplier_sku or '').strip() in inconsistent_skus:
            conflict = True
            reasons.append('invoice_sku_ean_conflict')
            warnings.append('Rovnaký dodávateľský kód má na faktúre odlišné EAN. Skontrolujte zhodu.')
        if len(sources) > 1:
            reasons.append('catalog_candidates_ambiguous')
            warnings.append('Viac produktov vo feede zodpovedá kódu alebo EAN. Skontrolujte zhodu.')
        if (invoice_eans and feed_eans and not invoice_eans.intersection(feed_eans)
                and _barcode_owner(index, invoice_eans | feed_eans) is None):
            conflict = True
            reasons.append('invoice_catalog_ean_conflict')
            warnings.append(f"EAN na faktúre ({', '.join(sorted(invoice_eans))}) sa nezhoduje s EAN vo feede ({', '.join(sorted(feed_eans))}).")
        if source:
            feed_resolution = index.resolve(RemoteIdentity(0, source.shop_code, barcodes=tuple(source.eans),
                supplier_id=session.supplier_id, supplier_sku=source.code, expected_product_id=resolution.product_id))
            if feed_resolution.status == 'conflict':
                conflict = True
                reasons.extend(feed_resolution.reasons)
                candidate_product_ids.update(feed_resolution.candidate_product_ids)
            elif resolution.product_id is None and feed_resolution.product_id is not None:
                resolution = feed_resolution
        if conflict and not warnings:
            warnings.append('Kód, EAN alebo uložené priradenie označujú odlišné produkty.')
        product = index.products.get(resolution.product_id) if not conflict else None
        if conflict:
            status = 'ambiguous'
        elif product:
            status = 'basic_card' if is_basic_card(product, has_shop=product.id in shops,
                import_fields=overrides.get(product.id, {}).get('import_fields')) else 'matched'
        elif source:
            status = 'feed_only'
        else:
            status = 'not_found' if verified else 'feed_unverified'
        code_available = bool((line.supplier_sku or '').strip() or source)
        can_prepare = status in ('basic_card', 'feed_only', 'not_found', 'feed_unverified') and code_available
        if not code_available and status != 'matched':
            warnings.append('Chýba dodávateľský kód. Doplňte identifikáciu položky pred založením karty.')
        if not verified:
            warnings.append('Feed sa nepodarilo overiť. Na prípravu možno použiť dostupné údaje z faktúry alebo posledného feedu.')
        eans = verified_barcodes((line.ean,)) or (tuple(sorted(index.product_barcodes.get(product.id, set()))) if product else ()) or tuple(source.eans if source else [])
        items.append({'line_id': line.id, 'status': status, 'product_id': product.id if product else None,
            'product_code': product.sku if product else (source.shop_code if source else None),
            'product_name': product.name if product else (source.name if source else None),
            'catalog_product_id': source.id if source else None, 'display_ean': '/'.join(eans),
            'stock_confirmed': bool(product and product.id in stock),
            'own_stock_qty': float(stock[product.id] or 0) if product and product.id in stock else None,
            'can_prepare': can_prepare, 'draft_id': links.get(str(line.id)), 'warnings': warnings,
            'conflict_reasons': sorted(set(reasons)) if conflict else [],
            'candidate_products': [{'id': id, 'code': index.products[id].sku, 'name': index.products[id].name}
                                   for id in sorted(candidate_product_ids) if id in index.products] if conflict else [],
            'candidate_sources': [{'id': p.id, 'code': p.code, 'name': p.name, 'eans': p.eans} for p in sources] if conflict else []})
        resolved[line.id] = (product, source, resolution)
    counts = Counter(item['status'] for item in items)
    return {'session_id': session.id, 'invoice_no': session.invoice_number, 'warehouse_id': session.warehouse_id,
            'feed_verified': verified, 'feed_warning': None if verified else 'Feed sa nepodarilo overiť.',
            'items': items, 'counts': {status: counts[status] for status in STATUSES}}, resolved


def _preparation_key(line, product, source):
    if product:
        return ('product', product.id)
    if source:
        return ('catalog', source.id)
    return ('supplier_sku', (line.supplier_sku or '').strip())


def _invoice_source(session, line, prefix, product, source):
    if source:
        result = source.model_copy(deep=True)
        result.id = line.id
        return result
    sku = (line.supplier_sku or '').strip()
    code = product.sku if product else canonical_supplier_sku(prefix, sku)
    purchase = line.unit_price
    if purchase is not None and (not purchase.is_finite() or purchase < 0):
        purchase = None
    # Draft merchandising prices have two decimals; receipt/FIFO retains the
    # untouched four-decimal invoice unit price on each individual line.
    purchase = purchase.quantize(Decimal('.01'), rounding=ROUND_HALF_UP) if purchase is not None else None
    return CatalogProduct(id=line.id, supplier=session.supplier.code, feed_key='invoice', code=sku,
        shop_code=code, name=(line.description or (product.name if product else sku))[:500],
        eans=list(verified_barcodes((line.ean,))),
        prices=CatalogPrices(currency=session.invoice_currency, purchase_net=purchase,
            vat_percent=line.vat_rate),
        raw_fields={'source': 'invoice', 'invoice_no': session.invoice_number, 'supplier_code': sku},
        source_hash=sha256(f'invoice:{session.id}:{line.id}:{sku}'.encode()).hexdigest(),
        warnings=['invoice_only_source'])


async def prepare_products(db, session, prefix, line_ids):
    """Caller holds the session lock; repeating a selection resumes its draft."""
    from inventory_hub.services import product_import
    if session.status not in (*ACTIVE, ReceivingStatus.completed):
        raise CatalogError('receiving_not_available', 'Táto príjemka už nie je dostupná na prípravu produktov.', 409)
    selected = set(line_ids)
    lines = {line.id: line for line in session.lines}
    if not selected or not selected.issubset(lines):
        raise CatalogError('receiving_lines_missing', 'Vyberte položky z tejto príjemky.', 422)
    report, resolved = await inspect_products(db, session, prefix)
    items = {item['line_id']: item for item in report['items']}
    links = dict((session.session_data or {}).get('product_drafts') or {})
    draft_ids = {links[str(id)] for id in selected if str(id) in links}
    if draft_ids:
        if len(draft_ids) != 1 or any(str(id) not in links for id in selected):
            raise CatalogError('receiving_drafts_mixed', 'Časť výberu už má rozpracovanú prípravu. Otvorte ju samostatne a potom založte ostatné karty.', 409)
        draft_id = next(iter(draft_ids))
        draft = await db.get(ProductImportDraft, draft_id)
        if draft and draft.supplier == session.supplier.code and (draft.document.get('receiving_origin') or {}).get('session_id') == session.id:
            return {'draft_id': draft.id, 'created': False, 'source_lookup': draft.document.get('source_lookup')}
        raise CatalogError('receiving_draft_missing', 'Uloženú prípravu sa nepodarilo načítať.', 409)
    invalid = [id for id in selected if not items[id]['can_prepare']]
    if invalid:
        raise CatalogError('receiving_product_not_ready', 'Vybrané položky sú už založené alebo majú nejasnú identifikáciu. Vyberte iba karty na založenie alebo doplnenie.', 409)
    groups = defaultdict(list)
    for line in session.lines:
        product, source, _ = resolved[line.id]
        if items[line.id]['status'] != 'ambiguous':
            groups[_preparation_key(line, product, source)].append(line)
    selected_keys = {_preparation_key(lines[id], *resolved[id][:2]) for id in selected}
    sources, receiving_rows = [], {}
    for key in sorted(selected_keys, key=str):
        family = sorted(groups[key], key=lambda line: line.id)
        representative = family[0]
        product, source, _ = resolved[representative.id]
        frozen = _invoice_source(session, representative, prefix, product, source)
        if not frozen.eans:
            frozen.eans = list(verified_barcodes([line.ean for line in family]))
        sources.append(frozen)
        receiving_rows[frozen.id] = {'line_ids': [line.id for line in family],
            'catalog_product_id': source.id if source else None, 'existing_product_id': product.id if product else None}
    cfg = supplier_config(session.supplier.code)
    active_shops = list((await db.scalars(select(Shop.code).where(Shop.is_active.is_(True),
        Shop.code.in_(['biketrek', 'xtrek'])).order_by(Shop.code))).all())
    preferred = cfg.get('default_shop') or 'biketrek'
    shop = preferred if preferred in active_shops else (active_shops[0] if active_shops else None)
    if not shop:
        raise CatalogError('shop_not_found', 'Pre prípravu produktov musí byť v Hube nastavený aspoň jeden e-shop.', 422)
    result = await product_import.create(db, DraftCreate(request_id=uuid4(), supplier=session.supplier.code,
        feed_key='receiving', product_ids=[source.id for source in sources], shop=shop,
        options=ShopImportOptions(currency=session.invoice_currency)), sources=sources,
        receiving_origin={'kind': 'receiving', 'supplier': session.supplier.code,
            'session_id': session.id, 'invoice_no': session.invoice_number}, receiving_rows=receiving_rows)
    for row in receiving_rows.values():
        for id in row['line_ids']:
            links[str(id)] = result['id']
    session.session_data = {**(session.session_data or {}), 'product_drafts': links}
    await db.flush()
    return {'draft_id': result['id'], 'created': True, 'source_lookup': result.get('source_lookup')}


async def reconcile_products(db, session, prefix):
    """Caller holds session lock. Safe rows continue even if other rows conflict."""
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
    report, resolved = await inspect_products(db, session, prefix)
    if session.status in ACTIVE:
        for line in session.lines:
            product, source, resolution = resolved[line.id]
            if product is None:
                continue
            line.product_id, line.product = product.id, product
            line.match_method = resolution.matched_by or 'supplier_catalog'
            if source:
                line.matched_supplier_product_id = source.id
        await db.flush()
    return report


async def bind_imported_products(db, draft, document):
    """Register original invoice aliases, never take receipt/line write locks.

    The save transaction already owns IDENTITY_WRITE_LOCK. Receipt reconciliation
    is deliberately separate so finalization's session -> identity order remains
    unchanged. Prices, quantities, source EAN and fingerprints are never changed.
    """
    origin = document.get('receiving_origin') or {}
    if not origin:
        return
    session = await db.get(ReceivingSession, origin['session_id'])
    if session is None or session.invoice_number != origin.get('invoice_no'):
        raise CatalogError('receiving_origin_changed', 'Pôvodnú príjemku sa nepodarilo overiť.', 409)
    from inventory_hub.db_models import Supplier
    supplier = await db.get(Supplier, session.supplier_id)
    if supplier is None or supplier.code != draft.supplier or origin.get('supplier') != draft.supplier:
        raise CatalogError('receiving_origin_changed', 'Dodávateľ príjemky sa nezhoduje s prípravou.', 409)
    prefix = get_supplier_prefix(supplier_config(draft.supplier))
    receiving_rows = document.get('receiving_rows') or {}
    invoice_lines = {line.id: line for line in (await db.scalars(select(ReceivingLine).where(
        ReceivingLine.session_id == session.id))).all()}
    service = ProductIdentifierService(db)
    for row in document.get('rows', []):
        product_id = row.get('hub_product_id')
        context = receiving_rows.get(str(row['id']), receiving_rows.get(row['id'], {}))
        if not product_id:
            continue
        for line_id in context.get('line_ids', []):
            line = invoice_lines.get(line_id)
            if line is None:
                raise CatalogError('receiving_origin_changed', 'Položka pôvodnej príjemky už neexistuje.', 409)
            identity = _identity(session, line, prefix, expected=product_id)
            index = await load_identity_index(db, 0, [identity], local=True)
            resolution = index.resolve(identity)
            if resolution.status == 'conflict' or (line.product_id is not None and line.product_id != product_id):
                raise CatalogError('receiving_identity_conflict', 'Uložená karta sa nezhoduje s identifikáciou položky faktúry.', 409)
            sku = (line.supplier_sku or '').strip()
            if sku and not await db.scalar(select(ProductIdentifier.id).where(
                    ProductIdentifier.product_id == product_id, ProductIdentifier.identifier_type == IdentifierType.supplier_sku,
                    ProductIdentifier.supplier_id == supplier.id, ProductIdentifier.value == sku)):
                await service.add_identifier(product_id, sku, IdentifierType.supplier_sku, supplier_id=supplier.id)
