"""Exact operator evidence for selected AI jobs; no eligibility or recovery action.

Database rows are returned as SQL JSON text, preserving numeric precision and
all stored fields. Files are observed twice, not advertised as an atomic DB/FS
snapshot. Unknown or unavailable evidence is explicitly incomplete.
"""
from datetime import datetime, timezone
import hashlib
from html import unescape
import json
import os
import re
import stat
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from inventory_hub import config_io
from inventory_hub.services.product_links import LinkError


MAX_JOBS = 8
MAX_DRAFTS = 16
MAX_JOURNALS = 32
MAX_DOCUMENT_BYTES = 16_000_000
MAX_TOTAL_BYTES = 32_000_000
MAX_RESPONSE_BYTES = 40_000_000
MAX_NODES = 400_000
UUID32 = re.compile(r'^[0-9a-f]{32}$')
SHOP = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
SECRET_KEYS = {'config', 'configuration', 'credentials', 'credential', 'authorization', 'auth',
    'password', 'passwd', 'secret', 'secrets', 'apikey', 'token', 'accesstoken',
    'refreshtoken', 'operatortoken', 'bearertoken', 'cookie', 'cookies', 'privatekey',
    'clientsecret', 'upgateslogin', 'apitoken', 'authtoken', 'authkey', 'accesskey', 'jwt'}
URL = re.compile(r'https?://[^\s<>"\']+', re.I)


class Incomplete(ValueError):
    pass


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _secret_key(value):
    key = re.sub(r'[_\s-]', '', value.casefold())
    return key in SECRET_KEYS or key.endswith(('password', 'apikey', 'accesstoken', 'apitoken',
                                               'authtoken', 'clientsecret', 'accesskey', 'privatekey'))


def _document(raw):
    """Never serve credentials accidentally embedded in an operation record."""
    value = json.loads(raw)
    pending, nodes = [(value, 0)], 0
    while pending:
        item, depth = pending.pop(); nodes += 1
        if nodes > MAX_NODES or depth > 64:
            raise Incomplete('document_structure_limit')
        if isinstance(item, dict):
            if any(_secret_key(key) for key in item):
                raise Incomplete('sensitive_document_not_returned')
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            if '-----BEGIN ' in item and 'PRIVATE KEY-----' in item:
                raise Incomplete('sensitive_document_not_returned')
            for match in URL.finditer(item):
                url = urlsplit(unescape(match.group()))
                if url.username is not None or url.password is not None or any(
                        _secret_key(key) for key, _ in parse_qsl(url.query, keep_blank_values=True)):
                    raise Incomplete('sensitive_document_not_returned')
    return value


class Budget:
    def __init__(self):
        self.bytes = 0

    def take(self, size):
        if size > MAX_DOCUMENT_BYTES:
            raise Incomplete('document_byte_limit')
        if self.bytes + size > MAX_TOTAL_BYTES:
            raise Incomplete('total_evidence_byte_limit')
        self.bytes += size


async def _rows(db, table, identifiers, budget):
    # Table names are a closed internal set. Values always remain parameters.
    if table not in {'ai_content_jobs', 'product_import_drafts'}:
        raise ValueError('Unsupported evidence table')
    if not identifiers:
        return {}, []
    parameters = {'ids': sorted(identifiers)}
    stats = (await db.execute(text(f'''SELECT count(*) AS count,
        coalesce(sum(octet_length(to_jsonb(t)::text)),0) AS bytes,
        coalesce(max(octet_length(to_jsonb(t)::text)),0) AS largest
        FROM {table} t WHERE t.id=ANY(CAST(:ids AS text[]))'''), parameters)).mappings().one()
    if stats['largest'] > MAX_DOCUMENT_BYTES:
        raise Incomplete('document_byte_limit')
    if budget.bytes + stats['bytes'] > MAX_TOTAL_BYTES:
        raise Incomplete('total_evidence_byte_limit')
    records = (await db.execute(text(f'''SELECT t.id,to_jsonb(t)::text AS raw
        FROM {table} t WHERE t.id=ANY(CAST(:ids AS text[])) ORDER BY t.id COLLATE "C"'''), parameters)).mappings().all()
    parsed, output = {}, []
    for row in records:
        raw = row['raw'].encode('utf-8'); budget.take(len(raw))
        value = _document(row['raw'])
        parsed[row['id']] = value
        output.append({'id': row['id'], 'bytes': len(raw), 'sha256': _hash(raw), 'raw_json': row['raw']})
    if stats['count'] != len(records) or stats['bytes'] != sum(row['bytes'] for row in output):
        raise Incomplete('database_evidence_changed')
    return parsed, output


def _uuid(value):
    if not isinstance(value, str) or not UUID32.fullmatch(value):
        raise Incomplete('invalid_linked_identifier')
    return value


