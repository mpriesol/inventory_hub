"""Bounded parent-content transport using the shared Upgates connection and budget.

The ordinary writer allows Slovak descriptions and verified parameters. A
separate opt-in helper builds source-price patches for the direct migration.
A verified physical leaf anchors the operation; the complete native family is
frozen and rechecked. Stock remains an observation, never a write input.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP

from inventory_hub.services import stock_publication_source as source
from inventory_hub.services.product_identity import barcode_keys
from inventory_hub.services.product_publication_source import plain
from inventory_hub.services import product_publication_source as publication


TEXT_FIELDS = frozenset(("long_description", "seo_title", "seo_description"))
FIELDS = TEXT_FIELDS | {'parameters'}
MISSING = {"missing": True}
OPERATIONAL_FIELDS = frozenset((
    "stock", "stocks", "stock_position", "availability", "availability_id", "availability_type",
    "variants_stock", "variants_availability", "variants_availability_id", "variants_availability_type",
    "can_add_to_basket_yn", "active_yn", "archived_yn", "limit_orders", "action_currently_yn",
))


def _error(code="identity_changed", status=409):
    return source.SourceError("atomer_content_" + code, status)


def _value(row, key):
    return deepcopy(row[key]) if key in row else deepcopy(MISSING)


def _fields(fields):
    if (not isinstance(fields, (list, tuple)) or not fields
            or any(not isinstance(key, str) or key not in FIELDS for key in fields)
            or len(set(fields)) != len(fields)):
        raise _error("invalid_fields", 422)
    return fields


def _target(identity):
    allowed = source.IDENTITY_KEYS | {"ean", "full_family", "parameters_yn"}
    if not isinstance(identity, dict) or set(identity) - allowed:
        raise _error("invalid_target", 422)
    if 'parameters_yn' in identity and identity['parameters_yn'] is not True:
        raise _error('invalid_target', 422)
    return source._target({key: value for key, value in identity.items() if key in source.IDENTITY_KEYS})


def _descriptions(parent):
    rows = parent.get("descriptions")
    if (not isinstance(rows, list) or any(not isinstance(row, dict)
            or not isinstance(row.get("language"), str) or not row["language"] for row in rows)
            or len({row["language"] for row in rows}) != len(rows)):
        raise _error("descriptions_unavailable", 502)
    selected = [row for row in rows if row["language"] == "sk"]
    if len(selected) != 1:
        raise _error("slovak_description_missing", 422)
    return selected[0]


def _family(parent, target):
    if (parent.get("code") != target["parent_code"] or parent.get("product_id") != target["product_id"]
            or not source._identifier(parent.get("product_id")) or parent.get("set_yn", False) is not False):
        raise _error()
    if "ean" in parent and parent["ean"] is not None and not isinstance(parent["ean"], str):
        raise _error("ean_not_text", 502)
    variants = parent.get("variants")
    if parent.get("variants_exists_yn") is True:
        if not isinstance(variants, list) or not 1 <= len(variants) <= 100:
            raise _error("family_incomplete")
    elif parent.get("variants_exists_yn") is False and variants in (None, []):
        variants = []
    else:
        raise _error("family_incomplete")
    identities, codes, identifiers = [], {parent["code"].casefold()}, set()
    for row in variants:
        if (not isinstance(row, dict) or not source._identifier(row.get("variant_id"))
                or row.get("product_id") != parent["product_id"] or row.get("product_code") != parent["code"]
                or not source._messages_ok(row.get("messages"))):
            raise _error()
        code = source._code(row.get("code"))
        if "ean" in row and row["ean"] is not None and not isinstance(row["ean"], str):
            raise _error("ean_not_text", 502)
        if code.casefold() in codes or row["variant_id"] in identifiers:
            raise _error("family_duplicate")
        codes.add(code.casefold()); identifiers.add(row["variant_id"])
        identities.append({"code": code, "product_id": parent["product_id"],
                           "variant_id": row["variant_id"], "ean": _value(row, "ean")})
    if target["variant_code"] is None:
        if variants or target["code"] != parent["code"]:
            raise _error()
        anchor = parent
    else:
        anchors = [row for row in variants if row["code"] == target["variant_code"]]
        if len(anchors) != 1 or anchors[0]["variant_id"] != target.get("variant_id"):
            raise _error()
        anchor = anchors[0]
    family = {"product_id": parent["product_id"], "code": parent["code"],
              "ean": _value(parent, "ean"), "manufacturer": _value(parent, "manufacturer"),
              "variants": sorted(identities, key=lambda row: row["code"])}
    return family, anchor


def _read(client, identity):
    target = _target(identity)
    payload = source._request(client, "get", "products", params={
        "codes": target["parent_code"], "page": 1, "current_page_items": 100})
    parent = source._one(payload, "products")
    family, anchor = _family(parent, target)
    if "full_family" in identity and family != identity["full_family"]:
        raise _error()
    if "ean" in identity and _value(anchor, "ean") != identity["ean"]:
        raise _error()
    _descriptions(parent)
    if identity.get('parameters_yn'):
        details = source._request(client, 'get', 'products/parameters', params={
            'codes': target['parent_code'], 'page': 1, 'current_page_items': 100})
        detail = source._one(details, 'products')
        if detail.get('code') != parent['code'] or detail.get('product_id') != parent['product_id']:
            raise _error('identity_changed')
        # This first parameter stage is deliberately limited to standalone cards.
        if parent.get('variants') or detail.get('variants'):
            raise _error('parameters_standalone_required', 422)
        from inventory_hub.services.ai_content_update import parameter_rows
        from inventory_hub.services.atomer_parameters import canonical_parameters
        from inventory_hub.adapters.atomer_content import AtomerSourceError
        from inventory_hub.services.catalog import CatalogError
        try:
            parent['parameters'] = canonical_parameters(parameter_rows(detail))
        except (AtomerSourceError, CatalogError):
            raise _error('parameters_unavailable', 502) from None
    frozen = {**target, "variant_id": target.get("variant_id"),
              "ean": _value(anchor, "ean"), "full_family": family}
    if identity.get('parameters_yn'):
        frozen['parameters_yn'] = True
    return plain({"identity": frozen, "parent": parent, "options": None})


def _read_remote(shop, identity, fingerprint):
    client = source._client(shop, fingerprint)
    try:
        return _read(client, identity)
    finally:
        client.session.close()


async def read_remote(shop, identity, fingerprint, prices=False):
    if prices:
        raise _error("invalid_fields", 422)
    return await asyncio.to_thread(_read_remote, shop, identity, fingerprint)


def field_values(remote, fields, options=None):
    _fields(fields)
    description = _descriptions(remote["parent"])
    if any(field in description and description[field] is not None
           and not isinstance(description[field], str) for field in fields if field in TEXT_FIELDS):
        raise _error("descriptions_unavailable", 502)
    values = {field: _value(description, field) for field in fields if field in TEXT_FIELDS}
    if 'parameters' in fields:
        if remote['identity'].get('parameters_yn') is not True or 'parameters' not in remote['parent']:
            raise _error('parameters_unavailable', 502)
        values['parameters'] = deepcopy(remote['parent']['parameters'])
    return values


def compare_values(before, after):
    """Exact SEO and structural HTML equality; text-only equality is insufficient."""
    if not isinstance(before, dict) or not isinstance(after, dict) or set(before) != set(after):
        return False
    from inventory_hub.adapters.atomer_content import html_fingerprint, AtomerSourceError
    for key in before:
        if key not in FIELDS:
            return False
        if before[key] == after[key]:
            continue
        if key == 'parameters':
            from inventory_hub.services.atomer_parameters import canonical_parameters
            try:
                if canonical_parameters(before[key]) != canonical_parameters(after[key]):
                    return False
            except AtomerSourceError:
                return False
            continue
        if key != "long_description" or not isinstance(before[key], str) or not isinstance(after[key], str):
            return False
        try:
            if html_fingerprint(before[key]) != html_fingerprint(after[key]):
                return False
        except AtomerSourceError:
            return False
    return True


def build_patch(values, remote, fields):
    _fields(fields)
    if not isinstance(values, dict) or set(values) != set(fields):
        raise _error("invalid_fields", 422)
    product = {'code': remote['identity']['parent_code']}
    texts = {key: deepcopy(value) for key, value in values.items() if key in TEXT_FIELDS}
    if texts:
        product['descriptions'] = [{'language': 'sk', **texts}]
    if 'parameters' in fields:
        product['parameters'] = deepcopy(values['parameters'])
    payload = {'products': [product]}
    _validate_payload(payload, remote["identity"])
    return {"payload": payload, "before": field_values(remote, fields), "after": deepcopy(values)}


def protected_observation(remote, fields=None):
    """Keep unrelated content separate from concurrently changing operations.

    A differing operational value is evidence to investigate, not permission to
    restore stock or a claim that this content PUT caused the difference.
    """
    fields = list(FIELDS) if fields is None else _fields(fields)
    parent = deepcopy(remote["parent"])
    operations = []
    for row in [parent, *(parent.get("variants") or [])]:
        operations.append({"code": row["code"], **{key: _value(row, key) for key in sorted(OPERATIONAL_FIELDS)}})
        for key in OPERATIONAL_FIELDS:
            row.pop(key, None)
        row.pop("last_update_time", None)
    for row in parent["descriptions"]:
        if row["language"] == "sk":
            for key in fields:
                row.pop(key, None)
    if 'parameters' in fields:
        parent.pop('parameters', None)
    parent["descriptions"] = sorted(parent["descriptions"], key=lambda row: row["language"])
    if isinstance(parent.get("variants"), list):
        parent["variants"] = sorted(parent["variants"], key=lambda row: row["code"])
    return plain({"content": parent, "operations": sorted(operations, key=lambda row: row["code"])})


SOURCE_PRICE_KEYS = ('price_original', 'price_with_vat', 'price_without_vat')


def _source_price_row(parent, options):
    """The explicit direct migration supports only an undiscounted SK/EUR list."""
    if options.get('prices_with_vat') is not True or options.get('currency') != 'EUR':
        raise _error('price_configuration_requires_review', 422)
    languages = [row for row in parent.get('prices', []) if row.get('language') == 'sk']
    if len(languages) != 1 or languages[0].get('currency') != 'EUR':
        raise _error('price_unknown', 422)
    lists = [row for row in languages[0].get('pricelists', []) if row.get('name') == options['pricelist']]
    if len(lists) != 1:
        raise _error('price_unknown', 422)
    return languages[0], lists[0]


def source_price_values(remote, options):
    _, row = _source_price_row(remote['parent'], options)
    return {key: format(publication.money(row.get(key)).normalize(), 'f') for key in SOURCE_PRICE_KEYS}


def build_source_price_patch(remote, gross, source_vat, options):
    """Separate opt-in price builder; ordinary content routes remain price-free."""
    if remote['identity']['variant_code'] is not None or remote['parent'].get('variants'):
        raise _error('price_variant_unsupported', 422)
    language, row = _source_price_row(remote['parent'], options)
    vat, gross = publication.money(source_vat), publication.money(gross)
    if vat > 100 or vat != publication.money(language.get('vat')):
        raise _error('price_vat_differs', 422)
    if gross != gross.quantize(Decimal('.01')):
        raise _error('price_precision_requires_review', 422)
    if (row.get('price_sale') is not None or remote['parent'].get('action_currently_yn') not in (False, 0)
            or (row.get('product_discount') is not None and publication.money(row['product_discount']) != 0)
            or publication.money(row.get('product_discount_real')) != 0
            or publication.money(row.get('price_original')) != publication.money(row.get('price_with_vat'))):
        raise _error('price_discount_requires_review', 422)
    price_remote = {'identity': remote['identity'], 'leaf': remote['parent'],
                    'options': {**options, 'language': 'sk'}}
    patch = publication.build_patch({'variant': {'vat_rate': vat}},
        {'effective': {'sale_price_gross': gross}}, price_remote, ['sale_price_gross'])
    after = {'price_original': gross, 'price_with_vat': gross,
             'price_without_vat': (gross / (1 + vat / 100)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)}
    return {'payload': patch['payload'], 'options': deepcopy(options), 'source_vat': str(vat),
            'gross': str(gross), 'before': source_price_values(remote, options),
            'after': {key: format(value.normalize(), 'f') for key, value in after.items()}}


def protected_except_source_price(observation, options):
    result = deepcopy(observation)
    _, row = _source_price_row(result['content'], options)
    for key in SOURCE_PRICE_KEYS:
        row.pop(key, None)
    return result


def _brand(value):
    return " ".join(value.split()).casefold() if isinstance(value, str) else ""


def verify_source(group, remote):
    """Require the complete source family and contradictory-identifier checks."""
    parent = remote["parent"]
    rows = group.get("rows") if isinstance(group, dict) else None
    if (not isinstance(rows, list) or not rows or group.get("source_code") != parent["code"]
            or len(rows) > 100):
        raise _error("source_family_mismatch", 422)
    target = parent.get("variants") or [parent]
    if len(rows) != len(target):
        raise _error("source_family_mismatch", 422)
    source_codes = [row.get("code") for row in rows if isinstance(row, dict)]
    if (len(source_codes) != len(rows) or any(not isinstance(code, str) or not code for code in source_codes)
            or len({code.casefold() for code in source_codes}) != len(rows)):
        raise _error("source_identity_ambiguous", 422)
    family = bool(parent.get("variants"))
    for row in rows:
        if row.get("parent_code") != (parent["code"] if family else ""):
            raise _error("source_family_mismatch", 422)
        if not _brand(row.get("manufacturer")) or _brand(row["manufacturer"]) != _brand(parent.get("manufacturer")):
            raise _error("source_brand_conflict", 422)
    by_code = {row["code"]: row for row in target}
    target_gtin, source_gtin = defaultdict(list), defaultdict(list)
    for row in target:
        for key in barcode_keys([row.get("ean")]):
            target_gtin[key].append(row)
    for row in rows:
        for key in barcode_keys([row.get("ean")]):
            source_gtin[key].append(row)
    used, matches = set(), []
    for row in rows:
        keys = barcode_keys([row.get("ean")])
        exact = by_code.get(row["code"])
        barcode_candidates = {candidate["code"]: candidate for key in keys for candidate in target_gtin[key]}
        if exact is not None:
            target_keys = barcode_keys([exact.get("ean")])
            if (keys and target_keys and not keys.intersection(target_keys)) or any(code != exact["code"] for code in barcode_candidates):
                raise _error("source_ean_conflict", 422)
            match, reason = exact, "exact_code"
        elif len(barcode_candidates) == 1 and all(len(source_gtin[key]) == 1 for key in keys):
            match, reason = next(iter(barcode_candidates.values())), "unique_valid_gtin"
        else:
            raise _error("source_identity_ambiguous", 422)
        if match["code"] in used:
            raise _error("source_identity_ambiguous", 422)
        used.add(match["code"])
        matches.append({"source_code": row["code"], "target_code": match["code"],
                        "product_id": parent["product_id"], "variant_id": match.get("variant_id"),
                        "source_ean": row.get("ean"), "target_ean": _value(match, "ean"), "matched_by": reason})
    if used != set(by_code):
        raise _error("source_family_mismatch", 422)
    return matches


def _validate_payload(payload, identity):
    target = _target(identity)
    frozen = identity.get("full_family")
    if (not isinstance(frozen, dict) or frozen.get("code") != target["parent_code"]
            or frozen.get("product_id") != target["product_id"]):
        raise _error("identity_unverified", 422)
    if not isinstance(payload, dict) or set(payload) != {"products"}:
        raise _error("invalid_payload", 422)
    rows = payload["products"]
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise _error("invalid_payload", 422)
    row = rows[0]
    if (set(row) - {'code', 'descriptions', 'parameters'} or row.get('code') != target['parent_code']
            or not ({'descriptions', 'parameters'} & set(row))):
        raise _error("invalid_payload", 422)
    # Reuse source validation at the final write boundary: the frozen journal
    # cannot broaden HTML or field-size rules when it is sent or recovered.
    from inventory_hub.adapters.atomer_content import source_values, AtomerSourceError
    try:
        if 'descriptions' in row:
            descriptions = row['descriptions']
            if (not isinstance(descriptions, list) or len(descriptions) != 1
                    or not isinstance(descriptions[0], dict)):
                raise _error('invalid_payload', 422)
            description = descriptions[0]
            fields = list(set(description) - {'language'})
            if description.get('language') != 'sk' or not fields or set(fields) - TEXT_FIELDS:
                raise _error('invalid_payload', 422)
            source_values({'rows': [description]}, fields)
        if 'parameters' in row:
            from inventory_hub.services.atomer_parameters import canonical_parameters
            if identity.get('parameters_yn') is not True or frozen.get('variants') or not row['parameters']:
                raise _error('invalid_payload', 422)
            canonical_parameters(row['parameters'])
    except AtomerSourceError:
        raise _error("invalid_payload", 422) from None


def _acknowledged(payload, identity):
    if not isinstance(payload, dict) or not source._messages_ok(payload.get("messages")):
        return False
    rows = payload.get("products")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return False
    parent = rows[0]
    if (parent.get("code") != identity["parent_code"] or parent.get("product_id") != identity["product_id"]
            or not source._identifier(parent.get("product_id")) or parent.get("updated_yn") is not True
            or not source._messages_ok(parent.get("messages"))):
        return False
    # Some responses report family acknowledgements even without an incoming
    # variants collection. They cannot introduce or change a native identity.
    variants = parent.get("variants")
    if variants in (None, []):
        return True
    expected = {row["code"]: row["variant_id"] for row in identity["full_family"]["variants"]}
    if (not isinstance(variants, list) or len(variants) != len(expected)
            or any(not isinstance(row, dict) for row in variants)
            or {row.get("code") for row in variants} != set(expected)):
        return False
    return all(row.get("variant_id") == expected[row["code"]] and source._identifier(row.get("variant_id"))
               and source._messages_ok(row.get("messages")) for row in variants)


def _write(shop, identity, fingerprint, payload):
    _validate_payload(payload, identity)
    client = source._client(shop, fingerprint)
    try:
        result = source._request(client, "put", "products", payload=payload)
        if not _acknowledged(result, identity):
            raise source.SourceError("atomer_content_write_unconfirmed", uncertain=True)
    finally:
        client.session.close()


async def write_once(shop, identity, fingerprint, payload):
    await asyncio.to_thread(_write, shop, identity, fingerprint, payload)
