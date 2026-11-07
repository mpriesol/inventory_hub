"""Deterministic, bounded PostgreSQL pages and complete input manifests."""
import hashlib
import json
import time
from dataclasses import dataclass, field

from sqlalchemy import text

from inventory_hub.services import product_dependency_scope as s


KEYS = {'product_import_drafts': ('id',), 'ai_content_jobs': ('id',),
    'atomer_catalog_jobs': ('campaign', 'family_key'), 'product_consolidations': ('id',),
    'suppliers': ('id',), 'supplier_products': ('id',), 'supplier_feeds': ('id',),
    'atomer_catalog_campaigns': ('id',), 'atomer_progress_items': ('run_id', 'code'),
    'atomer_review_work': ('kind', 'key'), 'shop_products': ('id',), 'shops': ('id',)}
TEXT_KEYS = {('product_import_drafts', 'id'), ('ai_content_jobs', 'id'),
    ('atomer_catalog_jobs', 'campaign'), ('atomer_catalog_jobs', 'family_key'),
    ('product_consolidations', 'id'), ('atomer_catalog_campaigns', 'id'),
    ('atomer_progress_items', 'code'), ('atomer_review_work', 'kind'), ('atomer_review_work', 'key')}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


class Manifest:
    """Domain-separated, length-framed records; page boundaries never enter."""
    def __init__(self, domain):
        self._hash = hashlib.sha256()
        self.count = self.bytes = 0
        self.add_frame(['typed-catalog-inventory-v2', domain])

    def add_frame(self, value):
        raw = encoded(value)
        self._hash.update(len(raw).to_bytes(8, 'big'))
        self._hash.update(raw)

    def row(self, row):
        self.add_frame([row['key'], row['bytes'], row['hash']])
        self.count += 1
        self.bytes += row['bytes']

    def result(self):
        return {'count': self.count, 'bytes': self.bytes, 'hash': self._hash.hexdigest()}


@dataclass
class Budget:
    max_row: int
    max_payload: int
    max_work: int
    max_seconds: float | None
    started: float = field(default_factory=time.monotonic)
    rows: int = 0
    bytes: int = 0
    peak_payload: int = 0
    query_keys: set = field(default_factory=set)
    reuse: object | None = None
    retained: int = 0

    def check_time(self):
        if self.max_seconds is not None and time.monotonic() - self.started > self.max_seconds:
            raise TimeoutError('database_inventory_time_limit')

    def consume(self, size, path):
        self.check_time()
        if size > self.max_row:
            raise s.Unverified('database_document_limit', path)
        if self.bytes + size > self.max_work:
            raise s.Unverified('database_total_document_limit', path)
        self.bytes += size
        self.rows += 1

    def retain(self, size):
        """Keep optimization storage inside the existing payload allowance."""
        self.retained = size
        if self.reuse is not None:
            self.reuse.reserve(size, self.max_payload)
        self.account_reuse()

    def account_reuse(self):
        cached = self.reuse.bytes if self.reuse is not None else 0
        self.peak_payload = max(self.peak_payload, self.retained + cached)


async def statistics(db, table, condition, params):
    if table not in KEYS:
        raise s.Unverified('unknown_inventory_table')
    row = (await db.execute(text(f'''SELECT count(*) AS count,
        coalesce(sum(octet_length(to_jsonb(t)::text)),0) AS bytes,
        coalesce(max(octet_length(to_jsonb(t)::text)),0) AS largest
        FROM {table} t WHERE {condition}'''), params)).mappings().one()
    return {key: int(row[key]) for key in ('count', 'bytes', 'largest')}


