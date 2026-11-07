"""Opt-in preservation of independently proven keeper invoice/paused receipts.

This is a read-only proof inside the consolidation transaction. It never binds
a receiving line or changes a document, quantity, price, status or stock. Full
server-side discovery includes NULL-product lines and saved draft identities.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import fcntl
import hashlib
import json
import re
from types import SimpleNamespace

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import selectinload

from inventory_hub import config_io
from inventory_hub.db_models import SupplierProduct
from inventory_hub.db_models_ext import ReceivingLine, ReceivingSession
from inventory_hub.product_import_models import ProductImportDraft
from inventory_hub.services import receiving_products
from inventory_hub.services.product_identity import RemoteIdentity, barcode_keys, load_identity_index
from inventory_hub.services.product_links import LinkError, digest
from inventory_hub.supplier_prefix import SupplierPrefixError, canonical_supplier_sku, get_supplier_prefix


FLAG = 'preserve_keeper_invoice_receiving'
MAX_SESSIONS = 1000
MAX_LINES = 20000
MAX_INVOICE_CLAIMS = 2000
# SHARE blocks INSERT/UPDATE/DELETE (including new NULL-product phantoms), not
# readers. NOWAIT is essential: receipt writers lock session before identity.
# A receipt may already own a row lock while waiting for our identity lock.
INPUT_TABLES = (
    'products', 'product_identifiers', 'shop_products', 'product_supply_sources',
    'product_sku_identities', 'product_editor_overrides', 'stock_balances',
    'stock_tracking', 'receiving_sessions', 'receiving_lines', 'scan_events',
    'receiving_scan_requests', 'uploaded_invoices', 'uploaded_invoice_lines',
    'supplier_products', 'supplier_feeds', 'suppliers', 'product_import_drafts',
)
PRODUCT_KEYS = {'product_id', 'hub_id', 'hub_product_id', 'expected_hub_product_id',
                'expected_product_id', 'existing_product_id', 'keeper_product_id',
                'canonical_product_id', 'product_ids', 'retired_product_ids'}


async def lock_inputs(db):
    try:
        await db.execute(text('LOCK TABLE ' + ','.join(INPUT_TABLES) + ' IN SHARE MODE NOWAIT'))
    except DBAPIError as error:
        if getattr(error.orig, 'sqlstate', None) == '55P03':
            raise LinkError('consolidation_keeper_dependency_writer_busy') from None
        raise


class Configurations:
    """Read-only lease on the same registry lock used by supplier config writes.

    Normal load_supplier may discover/write registry entries even with config
    write-back disabled. Do not call it while holding this shared lease.
    Missing/malformed/unreserved configuration is a hold, never repaired here.
    """
    def __init__(self):
        self.stream = None
        self.entries = None
        self.cache = {}
        self.hashes = {}

    def close(self):
        if self.stream is not None:
            fcntl.flock(self.stream, fcntl.LOCK_UN)
            self.stream.close()
            self.stream = None

    def get(self, supplier):
        if supplier in self.cache:
            return self.cache[supplier]
        try:
            root = config_io.DATA_ROOT / 'suppliers'
            if self.stream is None:
                path = root / '.product-prefixes.lock'
                if path.is_symlink():
                    raise ValueError()
                self.stream = path.open('r')
                try:
                    fcntl.flock(self.stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    self.stream.close()
                    self.stream = None
                    raise LinkError('consolidation_keeper_configuration_busy') from None
                registry = json.loads((root / '.product-prefixes.json').read_text())
                if registry.get('version') != 1 or not isinstance(registry.get('suppliers'), dict):
                    raise ValueError()
                for entry in registry['suppliers'].values():
                    if (not isinstance(entry, dict) or not isinstance(entry.get('prefix'), str)
                            or not isinstance(entry.get('locked'), bool)
                            or not isinstance(entry.get('retired_prefixes', []), list)):
                        raise ValueError()
                    if any(not isinstance(value, str) or not value for value in entry.get('retired_prefixes', [])):
                        raise ValueError()
                    for prefix in [entry['prefix'], *entry.get('retired_prefixes', [])]:
                        get_supplier_prefix({'product_code_prefix': prefix})
                self.entries = registry['suppliers']
            path = config_io.supplier_path(supplier)
            if not path.is_file() or path.is_symlink() or supplier not in self.entries:
                raise ValueError()
            raw = path.read_bytes()
            cfg = json.loads(raw)
            if not isinstance(cfg, dict):
                raise ValueError()
            cfg = config_io._norm_supplier(cfg)
            config_io._validate_prefix(supplier, get_supplier_prefix(cfg), self.entries)
            self.cache[supplier] = cfg
            self.hashes[supplier] = hashlib.sha256(raw).hexdigest()
            return cfg
        except LinkError:
            raise
        except (OSError, ValueError, TypeError, KeyError, AttributeError, SupplierPrefixError):
            raise LinkError('consolidation_keeper_configuration_unverified') from None

    def verify(self):
        for supplier, before in self.hashes.items():
            try:
                after = hashlib.sha256(config_io.supplier_path(supplier).read_bytes()).hexdigest()
            except OSError:
                raise LinkError('consolidation_keeper_configuration_changed') from None
            if after != before:
                raise LinkError('consolidation_keeper_configuration_changed')


@contextmanager
def configurations():
    inputs = Configurations()
    try:
        yield inputs
    finally:
        inputs.close()


def _product_mentions(value, ids):
    def selected(item):
        if isinstance(item, list):
            return any(selected(v) for v in item)
        return type(item) is int and item in ids or isinstance(item, str) and item in {str(id) for id in ids}
    if isinstance(value, dict):
        return any((key in PRODUCT_KEYS and selected(item)) or _product_mentions(item, ids)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_product_mentions(item, ids) for item in value)
    return False


def _string_mentions(value, tokens):
    if isinstance(value, dict):
        return any(_string_mentions(item, tokens) for item in value.values())
    if isinstance(value, list):
        return any(_string_mentions(item, tokens) for item in value)
    return isinstance(value, str) and value.casefold() in tokens


async def _source_owner(db, source_id, supplier_id, prefix):
    source = await db.get(SupplierProduct, source_id, populate_existing=True)
    if source is None or source.supplier_id != supplier_id:
        return None
    try:
        code = canonical_supplier_sku(prefix, source.supplier_sku)
    except (SupplierPrefixError, TypeError, AttributeError):
        return None
    identity = RemoteIdentity(0, code, barcodes=(source.ean or '',),
        supplier_id=supplier_id, supplier_sku=source.supplier_sku)
    resolution = (await load_identity_index(db, 0, [identity], local=True)).resolve(identity)
    return resolution.product_id if resolution.status != 'conflict' else None


async def _summary(db, table, condition, values):
    # table/condition are module constants, never client input. Only full hashes
    # and IDs leave this inspection; invoice/customer content stays in the DB.
    return dict((await db.execute(text(f'''SELECT count(*) AS count,
        md5(coalesce(string_agg(to_jsonb(t)::text,E'\\n' ORDER BY to_jsonb(t)::text),'')) AS hash
        FROM {table} t WHERE {condition}'''), values)).mappings().one())


def _mentions_line(line, tokens, gtins):
    sku = (line.supplier_sku or '').strip().casefold()
    raw = (line.ean or '').strip().casefold()
    return bool(sku and any(code.endswith(sku) for code in tokens)
                or raw and raw in tokens or barcode_keys((line.ean,)) & gtins)


async def inspect(db, raw, local, configurations):
    """All input tables and the lazy config lease remain held until commit.

    Every receipt is inspected so an unbound row cannot hide behind a missing
    FK, supplier feed alias, saved draft or an archived/other-status session.
    Scope limits or unverifiable inputs fail closed, without partial discovery.
    """
    ids = {raw['keeper_product_id'], *raw['retired_product_ids']}
    keeper, retired = raw['keeper_product_id'], set(raw['retired_product_ids'])
    counts = [await db.scalar(select(func.count()).select_from(model))
              for model in (ReceivingSession, ReceivingLine)]
    if counts[0] > MAX_SESSIONS or counts[1] > MAX_LINES:
        raise LinkError('consolidation_keeper_dependency_scope_too_large')
    claims = list((await db.execute(text('''SELECT * FROM uploaded_invoice_lines
        WHERE matched_product_id=ANY(CAST(:ids AS bigint[])) ORDER BY id LIMIT :limit'''),
        {'ids': sorted(ids), 'limit': MAX_INVOICE_CLAIMS + 1})).mappings())
    if len(claims) > MAX_INVOICE_CLAIMS:
        raise LinkError('consolidation_keeper_dependency_scope_too_large')
    sessions = list(await db.scalars(select(ReceivingSession).options(
        selectinload(ReceivingSession.lines).selectinload(ReceivingLine.product),
        selectinload(ReceivingSession.supplier), selectinload(ReceivingSession.warehouse))
        .order_by(ReceivingSession.id).execution_options(populate_existing=True)))
    tokens = {item['sku'].casefold() for item in local['products']}
    tokens.update(item['value'].casefold() for item in local['identifiers'] if item['value'])
    retired_tokens = {item['sku'].casefold() for item in local['products'] if item['id'] in retired}
    retired_tokens.update(item['value'].casefold() for item in local['identifiers']
                          if item['product_id'] in retired and item['value'])
    gtins = set().union(*(barcode_keys(item['identity']['eans']) for item in local['products']))
    blockers, proofs, related_sessions, source_ids, draft_ids = [], [], set(), set(), set()
    def block(code, **details):
        item = {'code': code, **details}
        if item not in blockers:
            blockers.append(item)
    session_by_id = {session.id: session for session in sessions}
    for session in sessions:
        cfg = configurations.get(session.supplier.code)
        prefix = get_supplier_prefix(cfg)
        data = session.session_data if session.session_data is not None else {}
        if not isinstance(data, dict):
            raise LinkError('consolidation_keeper_receiving_draft_unverified')
        links = data.get('product_drafts', {})
        if links is None:
            links = {}
        if not isinstance(links, dict) or any(not isinstance(v, str) for v in links.values()):
            raise LinkError('consolidation_keeper_receiving_draft_unverified')
        drafts = list(await db.scalars(select(ProductImportDraft).where(
            ProductImportDraft.id.in_(set(links.values()))))) if links else []
        if set(links.values()) - {draft.id for draft in drafts}:
            raise LinkError('consolidation_keeper_receiving_draft_unverified')
        pinned = _product_mentions(session.session_data or {}, ids)
        draft_mentions = any(_product_mentions(draft.document, ids) for draft in drafts)
        retired_pin = _product_mentions(session.session_data or {}, retired) or any(
            _product_mentions(draft.document, retired) or _string_mentions(draft.document, retired_tokens)
            for draft in drafts)
        identities = [receiving_products.raw_identity(session.supplier_id, line, prefix) for line in session.lines]
        index = await load_identity_index(db, 0, identities, local=True)
        try:
            report, _ = await receiving_products.inspect_products(db, session, prefix, supplier_cfg=deepcopy(cfg))
        except (ValueError, TypeError, KeyError, AttributeError):
            raise LinkError('consolidation_keeper_receiving_identity_unverified') from None
        enriched = {item['line_id']: item for item in report['items']}
        if set(enriched) != {line.id for line in session.lines}:
            raise LinkError('consolidation_keeper_receiving_identity_unverified')
        if retired_pin:
            related_sessions.add(session.id)
            block('consolidation_keeper_retired_receiving_pin', session_id=session.id)
        related = []
        for line, identity in zip(session.lines, identities):
            resolution = index.resolve(identity)
            item = enriched[line.id]
            owners = {resolution.product_id, item['product_id'], line.product_id,
                      *resolution.candidate_product_ids, *(p['id'] for p in item['candidate_products'])}
            source_owner = await _source_owner(db, line.matched_supplier_product_id, session.supplier_id, prefix) \
                if line.matched_supplier_product_id is not None else None
            if not (owners & ids or source_owner in ids or _mentions_line(line, tokens, gtins)):
                continue
            related.append(line.id)
            related_sessions.add(session.id)
            if (session.status.value != 'paused' or line.product_id not in (None, keeper)
                    or resolution.status == 'conflict' or resolution.product_id != keeper
                    or resolution.matched_by == 'assigned_product'
                    or item['status'] != 'matched' or item['product_id'] != keeper
                    or item['conflict_reasons'] or item['warnings'] or owners & retired
                    or line.matched_supplier_product_id is not None and source_owner != keeper):
                block('consolidation_keeper_receiving_identity_unverified', session_id=session.id, line_id=line.id)
                continue
            proofs.append({'session_id': session.id, 'line_id': line.id,
                'stored_product_id': line.product_id, 'raw_product_id': resolution.product_id,
                'raw_matched_by': resolution.matched_by,
                'enriched_product_id': item['product_id'], 'catalog_product_id': item['catalog_product_id'],
                'draft_id': item['draft_id'], 'supplier_id': session.supplier_id,
                'stored_supplier_product_id': line.matched_supplier_product_id,
                'configuration_hash': configurations.hashes[session.supplier.code]})
            source_ids.update(id for id in (item['catalog_product_id'], line.matched_supplier_product_id) if id is not None)
        if pinned or draft_mentions:
            related_sessions.add(session.id)
            # Frozen pending drafts are not made eligible by this narrow option.
            if draft_mentions or not related:
                block('consolidation_keeper_receiving_draft_unverified', session_id=session.id)
        if session.id in related_sessions and session.status.value != 'paused':
            block('consolidation_keeper_receiving_not_paused', session_id=session.id)
        if session.id in related_sessions:
            draft_ids.update(draft.id for draft in drafts)
            if drafts:
                # A direct receiving link is executable lineage even when a
                # partial/empty draft contains no selected product token. No
                # import intent is exempted by this preservation option.
                block('consolidation_keeper_receiving_draft_unverified', session_id=session.id)
    invoice_ids = sorted({line['invoice_id'] for line in claims})
    headers = list((await db.execute(text('SELECT * FROM uploaded_invoices WHERE id=ANY(CAST(:ids AS bigint[])) ORDER BY id'),
        {'ids': invoice_ids})).mappings()) if invoice_ids else []
    header_by_id = {row['id']: row for row in headers}
    allowed_invoices = []
    for line in claims:
        header = header_by_id.get(line['invoice_id'])
        if (line['matched_product_id'] != keeper or header is None or header['direction'] != 'purchase'
                or header['document_type'] != 'invoice' or header['source_kind'] not in ('supplier', 'upload')
                or not header['is_parsed'] or header['parse_error']
                or not re.fullmatch(r'[a-f0-9]{64}', header['source_hash'] or '')
                or _product_mentions(line['raw_data'], retired) or _product_mentions(header['source_data'], retired)):
            block('consolidation_keeper_invoice_unverified', line_id=line['id'])
            continue
        supplier = next((s.supplier for s in sessions if s.supplier_id == header['supplier_id']), None)
        if supplier is None:
            from inventory_hub.db_models import Supplier
            supplier = await db.get(Supplier, header['supplier_id'])
        if supplier is None:
            block('consolidation_keeper_invoice_unverified', line_id=line['id'])
            continue
        cfg = configurations.get(supplier.code)
        identity = receiving_products.raw_identity(supplier.id, SimpleNamespace(
            supplier_sku=line['supplier_sku'], ean=line['ean'], product_id=line['matched_product_id']), get_supplier_prefix(cfg))
        identity = replace(identity, barcodes=(line['ean'] or '', *(line['barcode_values'] or [])))
        resolution = (await load_identity_index(db, 0, [identity], local=True)).resolve(identity)
        if (resolution.status == 'conflict' or resolution.product_id != keeper
                or resolution.matched_by == 'assigned_product'):
            block('consolidation_keeper_invoice_identity_unverified', line_id=line['id'])
            continue
        source_id = line['matched_supplier_product_id']
        if source_id is not None:
            if await _source_owner(db, source_id, supplier.id, get_supplier_prefix(cfg)) != keeper:
                block('consolidation_keeper_invoice_identity_unverified', line_id=line['id'])
                continue
            source_ids.add(source_id)
        session_id = header['receiving_session_id']
        if session_id is not None:
            session = session_by_id.get(session_id)
            if (session is None or session.supplier_id != header['supplier_id']
                    or session.invoice_number != header['invoice_number'] or session.status.value != 'paused'
                    or not any(proof['session_id'] == session_id for proof in proofs)):
                block('consolidation_keeper_invoice_receiving_unverified', line_id=line['id'])
                continue
            related_sessions.add(session_id)
        allowed_invoices.append(line['id'])
    # Preserve all columns of the relevant complete documents/sessions, not
    # merely the FK, a displayed Decimal conversion, or a quantity subtotal.
    sessions_ids = sorted(related_sessions)
    facts = {}
    for table, condition, selected in (
        ('uploaded_invoices', 't.id=ANY(CAST(:ids AS bigint[]))', invoice_ids),
        ('uploaded_invoice_lines', 't.invoice_id=ANY(CAST(:ids AS bigint[]))', invoice_ids),
        ('receiving_sessions', 't.id=ANY(CAST(:ids AS bigint[]))', sessions_ids),
        ('receiving_lines', 't.session_id=ANY(CAST(:ids AS bigint[]))', sessions_ids),
        ('scan_events', 't.receiving_session_id=ANY(CAST(:ids AS bigint[]))', sessions_ids),
        ('receiving_scan_requests', 't.session_id=ANY(CAST(:ids AS bigint[]))', sessions_ids),
        ('supplier_products', 't.id=ANY(CAST(:ids AS bigint[]))', sorted(source_ids)),
        ('supplier_feeds', 't.id IN (SELECT source_feed_id FROM supplier_products WHERE id=ANY(CAST(:ids AS bigint[])))', sorted(source_ids)),
    ):
        facts[table] = await _summary(db, table, condition, {'ids': selected})
    facts['product_import_drafts'] = await _summary(db, 'product_import_drafts',
        't.id=ANY(CAST(:ids AS text[]))', {'ids': sorted(draft_ids)})
    configurations.verify()
    return {'version': 1, 'blockers': sorted(blockers, key=lambda item: json.dumps(item, sort_keys=True)),
        'allowed_invoice_line_ids': [] if blockers else sorted(allowed_invoices),
        'allowed_receiving_line_ids': [] if blockers else sorted(proof['line_id'] for proof in proofs),
        'invoice_ids': invoice_ids, 'session_ids': sessions_ids, 'facts': facts,
        'proofs': sorted(proofs, key=lambda item: (item['session_id'], item['line_id'])),
        'configuration_hashes': dict(sorted(configurations.hashes.items())),
        'scope_counts': {'sessions_scanned': counts[0], 'receiving_lines_scanned': counts[1]},
        'preserved_rows_hash': digest(facts)}
