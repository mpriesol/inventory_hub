"""Read the selected Atomer leaves and actual current-family write anchors."""
import re

from inventory_hub.services import product_dependency_scope as s
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.services.product_link_source import _url


DOCUMENT = set('payload availability flags related missing_related seo_keywords ambiguous_source_references missing_source_references variant_axis_origins missing_categories admin_price_changes title_from_code barcode_corrections source_barcode_placeholders description_sources manufacturer_origin source_manufacturer_hint unspecified_weights source_parent_barcode shipping_weight_rounding partial_vat new_parent_only source_bonus_points missing_source_features variant_main_origin source_family_codes deferred_leaves source_duplicate_ids source_parent_code_alias parent_vat_from_leaves managed_label_ids issues vat_create_only extension extension_plan remaining_create_variants'.split())
RECEIPT = set('dispatched ack status issues deferred identity_verified verified_fields product_id variants cards content_requested content_result content_codes content_anchors content_attempts content_intent related_intent catalog_deferred rollover previous_stage previous_guards previous_receipt previous_document adopt_content reexpand rebuild_after_observation isolate isolate_phase tranche_verified remaining_content'.split())
EVIDENCE = set('status reason card_id hub_sku xtrek biketrek ean scope source_state verification_scope content_pending content_product_id card_action biketrek_check barcode_evidence source_card'.split())
DETAIL = set('code card_state family_key variant_label family_title family_name shop_url shop_admin_url kind state parent name ean hub_id hub_sku candidates target_code target_parent target_ean product_id variant_id match reasons bike_state bike source_state availability_review'.split())
NAVIGATION = ('shop_url', 'shop_admin_url')
NATIVE = {'product_id', 'variant_id', 'code', 'parent', 'link_id'} | set(NAVIGATION)
REVIEW_SCOPES = ('default', 'both', 'xtrek', 'biketrek')
# Known raw report/source spellings, never normalized into barcodes. Numeric
# component lengths are bounded so a new GTIN/GTIN separator is not accepted
# as a manufacturer article. The shared compound splitter remains unchanged.
RAW_TOKEN = re.compile(r"(?:'[0-9]{13}|[0-9]{13}\."
    r"|[0-9]{6}\.[0-9]{3}\.[0-9]{2}"
    r"|[A-Z]{2}[A-Z0-9]-[0-9]{4}(?:-[0-9]{3})?"
    r"|[A-Z]{2}-[0-9]{4}[A-Z]{2}-[0-9]{3}[a-z0-9]{1,3})")


def _review_guard(value, path):
    if (not isinstance(value, list) or len(value) != 3 or type(value[0]) is not int or value[0] < 0
            or value[1] not in ('open', 'queued', 'working', 'blocked', 'done') or value[2] not in REVIEW_SCOPES):
        raise s.Unverified('atomer_review_guard_shape_unknown', path)
    return value


def _selection(value, family_key):
    guards = s.obj(value, {'group', 'rows', 'scope', 'own', 'excluded', 'blocked'},
                   ('group', 'rows', 'scope', 'own', 'excluded', 'blocked'), '/guards')
    group = _review_guard(guards['group'], '/guards/group')
    rows = s.obj(guards['rows'], path='/guards/rows')
    excluded, blocked, scopes = (s.obj(guards[key], path='/guards/' + key) for key in ('excluded', 'blocked', 'scope'))
    if not rows or len(rows) > s.MAX_ITEMS or not (set(excluded) | set(blocked)) <= set(rows) or set(scopes) != set(rows):
        raise s.Unverified('atomer_selection_unverified', '/guards')
    for code, row in rows.items():
        s.code(code, '/guards/rows')
        _review_guard(row, '/guards/rows/' + code)
        if scopes[code] not in REVIEW_SCOPES:
            raise s.Unverified('atomer_shop_scope_unknown', '/guards/scope/' + code)
        if scopes[code] != (row[2] if row[2] != 'default' else group[2]):
            raise s.Unverified('atomer_effective_scope_changed', '/guards/scope/' + code)
    if any(not isinstance(value, str) for value in [*excluded.values(), *blocked.values()]):
        raise s.Unverified('atomer_exclusion_shape_unknown', '/guards')
    seen = set()
    for owned in s.array(guards['own'], '/guards/own'):
        if (not isinstance(owned, list) or len(owned) != 3 or type(owned[2]) is not int or owned[2] < 0
                or not isinstance(owned[1], str) or not ((owned[0] == 'item' and owned[1] in rows)
                        or (owned[0] == 'family' and owned[1] == family_key))):
            raise s.Unverified('atomer_review_owner_scope_unknown', '/guards/own')
        key = (owned[0], owned[1])
        guard = group if owned[0] == 'family' else rows[owned[1]]
        if key in seen or guard[0] != owned[2] or guard[1] != 'working':
            raise s.Unverified('atomer_review_owner_scope_unknown', '/guards/own')
        seen.add(key)
    return guards


