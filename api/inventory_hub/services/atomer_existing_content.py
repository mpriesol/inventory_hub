"""Compare pinned source content to existing, ID-anchored xTrek families.

Plans contain a complete native before-image. The engine commits that intent
before dispatch. A retry only observes the original plan. Every SKU, GTIN,
stock field, unrelated variant and shop-only value remains outside the patch.
"""
from copy import deepcopy
import json

from inventory_hub.services import atomer_new_native as native
from inventory_hub.services.atomer_generations import same_image, send as update_products
from inventory_hub.services.atomer_review_targets import ReviewError
from inventory_hub.services.product_identity import barcode_keys
from inventory_hub.services.product_publication_source import plain
from inventory_hub.services.sku_policy import brand_key


TEXT = ('title', 'short_description', 'long_description', 'seo_title', 'seo_description')
PRICE_VALUES = ('price_original', 'price_with_vat', 'price_without_vat')
REASONS = {
    'existing_content_parent_identity': 'Nesedí natívne ID alebo SKU rodiča, prípadne je rodič archivovaný alebo je súpravou.',
    'existing_content_product_type': 'Atomer a živý xTrek sa nezhodujú, či ide o samostatný produkt alebo rodinu variantov.',
    'existing_content_leaf_anchors': 'Chýba jednoznačná väzba na natívne ID pre všetky vybrané varianty.',
    'existing_content_parent_ean': 'EAN rodiča v Atomeri a xTreku označujú rozdielne výrobky.',
    'existing_content_manufacturer': 'Výrobca v Atomeri nesedí so živým produktom xTrek.',
    'existing_content_leaf_identity': 'Natívny variant sa zmenil alebo patrí inému rodičovi.',
    'existing_content_leaf_ean': 'EAN zdrojového a natívneho variantu označujú rozdielne výrobky.',
    'existing_content_axes_occupied': 'Výsledná kombinácia parametrov by patrila dvom rôznym natívnym variantom.',
    'existing_content_axes_ambiguous': 'Existujúci variant nemá jednoznačnú hodnotu v každej upravovanej osi.',
    'existing_content_parameter_ambiguous': 'Parameter má viac možných verzií názvu. Treba potvrdiť úpravu správneho parametra.',
    'existing_content_gallery_ambiguous': 'Pôvodný obrázok zodpovedá viacerým súborom v galérii xTrek.',
    'existing_content_siblings_changed': 'Zloženie rodiny sa po uložení zámeru zmenilo.',
    'existing_content_protected_field_changed': 'Po zápise sa zmenil niektorý chránený údaj, sklad alebo identita variantu.',
    'existing_content_incomplete_native_data': 'Upgates neposkytol úplné mapované údaje na jednoznačné overenie.',
}


def _require(condition, message):
    if not condition:
        raise ReviewError(message)


def _map_by(rows, field):
    _require(isinstance(rows, list), 'existing_content_native_shape')
    result = {row[field]: row for row in rows}
    _require(len(result) == len(rows), 'existing_content_native_duplicate')
    return result


def _equal_ean(planned, live):
    # An empty Atomer cell does not revoke an existing barcode.
    if not planned or planned == live:
        return True
    return bool(barcode_keys([planned]).intersection(barcode_keys([live])))