async def pages(db, table, condition, params, *, budget, page_rows, page_bytes,
                manifest, expected, payload_retained=0):
    """Fetch a byte-bounded PK prefix; never advance over an unfetched row."""
    columns = KEYS[table]
    order = [f't."{key}"' + (' COLLATE "C"' if (table, key) in TEXT_KEYS else '') for key in columns]
    cursor, seen_count, seen_bytes = None, 0, 0
    if page_rows < 1 or page_bytes < 1:
        raise s.Unverified('invalid_inventory_page_budget')
    if expected['largest'] > budget.max_row:
        raise s.Unverified('database_document_limit', table)
    # Count/bytes and these pages share the caller's RR snapshot or apply's
    # complete table fences. Once every counted row was read there is no tail
    # left to query, including the exactly empty selection.
    while seen_count < expected['count']:
        budget.check_time()
        continuation = ''
        values = dict(params)
        if cursor is not None:
            lhs = '(' + ','.join(order) + ')'
            rhs = '(' + ','.join(f':inventory_cursor_{i}' for i in range(len(columns))) + ')'
            continuation = f' AND {lhs}>{rhs}'
            values.update({f'inventory_cursor_{i}': value for i, value in enumerate(cursor)})
        values.update(inventory_page_rows=page_rows, inventory_page_bytes=page_bytes,
            inventory_max_row=budget.max_row, inventory_remaining_work=budget.max_work-budget.bytes,
            inventory_remaining_payload=budget.max_payload-payload_retained)
        key = 'jsonb_build_array(' + ','.join(f't."{name}"' for name in columns) + ')'
        order_sql = ','.join(order)
        rows = (await db.execute(text(f'''WITH selected AS (
            SELECT {key} AS key,to_jsonb(t) AS document,
                octet_length(to_jsonb(t)::text) AS bytes,
                encode(sha256(convert_to(to_jsonb(t)::text,'UTF8')),'hex') AS hash,
                row_number() OVER(ORDER BY {order_sql}) AS position
            FROM {table} t WHERE ({condition}) {continuation}
            ORDER BY {order_sql} LIMIT :inventory_page_rows
            ), sized AS (SELECT *,sum(bytes) OVER(ORDER BY position) AS total_bytes FROM selected)
            SELECT key,hash,bytes,CASE WHEN bytes<=:inventory_max_row
                AND total_bytes<=:inventory_remaining_work
                AND total_bytes<=:inventory_remaining_payload
                AND (total_bytes<=:inventory_page_bytes OR position=1)
                THEN document ELSE NULL END AS document
            FROM sized ORDER BY position'''), values)).mappings().all()
        budget.check_time()
        if not rows:
            break
        chosen = []
        for row in rows:
            if row['document'] is None:
                if chosen:
                    break
                if row['bytes'] > budget.max_row:
                    raise s.Unverified('database_document_limit', table)
                if row['bytes'] > budget.max_work-budget.bytes:
                    raise s.Unverified('database_total_document_limit', table)
                raise s.Unverified('database_payload_limit', table)
            record = dict(row)
            record['bytes'] = int(record['bytes'])
            budget.peak_payload = max(budget.peak_payload, record['bytes'])
            budget.consume(record['bytes'], table)
            manifest.row(record)
            chosen.append(record)
            seen_count += 1
            seen_bytes += record['bytes']
        cursor = chosen[-1]['key']
        yield chosen
    if seen_count != expected['count'] or seen_bytes != expected['bytes']:
        raise s.Unverified('database_scope_changed_during_read', table)