def _current_reviews(family, guards):
    """Read every current guard before comparing; a mismatch is not a schema exemption."""
    current, groups, scopes = {}, [], {}
    for code, row in family.items():
        at = '/family/' + code
        s.obj(row, {'detail', 'revision', 'status', 'scope', 'family_guard'}, ('detail', 'family_guard'), at)
        current[code] = _review_guard([row.get('revision') if row.get('revision') is not None else 0,
            row.get('status') if row.get('status') is not None else 'open',
            row.get('scope') if row.get('scope') is not None else 'default'], at + '/review')
        group = _review_guard(row['family_guard'], at + '/family_guard')
        if groups and groups[0] != group:
            raise s.Unverified('atomer_family_review_guard_inconsistent', at + '/family_guard')
        groups.append(group)
        scopes[code] = current[code][2] if current[code][2] != 'default' else group[2]
    return current != guards['rows'] or groups[0] != guards['group'], scopes


def _native_summary(proof, value, path, rows, *, variants_key='variants'):
    """The executor uses receipt and acknowledgement IDs after failed or partial reads too."""
    pid = s.positive(value.get('product_id'), path + '/product_id', nullable=True)
    variants = s.obj(value.get(variants_key, {}), path=path + '/' + variants_key)
    if not set(variants) <= set(rows) or variants and pid is None:
        raise s.Unverified('atomer_native_receipt_scope_unverified', path)
    if pid is not None:
        # This frozen parent can be observed, completed or used by a later
        # content phase. Neither current status nor a new scope erases it.
        proof.parent_write('xtrek', None, pid, path + '/product_id')
    for code, vid in variants.items():
        proof.leaf(code, 'xtrek', path + '/' + variants_key)
        proof.native_leaf('xtrek', pid, s.positive(vid, path + '/' + variants_key + '/' + code),
                          path + '/' + variants_key + '/' + code)


def _barcodes(proof, value, path, *, source_code):
    """Audit the original scalar, without promoting a legacy token to a GTIN.

    Progress reports preserve raw source/target barcodes even when the card
    writer rejects them. Use its shared splitter, never a digit-extraction
    regex; an unsupported separator cannot hide a possible compound member.
    """
    s.code(value, path, blank=True)
    if not value:
        return
    parts = ProductIdentifierService.split_compound_ean(value)
    if not parts or len(parts) > 50:
        raise s.Unverified('atomer_barcode_tokens_unverified', path)
    tokens = [token for token, _ in parts]
    repeats_sku = (value == source_code and re.fullmatch(r'[A-Za-z0-9_.-]+', value)
                   and any(char.isalpha() for char in value))
    if not repeats_sku and any(not (re.fullmatch(r'[A-Za-z0-9]+', token)
            or RAW_TOKEN.fullmatch(token) or token.isalpha()) for token in tokens):
        raise s.Unverified('atomer_barcode_tokens_unverified', path)
    for token in dict.fromkeys([value, *tokens]):
        if token.casefold() in proof.scope.aliases or token.casefold() in proof.scope.skus:
            proof.hit(path, 'atomer_raw_barcode', token)
    valid = list(s.verified_barcodes(tokens))
    proof.eans(valid, path)
    if tokens != valid or len(tokens) != 1 or value != tokens[0]:
        proof.context(path, 'audited_atomer_raw_barcode_tokens')