def _targets(doc, live, anchors):
    root = doc['payload']
    _require(live and live['product_id'] == anchors.get('product_id')
             and live['code'] == anchors.get('product_code', root['code'])
             and not live.get('set_yn') and not live.get('archived_yn'), 'existing_content_parent_identity')
    wanted = _map_by(root.get('variants', []), 'code')
    actual = _map_by(live['variants'], 'variant_id')
    _require(bool(wanted) == bool(actual), 'existing_content_product_type')
    codes_in_family = {v['code'].casefold() for v in actual.values()}
    _require(len(codes_in_family) == len(actual) and live['code'].casefold() not in codes_in_family,
             'existing_content_duplicate_sku')
    ids = anchors.get('variant_ids', {})
    codes = anchors.get('variant_codes', {})
    _require(ids.keys() == wanted.keys() and len(set(ids.values())) == len(ids)
             and codes.keys() <= wanted.keys(), 'existing_content_leaf_anchors')
    _require(_equal_ean(root.get('ean'), live.get('ean')), 'existing_content_parent_ean')
    # Supplier aliases resolved by brand_key are acceptable. A different nonempty
    # manufacturer is conflicting identity evidence, never a content correction.
    if root.get('manufacturer') and live.get('manufacturer'):
        _require(brand_key(root['manufacturer']) == brand_key(live['manufacturer']), 'existing_content_manufacturer')
    rows = []
    for key, expected in wanted.items():
        row = actual.get(ids[key])
        _require(row and row['code'] == codes.get(key, key)
                 and row.get('product_id') == live['product_id'] and row.get('product_code') == live['code'],
                 'existing_content_leaf_identity')
        _require(_equal_ean(expected.get('ean'), row.get('ean')), 'existing_content_leaf_ean')
        rows.append((key, expected, row))
    allow_parent = anchors.get('allow_parent_write') is True and set(ids.values()) == set(actual)
    return rows, allow_parent


def _sku_params(rows):
    # The native read schema carries IDs and localized labels. Keep all languages
    # and existing parameter IDs when updating only Slovak source values.
    _require(isinstance(rows, list) and native._parameters(rows) is not None, 'existing_content_parameter_shape')
    return {native._sk(p['descriptions'], 'name'): p for p in rows}


def _parameters(existing, desired):
    current = _sku_params(existing)
    wanted = _sku_params(desired)
    old_values = native._parameters(existing)
    new_values = native._parameters(desired)
    changed = [k for k in wanted if old_values.get(k) != new_values[k]]
    if not changed:
        return None
    merged = deepcopy(current)
    for name in changed:
        _require(not any(n != name and n.casefold() == name.casefold() for n in current),
                 'existing_content_parameter_ambiguous')
        if name not in current:
            merged[name] = wanted[name]
            continue
        old = {native._sk(value['descriptions'], 'value'): value for value in current[name]['values']}
        updated = []
        for value in wanted[name]['values']:
            sk = native._sk(value['descriptions'], 'value')
            updated.append(old.get(sk, value))
        merged[name]['values'] = updated
    return list(merged.values())


def _gallery(existing, wanted, variant):
    if not wanted or not native._image_issues({'images': existing}, wanted, variant):
        return None
    _require(isinstance(existing, list), 'existing_content_gallery_unavailable')
    by_id = _map_by(existing, 'file_id')
    _require(all(native._positive(i) for i in by_id), 'existing_content_gallery_identity')
    used = set()
    result = []
    for index, expected in enumerate(wanted):
        matches = [im for im in existing if same_image(im.get('url'), expected['url'])]
        _require(len(matches) < 2, 'existing_content_gallery_ambiguous')
        if not matches:
            result.append(dict(deepcopy(expected), position=index + 1))
            continue
        image = matches[0]
        file_id = image['file_id']
        _require(file_id not in used, 'existing_content_gallery_ambiguous')
        used.add(file_id)
        next_image = {k: deepcopy(v) for k, v in image.items()
                      if k in ('file_id', 'titles', 'languages', 'main_yn', 'list_yn')}
        next_image.update(position=index + 1, main_yn=bool(expected.get('main_yn')))
        if not variant:
            next_image['list_yn'] = bool(expected.get('list_yn'))
        title = native._sk(expected.get('titles', []), 'title')
        if title:
            titles = _map_by(next_image.get('titles', []), 'language')
            titles['sk'] = dict(language='sk', title=title)
            next_image['titles'] = list(titles.values())
        result.append(next_image)
    for image in sorted(existing, key=lambda im: im['position']):
        if image['file_id'] not in used:
            entry = {k: deepcopy(image[k]) for k in ('file_id', 'titles', 'languages', 'list_yn') if k in image}
            entry.update(position=len(result) + 1, main_yn=False)
            if not variant:
                entry['list_yn'] = False
            result.append(entry)
    return result


