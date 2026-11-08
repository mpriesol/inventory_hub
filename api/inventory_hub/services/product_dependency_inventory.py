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
    'atomer_catalog_campaigns': ('id',), 'atomer_progress_runs': ('id',), 'atomer_progress_items': ('run_id', 'code'),
    'atomer_review_work': ('kind', 'key'), 'shop_products': ('id',), 'shops': ('id',)}
TEXT_KEYS = {('product_import_drafts', 'id'), ('ai_content_jobs', 'id'),
    ('atomer_catalog_jobs', 'campaign'), ('atomer_catalog_jobs', 'family_key'),
    ('product_consolidations', 'id'), ('atomer_catalog_campaigns', 'id'),
    ('atomer_progress_items', 'code'), ('atomer_review_work', 'kind'), ('atomer_review_work', 'key')}


# Only these two shared headers have a closed interpreter input contract. The
# original complete row still supplies its hash/bytes to every logical edge.
# In particular, selection is kept whole: barcode_corrections carries identity.
HEADER_PROJECTIONS = {
    'atomer-campaign-v1': ('atomer_campaign', 'atomer_catalog_campaigns',
        ('id', 'state', 'checked_run_id', 'source_sha', 'admin_sha', 'selection')),
    'atomer-current-run-v1': ('atomer_current_run', 'atomer_progress_runs',
        ('id', 'source_sha256', 'admin_export_sha256')),
}


def header_fields(profile, role, table):
    if profile is None:
        return None
    configured = HEADER_PROJECTIONS.get(profile) if isinstance(profile, str) else None
    if configured is None or configured[:2] != (role, table):
        raise s.Unverified('unknown_linked_header_projection', table)
    return configured[2]


def project_headers(rows, fields, *, cached=False):
    result = []
    for row in rows:
        document = row.get('document')
        if (not isinstance(document, dict) or not set(fields) <= document.keys()
                or cached and set(document) != set(fields)):
            raise s.Unverified('linked_header_projection_incomplete')
        result.append({**row, 'document': {key: document[key] for key in fields}})
    return result


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
    linked_reuse: object | None = None
    retained: int = 0
    reused_linked_rows: int = 0
    reused_linked_bytes: int = 0

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

    def retain(self, size, *, record_peak=True):
        """Keep optimization storage inside the existing payload allowance."""
        self.retained = size
        self.account_reuse(record_peak=record_peak)

    def account_reuse(self, *, record_peak=True):
        # Both caches share the raw-operation allowance. Refresh the other's
        # headroom after every put, before it can allocate another result.
        if self.reuse is not None:
            self.reuse.reserve(self.retained + (self.linked_reuse.bytes if self.linked_reuse else 0),
                               self.max_payload)
        if self.linked_reuse is not None:
            self.linked_reuse.reserve(self.retained + (self.reuse.bytes if self.reuse else 0),
                                      self.max_payload)
        cached = sum(memo.bytes for memo in (self.reuse, self.linked_reuse) if memo is not None)
        if record_peak:
            self.peak_payload = max(self.peak_payload, self.retained + cached)


async def statistics(db, table, condition, params):
    if table not in KEYS:
        raise s.Unverified('unknown_inventory_table')
    row = (await db.execute(text(f'''SELECT count(*) AS count,
        coalesce(sum(octet_length(to_jsonb(t)::text)),0) AS bytes,
        coalesce(max(octet_length(to_jsonb(t)::text)),0) AS largest
        FROM {table} t WHERE {condition}'''), params)).mappings().one()
    return {key: int(row[key]) for key in ('count', 'bytes', 'largest')}