def _barcode_correction(proof, value, path, *, source_code, saved=False):
    allowed = {'before', 'ean', 'parent', 'manufacturer_or_supplier', 'evidence', 'required_card_id', 'required_atomer_id'}
    s.obj(value, allowed | ({'type', 'native_id'} if saved else set()), path=path)
    if value.get('ean') is not None:
        proof.eans([value['ean']], path + '/ean')
    if value.get('before') is not None:
        _barcodes(proof, value['before'], path + '/before', source_code=source_code)
    proof.hub(value.get('required_card_id'), path + '/required_card_id')


def _selected_code(proof, value, path):
    value = s.code(value, path)
    if (value.casefold() in proof.scope.skus | proof.scope.aliases
            or any(value.casefold() == candidate for _, candidate in proof.scope.sales)):
        proof.hit(path, 'selected_atomer_source_code', value)


def _native(proof, value, namespace, path, *, card_id, claims, parent_effect=False):
    s.obj(value, NATIVE, ('product_id', 'code'), path)
    for key in NAVIGATION:
        if value.get(key) is not None:
            if _url(value[key]) != value[key]:
                raise s.Unverified('atomer_navigation_shape_unknown', path + '/' + key)
            proof.context(path + '/' + key, 'known_shop_navigation_url')
    pid, vid = s.positive(value['product_id']), s.positive(value.get('variant_id'), nullable=True)
    proof.native_leaf(namespace, pid, vid, path)
    proof.leaf(value['code'], namespace, path + '/code')
    proof.mapping(value.get('link_id'), path + '/link_id')
    link_id = s.positive(value.get('link_id'), nullable=True)
    claim = claims.get(link_id)
    if (claim is None or claim['product_id'] != card_id or claim['shop_code'] != namespace
            or str(claim.get('external_product_id')) != str(pid)
            or str(claim.get('external_variant_id') or '') != str(vid or '')
            or claim.get('external_code') != value['code'] or claim.get('retired_at') is not None
            or claim.get('is_variant') != (vid is not None)):
        raise s.Unverified('atomer_saved_mapping_lineage_unverified', path)
    if parent_effect:
        proof.parent_write(namespace, claim.get('parent_code') or value['code'], pid, path)
        if value.get('parent'):
            proof.parent_write(namespace, value['parent'], pid, path + '/parent')
    elif value.get('parent'):
        proof.context(path + '/parent', 'historical_other_shop_container_exact_leaf_owner_checked')


def _native_kind(proof, detail, family_key, path):
    """The report stores source kind and an independently observed target leaf.

    A recorded kind_differs review is still a conflict for the migration
    writer. Its exact native scope remains knowable, including parent writes.
    """
    pid = s.positive(detail['product_id'], path + '/product_id')
    vid = s.positive(detail.get('variant_id'), path + '/variant_id', nullable=True)
    if (detail['kind'] == 'variant') != (vid is not None):
        parent = s.code(detail.get('parent'), path + '/parent', blank=True)
        variant = bool(parent and parent != detail['code'])
        expected = ('v:' + parent) if variant else ('s:' + detail['code'])
        reasons = s.array(detail.get('reasons'), path + '/reasons', nonempty=True)
        if (variant != (detail['kind'] == 'variant') or family_key != expected
                or detail.get('state') != 'review' or 'kind_differs' not in reasons
                or any(not isinstance(reason, str) for reason in reasons)):
            raise s.Unverified('atomer_native_leaf_kind_changed', path)
        s.code(detail.get('target_code'), path + '/target_code')
        target_parent = detail.get('target_parent')
        if (vid is not None and not target_parent or vid is None and target_parent not in (None, '')
                or target_parent is not None and not isinstance(target_parent, str)):
            raise s.Unverified('atomer_native_leaf_kind_changed', path)
        if target_parent:
            s.code(target_parent, path + '/target_parent')
        proof.context(path, 'reported_atomer_source_native_kind_conflict')
    return pid, vid