def _categories(current, expected):
    old = _map_by(current, 'category_id')
    wanted = _map_by(expected, 'category_id')
    _require(sum(r.get('main_yn') is True for r in wanted.values()) == 1, 'existing_content_source_category')
    if all(key in old and old[key].get('main_yn') is r['main_yn'] for key, r in wanted.items()) and not any(
        r.get('main_yn') and key not in wanted for key, r in old.items()
    ):
        return None
    union = {i: {k: v for k, v in row.items() if k in ('category_id', 'position')} for i, row in old.items()}
    for i in old.keys() | wanted.keys():
        union.setdefault(i, {'category_id': i})['main_yn'] = bool(wanted.get(i, {}).get('main_yn'))
    return list(union.values())


def _labels(row, expected, managed, inherited_changes):
    actual = _map_by(row['labels'], 'label_id')
    desired = {x['label_id']: x['active_yn'] for x in expected}
    changed = {i for i in managed if bool(actual.get(i, {}).get('active_currently_yn')) != desired.get(i, False)}
    # An inherited false can turn true within this same request if the parent
    # flag changes. Materialize the selected child's source value in that case.
    changed |= {i for i in inherited_changes if actual.get(i, {}).get('active_yn') is None}
    if not changed:
        return None, set()
    entries = {i: {k: v for k, v in label.items() if k in ('label_id', 'active_yn', 'active_from', 'active_to')}
               for i, label in actual.items()}
    for i in changed:
        entries[i] = dict(label_id=i, active_yn=desired.get(i, False), active_from=None, active_to=None)
    return list(entries.values()), changed


def _describe(row, desired):
    current = _map_by(row['descriptions'], 'language')
    result = []
    for source in desired:
        language = source['language']
        _require(language in current, 'existing_content_language_missing')
        changes = {k: source[k] for k in TEXT if k in source and
                   not native._same_text(current[language].get(k), source[k], k in ('short_description', 'long_description'))}
        if changes:
            result.append(dict(language=language, **changes))
    return result


def _field_patch(row, wanted, doc, variant, changed_parent_labels):
    patch = {}
    if row.get('active_yn') is not wanted['active_yn']:
        patch['active_yn'] = wanted['active_yn']
    if (wanted['code'] not in doc.get('unspecified_weights', ())
            and native._numeric(row.get('weight')) != native._numeric(wanted.get('weight'))):
        patch['weight'] = wanted['weight']
    if 'availability_id' in wanted and (row.get('availability_id') != wanted['availability_id']
            or (row.get('availability') or '').casefold() != doc['availability'][wanted['code']].casefold()):
        patch['availability_id'] = wanted['availability_id']
    # Neither operator source explicitly names a default variant. The writer
    # chooses one only at original creation. On an existing parent it is protected.
    planned_price = wanted['prices']
    if not native._price_same(row, planned_price, doc['payload']['vats']['sk']):
        patch['prices'] = planned_price
    managed = doc.get('managed_label_ids', [])
    labels, changed_labels = _labels(row, wanted.get('labels', []), managed, changed_parent_labels)
    if labels is not None:
        patch['labels'] = labels
    parameters = _parameters(row['parameters_new'], wanted.get('parameters', []))
    if parameters is not None:
        patch['parameters'] = parameters
    images = _gallery(row['images'], wanted.get('images', []), variant)
    if images is not None:
        patch['images'] = images
        if variant and 'image' in wanted:
            patch['image'] = {k: v for k, v in images[0].items() if k in ('file_id', 'url')}
    return patch, changed_labels


