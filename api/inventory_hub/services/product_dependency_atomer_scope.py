"""Read the selected Atomer leaves and actual current-family write anchors."""
from inventory_hub.services import product_dependency_scope as s


DOCUMENT = set('payload availability flags related missing_related seo_keywords ambiguous_source_references missing_source_references variant_axis_origins missing_categories admin_price_changes title_from_code barcode_corrections source_barcode_placeholders description_sources manufacturer_origin source_manufacturer_hint unspecified_weights source_parent_barcode shipping_weight_rounding partial_vat new_parent_only source_bonus_points missing_source_features variant_main_origin source_family_codes deferred_leaves source_duplicate_ids source_parent_code_alias parent_vat_from_leaves managed_label_ids issues vat_create_only extension extension_plan remaining_create_variants'.split())
RECEIPT = set('dispatched ack status issues deferred identity_verified verified_fields product_id variants cards content_requested content_result content_codes content_anchors content_attempts content_intent related_intent catalog_deferred rollover previous_stage previous_guards previous_receipt previous_document adopt_content reexpand rebuild_after_observation isolate isolate_phase tranche_verified remaining_content'.split())
EVIDENCE = set('status reason card_id hub_sku xtrek biketrek ean scope source_state verification_scope content_pending content_product_id card_action biketrek_check barcode_evidence source_card'.split())
DETAIL = set('code card_state family_key variant_label family_title shop_url shop_admin_url kind state parent name ean hub_id hub_sku candidates target_code target_parent target_ean product_id variant_id match reasons bike_state bike source_state'.split())
NATIVE = {'product_id', 'variant_id', 'code', 'parent', 'link_id'}


def _selected_code(proof, value, path):
    value = s.code(value, path)
    if (value.casefold() in proof.scope.skus | proof.scope.aliases
            or any(value.casefold() == candidate for _, candidate in proof.scope.sales)):
        proof.hit(path, 'selected_atomer_source_code', value)


def _native(proof, value, namespace, path, *, card_id, claims, parent_effect=False):
    s.obj(value, NATIVE, ('product_id', 'code'), path)
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
    elif value.get('parent'):
        proof.context(path + '/parent', 'historical_other_shop_container_exact_leaf_owner_checked')


