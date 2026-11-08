"""Pure bounded catalog journal observations, including result/event sidecars."""
import hashlib
import json

from inventory_hub import config_io
from inventory_hub.catalog_types import CatalogProduct, ImportItem, ImportPriceLine, ShopImportPreview
from inventory_hub.services import product_dependency_scope as s


MAX_FILES = 1000
MAX_FILE_BYTES = 16000000
MAX_TOTAL_BYTES = 64000000
TERMINAL = {'created', 'exists', 'skipped', 'linked', 'verified', 'completed'}


def inventory(shop_codes):
    """Bounded directory membership only; it is not a file-content snapshot."""
    manifests, identifiers, directories = {}, set(), set()
    for namespace in sorted(shop_codes):
        directory = config_io.shop_path(namespace).parent / 'catalog-imports'
        if directory in directories:
            continue
        directories.add(directory)
        if directory.is_symlink():
            raise s.Unverified('journal_directory_symlink')
        paths = sorted(path for path in directory.glob('*.json')
                       if '.result.' not in path.name and not path.name.endswith('.result.json'))
        if len(paths) > MAX_FILES:
            raise s.Unverified('journal_count_limit')
        for path in paths:
            if not s.UUID32.fullmatch(path.stem):
                raise s.Unverified('journal_filename_unverified')
            identifiers.add((namespace, path.stem))
        manifests[namespace] = s.digest([path.name for path in paths])
    return manifests, identifiers


def iter_observations(shop_codes):
    """Yield one complete file set at a time; no config/HTTP/recovery calls."""
    from inventory_hub.services.catalog_import import _load_job
    total, directories = 0, set()
    for namespace in sorted(shop_codes):
        directory = config_io.shop_path(namespace).parent / 'catalog-imports'
        if directory in directories:
            continue
        directories.add(directory)
        if directory.is_symlink():
            raise s.Unverified('journal_directory_symlink')
        paths = sorted(path for path in directory.glob('*.json')
                       if '.result.' not in path.name and not path.name.endswith('.result.json'))
        if len(paths) > MAX_FILES:
            raise s.Unverified('journal_count_limit')
        names = [path.name for path in paths]
        yield 'directory', namespace, s.digest(names)
        for path in paths:
            if not s.UUID32.fullmatch(path.stem):
                raise s.Unverified('journal_filename_unverified')
            files = [path, path.with_suffix('.result.json'), path.with_suffix('.events.jsonl')]
            saved, hashes = {}, {}
            for file in files:
                if file.is_symlink():
                    raise s.Unverified('journal_symlink')
                if not file.exists():
                    saved[file] = None
                    hashes[file.suffix + ('.result' if '.result.' in file.name else '')] = None
                    continue
                size = file.stat().st_size
                total += size
                if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                    raise s.Unverified('journal_byte_limit')
                content = file.read_bytes()
                if len(content) != size:
                    raise s.Unverified('journal_changed_during_read')
                saved[file] = content
                hashes[file.name[len(path.stem):]] = hashlib.sha256(content).hexdigest()
            # _load_job merges the durable events while retaining full frozen
            # payloads. import_result would write recovery states and is forbidden.
            merged = _load_job(path)
            s.bounded(merged)
            for file, content in saved.items():
                current = file.read_bytes() if file.exists() else None
                if file.is_symlink() or current != content:
                    raise s.Unverified('journal_changed_during_read')
            yield 'journal', (namespace, path.stem), {'document': merged, 'hash': s.digest(merged),
                'file_hashes': hashes, 'bytes': sum(len(value) for value in saved.values() if value is not None)}
        if names != sorted(path.name for path in directory.glob('*.json')
                           if '.result.' not in path.name and not path.name.endswith('.result.json')):
            raise s.Unverified('journal_directory_changed_during_read')


def observe(shop_codes):
    """Compatibility projection for callers explicitly needing bounded bodies."""
    records, manifests = {}, {}
    for kind, key, value in iter_observations(shop_codes):
        (manifests if kind == 'directory' else records)[key] = value
    return records, manifests