def _root_patch(live, root, doc):
    patch, labels = _field_patch(live, root, doc, False, set())
    if root.get('manufacturer') and not live.get('manufacturer'):
        patch['manufacturer'] = root['manufacturer']
    if native._numeric((live.get('vats') or {}).get('sk')) != native._numeric(root['vats']['sk']):
        patch['vats'] = {'sk': root['vats']['sk']}
    desired_text = root['descriptions']
    if doc.get('title_from_code'):
        desired_text = [{k: v for k, v in language.items() if k != 'title'} for language in desired_text]
    descriptions = _describe(live, desired_text)
    if descriptions:
        patch['descriptions'] = descriptions
    categories = _categories(live['categories'], root['categories'])
    if categories:
        patch['categories'] = categories
    related = set(live['related'])
    if set(root.get('related', [])) - related:
        patch['related'] = sorted(related | set(root['related']))
    return patch, labels


def _protect_existing_price(row, patch, issues, *, tax_allowed):
    """A pre-existing sale must not turn a new gross base price into a second discount."""
    if 'prices' not in patch:
        return
    prices = [p for p in row['prices'] if p.get('language') == 'sk' and p.get('currency') == 'EUR']
    def hold(reason):
        issues.append(row['code'] + ' · ' + reason)
        patch.pop('prices')
    if len(prices) != 1:
        hold('nepodarilo sa jednoznačne overiť slovenský cenník v EUR')
        return
    if not tax_allowed:
        hold('spoločná sadzba DPH sa líši a patrí aj chránenému variantu')
        return
    wanted_lists = {p['name'] for group in patch['prices'] for p in group['pricelists']}
    current = {p['name']: p for p in prices[0]['pricelists'] if p['name'] in wanted_lists}
    if current.keys() != wanted_lists:
        hold('cieľový cenník na existujúcom produkte chýba')
        return
    for price in current.values():
        if (price.get('price_sale') is not None
                or any(native._numeric(price.get(k) or 0) != 0 for k in ('product_discount', 'product_discount_real'))
                or native._numeric(price.get('price_original')) != native._numeric(price.get('price_with_vat'))):
            hold('existujúca zľava v cenníku vyžaduje samostatné zosúladenie akciovej a základnej ceny')
            return


def _axis_safety(live, leaf_patches):
    if not any('parameters' in patch for patch in leaf_patches.values()):
        return
    signatures = set()
    for leaf in live['variants']:
        proposed = leaf_patches.get(leaf['code'], {}).get('parameters', leaf['parameters_new'])
        values = native._parameters(proposed)
        _require(values and all(len(items) == 1 for items in values.values()), 'existing_content_axes_ambiguous')
        signature = native._signature(values)
        _require(signature not in signatures, 'existing_content_axes_occupied')
        signatures.add(signature)


def _problem(reason, live=None):
    return dict(status='conflict', issues=[REASONS.get(reason, reason)], reason_codes=[reason],
                deferred=[], identity_verified=False,
                verified_fields=False, product_id=(live or {}).get('product_id'), variants={}, native=plain(live))