def _availability_review(proof, detail, path):
    """Read the exact unresolved label pair emitted by apply_availability_reviews.

    The generation writer records this only for an exact native variant whose
    source code is unchanged. Labels add no identity; the ordinary leaf, owner
    and possible parent-write checks still run below.
    """
    if 'availability_review' not in detail:
        return
    at = path + '/availability_review'
    value = s.obj(detail['availability_review'], {'atomer', 'xtrek'}, ('atomer', 'xtrek'), at)
    expected = s.code(value['atomer'], at + '/atomer')
    actual = value['xtrek']
    if actual is not None:
        s.code(actual, at + '/xtrek', blank=True)
    reasons = s.array(detail.get('reasons'), path + '/reasons', nonempty=True)
    if (detail.get('kind') != 'variant' or detail.get('state') != 'review'
            or detail.get('target_code') != detail['code']
            or 'availability_differs' not in reasons or any(not isinstance(reason, str) for reason in reasons)
            or (actual or '').casefold() == expected.casefold()):
        raise s.Unverified('atomer_availability_review_unverified', at)
    s.positive(detail.get('product_id'), path + '/product_id')
    s.positive(detail.get('variant_id'), path + '/variant_id')
    proof.context(at, 'recorded_atomer_availability_conflict')


def inspect(proof, raw, family, campaign, claims, head):
    if raw.get('stage') not in ('pending', 'prepared', 'sending', 'observe', 'cards', 'related',
                              'fidelity', 'updating', 'partial', 'blocked', 'verified', 'excluded'):
        raise s.Unverified('atomer_job_stage_unknown', '/stage')
    guards = _selection(raw.get('guards'), raw['family_key'])
    rows, scopes = guards['rows'], guards['scope']
    # Dictionary KEYS, not values, are the executor's selected source codes.
    selected = [value for value in rows if value not in guards['excluded'] and value not in guards['blocked']]
    for value in selected:
        _selected_code(proof, value, '/guards/rows/' + value)
    if (campaign is None or campaign.get('id') != raw.get('campaign') or not family
            or set(family) != set(rows)):
        raise s.Unverified('atomer_current_family_missing', '/guards')
    if campaign.get('state') not in ('paused', 'waiting', 'running', 'review', 'done', 'source_changed'):
        raise s.Unverified('atomer_campaign_state_unknown', '/campaign')
    # _lock_current selects this latest header before every next family read.
    # Its complete row is frozen alongside the campaign and current family.
    if (head is None or campaign.get('checked_run_id') != head.get('id')
            or (campaign.get('source_sha'), campaign.get('admin_sha'))
                != (head.get('source_sha256'), head.get('admin_export_sha256'))):
        raise s.Unverified('atomer_current_run_changed', '/campaign/checked_run_id')
    review_changed, current_scopes = _current_reviews(family, guards)
    potential = set(rows) if review_changed else set(selected)
    if review_changed:
        # _finish advances reviews while retaining the frozen guards. Recheck
        # can claim again, preserving old receipts/intents. Prove the union of
        # both finite scopes, including formerly blocked/excluded siblings.
        proof.context('/guards', 'saved_and_current_atomer_review_scope_union')
        for value in rows:
            _selected_code(proof, value, '/family/' + value + '/code')
    parent_codes = {value for value in potential if scopes[value] != 'biketrek'
                    or review_changed and current_scopes[value] != 'biketrek'}
    receipt = s.obj(raw.get('receipt'), RECEIPT, path='/receipt')
    for key in ('previous_guards', 'previous_receipt', 'previous_document'):
        if receipt.get(key) is not None:
            # No current writer emits this older nested history. The current
            # mapping closure cannot prove all of its prior claims: never drop
            # it just because a newer review is disjoint.
            raise s.Unverified('atomer_frozen_history_unverified', '/receipt/' + key)
    document = raw.get('document')
    if document is not None:
        s.obj(document, DOCUMENT, ('payload',), '/document')
    cards = s.obj(receipt.get('cards', {}), path='/receipt/cards')
    if not set(cards) <= set(rows):
        raise s.Unverified('atomer_card_scope_changed', '/receipt/cards')
    _native_summary(proof, receipt, '/receipt', rows)
    if receipt.get('ack') is not None:
        ack = s.obj(receipt['ack'], {'product_id', 'variant_ids', 'issues', 'inserted', 'status'}, path='/receipt/ack')
        _native_summary(proof, ack, '/receipt/ack', rows, variants_key='variant_ids')
    if receipt.get('content_result') is not None:
        result = s.obj(receipt['content_result'], {'status', 'identity_verified', 'verified_fields', 'product_id',
            'variants', 'issues', 'deferred'}, path='/receipt/content_result')
        _native_summary(proof, result, '/receipt/content_result', rows)
    pending_parent = raw.get('stage') in ('fidelity', 'updating', 'related')
    for value in rows:
        at = '/family/' + value
        if value not in selected:
            proof.context(at, 'excluded_or_blocked_sibling_still_inspected')
        detail = s.obj(family[value].get('detail'), DETAIL, ('code', 'kind'), at)
        if (detail['code'] != value or detail['kind'] not in ('variant', 'standalone')
                or 'family_key' in detail and detail['family_key'] != raw['family_key']):
            raise s.Unverified('atomer_source_leaf_changed', at)
        if 'family_name' in detail and not isinstance(detail['family_name'], str):
            raise s.Unverified('atomer_family_name_shape_unknown', at + '/family_name')
        _availability_review(proof, detail, at)
        proof.hub(detail.get('hub_id'), at + '/hub_id')
        for candidate in s.array(detail.get('candidates', []), at + '/candidates'):
            proof.hub(candidate, at + '/candidates')
        if detail.get('hub_sku'):
            proof.leaf(detail['hub_sku'], None, at + '/hub_sku', canonical=True)
        for key in ('ean', 'target_ean'):
            if detail.get(key) is not None:
                _barcodes(proof, detail[key], at + '/' + key, source_code=value)
        if detail.get('target_code'):
            proof.leaf(detail['target_code'], 'xtrek', at + '/target_code')
        if detail.get('product_id') is not None:
            pid, vid = _native_kind(proof, detail, raw['family_key'], at)
            proof.native_leaf('xtrek', pid, vid, at)
            if value in parent_codes or pending_parent and scopes[value] != 'biketrek':
                proof.parent_write('xtrek', detail.get('target_parent') or detail.get('target_code'), detail['product_id'], at)
        elif (detail.get('variant_id') is not None or detail.get('target_code') not in (None, '')
              or detail.get('target_parent') not in (None, '')):
            raise s.Unverified('atomer_native_leaf_incomplete', at)
        saved = cards.get(value)
        if saved is not None:
            s.obj(saved, {'state', 'evidence'}, ('state', 'evidence'), '/receipt/cards/' + value)
            evidence = s.obj(saved['evidence'], EVIDENCE, path='/receipt/cards/' + value + '/evidence')
            if evidence.get('source_card') is not None:
                raise s.Unverified('atomer_frozen_source_card_unverified', '/receipt/cards/' + value + '/evidence/source_card')
            if evidence.get('barcode_evidence') is not None:
                _barcode_correction(proof, evidence['barcode_evidence'],
                                    '/receipt/cards/' + value + '/evidence/barcode_evidence', source_code=value, saved=True)
            card_id = s.positive(evidence.get('card_id'), nullable=True)
            proof.hub(card_id, '/receipt/cards/' + value + '/evidence/card_id')
            if evidence.get('ean') is not None:
                _barcodes(proof, evidence['ean'], '/receipt/cards/' + value + '/evidence/ean', source_code=value)
            if evidence.get('hub_sku'):
                proof.leaf(evidence['hub_sku'], None, '/receipt/cards/' + value + '/evidence/hub_sku', canonical=True)
            for namespace in ('biketrek', 'xtrek'):
                if evidence.get(namespace):
                    _native(proof, evidence[namespace], namespace, '/receipt/cards/' + value + '/evidence/' + namespace,
                            card_id=card_id, claims=claims,
                            parent_effect=namespace == 'xtrek' and (pending_parent or value in parent_codes))
            if evidence.get('content_product_id'):
                proof.parent_write('xtrek', None, evidence['content_product_id'], '/receipt/cards/' + value + '/evidence/content_product_id')
        elif pending_parent and value in selected:
            raise s.Unverified('atomer_pending_content_owner_unverified', '/receipt/cards')
    if document:
        extension = document.get('extension')
        pid = receipt.get('product_id')
        if extension:
            s.obj(extension, {'product_id', 'existing_report_run', 'existing'}, ('product_id', 'existing'), '/document/extension')
            pid = extension['product_id']
            existing = s.obj(extension['existing'], path='/document/extension/existing')
            if not set(existing) <= set(rows):
                raise s.Unverified('atomer_extension_scope_unverified', '/document/extension/existing')
            for value, leaf in existing.items():
                s.obj(leaf, {'product_id', 'variant_id'}, ('variant_id',), '/document/extension/existing/' + value)
                proof.leaf(value, 'xtrek', '/document/extension/existing')
                proof.native_leaf('xtrek', leaf.get('product_id', pid), leaf.get('variant_id'), '/document/extension/existing')
        s.payload(proof, document['payload'], 'xtrek', '/document/payload',
                  parent_update=bool(extension) or pending_parent or review_changed, pid=pid)
        if document.get('remaining_create_variants'):
            s.payload(proof, {'code': document['payload']['code'], 'variants': document['remaining_create_variants']},
                      'xtrek', '/document/remaining_create_variants',
                      parent_update=bool(extension) or pending_parent or review_changed, pid=pid)
        for value in s.array(document.get('source_family_codes', []), '/document/source_family_codes'):
            s.code(value, '/document/source_family_codes')
            if value not in rows:
                raise s.Unverified('atomer_document_family_scope_unverified', '/document/source_family_codes')
            _selected_code(proof, value, '/document/source_family_codes')
        if document.get('extension_plan') is not None:
            raise s.Unverified('atomer_extension_intent_requires_separate_proof', '/document/extension_plan')
    anchors = receipt.get('content_anchors')
    if anchors is not None:
        s.obj(anchors, {'product_id', 'product_code', 'variant_ids', 'variant_codes', 'allow_parent_write'},
              ('product_id', 'product_code', 'variant_ids', 'variant_codes'), '/receipt/content_anchors')
        proof.parent_write('xtrek', anchors['product_code'], anchors['product_id'], '/receipt/content_anchors')
        ids = s.obj(anchors['variant_ids'], path='/receipt/content_anchors/variant_ids')
        codes = s.obj(anchors['variant_codes'], path='/receipt/content_anchors/variant_codes')
        if set(ids) != set(codes) or not set(ids) <= set(rows):
            raise s.Unverified('atomer_content_scope_incomplete', '/receipt/content_anchors')
        for key, vid in ids.items():
            proof.native_leaf('xtrek', anchors['product_id'], s.positive(vid, '/receipt/content_anchors/variant_ids'),
                              '/receipt/content_anchors/variant_ids')
            proof.leaf(codes[key], 'xtrek', '/receipt/content_anchors/variant_codes')
    if receipt.get('content_intent') is not None:
        # Already-sending content is deliberately not reinterpreted from a
        # truncated plan. An unknown/pending physical effect keeps the hold.
        raise s.Unverified('atomer_content_intent_pending', '/receipt/content_intent')
    for value in s.array(receipt.get('content_codes', []), '/receipt/content_codes'):
        if value not in rows:
            raise s.Unverified('atomer_content_code_outside_family', '/receipt/content_codes')
        _selected_code(proof, value, '/receipt/content_codes')
    selection = s.obj(campaign.get('selection') or {}, path='/campaign/selection')
    corrections = s.obj(selection.get('barcode_corrections') or {}, path='/campaign/selection/barcode_corrections')
    for value in potential:
        correction = corrections.get(value)
        if correction is not None:
            _barcode_correction(proof, correction, '/campaign/selection/barcode_corrections/' + value, source_code=value)
