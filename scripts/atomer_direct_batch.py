#!/usr/bin/env python3
"""One-off, explicitly invoked xTrek content migration using the existing validators.

No database connection, product creation, stock writes, scheduler or credentials on disk.
The private run directory is the durable journal; never delete it to retry a PUT.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import getpass
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
import threading
from urllib.parse import urlsplit
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'api'))
import requests
from lxml import etree
from inventory_hub.adapters.atomer_content import parse_selected, source_values, AtomerSourceError
from inventory_hub.services import atomer_content_source as content
from inventory_hub.services.atomer_parameters import source_parameters, canonical_parameters, format_for_upgates
from inventory_hub.services.ai_content_update import parameter_rows
from inventory_hub.services.product_publication_source import plain

BASE_URL = 'https://xtrek.admin.s22.upgates.com/api/v2'
TEXT_FIELDS = ['long_description', 'seo_title', 'seo_description']
FIELDS = TEXT_FIELDS + ['parameters']
BATCH_SIZE = 20
IMAGE_CLIENT = threading.local()


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    """Flush both data and directory before considering a write intent durable."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    with open(temporary, 'w', encoding='utf-8') as handle:
        os.chmod(temporary, 0o600)
        json.dump(plain(value), handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def load(path):
    return json.loads(Path(path).read_text())


class Blocked(Exception):
    pass


class Halt(Exception):
    pass


def chunks(rows, size=BATCH_SIZE):
    for position in range(0, len(rows), size):
        yield rows[position:position + size]


class Api:
    def __init__(self, login, key, root):
        self.session = requests.Session()
        self.session.auth = (login, key)
        self.session.headers.update({'Accept': 'application/json'})
        self.root, self.remaining, self.calls = Path(root), None, 0

    def request(self, method, endpoint, *, codes=None, payload=None):
        if endpoint not in {'products', 'products/parameters', 'config', 'languages', 'pricelists'}:
            raise Halt('endpoint_not_allowed')
        if method != 'GET' and (method != 'PUT' or endpoint != 'products'):
            raise Halt('method_not_allowed')
        if self.remaining is not None and self.remaining <= 8:
            raise Halt('api_budget_reserved_for_recovery')
        options = {'timeout': (10, 45), 'allow_redirects': False}
        if codes is not None:
            if not codes or len(codes) > BATCH_SIZE or len(set(codes)) != len(codes):
                raise Halt('invalid_batch_selection')
            for code in codes:
                content.source._code(code)
            options['params'] = {'codes': ';'.join(codes), 'page': 1, 'current_page_items': 100}
        if payload is not None:
            options['json'] = payload
        self.calls += 1
        try:
            response = self.session.request(method, BASE_URL + '/' + endpoint, **options)
        except requests.RequestException:
            raise Halt('api_network_failure_' + method.lower()) from None
        headers = {key.lower(): value for key, value in response.headers.items()
                   if key.lower().startswith('x-rate-limit') or key.lower() == 'retry-after'}
        hour, day = headers.get('x-rate-limit-hour-remaining', ''), headers.get('x-rate-limit-day-remaining', '')
        self.remaining = int(hour) + int(day) if hour.isdigit() and day.isdigit() else None
        save(self.root / 'quota.json', {'at': now(), 'headers': headers, 'calls_this_process': self.calls})
        if response.status_code != 200:
            # No credential-bearing exception text or automatic PUT retry.
            raise Halt('api_http_' + str(response.status_code) + '_' + method.lower())
        if len(response.content) > 16_000_000:
            raise Halt('api_response_too_large')
        try:
            data = json.loads(response.content, parse_float=Decimal)
        except (ValueError, UnicodeError):
            raise Halt('api_invalid_json') from None
        if not isinstance(data, dict) or not content.source._messages_ok(data.get('messages')):
            raise Halt('api_invalid_response')
        return plain(data)

    def products(self, codes, endpoint='products'):
        data = self.request('GET', endpoint, codes=codes)
        rows = data.get('products')
        if (not isinstance(rows, list) or data.get('current_page') != 1
                or data.get('number_of_pages') not in (0, 1)
                or data.get('number_of_items') != len(rows)
                or data.get('current_page_items') != len(rows)
                or any(not isinstance(row, dict) or not content.source._messages_ok(row.get('messages')) for row in rows)):
            raise Halt('incomplete_batch_response')
        result = {row.get('code'): row for row in rows}
        if len(result) != len(rows) or set(result) - set(codes):
            raise Halt('unexpected_batch_identity')
        return result

    def read(self, codes):
        parents = self.products(codes)
        details = self.products(codes, 'products/parameters')
        result = {}
        for code, row in parents.items():
            detail = details.get(code)
            if detail is None or detail.get('product_id') != row.get('product_id'):
                continue
            if row.get('variants') or detail.get('variants'):
                continue
            parent = deepcopy(row)
            parent['parameters'] = canonical_parameters(parameter_rows(detail))
            identity = {'code': code, 'parent_code': code, 'variant_code': None,
                        'variant_id': None, 'product_id': parent.get('product_id')}
            family, anchor = content._family(parent, identity)
            content._descriptions(parent)
            identity.update(full_family=family, ean=content._value(anchor, 'ean'), parameters_yn=True)
            result[code] = {'identity': identity, 'parent': parent, 'options': None}
        return result


def prepare_sources(source_file, audit_zip, root, expected_sha):
    path = root / 'sources.json'
    if path.exists():
        previous = load(path)
        with open(source_file, 'rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != expected_sha or previous['source_sha256'] != expected_sha:
            raise Halt('source_changed')
        return previous
    with zipfile.ZipFile(audit_zip) as archive:
        def entry(suffix):
            names = [name for name in archive.namelist() if name.endswith(suffix)]
            if len(names) != 1:
                raise Halt('ambiguous_audit_entry')
            return archive.read(names[0])
        identities = [json.loads(line) for line in entry('/identity_map.jsonl').splitlines()]
        dictionary = json.loads(entry('/parameter_dictionary.json'))
    selected = [row for row in identities if not row.get('parent_id')
                and row.get('upgates_product_id') and not row.get('upgates_variant_id')
                and not row.get('conflicts')]
    if (len({row['buyers_id'] for row in selected}) != len(selected)
            or len({row['upgates_product_id'] for row in selected}) != len(selected)):
        raise Halt('duplicate_candidate_identity')
    names = {key: value['name'] for key, value in dictionary.items()
             if value.get('status') == 'consistent_ordered_vector_mapping'
             and len(value.get('names', {})) == 1}
    records = []
    for batch in chunks(selected, 10):
        parsed, errors = parse_candidates(source_file, [row['buyers_id'] for row in batch])
        if parsed['sha256'] != expected_sha:
            raise Halt('source_hash_mismatch')
        for candidate in batch:
            code = candidate['buyers_id']
            if code in errors:
                records.append({'code': code, 'target_code': candidate['upgates_code'],
                                'product_id': candidate['upgates_product_id'],
                                'state': 'blocked_source', 'reason': errors[code]})
                continue
            group = parsed['groups'][code]
            row = {'code': code, 'target_code': candidate['upgates_code'],
                   'product_id': candidate['upgates_product_id'], 'hub_id': candidate.get('hub_id'),
                   'hub_sku': candidate.get('hub_sku'), 'group': group, 'state': 'source_ready'}
            try:
                if code != row['target_code']:
                    raise Blocked('source_target_code_differs')
                values = source_values(group, TEXT_FIELDS)
                values['long_description'] = format_for_upgates(values['long_description'])
                values['parameters'] = source_parameters(group, names, [])
                xml = etree.fromstring(group['rows'][0]['raw_xml'].encode())
                images = [xml.findtext('MAIN_IMAGE')] + [image.text for image in xml.findall('ALTER_IMAGES/IMAGE')]
                if any(not image or not image.startswith('https://') for image in images):
                    raise Blocked('source_gallery_unknown')
                price = Decimal(xml.findtext('PRICE_VAT') or '')
                if not price.is_finite() or price < 0:
                    raise Blocked('source_price_unknown')
                row.update(values=values, images=images, price_vat=format(price, 'f'),
                           name=xml.findtext('PRODUCT_NAME'))
            except (AtomerSourceError, Blocked) as exc:
                row.update(state='blocked_source', reason=str(exc))
            except Exception:
                row.update(state='blocked_source', reason='invalid_source_metadata')
            records.append(row)
        if len(records) % 100 == 0:
            print(json.dumps({'phase': 'source', 'processed': len(records), 'total': len(selected)}), flush=True)
    result = {'created_at': now(), 'source_sha256': expected_sha,
              'total_source_rows': parsed['total_rows'], 'records': records,
              'parameter_names': names, 'base_url': BASE_URL}
    save(path, result)
    return result


def parse_candidates(source_file, codes):
    """Isolate stale candidate selections without weakening the XML parser."""
    try:
        with open(source_file, 'rb') as stream:
            return parse_selected(stream, codes), {}
    except AtomerSourceError as exc:
        if exc.code not in {'atomer_source_selection_missing', 'atomer_source_duplicate_code',
                            'atomer_source_mixed_family', 'atomer_source_family_too_large'}:
            raise
        if len(codes) == 1:
            # Still obtain the verified full-file hash; the caller requires it.
            from inventory_hub.adapters.atomer_content import _preflight
            with open(source_file, 'rb') as stream:
                sha, size = _preflight(stream)
            return {'sha256': sha, 'size_bytes': size, 'total_rows': None, 'groups': {}}, {codes[0]: exc.code}
        middle = len(codes) // 2
        left, left_errors = parse_candidates(source_file, codes[:middle])
        right, right_errors = parse_candidates(source_file, codes[middle:])
        if left['sha256'] != right['sha256']:
            raise Halt('source_changed')
        left['groups'].update(right['groups'])
        left['total_rows'] = left['total_rows'] or right['total_rows']
        return left, {**left_errors, **right_errors}


def record_path(root, code):
    return root / 'records' / (hashlib.sha256(code.encode()).hexdigest() + '.json')


def prepare_price_conflicts(source_file, audit_zip, root, previous_root, expected_sha):
    """New journal, exact prior price-conflict selection, unchanged source bytes."""
    previous_root = previous_root.resolve()
    if previous_root == root or not (previous_root / 'sources.json').is_file():
        raise Halt('price_run_requires_existing_separate_journal')
    previous = prepare_sources(source_file, audit_zip, previous_root, expected_sha)
    candidates = []
    for row in previous['records']:
        old = load(record_path(previous_root, row['code']))
        if old['state'] == 'blocked_preflight' and old.get('reason') == 'source_target_price_differs':
            if old['source_sha256'] != expected_sha or old['source_candidate'] != row or row['state'] != 'source_ready':
                raise Halt('price_selection_source_changed')
            candidates.append(row)
    if not candidates:
        raise Halt('no_prior_price_conflicts')
    result = {**previous, 'records': candidates, 'price_policy': 'atomer',
              'prior_journal': str(previous_root)}
    path = root / 'sources.json'
    if path.exists():
        if load(path) != result:
            raise Halt('price_run_selection_changed')
    else:
        save(path, result)
    return result


def verify_price(remote, source, options):
    rows = [row for row in remote['parent'].get('prices', []) if row.get('language') == 'sk']
    if len(rows) != 1 or rows[0].get('currency') != 'EUR':
        raise Blocked('target_price_unknown')
    prices = [row for row in rows[0].get('pricelists', []) if row.get('name') == options['pricelist']]
    if len(prices) != 1 or prices[0].get('price_with_vat') is None:
        raise Blocked('target_price_unknown')
    if Decimal(str(prices[0]['price_with_vat'])) != Decimal(source['price_vat']):
        raise Blocked('source_target_price_differs')


def image_bytes(url, root):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.query
            or parsed.hostname not in {'www.xtrek.sk', 'xtrek.sk', 'xtrek.s22.cdn-upgates.com'}):
        raise Blocked('gallery_origin_requires_review')
    cache = root / 'images' / (hashlib.sha256(url.encode()).hexdigest() + '.bin')
    if cache.exists():
        return cache.read_bytes()
    try:
        # Separate anonymous request: never send API credentials to image hosts.
        if not hasattr(IMAGE_CLIENT, 'session'):
            IMAGE_CLIENT.session = requests.Session()
            IMAGE_CLIENT.session.headers['User-Agent'] = 'InventoryHub-MigrationGalleryVerification/1.0'
        with IMAGE_CLIENT.session.get(url, timeout=(10, 25), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise Blocked('gallery_download_failed')
            parts, size, started = [], 0, time.monotonic()
            for part in response.iter_content(64 * 1024):
                size += len(part)
                if size > 20_000_000 or time.monotonic() - started > 60:
                    raise Blocked('gallery_download_limit')
                parts.append(part)
            raw = b''.join(parts)
    except requests.RequestException:
        raise Blocked('gallery_download_failed') from None
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(raw)
    return raw


def gallery(source, remote, root):
    from PIL import Image, ImageOps
    rows = remote['parent'].get('images') or []
    if (len(rows) != len(source['images']) or not rows
            or [bool(row.get('main_yn')) for row in rows] != [True] + [False] * (len(rows) - 1)):
        raise Blocked('gallery_order_or_count_differs')
    proof = []
    for original, row in zip(source['images'], rows):
        target = row.get('url')
        left, right = image_bytes(original, root), image_bytes(target, root)
        item = {'source_url': original, 'target_url': target,
                'source_sha256': hashlib.sha256(left).hexdigest(), 'target_sha256': hashlib.sha256(right).hexdigest()}
        with Image.open(io.BytesIO(left)) as a, Image.open(io.BytesIO(right)) as b:
            if getattr(a, 'n_frames', 1) != 1 or getattr(b, 'n_frames', 1) != 1:
                raise Blocked('animated_gallery_requires_review')
            a, b = ImageOps.exif_transpose(a).convert('RGBA'), ImageOps.exif_transpose(b).convert('RGBA')
            item.update(source_size=list(a.size), target_size=list(b.size))
            if left == right:
                item['result'] = 'identical_bytes'
            elif a.size == b.size and a.tobytes() == b.tobytes():
                item['result'] = 'identical_pixels'
            else:
                # No heuristic silently promotes a different photo to verified.
                item['result'] = 'visual_review_required'
        proof.append(item)
    return proof


def make_record(source, remote, names, options, price_policy=None):
    if remote['parent'].get('product_id') != source['product_id']:
        raise Blocked('native_identity_changed')
    evidence = content.verify_source(source['group'], remote)
    price_patch = None
    if price_policy == 'atomer':
        xml = etree.fromstring(source['group']['rows'][0]['raw_xml'].encode())
        price_patch = content.build_source_price_patch(remote, source['price_vat'], xml.findtext('VAT'), options)
    else:
        verify_price(remote, source, options)
    values = deepcopy(source['values'])
    values['parameters'] = source_parameters(source['group'], names, remote['parent']['parameters'])
    patch = content.build_patch(values, remote, FIELDS)
    content.build_patch(patch['before'], remote, FIELDS)  # Require restorable originals.
    record = {'code': source['code'], 'operation_id': str(uuid.uuid4()), 'created_at': now(),
            'state': 'prepared', 'fields': FIELDS, 'identity_evidence': evidence,
            'remote_before': remote, 'protected_before': content.protected_observation(remote, FIELDS), **patch}
    if price_patch:
        record['price_patch'] = price_patch
        record['payload']['products'][0]['prices'] = deepcopy(price_patch['payload']['products'][0]['prices'])
    return record


def record_is_noop(record):
    price = record.get('price_patch')
    return content.compare_values(record['before'], record['after']) and (not price or price['before'] == price['after'])


def revalidate_price_config(api, records):
    data = api.request('GET', 'config')
    languages = [row for row in data.get('languages', []) if row.get('language_id') == 'sk' and row.get('active_yn') is True]
    lists = [row for row in data.get('pricelists', []) if row.get('default_yn') is True]
    if (data.get('config', {}).get('prices_with_vat_yn') is not True or len(languages) != 1
            or languages[0].get('currency_id') != 'EUR' or len(lists) != 1):
        raise Halt('price_configuration_changed')
    current = {'pricelist': lists[0]['name'], 'currency': 'EUR', 'prices_with_vat': True}
    if any(row['price_patch']['options'] != current for row in records):
        raise Halt('price_configuration_changed')


def scan(api, sources, root):
    config = api.request('GET', 'config').get('config', {})
    languages = api.request('GET', 'languages').get('languages', [])
    pricelists = api.request('GET', 'pricelists').get('pricelists', [])
    selected_language = [row for row in languages if row.get('language_id') == 'sk' and row.get('active_yn') is True]
    default_lists = [row for row in pricelists if row.get('default_yn') is True]
    if (config.get('prices_with_vat_yn') is not True or len(selected_language) != 1
            or selected_language[0].get('currency_id') != 'EUR' or len(default_lists) != 1):
        raise Halt('price_configuration_requires_review')
    options = {'pricelist': default_lists[0]['name'], 'currency': 'EUR', 'prices_with_vat': True}
    save(root / 'price-options.json', options)
    todo = [row for row in sources['records'] if not record_path(root, row['code']).exists()]
    for batch in chunks(todo):
        candidates = [row for row in batch if row['state'] == 'source_ready']
        remote = api.read([row['target_code'] for row in candidates]) if candidates else {}
        records = []
        for row in batch:
            record = {'code': row['code'], 'state': row['state'], 'reason': row.get('reason')}
            if row['state'] == 'source_ready':
                try:
                    if row['target_code'] not in remote:
                        raise Blocked('target_missing_or_not_standalone')
                    record = make_record(row, remote[row['target_code']], sources['parameter_names'], options, sources.get('price_policy'))
                except Exception as exc:
                    record.update(state='blocked_preflight', reason=getattr(exc, 'code', str(exc) if isinstance(exc, Blocked) else type(exc).__name__))
            record.update(source_sha256=sources['source_sha256'], source_candidate=row)
            records.append(record)
        for record in records:
            save(record_path(root, record['code']), record)
        summary(root, sources)
    pending = [load(path) for path in (root / 'records').glob('*.json')]
    pending = [row for row in pending if row['state'] == 'prepared']
    def check(record):
        try:
            proof = gallery(record['source_candidate'], record['remote_before'], root)
            record['gallery'] = proof
            if any(item['result'] == 'visual_review_required' for item in proof):
                record.update(state='gallery_review', reason='gallery_visual_review_required')
            else:
                record['state'] = 'no_op' if record_is_noop(record) else 'ready'
        except Exception as exc:
            record.update(state='blocked_gallery', reason=str(exc) if isinstance(exc, Blocked) else type(exc).__name__)
        save(record_path(root, record['code']), record)
    # Only anonymous images run concurrently; authenticated API stays sequential.
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(check, record) for record in pending]
        for count, future in enumerate(as_completed(futures), 1):
            future.result()
            if count % 20 == 0 or count == len(pending):
                summary(root, sources)


def revalidate(record, remote):
    if remote is None or remote['identity'] != record['remote_before']['identity']:
        raise Blocked('identity_changed_since_preview')
    if not content.compare_values(content.field_values(remote, FIELDS), record['before']):
        raise Blocked('content_changed_since_preview')
    if content.protected_observation(remote, FIELDS) != record['protected_before']:
        raise Blocked('protected_fields_changed_since_preview')
    payload = deepcopy(record['payload'])
    if record.get('price_patch'):
        price = record['price_patch']
        source = record['source_candidate']
        xml = etree.fromstring(source['group']['rows'][0]['raw_xml'].encode())
        rebuilt = content.build_source_price_patch(remote, source['price_vat'], xml.findtext('VAT'), price['options'])
        if rebuilt != price or payload['products'][0].pop('prices', None) != price['payload']['products'][0]['prices']:
            raise Blocked('price_payload_changed')
    content._validate_payload(payload, remote['identity'])


def validate_outcome(record, remote, acknowledged):
    if not acknowledged:
        return 'uncertain', 'write_not_acknowledged'
    if remote is None or remote['identity'] != record['remote_before']['identity']:
        return 'uncertain', 'readback_identity_changed'
    if not content.compare_values(content.field_values(remote, FIELDS), record['after']):
        return 'uncertain', 'readback_content_differs'
    protected = content.protected_observation(remote, FIELDS)
    expected_protected = record['protected_before']
    if record.get('price_patch'):
        price = record['price_patch']
        try:
            if content.source_price_values(remote, price['options']) != price['after']:
                return 'uncertain', 'readback_price_differs'
            protected = content.protected_except_source_price(protected, price['options'])
            expected_protected = content.protected_except_source_price(expected_protected, price['options'])
        except content.source.SourceError:
            return 'uncertain', 'readback_price_unknown'
    if protected != expected_protected:
        return 'review_after_write', 'readback_protected_fields_differ'
    return 'completed', None


def apply(api, sources, root, limit):
    records = [load(path) for path in sorted((root / 'records').glob('*.json'))]
    if any(row.get('price_patch') for row in records) and sources.get('price_policy') != 'atomer':
        raise Halt('price_policy_not_enabled')
    if sources.get('price_policy') == 'atomer':
        selected = {row['code']: row for row in sources['records']}
        if any(row.get('price_patch') and row.get('source_candidate') != selected.get(row['code']) for row in records):
            raise Halt('price_selection_source_changed')
    if any(row['state'] in {'sending', 'uncertain', 'review_after_write'} for row in records):
        raise Halt('unresolved_prior_write_no_retry')
    todo = [row for row in records if row['state'] == 'ready']
    if limit:
        todo = todo[:limit]
    for batch in chunks(todo):
        priced = [row for row in batch if row.get('price_patch')]
        if priced:
            revalidate_price_config(api, priced)
        if api.remaining is not None and api.remaining < 20:
            raise Halt('api_budget_reserved_for_recovery')
        before = api.read([row['code'] for row in batch])
        if api.remaining is None or api.remaining < 12:
            raise Halt('api_budget_unknown_or_reserved_for_recovery')
        eligible = []
        for row in batch:
            try:
                if row['source_sha256'] != sources['source_sha256']:
                    raise Blocked('source_changed')
                revalidate(row, before.get(row['code']))
                eligible.append(row)
            except Blocked as exc:
                row.update(state='blocked_changed', reason=str(exc))
                save(record_path(root, row['code']), row)
        if not eligible:
            continue
        payload = {'products': [row['payload']['products'][0] for row in eligible]}
        batch_id = str(uuid.uuid4())
        save(root / 'batches' / (batch_id + '.json'), {'state': 'sending', 'started_at': now(),
             'codes': [row['code'] for row in eligible], 'payload': payload})
        for row in eligible:
            row.update(state='sending', batch_id=batch_id, sent_at=now())
            save(record_path(root, row['code']), row)
        try:
            ack = api.request('PUT', 'products', payload=payload)
            save(root / 'batches' / (batch_id + '-ack.json'), ack)
            seen = ack.get('products')
            if (not isinstance(seen, list) or len(seen) != len(eligible)
                    or any(not isinstance(value, dict) for value in seen)
                    or {value.get('code') for value in seen} != {row['code'] for row in eligible}):
                raise Halt('batch_ack_incomplete')
            acks = {value['code']: value for value in seen}
            after = api.read([row['code'] for row in eligible])
            for row in eligible:
                ok = content._acknowledged({'products': [acks[row['code']]]}, row['remote_before']['identity'])
                state, reason = validate_outcome(row, after.get(row['code']), ok)
                row.update(state=state, reason=reason, acknowledged=ok, observed=after.get(row['code']), checked_at=now())
                save(record_path(root, row['code']), row)
            summary(root, sources)
            if any(row['state'] != 'completed' for row in eligible):
                raise Halt('batch_requires_review_no_retry')
        except Exception:
            for row in eligible:
                if row['state'] == 'sending':
                    row.update(state='uncertain', reason='write_outcome_requires_review')
                    save(record_path(root, row['code']), row)
            raise


def verify(api, sources, root):
    records = [load(path) for path in sorted((root / 'records').glob('*.json'))]
    for batch in chunks([row for row in records if row['state'] in {'completed', 'no_op'}]):
        remote = api.read([row['code'] for row in batch])
        for row in batch:
            state, reason = validate_outcome(row, remote.get(row['code']), True)
            row['second_pass'] = {'at': now(), 'no_op': state == 'completed', 'reason': reason,
                                  'observed': remote.get(row['code'])}
            save(record_path(root, row['code']), row)
        summary(root, sources)


def summary(root, sources):
    rows = [load(path) for path in (root / 'records').glob('*.json')]
    result = {'at': now(), 'candidates': len(sources['records']), 'processed': len(rows),
              'states': dict(Counter(row['state'] for row in rows)),
              'reasons': dict(Counter(row.get('reason') for row in rows if row.get('reason'))),
              'second_pass_no_op': sum(row.get('second_pass', {}).get('no_op') is True for row in rows)}
    save(root / 'summary.json', result)
    print(json.dumps(result), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'scan', 'apply', 'verify', 'summary'])
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--audit', required=True, type=Path)
    parser.add_argument('--source-sha256', required=True)
    parser.add_argument('--run-dir', required=True, type=Path)
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--credentials-stdin', action='store_true')
    parser.add_argument('--atomer-prices-from', type=Path,
                        help='Explicitly authorize source prices for prior price conflicts in a separate journal')
    args = parser.parse_args()
    root = args.run_dir.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = open(root / 'run.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    sources = (prepare_price_conflicts(args.source, args.audit, root, args.atomer_prices_from, args.source_sha256)
               if args.atomer_prices_from else prepare_sources(args.source, args.audit, root, args.source_sha256))
    if sources.get('price_policy') == 'atomer' and args.atomer_prices_from is None:
        raise Halt('price_policy_flag_required')
    if args.action in {'prepare', 'summary'}:
        summary(root, sources)
        return
    if args.credentials_stdin:
        credentials = json.loads(sys.stdin.readline())
        login, key = credentials['login'], credentials['key']
    else:
        login, key = getpass.getpass('API login: '), getpass.getpass('API key: ')
    api = Api(login, key, root)
    try:
        if args.action == 'scan':
            scan(api, sources, root)
        elif args.action == 'apply':
            apply(api, sources, root, args.limit)
        else:
            verify(api, sources, root)
    finally:
        api.session.close()
        summary(root, sources)


if __name__ == '__main__':
    try:
        main()
    except (Halt, Blocked, AtomerSourceError) as error:
        print(json.dumps({'halted': str(error)}), flush=True)
        sys.exit(2)