def _assess(doc, live, anchors):
    rows, parent_allowed = _targets(doc, live, anchors)
    root = doc['payload']
    parent_patch, changed_labels = _root_patch(live, root, doc)
    issues, deferred = [], []
    tax_allowed = parent_allowed or native._numeric((live.get('vats') or {}).get('sk')) == native._numeric(root['vats']['sk'])
    _protect_existing_price(live, parent_patch, issues, tax_allowed=tax_allowed)
    if not parent_allowed and parent_patch:
        issues.append('Spoločné údaje rodiča majú rozdiel, ale rodina obsahuje chránené alebo nezahrnuté varianty: '
                      + ', '.join(sorted(parent_patch)))
        parent_patch, changed_labels = {}, set()
    child_patches = {}
    for key, wanted, row in rows:
        pending, _ = _field_patch(row, wanted, doc, True, changed_labels)
        _protect_existing_price(row, pending, issues, tax_allowed=tax_allowed)
        if pending:
            child_patches[row['code']] = pending
    try:
        _axis_safety(live, child_patches)
    except ReviewError as error:
        issues.append(REASONS.get(error.code, error.code))
        for changes in child_patches.values():
            changes.pop('parameters', None)
    child_patches = {key: fields for key, fields in child_patches.items() if fields}
    if len(child_patches) > native.MAX_BATCH:
        # Only local, independently verifiable leaf values go into early tranches.
        # Default-variant and axis swaps need a single coherent family change.
        safe = {key: {k: v for k, v in change.items() if k not in ('main_yn', 'parameters')}
                for key, change in child_patches.items()}
        child_patches = dict(list((key, v) for key, v in safe.items() if v)[:native.MAX_BATCH])
        parent_patch = {}
        if child_patches:
            deferred.append('content_tranche')
        else:
            issues.append('Viac než 100 vzájomne závislých zmien kombinácií alebo predvoleného variantu.')
    issues.extend(doc.get('issues', []))
    if doc.get('missing_source_features'):
        deferred.append('missing_source_features')
        issues.extend(doc['missing_source_features'])
    for field in ('missing_categories', 'missing_related'):
        missing = doc.get(field, [])
        if missing:
            deferred.append(field)
            issues.append(field + ' · ' + ', '.join(missing))
    payload = dict(code=live['code'], **parent_patch)
    if child_patches:
        payload['variants'] = [dict(code=k, **v) for k, v in child_patches.items()]
    needs_write = len(payload) > 1
    status = 'ready' if needs_write else 'partial' if issues else 'verified'
    return dict(status=status, issues=issues, deferred=deferred, identity_verified=True,
                verified_fields=status == 'verified', product_id=live['product_id'],
                variants={key: row['variant_id'] for key, _, row in rows}, native=plain(live),
                payload=payload, anchors=anchors)


def _protected_prices(row, patch, tax_changed):
    grouped = {r['language']: r for r in patch.get('prices', [])}
    prices = row.get('prices', [])
    for group in prices:
        language = group.get('language')
        planned = grouped.get(language) if group.get('currency') == 'EUR' else None
        if planned:
            group.pop('price_common', None)
        if tax_changed and language == 'sk':
            group.pop('vat', None)
        written = {p['name'] for p in (planned or {}).get('pricelists', [])}
        for record in group.get('pricelists', []):
            if record.get('name') in written:
                for key in PRICE_VALUES:
                    record.pop(key, None)
            elif tax_changed and language == 'sk':
                record.pop('price_without_vat', None)


def _protected_parameters(row, before, patch):
    previous = _sku_params(before['parameters_new'])
    current = _sku_params(row['parameters_new'])
    written = _sku_params(patch['parameters'])
    old_values = native._parameters(before['parameters_new'])
    planned_values = native._parameters(patch['parameters'])
    for name in written:
        if old_values.get(name) == planned_values.get(name):
            continue
        if name not in previous:
            current.pop(name, None)
        elif name in current:
            retained = set(old_values[name]).intersection(planned_values[name])
            current[name]['values'] = [v for v in current[name]['values']
                                       if native._sk(v['descriptions'], 'value') in retained]
    row['parameters_new'] = current
    # This legacy API mirror is derived from parameters_new and contains no IDs.
    row.pop('parameters', None)


def _protected_images(row, before, patch):
    previous = _map_by(before['images'], 'file_id')
    images = _map_by(row['images'], 'file_id')
    source = [i['url'] for i in patch['images'] if 'url' in i]
    # Accept only the source files explicitly added by this request.
    for key in images.keys() - previous.keys():
        if any(same_image(images[key].get('url'), url) for url in source):
            del images[key]
    for intended in patch['images']:
        file_id = intended.get('file_id')
        if not file_id or file_id not in images:
            continue
        protected = images[file_id]
        for field in ('main_yn', 'list_yn', 'position'):
            if field in intended and intended[field] != previous[file_id].get(field):
                protected.pop(field, None)
        wanted_title = native._sk(intended.get('titles', []), 'title')
        if wanted_title != native._sk(previous[file_id].get('titles', []), 'title'):
            protected['titles'] = [t for t in protected.get('titles', []) if t['language'] != 'sk']
    row['images'] = images