def _staging(proof, staging, sources, supplier, drafts, *, settled):
    """Retain frozen intent and every current draft owner, including old previews.

    A changed content hash prevents a fresh product POST in assert_snapshot;
    it does not settle an old uncertain result. register_created may still
    reconcile its frozen sources, using saved owners or the legacy draft lookup.
    This proof models both scopes without changing either writer guard.
    """
    s.obj(staging, {'draft_id', 'hash', 'saved_product_ids', 'supplier_availability_expires_at',
                   'supplier_availability_observed_at'}, ('draft_id', 'hash'), '/staging')
    identifier, frozen_hash = staging['draft_id'], staging['hash']
    if not isinstance(identifier, str) or not s.UUID32.fullmatch(identifier):
        raise s.Unverified('journal_draft_link_unverified', '/staging/draft_id')
    if (not isinstance(frozen_hash, str) or len(frozen_hash) != 64
            or any(char not in '0123456789abcdef' for char in frozen_hash)):
        raise s.Unverified('journal_draft_hash_unverified', '/staging/hash')
    saved = s.obj(staging.get('saved_product_ids', {}), path='/staging/saved_product_ids')
    if saved and set(saved) != {str(sid) for sid in sources}:
        raise s.Unverified('journal_saved_source_ids_changed', '/staging/saved_product_ids')
    for sid, pid in saved.items():
        proof.hub(s.positive(pid, '/staging/saved_product_ids/' + sid), '/staging/saved_product_ids/' + sid)
    linked = drafts.get(identifier)
    if linked is None:
        raise s.Unverified('journal_draft_missing', '/staging/draft_id')
    if linked.get('id') != identifier or linked.get('supplier') != supplier:
        raise s.Unverified('journal_draft_source_lineage_changed', '/staging/draft_id')
    rows = s.draft(proof, linked, '/linked_draft/document')
    proof.linked_drafts.add(identifier)
    by_id = {row['id']: row for row in rows}
    if not set(sources) <= set(by_id) or any(
            source.feed_key != by_id[sid]['source'].get('feed_key', 'products')
            for sid, source in sources.items()):
        raise s.Unverified('journal_draft_source_lineage_changed', '/linked_draft/document/rows')
    # All selected current owners must remain explicit, even when the frozen
    # map names a different owner. s.draft above also retains current codes,
    # GTINs, expected owners, AI baselines and every other row/publication.
    for sid in sources:
        pid = by_id[sid].get('hub_product_id')
        if not pid:
            raise s.Unverified('journal_saved_owner_changed', '/linked_draft/document/rows')
    if not saved:
        proof.context('/staging/saved_product_ids', 'legacy_current_draft_owner_lookup')
    if not settled:
        # Keep the old settled-journal cost: full row/file hashes already freeze
        # its evidence; this extra content digest only explains an old intent.
        from inventory_hub.services.product_import import content_hash
        if (frozen_hash != linked['document'].get('saved_hash')
                or frozen_hash != content_hash(linked['document'])):
            proof.context('/staging/hash', 'changed_snapshot_retains_frozen_and_current_scope')