async def documents(db, table, condition, params, *, budget, page_rows, manifest, expected):
    """PK metadata pages, one raw parent at a time, leaving room for its closure.

    A full parent page must not occupy the linked-closure payload allowance:
    otherwise changing only page_rows could change an operation's eligibility.
    Metadata is bounded; the full row is fetched and verified before yielding.
    """
    columns = KEYS[table]
    order = [f't."{key}"' + (' COLLATE "C"' if (table, key) in TEXT_KEYS else '') for key in columns]
    key_sql = 'jsonb_build_array(' + ','.join(f't."{name}"' for name in columns) + ')'
    order_sql = ','.join(order)
    cursor, seen_count, seen_bytes = None, 0, 0
    if page_rows < 1:
        raise s.Unverified('invalid_inventory_page_budget')
    while seen_count < expected['count']:
        budget.check_time()
        continuation, values = '', dict(params)
        if cursor is not None:
            continuation = ' AND (' + ','.join(order) + ')>(' + ','.join(
                f':inventory_cursor_{i}' for i in range(len(columns))) + ')'
            values.update({f'inventory_cursor_{i}': value for i, value in enumerate(cursor)})
        values['inventory_page_rows'] = page_rows
        metadata = (await db.execute(text(f'''SELECT {key_sql} AS key,
            octet_length(to_jsonb(t)::text) AS bytes,
            encode(sha256(convert_to(to_jsonb(t)::text,'UTF8')),'hex') AS hash
            FROM {table} t WHERE ({condition}) {continuation}
            ORDER BY {order_sql} LIMIT :inventory_page_rows'''), values)).mappings().all()
        if not metadata:
            break
        for meta in metadata:
            budget.consume(int(meta['bytes']), table)
            if meta['bytes'] > budget.max_payload:
                raise s.Unverified('database_payload_limit', table)
            budget.retain(int(meta['bytes']))
            keys = {f'inventory_key_{i}': value for i, value in enumerate(meta['key'])}
            exact = ' AND '.join(f't."{name}"=:inventory_key_{i}' for i, name in enumerate(columns))
            row = (await db.execute(text(f'''SELECT {key_sql} AS key,to_jsonb(t) AS document,
                octet_length(to_jsonb(t)::text) AS bytes,
                encode(sha256(convert_to(to_jsonb(t)::text,'UTF8')),'hex') AS hash
                FROM {table} t WHERE ({condition}) AND {exact}'''), {**params, **keys})).mappings().one_or_none()
            budget.check_time()
            if row is None or any(row[name] != meta[name] for name in ('key', 'bytes', 'hash')):
                raise s.Unverified('database_scope_changed_during_read', table)
            record = dict(row)
            record['bytes'] = int(record['bytes'])
            manifest.row(record)
            seen_count += 1
            seen_bytes += record['bytes']
            yield record
        cursor = metadata[-1]['key']
    if seen_count != expected['count'] or seen_bytes != expected['bytes']:
        raise s.Unverified('database_scope_changed_during_read', table)


class Closure:
    """Only one operation's complete linked inputs are retained at a time."""
    def __init__(self, db, *, budget, page_rows, page_bytes, retained, max_linked_rows):
        self.db, self.budget = db, budget
        self.page_rows, self.page_bytes = page_rows, page_bytes
        self.retained = retained
        self.budget.retain(retained)
        self.max_linked_rows = max_linked_rows
        self.root = Manifest('linked-closure')
        self.edges = self.bytes = 0

    async def rows(self, role, table, condition, params):
        expected = await statistics(self.db, table, condition, params)
        self.budget.check_time()
        if expected['count'] > self.max_linked_rows:
            raise s.Unverified('linked_identity_scope_limit', role)
        if self.retained + expected['bytes'] > self.budget.max_payload:
            raise s.Unverified('database_payload_limit', role)
        # Evict reusable results before materializing this complete closure.
        # A cache must never make a previously admissible operation too large.
        self.budget.retain(self.retained + expected['bytes'])
        root = Manifest(['linked', role, table])
        result = []
        async for page in pages(self.db, table, condition, params, budget=self.budget,
                page_rows=self.page_rows, page_bytes=self.page_bytes, manifest=root, expected=expected,
                payload_retained=self.retained):
            result.extend(page)
            size = sum(row['bytes'] for row in page)
            self.retained += size
            self.budget.peak_payload = max(self.budget.peak_payload, self.retained)
        summary = root.result()
        self.root.add_frame([role, table, s.digest(params), summary])
        self.edges += summary['count']
        self.bytes += summary['bytes']
        return result

    def result(self):
        return {'hash': self.root.result()['hash'], 'edges': self.edges, 'bytes': self.bytes}