def _shop(value):
    if not isinstance(value, str) or not SHOP.fullmatch(value):
        raise Incomplete('invalid_linked_shop')
    return value


def _ref(refs, shop, identifier):
    refs.add((_shop(shop), _uuid(identifier)))
    if len(refs) > MAX_JOURNALS:
        raise Incomplete('journal_count_limit')


def _draft_refs(raw, refs):
    document = raw.get('document')
    if not isinstance(document, dict) or not isinstance(document.get('rows'), list):
        raise Incomplete('linked_draft_shape_unverified')
    targets = document.get('publications')
    if targets is not None:
        if not isinstance(targets, dict):
            raise Incomplete('draft_publications_shape_unverified')
        for shop, preview in targets.items():
            if not isinstance(preview, dict):
                raise Incomplete('draft_publications_shape_unverified')
            _ref(refs, shop, preview.get('preview_id'))
        # A multishop aggregate ID is not an importer file. The authoritative
        # target map is exactly publication_previews()'s dispatch contract.
    elif document.get('publication') is not None:
        preview = document['publication']
        if not isinstance(preview, dict):
            raise Incomplete('draft_publication_shape_unverified')
        _ref(refs, raw.get('shop'), preview.get('preview_id'))


def _file(shop, identifier, suffix):
    """Open each directory component without following symlinks; never create."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = None
    try:
        descriptor = os.open(config_io.DATA_ROOT, flags)
        for part in ('shops', shop, 'catalog-imports'):
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor); descriptor = child
        handle = os.open(identifier + suffix, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(handle, 'rb') as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_DOCUMENT_BYTES:
                raise Incomplete('journal_file_type_or_size_unverified')
            raw = source.read(MAX_DOCUMENT_BYTES + 1)
            after = os.fstat(source.fileno())
            if len(raw) != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ino) != (
                    after.st_size, after.st_mtime_ns, after.st_ino):
                raise Incomplete('journal_changed_during_read')
            return raw
    except FileNotFoundError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _journal(shop, identifier, budget=None):
    budget = budget or Budget()
    files, parsed = {}, {}
    for suffix in ('.json', '.result.json', '.events.jsonl'):
        raw = _file(shop, identifier, suffix)
        if raw is None:
            files[suffix] = {'present': False, 'bytes': 0, 'sha256': None}
            continue
        budget.take(len(raw))
        value = raw.decode('utf-8')
        if suffix == '.events.jsonl':
            # An incomplete line is not silently discarded or presented as a
            # complete intent. No partial file body is returned on failure.
            if value and not value.endswith('\n'):
                raise Incomplete('journal_event_incomplete_line')
            for line in value.splitlines():
                _document(line)
        else:
            parsed[suffix] = _document(value)
            if not isinstance(parsed[suffix], dict):
                raise Incomplete('journal_document_shape_unverified')
        files[suffix] = {'present': True, 'bytes': len(raw), 'sha256': _hash(raw), 'raw_utf8': value}
    if '.json' not in parsed:
        raise Incomplete('referenced_journal_missing')
    return {'shop_code': shop, 'journal_id': identifier, 'files': files}, parsed['.json']


def _fingerprint(journal):
    return [(name, value['present'], value['bytes'], value['sha256']) for name, value in journal['files'].items()]


async def inspect(db, identifier, job_ids):
    if (not isinstance(job_ids, list) or not 1 <= len(job_ids) <= MAX_JOBS
            or any(not isinstance(value, str) or not UUID32.fullmatch(value) for value in job_ids)
            or len(set(job_ids)) != len(job_ids)):
        raise LinkError('consolidation_evidence_invalid_job_ids', 422)
    await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
    await db.execute(text("SET LOCAL statement_timeout='10000ms'"))
    row = (await db.execute(text('''SELECT id,preview_hash,status FROM product_consolidations
        WHERE id=:id'''), {'id': str(identifier)})).mappings().one_or_none()
    if row is None:
        raise LinkError('consolidation_not_found', 404)
    snapshot = (await db.scalar(text('SELECT transaction_timestamp()'))).isoformat()
    result = {'consolidation_id': row['id'], 'preview_hash': row['preview_hash'], 'preview_status': row['status'],
        'purpose': 'selected_ai_lineage_evidence_only', 'read_only': True, 'remote_requests': 0,
        'guard_decision_changed': False, 'eligibility_assessed': False, 'complete': False,
        'database_snapshot_at': snapshot, 'requested_job_ids': sorted(job_ids), 'jobs': [], 'drafts': [],
        'selected_job_backlinks': [], 'publication_journals': [], 'problems': [],
        'limits': {'jobs': MAX_JOBS, 'drafts': MAX_DRAFTS, 'journals': MAX_JOURNALS,
            'document_bytes': MAX_DOCUMENT_BYTES, 'total_raw_bytes': MAX_TOTAL_BYTES,
            'response_bytes': MAX_RESPONSE_BYTES},
        'interpretation': 'Exact selected records only; no global dependency clearance. Database snapshot is repeatable read. '
            'Journal files are two bounded observations, not an atomic filesystem snapshot or publication lock.'}
    budget, refs, wanted, drafts, observed = Budget(), set(), set(), {}, {}
    try:
        jobs, result['jobs'] = await _rows(db, 'ai_content_jobs', set(job_ids), budget)
        if set(jobs) != set(job_ids):
            raise Incomplete('selected_ai_job_missing')
        for job in jobs.values():
            ctx = job.get('context')
            if not isinstance(ctx, dict):
                raise Incomplete('ai_context_shape_unverified')
            if ctx.get('staging_id') is not None:
                wanted.add(_uuid(ctx['staging_id']))
            authorization = ctx.get('staging_auto_publication')
            if authorization is not None:
                if not isinstance(authorization, dict):
                    raise Incomplete('staging_authorization_shape_unverified')
                wanted.add(_uuid(authorization.get('draft_id')))
                _uuid(authorization.get('preview_id'))
        while True:
            if len(wanted) > MAX_DRAFTS:
                raise Incomplete('linked_draft_count_limit')
            missing = wanted - drafts.keys()
            if missing:
                loaded, records = await _rows(db, 'product_import_drafts', missing, budget)
                result['drafts'].extend(records); drafts.update(loaded)
                if set(loaded) != missing:
                    raise Incomplete('linked_draft_missing')
                for draft in loaded.values():
                    _draft_refs(draft, refs)
            # A multishop AI preview may name the aggregate; it is not a file.
            for job in jobs.values():
                ctx = job['context']; authorization = ctx.get('staging_auto_publication') or {}
                for candidate, draft_id in ((job.get('preview_id'), ctx.get('staging_id')),
                                           (authorization.get('preview_id'), authorization.get('draft_id'))):
                    if candidate is None:
                        continue
                    _uuid(candidate)
                    draft = drafts.get(draft_id, {})
                    doc = draft.get('document') or {}; targets = doc.get('publications')
                    aggregate = doc.get('publication') or {}
                    if not (isinstance(targets, dict) and len(targets) > 1
                            and aggregate.get('preview_id') == candidate):
                        _ref(refs, ctx.get('shop'), candidate)
            pending = refs - observed.keys()
            if not pending and not (wanted - drafts.keys()):
                break
            for shop, journal_id in sorted(pending):
                # Only configured local shop namespaces, without reading their
                # connection configuration or credentials.
                exists = await db.scalar(text('SELECT id FROM shops WHERE code=:code'), {'code': shop})
                if exists is None:
                    raise Incomplete('linked_shop_missing')
                journal, base = _journal(shop, journal_id, budget)
                observed[(shop, journal_id)] = journal
                result['publication_journals'].append(journal)
                staging = base.get('staging')
                if staging is not None:
                    if not isinstance(staging, dict):
                        raise Incomplete('journal_staging_shape_unverified')
                    wanted.add(_uuid(staging.get('draft_id')))
        for job in jobs.values():
            draft_id = job['context'].get('staging_id')
            if draft_id in drafts:
                rows = drafts[draft_id]['document']['rows']
                if any(not isinstance(value, dict) for value in rows):
                    raise Incomplete('draft_row_shape_unverified')
                result['selected_job_backlinks'].append({'job_id': job['id'], 'draft_id': draft_id,
                    'current_row_ids': [value.get('id') for value in rows if value.get('ai_job_id') == job['id']],
                    'automation_job_member': job['id'] in (drafts[draft_id]['document'].get('automation') or {}).get('job_ids', [])})
        for key, first in sorted(observed.items()):
            second, _ = _journal(*key)
            if _fingerprint(first) != _fingerprint(second):
                raise Incomplete('journal_changed_between_observations')
        result['complete'] = True
    except Incomplete as error:
        result['problems'].append(str(error))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        result['problems'].append('evidence_shape_or_file_unverified')
    except DBAPIError:
        await db.rollback()
        result['problems'].append('database_evidence_unavailable')
    result['raw_bytes'] = budget.bytes
    result['observed_at'] = datetime.now(timezone.utc).isoformat()
    if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) + 128 > MAX_RESPONSE_BYTES:
        result.update(complete=False, jobs=[], drafts=[], selected_job_backlinks=[], publication_journals=[],
                      problems=['evidence_response_byte_limit'])
    result['evidence_hash'] = _hash(json.dumps(result, sort_keys=True, ensure_ascii=False).encode('utf-8'))
    return result