async def selection_head(db, table, condition, params, *, budget, page_rows,
                         page_bytes, retained, max_linked_rows):
    """Complete selection facts and a bounded first page in one round trip.

    Facts cover every matched row, even when no body is admissible. Reserve
    the largest possible first page before decoding it; the caller then uses
    the same whole-closure admission and pagination checks as an ordinary
    metadata-only read. Both branches run in the caller's existing snapshot.
    """
    if table not in KEYS:
        raise s.Unverified('unknown_inventory_table')
    if page_rows < 1 or page_bytes < 1:
        raise s.Unverified('invalid_inventory_page_budget')
    available = budget.max_payload - retained
    if available < 0:
        raise s.Unverified('database_payload_limit', table)
    reserve = min(max(budget.max_row, page_bytes), available,
                  max(0, budget.max_work - budget.bytes))
    cached = sum(memo.bytes for memo in (budget.reuse, budget.linked_reuse) if memo is not None)
    if retained + reserve + cached > budget.max_payload:
        # A speculative prefix must not evict evidence that the exact body
        # would leave cached. Otherwise a tiny/empty miss can force later
        # re-reads and change admission near the work-byte limit.
        budget.check_time()
        return await statistics(db, table, condition, params), None
    # This is admission headroom, not an observed body size. Cache allowances
    # are enforced now; record the actual decoded prefix immediately below.
    budget.retain(retained + reserve, record_peak=False)
    budget.check_time()
    columns = KEYS[table]
    order = [f't."{key}"' + (' COLLATE "C"' if (table, key) in TEXT_KEYS else '') for key in columns]
    key_sql = 'jsonb_build_array(' + ','.join(f't."{name}"' for name in columns) + ')'
    order_sql = ','.join(order)
    values = {**params, 'inventory_page_rows': page_rows, 'inventory_page_bytes': page_bytes,
        'inventory_max_row': budget.max_row, 'inventory_remaining_work': budget.max_work - budget.bytes,
        'inventory_remaining_payload': available, 'inventory_max_linked_rows': max_linked_rows}
    rows = (await db.execute(text(f'''WITH facts AS MATERIALIZED (
        SELECT count(*) AS count,coalesce(sum(octet_length(to_jsonb(t)::text)),0) AS bytes,
            coalesce(max(octet_length(to_jsonb(t)::text)),0) AS largest
        FROM {table} t WHERE {condition}
        ) SELECT facts.count AS selection_count,facts.bytes AS selection_bytes,
            facts.largest AS selection_largest,body.key,body.hash,body.bytes,body.document
        FROM facts LEFT JOIN LATERAL (
            WITH selected AS (
                SELECT {key_sql} AS key,to_jsonb(t) AS document,
                    octet_length(to_jsonb(t)::text) AS bytes,
                    encode(sha256(convert_to(to_jsonb(t)::text,'UTF8')),'hex') AS hash,
                    row_number() OVER(ORDER BY {order_sql}) AS position
                FROM {table} t WHERE ({condition}) AND facts.count>0
                    AND facts.count<=:inventory_max_linked_rows
                    AND facts.bytes<=:inventory_remaining_payload
                    AND facts.largest<=:inventory_max_row
                ORDER BY {order_sql} LIMIT :inventory_page_rows
            ), sized AS (SELECT *,sum(bytes) OVER(ORDER BY position) AS total_bytes FROM selected)
            SELECT key,hash,bytes,position,CASE WHEN bytes<=:inventory_max_row
                AND total_bytes<=:inventory_remaining_work
                AND total_bytes<=:inventory_remaining_payload
                AND (total_bytes<=:inventory_page_bytes OR position=1)
                THEN document ELSE NULL END AS document
            FROM sized
        ) body ON true ORDER BY body.position'''), values)).mappings().all()
    if not rows:
        raise s.Unverified('database_scope_changed_during_read', table)
    decoded = sum(int(row['bytes']) for row in rows if row['document'] is not None)
    if decoded > reserve:
        raise s.Unverified('database_payload_limit', table)
    budget.retain(retained + decoded)
    budget.check_time()
    expected = {name: int(rows[0]['selection_' + name]) for name in ('count', 'bytes', 'largest')}
    result = []
    for row in rows:
        if any(int(row['selection_' + name]) != expected[name] for name in expected):
            raise s.Unverified('database_scope_changed_during_read', table)
        if row['key'] is not None:
            result.append({name: row[name] for name in ('key', 'hash', 'bytes', 'document')})
    return expected, result