def parse(proof, job, namespace, identifier, drafts):
    s.obj(job, {'preview', 'sources', 'target', 'prices_with_vat', 'supplier_availability', 'result',
                'supplier_meta', 'staging', 'content_approval'}, ('preview', 'sources', 'result'), '/journal')
    preview = s.obj(job['preview'], ShopImportPreview.model_fields, ('preview_id', 'shop', 'supplier', 'items'), '/preview')
    if preview['preview_id'] != identifier or preview['shop'] != namespace:
        raise s.Unverified('journal_target_changed', '/preview')
    supplier = s.shop(preview['supplier'])
    source_rows, sources = s.array(job['sources'], '/sources', nonempty=True), {}
    for i, raw in enumerate(source_rows):
        source = s.model(CatalogProduct, raw, f'/sources/{i}')
        if source.supplier != supplier or source.id <= 0 or source.id in sources:
            raise s.Unverified('journal_source_lineage_unverified', '/sources')
        sources[source.id] = source
        proof.importer(source, f'/sources/{i}', supplier=supplier, targets=[namespace])
    previews, all_ids = {}, set()
    for i, item in enumerate(s.array(preview['items'], '/preview/items', nonempty=True)):
        s.obj(item, set(ImportItem.model_fields) | {'product_write_attempted'}, ('code', 'product_ids', 'payload', 'status'), f'/preview/items/{i}')
        ids = [s.positive(value) for value in s.array(item['product_ids'], '/preview/items/product_ids', nonempty=True)]
        if len(ids) != len(set(ids)) or not set(ids) <= set(sources) or set(ids) & all_ids or item['code'] in previews:
            raise s.Unverified('journal_item_source_selection_changed', '/preview/items')
        all_ids.update(ids)
        previews[item['code']] = item
        if not set(item.get('existing_product_ids', [])) <= set(ids):
            raise s.Unverified('journal_existing_source_ids_changed', '/preview/items/existing_product_ids')
        # ImportItem.existing_product_ids are source IDs, never Hub card IDs.
        payload_codes = s.payload(proof, item['payload'], namespace, f'/preview/items/{i}/payload')
        if payload_codes != {sources[source_id].shop_code for source_id in ids}:
            raise s.Unverified('journal_payload_source_codes_changed', '/preview/items')
    if all_ids != set(sources):
        raise s.Unverified('journal_unselected_source_scope', '/sources')
    for i, line in enumerate(s.array(preview.get('price_lines', []), '/preview/price_lines')):
        s.obj(line, ImportPriceLine.model_fields, ('product_id', 'code'), f'/preview/price_lines/{i}')
        sid = s.positive(line['product_id'])
        if sid not in sources or line['code'] != sources[sid].shop_code:
            raise s.Unverified('journal_price_source_lineage_changed', '/preview/price_lines')
        from inventory_hub.catalog_types import CatalogShopMatch
        for j, match in enumerate(s.array(line.get('shop_matches', []), '/preview/price_lines/shop_matches')):
            s.obj(match, CatalogShopMatch.model_fields, ('matched_by', 'value', 'code'), '/preview/price_lines/shop_matches')
            if match['matched_by'] not in ('code', 'ean', 'parent_code'):
                raise s.Unverified('unknown_comparison_match', '/preview/price_lines/shop_matches')
            from inventory_hub.services.catalog_identity import parent_shop_code
            source = sources[sid]
            if ((match['matched_by'] == 'code' and match['value'].casefold() != source.shop_code.casefold())
                    or (match['matched_by'] == 'ean' and not s.barcode_keys([match['value']]) & s.barcode_keys(source.eans))
                    or (match['matched_by'] == 'parent_code' and (
                        source.variant_relationship != 'explicit' or not source.group_code
                        or match['value'] != parent_shop_code(source)))):
                raise s.Unverified('comparison_source_identity_changed', '/preview/price_lines/shop_matches')
            proof.context(f'/preview/price_lines/{i}/shop_matches/{j}', 'comparison_only_no_existing_parent_put')
    result = job['result']
    states = []
    if result is not None:
        s.obj(result, {'preview_id', 'shop', 'options', 'prices_with_vat', 'status', 'items', 'errors', 'updated_at', 'shop_check'},
              ('preview_id', 'shop', 'status', 'items'), '/result')
        if result['preview_id'] != identifier or result['shop'] != namespace:
            raise s.Unverified('journal_result_target_changed', '/result')
        found = set()
        for i, item in enumerate(s.array(result['items'], '/result/items', nonempty=True)):
            s.obj(item, set(ImportItem.model_fields) | {'product_write_attempted'}, ('code', 'product_ids', 'payload', 'status'), f'/result/items/{i}')
            original = previews.get(item['code'])
            if (original is None or item['code'] in found or original['payload'] != item['payload']
                    or original['product_ids'] != item['product_ids']):
                raise s.Unverified('journal_result_item_changed', '/result/items')
            found.add(item['code'])
            states.append(item['status'])
        if found != set(previews):
            raise s.Unverified('journal_result_scope_incomplete', '/result/items')
    settled = bool(states) and set(states) <= TERMINAL
    staging = job.get('staging')
    if staging:
        _staging(proof, staging, sources, supplier, drafts, settled=settled)
    return settled