def _project_node(actual, before, patch, tax_changed):
    row = deepcopy(actual)
    row.pop('last_update_time', None)
    for field in ('active_yn', 'main_yn', 'weight', 'manufacturer', 'image'):
        if field in patch:
            row.pop(field, None)
    if 'manufacturer' in patch:
        row.pop('manufacturer_id', None)
    if 'availability_id' in patch:
        for field in ('availability', 'availability_id', 'availability_type'):
            row.pop(field, None)
    if 'vats' in patch:
        for language in patch['vats']:
            row['vats'].pop(language, None)
    for edited in patch.get('descriptions', []):
        local = _map_by(row['descriptions'], 'language')[edited['language']]
        for field in edited.keys() - {'language'}:
            local.pop(field, None)
    if 'prices' in patch or tax_changed:
        _protected_prices(row, patch, tax_changed)
    if 'labels' in patch:
        old = _map_by(before['labels'], 'label_id')
        labels = _map_by(row['labels'], 'label_id')
        for intended in patch['labels']:
            lid = intended['label_id']
            if lid not in old:
                labels.pop(lid, None)
            elif any(old[lid].get(k) != v for k, v in intended.items() if k != 'label_id'):
                if lid in labels:
                    for flag in ('active_currently_yn', 'active_yn', 'active_from', 'active_to'):
                        labels[lid].pop(flag, None)
        row['labels'] = labels
    if 'parameters' in patch:
        _protected_parameters(row, before, patch)
    if 'images' in patch:
        _protected_images(row, before, patch)
    if 'related' in patch:
        additions = set(patch['related']) - set(before['related'])
        row['related'] = sorted(set(row['related']) - additions)
    if 'categories' in patch:
        old = _map_by(before['categories'], 'category_id')
        current = _map_by(row['categories'], 'category_id')
        for category in patch['categories']:
            key = category['category_id']
            if key not in old:
                current.pop(key, None)
            elif key in current and old[key]['main_yn'] != category['main_yn']:
                current[key].pop('main_yn', None)
        row['categories'] = current
    return row


def _protected(live, plan):
    before, patch = plan['before'], plan['payload']
    tax_changed = 'vats' in patch
    root = _project_node(live, before, patch, tax_changed)
    leaves = _map_by(root.pop('variants'), 'code')
    old = _map_by(before['variants'], 'code')
    written = _map_by(patch.get('variants', []), 'code')
    if any('availability_id' in v or 'active_yn' in v for v in written.values()):
        for field in ('variants_availability', 'variants_availability_id', 'variants_availability_type'):
            root.pop(field, None)
    _require(leaves.keys() == old.keys(), 'existing_content_siblings_changed')
    children = {k: _project_node(row, old[k], written.get(k, {}), tax_changed) for k, row in leaves.items()}
    return plain(dict(parent=root, variants=children))


def _read_documents(documents, fingerprint, expected_ids):
    selected = native._entries(documents)
    _require(selected.keys() == expected_ids.keys()
             and all(native._positive(a.get('product_id')) for a in expected_ids.values()),
             'existing_content_id_invalid')
    with native._connection(fingerprint) as client:
        observed = native._read(client, selected, expected_ids)
    return selected, observed


def _download_queue(client, pids):
    """The gallery is populated asynchronously, so absence in the gallery alone is insufficient."""
    if not pids:
        return []
    filters = dict(product_ids=';'.join(str(i) for i in sorted(pids)), current_page_items=100)
    queued = []
    for page in range(1, 51):
        reply = native.transport._request(client, 'get', 'products/images-queue', params={**filters, 'page': page})
        rows = reply.get('images_queue')
        _require(isinstance(rows, list) and len(rows) <= 100 and native.transport._messages_ok(reply.get('messages')),
                 'existing_content_gallery_queue_incomplete')
        for row in rows:
            _require(row.get('product_id') in pids and isinstance(row.get('url'), str) and row['url'],
                     'existing_content_gallery_queue_identity')
        queued.extend(rows)
        if len(rows) < 100:
            return queued
    raise ReviewError('existing_content_gallery_queue_limit')