async def pages(db, table, condition, params, *, budget, page_rows, page_bytes,
                manifest, expected, payload_retained=0, initial=None):
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
        if initial is not None:
            rows, initial = initial, None
        else:
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
    """One complete operation, with bounded immutable inputs from this scan."""
    def __init__(self, db, *, budget, page_rows, page_bytes, retained, max_linked_rows):
        self.db, self.budget = db, budget
        self.page_rows, self.page_bytes = page_rows, page_bytes
        self.retained = retained
        self.budget.retain(retained)
        self.max_linked_rows = max_linked_rows
        self.root = Manifest('linked-closure')
        self.edges = self.bytes = 0

    async def rows(self, role, table, condition, params, *, header_projection=None):
        if table not in KEYS:
            raise s.Unverified('unknown_inventory_table')
        fields = header_fields(header_projection, role, table)
        memo = self.budget.linked_reuse
        key = (table, condition, s.digest(params))
        if fields is not None:
            # A full-row caller can never receive a projected cached body.
            key += (header_projection,)
        # These facts are retained only with a complete successful selection,
        # inside this inspection's RR snapshot or apply's complete table fences.
        expected = memo.metadata(key) if memo is not None else None
        initial = None
        if expected is None:
            expected, initial = await selection_head(self.db, table, condition, params,
                budget=self.budget, page_rows=self.page_rows, page_bytes=self.page_bytes,
                retained=self.retained, max_linked_rows=self.max_linked_rows)
        self.budget.check_time()
        if expected['count'] > self.max_linked_rows:
            raise s.Unverified('linked_identity_scope_limit', role)
        if self.retained + expected['bytes'] > self.budget.max_payload:
            raise s.Unverified('database_payload_limit', role)
        # Evict reusable results before materializing this complete closure.
        # A cache must never make a previously admissible operation too large.
        self.budget.retain(self.retained + expected['bytes'])
        if expected['largest'] > self.budget.max_row:
            raise s.Unverified('database_document_limit', table)
        root = Manifest(['linked', role, table])
        # Reserve the entire raw closure before decoding. Eviction can turn a
        # hit into a miss; no serialized entry is held outside that allowance.
        result = memo.get(key) if memo is not None else None
        if result is not None:
            if fields is not None:
                result = project_headers(result, fields, cached=True)
            for row in result:
                self.budget.check_time()
                root.row(row)
            observed = root.result()
            if (observed['count'] != expected['count'] or observed['bytes'] != expected['bytes']
                    or max((row['bytes'] for row in result), default=0) != expected['largest']):
                raise s.Unverified('database_scope_changed_during_read', table)
            self.retained += observed['bytes']
            self.budget.reused_linked_rows += observed['count']
            self.budget.reused_linked_bytes += observed['bytes']
        else:
            result = []
            async for page in pages(self.db, table, condition, params, budget=self.budget,
                    page_rows=self.page_rows, page_bytes=self.page_bytes, manifest=root, expected=expected,
                    payload_retained=self.retained, initial=initial):
                result.extend(page)
                size = sum(row['bytes'] for row in page)
                self.retained += size
            if fields is not None:
                result = project_headers(result, fields)
            # Only a complete selection may enter the per-inspection cache.
            # Fresh JSON decoding preserves the caller's mutation isolation.
            if memo is not None:
                memo.put(key, result, metadata=expected)
                self.budget.account_reuse()
        summary = root.result()
        self.root.add_frame([role, table, s.digest(params), summary])
        self.edges += summary['count']
        self.bytes += summary['bytes']
        return result

    def result(self):
        return {'hash': self.root.result()['hash'], 'edges': self.edges, 'bytes': self.bytes}