def inspect(proof, raw, family, campaign, claims):
    guards = s.obj(raw.get('guards'), {'group', 'rows', 'scope', 'own', 'excluded', 'blocked'},
                   ('group', 'rows', 'scope', 'own', 'excluded', 'blocked'), '/guards')
    rows = s.obj(guards['rows'])
    excluded, blocked, scopes = (s.obj(guards[key]) for key in ('excluded', 'blocked', 'scope'))
    if not rows or len(rows) > s.MAX_ITEMS or not (set(excluded) | set(blocked)) <= set(rows) or set(scopes) != set(rows):
        raise s.Unverified('atomer_selection_unverified', '/guards')
    if any(value not in ('default', 'both', 'xtrek', 'biketrek', None) for value in scopes.values()):
        raise s.Unverified('atomer_shop_scope_unknown', '/guards/scope')
    if any(not isinstance(value, str) for value in [*excluded.values(), *blocked.values()]):
        raise s.Unverified('atomer_exclusion_shape_unknown', '/guards')
    for owned in s.array(guards['own'], '/guards/own'):
        if (not isinstance(owned, list) or len(owned) != 3 or type(owned[2]) is not int or owned[2] < 0
                or not ((owned[0] == 'item' and owned[1] in rows)
                        or (owned[0] == 'family' and owned[1] == raw['family_key']))):
            raise s.Unverified('atomer_review_owner_scope_unknown', '/guards/own')
    # Dictionary KEYS, not values, are the executor's selected source codes.
    selected = [value for value in rows if value not in excluded and value not in blocked]
    for value in selected:
        _selected_code(proof, value, '/guards/rows/' + value)
    if (campaign is None or campaign.get('id') != raw.get('campaign') or not family
            or set(family) != set(rows)):
        raise s.Unverified('atomer_current_family_missing', '/guards')
    if campaign.get('state') not in ('paused', 'waiting', 'running', 'review', 'done', 'source_changed'):
        raise s.Unverified('atomer_campaign_state_unknown', '/campaign')
    for value, row in family.items():
        expected = [row.get('revision') or 0, row.get('status') or 'open', row.get('scope') or 'default']
        if rows[value] != expected:
            raise s.Unverified('atomer_review_guard_changed', '/guards/rows')
        if guards['group'] != row['family_guard']:
            raise s.Unverified('atomer_family_review_guard_changed', '/guards/group')
    receipt = s.obj(raw.get('receipt'), RECEIPT, path='/receipt')
    document = raw.get('document')
    if document is not None:
        s.obj(document, DOCUMENT, ('payload',), '/document')
    cards = s.obj(receipt.get('cards', {}), path='/receipt/cards')
    if not set(cards) <= set(rows):
        raise s.Unverified('atomer_card_scope_changed', '/receipt/cards')
    pending_parent = raw.get('stage') in ('fidelity', 'updating', 'related')
    for value in rows:
        at = '/family/' + value
        if value not in selected:
            proof.context(at, 'excluded_or_blocked_sibling_still_inspected')
        detail = s.obj(family[value].get('detail'), DETAIL, ('code', 'kind'), at)
        if detail['code'] != value or detail['kind'] not in ('variant', 'standalone'):
            raise s.Unverified('atomer_source_leaf_changed', at)
        proof.hub(detail.get('hub_id'), at + '/hub_id')
        for candidate in s.array(detail.get('candidates', []), at + '/candidates'):
            proof.hub(candidate, at + '/candidates')
        if detail.get('hub_sku'):
            proof.leaf(detail['hub_sku'], None, at + '/hub_sku', canonical=True)
        for key in ('ean', 'target_ean'):
            if detail.get(key):
                proof.eans([detail[key]], at + '/' + key)
        if detail.get('target_code'):
            proof.leaf(detail['target_code'], 'xtrek', at + '/target_code')
        if detail.get('product_id'):
            if (detail['kind'] == 'variant') != bool(detail.get('variant_id')):
                raise s.Unverified('atomer_native_leaf_kind_changed', at)
            proof.native_leaf('xtrek', detail['product_id'], detail.get('variant_id'), at)
            if pending_parent and scopes[value] != 'biketrek':
                proof.parent_write('xtrek', detail.get('target_parent') or detail.get('target_code'), detail['product_id'], at)
        saved = cards.get(value)
        if saved:
            s.obj(saved, {'state', 'evidence'}, ('state', 'evidence'), '/receipt/cards/' + value)
            evidence = s.obj(saved['evidence'], EVIDENCE, path='/receipt/cards/' + value + '/evidence')
            card_id = s.positive(evidence.get('card_id'), nullable=True)
            proof.hub(card_id, '/receipt/cards/' + value + '/evidence/card_id')
            if evidence.get('ean'):
                proof.eans([evidence['ean']], '/receipt/cards/' + value + '/evidence/ean')
            if evidence.get('hub_sku'):
                proof.leaf(evidence['hub_sku'], None, '/receipt/cards/' + value + '/evidence/hub_sku', canonical=True)
            for namespace in ('biketrek', 'xtrek'):
                if evidence.get(namespace):
                    _native(proof, evidence[namespace], namespace, '/receipt/cards/' + value + '/evidence/' + namespace,
                            card_id=card_id, claims=claims, parent_effect=namespace == 'xtrek' and pending_parent)
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
            for value, leaf in s.obj(extension['existing']).items():
                proof.leaf(value, 'xtrek', '/document/extension/existing')
                proof.native_leaf('xtrek', leaf.get('product_id', pid), leaf.get('variant_id'), '/document/extension/existing')
        s.payload(proof, document['payload'], 'xtrek', '/document/payload',
                  parent_update=bool(extension) or pending_parent, pid=pid)
        if document.get('remaining_create_variants'):
            s.payload(proof, {'code': document['payload']['code'], 'variants': document['remaining_create_variants']},
                      'xtrek', '/document/remaining_create_variants', parent_update=bool(extension), pid=pid)
        for value in s.array(document.get('source_family_codes', []), '/document/source_family_codes'):
            if value in selected:
                _selected_code(proof, value, '/document/source_family_codes')
        if document.get('extension_plan'):
            raise s.Unverified('atomer_extension_intent_requires_separate_proof', '/document/extension_plan')
    anchors = receipt.get('content_anchors')
    if anchors:
        s.obj(anchors, {'product_id', 'product_code', 'variant_ids', 'variant_codes', 'allow_parent_write'},
              ('product_id', 'product_code', 'variant_ids', 'variant_codes'), '/receipt/content_anchors')
        proof.parent_write('xtrek', anchors['product_code'], anchors['product_id'], '/receipt/content_anchors')
        for key, vid in s.obj(anchors['variant_ids']).items():
            if key not in anchors['variant_codes']:
                raise s.Unverified('atomer_content_scope_incomplete', '/receipt/content_anchors')
            proof.native_leaf('xtrek', anchors['product_id'], vid, '/receipt/content_anchors/variant_ids')
            proof.leaf(anchors['variant_codes'][key], 'xtrek', '/receipt/content_anchors/variant_codes')
    if receipt.get('content_intent'):
        # Already-sending content is deliberately not reinterpreted from a
        # truncated plan. An unknown/pending physical effect keeps the hold.
        raise s.Unverified('atomer_content_intent_pending', '/receipt/content_intent')
    for value in s.array(receipt.get('content_codes', []), '/receipt/content_codes'):
        if value not in rows:
            raise s.Unverified('atomer_content_code_outside_family', '/receipt/content_codes')
        _selected_code(proof, value, '/receipt/content_codes')
    corrections = (campaign.get('selection') or {}).get('barcode_corrections', {})
    for value in selected:
        correction = corrections.get(value)
        if correction:
            s.obj(correction)
            if correction.get('ean'):
                proof.eans([correction['ean']], '/campaign/selection/barcode_corrections/' + value)