def _gallery_writes(plan):
    patch = plan['payload']
    return [(leaf, {im['url'] for im in leaf.get('images', []) if im.get('url')})
            for leaf in [patch, *patch.get('variants', [])] if any(im.get('url') for im in leaf.get('images', []))]


def _waiting(plan, queue):
    pid = plan['product_id']
    return {node['code'] for node, urls in _gallery_writes(plan) for entry in queue
            if entry['product_id'] == pid and (entry.get('variant_code') or entry.get('product_code')) == node['code']
            and entry['url'] in urls}


def _defer_gallery(plan, waiting):
    if not waiting:
        return
    patch = plan['payload']
    for node in [patch, *patch.get('variants', [])]:
        if node['code'] in waiting:
            node.pop('images', None)
            node.pop('image', None)
    if 'variants' in patch:
        patch['variants'] = [row for row in patch['variants'] if row.keys() - {'code'}]
        if not patch['variants']:
            del patch['variants']
    plan['issues'].append('Obrázky už sú vo fronte Upgates: ' + ', '.join(sorted(waiting)))
    plan['deferred'].append('images')
    plan['gallery_wait'] = True
    if not patch.keys() - {'code'}:
        plan['status'] = 'partial'


def prepare_content(documents, fingerprint, expected_ids):
    """Return specific ID-anchored diffs; no write is performed in this phase."""
    selected, observed = _read_documents(documents, fingerprint, expected_ids)
    result = {}
    for key, doc in selected.items():
        live = observed.get(key)
        try:
            plan = _assess(doc, live, expected_ids[key])
            if plan['status'] == 'ready':
                plan['before'] = plain(live)
            result[key] = plan
        except ReviewError as exc:
            result[key] = _problem(exc.code, live)
        except (KeyError, TypeError, ValueError):
            result[key] = _problem('existing_content_incomplete_native_data', live)
    galleries = [p for p in result.values() if p['status'] == 'ready' and _gallery_writes(p)]
    if galleries:
        with native._connection(fingerprint) as client:
            queue = _download_queue(client, {p['product_id'] for p in galleries})
        for plan in galleries:
            _defer_gallery(plan, _waiting(plan, queue))
    return result


def _validate_patch(plan):
    root = plan['payload']
    allowed = {'code', 'active_yn', 'weight', 'prices', 'availability_id', 'labels', 'parameters', 'images'}
    _require(set(root) <= allowed | {'vats', 'manufacturer', 'related', 'categories', 'descriptions', 'variants'}
             and root['code'] == plan['before']['code'], 'existing_content_invalid_patch')
    targeted = {r['code'] for r in plan['before']['variants']
                if r['variant_id'] in plan['anchors']['variant_ids'].values()}
    entire_family = (plan['anchors'].get('allow_parent_write') is True
                     and len(targeted) == len(plan['before']['variants']))
    if not entire_family:
        _require(not root.keys() - {'code', 'variants'} and not any('main_yn' in v for v in root.get('variants', [])),
                 'existing_content_scope_changed')
    for leaf in root.get('variants', []):
        _require(set(leaf) <= allowed | {'image'} and leaf['code'] in targeted, 'existing_content_invalid_patch')
    # Also validate every nested key through the existing production import guard.
    from inventory_hub.services.catalog_import import _assert_payload
    _assert_payload({**root, 'active_yn': bool(root.get('active_yn'))}, expected_active=True)


def send_content(plans, fingerprint):
    """Re-read the full pinned family, then dispatch each eligible SKU at most once."""
    _require(isinstance(plans, dict) and 0 < len(plans) <= native.MAX_BATCH, 'new_batch_limit')
    _require(all(p.get('status') == 'ready' and p.get('before') for p in plans.values()),
             'existing_content_invalid_plan')
    _require(sum(max(1, len(p['payload'].get('variants', []))) for p in plans.values()) <= native.MAX_BATCH,
             'new_batch_limit')
    ids = {code: plan['anchors'] for code, plan in plans.items()}
    result, patches = {}, []
    with native._before_write(fingerprint) as client:
        current = native._read(client, plans, ids)
        galleries = {p['product_id'] for p in plans.values() if _gallery_writes(p)}
        queue = _download_queue(client, galleries)
        refs = {target for p in plans.values()
                for target in set(p['payload'].get('related', [])) - set(p['before']['related'])}
        targets = native._sparse(client, refs, 'products/simple', filters={'variants_yn': 'false'}) if refs else []
        present = {p['code'] for p in targets}
        _require(present <= refs, 'new_native_filter_unconfirmed')
        for key, plan in plans.items():
            _validate_patch(plan)
            missing = set(plan['payload'].get('related', [])) - set(plan['before']['related']) - present
            if plain(current.get(key)) != plan['before']:
                result[key] = dict(status='conflict', issues=['Natívna rodina sa od náhľadu zmenila.'])
            elif missing:
                result[key] = dict(status='conflict', issues=['Chýbajú živé súvisiace produkty: ' + ', '.join(sorted(missing))])
            elif _waiting(plan, queue):
                result[key] = dict(status='conflict', issues=['Obrázok sa po náhľade už zaradil do frontu Upgates.'])
            else:
                patches.append(plan['payload'])
                result[key] = dict(status='sent', issues=[])
    if patches:
        _require(len(json.dumps(patches, ensure_ascii=False).encode()) <= native.MAX_REQUEST_BYTES, 'new_native_payload_limit')
        message = update_products(patches, fingerprint)
        if message:
            for outcome in result.values():
                if outcome['status'] == 'sent':
                    outcome['issues'].append(message)
    return result


def _outstanding(before, after):
    original = {before['code']: before, **_map_by(before.get('variants', []), 'code')}
    current = {after['code']: after, **_map_by(after.get('variants', []), 'code')}
    return any((original[k].keys() & current[k].keys()) - {'code', 'variants'} for k in original.keys() & current.keys())


def observe_content(documents, fingerprint, plans):
    """Verify the committed edit and every protected value after a success or a lost acknowledgement."""
    selected, found = _read_documents(documents, fingerprint, {k: p['anchors'] for k, p in plans.items()})
    result = {}
    for key, doc in selected.items():
        plan, live = plans[key], found.get(key)
        try:
            record = _assess(doc, live, plan['anchors'])
            _require(_protected(plan['before'], plan) == _protected(live, plan), 'existing_content_protected_field_changed')
            pending = record['payload']
            fields = sorted((set(pending) - {'code', 'variants'}) | {
                v['code'] + '/' + field for v in pending.get('variants', []) for field in v.keys() - {'code'}
            })
            if record['status'] == 'ready':
                record['status'] = 'partial'
                record['issues'].append('Nepotvrdené polia: ' + ', '.join(fields))
                if 'images' in pending or any('images' in v for v in pending.get('variants', [])):
                    record['deferred'].append('images')
            record['tranche_verified'] = not _outstanding(plan['payload'], pending)
            record['remaining_fields'] = fields
            record['remaining_content'] = bool(fields and 'content_tranche' in plan.get('deferred', [])
                                               and record['tranche_verified'])
            if record['remaining_content'] and 'content_tranche' not in record['deferred']:
                record['deferred'].append('content_tranche')
            record.pop('payload', None)
            result[key] = record
        except ReviewError as exc:
            result[key] = _problem(exc.code, live)
        except (KeyError, TypeError, ValueError):
            result[key] = _problem('existing_content_incomplete_native_data', live)
    return result
