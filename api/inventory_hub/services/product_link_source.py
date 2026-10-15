"""Bounded, fresh Upgates identity reads for existing-card linking.

`codes` filters parent codes. Products are requested without their variant
payload; only an explicit non-parent, non-set product is a standalone leaf.
The flat products/variants list is filtered by exact variant codes and supplies
parent IDs, EANs and parameters without downloading large sibling families. No
catalog cache, credentials, prices, quantities or remote writes are exposed.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any
from urllib.parse import urlsplit

import requests

from inventory_hub.services.product_identity import verified_barcodes
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.services.upgates import (
    UpgatesClient, UpgatesError, UpgatesParameterError, _retry_after_seconds,
    connection_fingerprint, product_title, variant_attributes,
)
from inventory_hub.services.upgates_budget import AdmissionDenied

MAX_CODES = 100
MAX_REQUESTS = 8
MAX_BYTES = 4_000_000
MAX_PARENTS = 200
MAX_LEAVES = 200


class SourceError(Exception):
    """Only stable codes and bounded retry metadata cross this boundary."""

    def __init__(self, code: str, status: int = 502, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after = retry_after


def _need(condition: bool, code: str, status: int = 502) -> None:
    if not condition:
        raise SourceError(code, status)


def _codes(values: list[str]) -> list[str]:
    _need(isinstance(values, list) and 0 < len(values) <= MAX_CODES, "source_codes_invalid", 422)
    _need(all(isinstance(code, str) and code == code.strip() and 0 < len(code) <= 100
              and ";" not in code and not any(ord(c) < 32 or ord(c) == 127 for c in code)
              for code in values), "source_codes_invalid", 422)
    return sorted(set(values))


def _native_id(value: Any) -> str:
    text = str(value)
    _need(re.fullmatch(r"[1-9][0-9]{0,19}", text) is not None, "source_native_id_invalid")
    return text


def _code(value: Any) -> str:
    _need(isinstance(value, str) and 0 < len(value) <= 100 and value == value.strip()
          and not any(ord(c) < 32 or ord(c) == 127 for c in value), "source_code_invalid")
    return value


def _count(value: Any, *, maximum: int) -> int:
    _need(not isinstance(value, bool) and re.fullmatch(r"[0-9]{1,9}", str(value)) is not None,
          "source_pagination_invalid")
    result = int(value)
    _need(result <= maximum, "source_result_limit")
    return result


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        _need(key not in value, "source_duplicate_json_key")
        value[key] = item
    return value


def _nonfinite(_value):
    raise SourceError("source_nonfinite_json")


def _url(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 2000 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return None


def _navigation(parent: dict, item: dict) -> dict:
    """Only URLs actually supplied by this parent/leaf, never guessed routes."""
    def urls(value):
        descriptions = value.get('descriptions')
        descriptions = descriptions if isinstance(descriptions, list) else []
        return [d.get('url') for d in descriptions if isinstance(d, dict) and d.get('language') == 'sk'] + [
            d.get('url') for d in descriptions if isinstance(d, dict)] + [value.get('url')]
    return {'shop_url': next((url for value in [*urls(item), *urls(parent)] if (url := _url(value))), None),
            'shop_admin_url': _url(item.get('admin_url')) or _url(parent.get('admin_url'))}


def _leaf(parent: dict, item: dict, *, variant: bool) -> dict:
    code, parent_code = _code(item.get("code")), _code(parent.get("code"))
    parent_id = _native_id(parent.get("product_id"))
    if variant:
        if item.get("product_id") is not None:
            _need(_native_id(item["product_id"]) == parent_id, "source_parent_changed")
        if item.get("product_code") is not None:
            _need(item["product_code"] == parent_code, "source_parent_changed")
    else:
        _need(parent.get("variants_exists_yn") is False and parent.get("set_yn", False) is False
              and parent.get("variants") in (None, []), "source_product_kind_unknown")
    raw = item.get("ean")
    raw = raw if isinstance(raw, list) else [] if raw is None or raw == '' else [raw]
    _need(len(raw) <= 100 and all(isinstance(x, (str, int)) and not isinstance(x, bool) for x in raw),
          "source_ean_invalid")
    raw = [str(x) for x in raw if str(x).strip()]
    _need(all(len(x) <= 1000 for x in raw), "source_ean_invalid")
    eans = sorted(verified_barcodes(raw))
    invalid = sorted({value for entry in raw for value, _kind in ProductIdentifierService.split_compound_ean(entry)
                      if not verified_barcodes((value,))} |
                     {entry for entry in raw if not ProductIdentifierService.split_compound_ean(entry)})
    warnings = ["unverified_barcode"] if invalid else []
    try:
        attributes = variant_attributes(item, strict=True)
    except UpgatesParameterError:
        raise SourceError("source_parameters_invalid") from None
    attributes = sorted(attributes, key=lambda x: (x["name"].casefold(), x["value"]))
    if variant and not attributes:
        warnings.append("variant_axes_unknown")
    name = product_title(parent)
    _need(len(name) <= 2000, "source_name_invalid")
    return {"code": code, "parent_code": parent_code, "parent_id": parent_id,
            "is_variant": variant, "external_id": _native_id(item.get("variant_id")) if variant else parent_id,
            "name": name, "eans": eans, "raw_barcodes": raw, "invalid_barcodes": invalid,
            "attributes": attributes, "warnings": warnings, **_navigation(parent, item)}


def _key(leaf: dict) -> tuple:
    return leaf["code"], leaf["is_variant"], leaf["external_id"], leaf["parent_id"], leaf["parent_code"]


class _Reader:
    def __init__(self, shop_code: str, expected_fingerprint: str | None, max_requests: int):
        _need(type(max_requests) is int and 1 <= max_requests <= MAX_REQUESTS, "source_request_limit", 422)
        self.max_requests, self.requests = max_requests, 0
        try:
            self.client = UpgatesClient.from_shop(shop_code)
        except (UpgatesError, ValueError, OSError):
            raise SourceError("source_configuration_invalid", 409) from None
        self.client.log_dir = None
        self.fingerprint = connection_fingerprint(self.client.base_url, self.client.session.auth[0])
        if not self.fingerprint or (expected_fingerprint is not None and self.fingerprint != expected_fingerprint):
            self.close()
            raise SourceError("source_connection_changed", 409)

    def close(self):
        self.client.session.close()

    def get(self, path: str, params: dict) -> dict:
        _need(self.requests < self.max_requests, "source_request_limit", 409)
        self.requests += 1
        response = None
        try:
            response = self.client.session.get(self.client.base_url + path, params=params,
                timeout=(5, 20), verify=True, allow_redirects=False, stream=True)
            if response.status_code != 200:
                status = response.status_code
                code = "source_rate_limited" if status == 429 else "source_authentication_failed" if status in (401, 403) else "source_http_failed"
                raise SourceError(code, 429 if status == 429 else 502,
                                  _retry_after_seconds(response.headers.get("Retry-After")))
            content, start = bytearray(), time.monotonic()
            for chunk in response.iter_content(65536):
                content.extend(chunk)
                _need(len(content) <= MAX_BYTES and time.monotonic() - start <= 30, "source_response_limit")
            try:
                data = json.loads(content, object_pairs_hook=_unique_object, parse_constant=_nonfinite)
            except (ValueError, UnicodeError):
                raise SourceError("source_json_invalid") from None
            _need(isinstance(data, dict) and not data.get("messages"), "source_response_needs_review")
            return data
        except AdmissionDenied as exc:
            raise SourceError("source_rate_limited", 429, exc.retry_after) from None
        except requests.RequestException:
            raise SourceError("source_unavailable", 502) from None
        finally:
            if response is not None:
                response.close()

    def pages(self, path: str, field: str, filters: dict) -> list[dict]:
        rows, pages, total, page = [], None, None, 1
        while pages is None or page <= pages:
            data = self.get(path, {**filters, "page": page, "current_page_items": 100})
            batch = data.get(field)
            _need(isinstance(batch, list) and len(batch) <= 100 and all(isinstance(x, dict) for x in batch),
                  "source_rows_invalid")
            _need("number_of_pages" in data and "number_of_items" in data, "source_pagination_invalid")
            current_pages = _count(data["number_of_pages"], maximum=MAX_REQUESTS)
            current_total = _count(data["number_of_items"], maximum=MAX_PARENTS)
            _need(current_pages > 0 or not batch, "source_pagination_invalid")
            if "current_page" in data:
                _need(_count(data["current_page"], maximum=MAX_REQUESTS) == page, "source_pagination_invalid")
            if pages is not None:
                _need((pages, total) == (current_pages, current_total), "source_changed_during_read", 409)
            pages, total = current_pages, current_total
            rows.extend(batch)
            _need(len(rows) <= MAX_PARENTS, "source_result_limit")
            page += 1
        _need(len(rows) == total, "source_pagination_incomplete")
        return rows


def _discover(shop_code: str, codes: list[str], expected_fingerprint: str | None, max_requests: int) -> dict:
    codes = _codes(codes)
    wanted, folded = set(codes), {code.casefold() for code in codes}
    reader = _Reader(shop_code, expected_fingerprint, max_requests)
    leaves = {}
    try:
        # This switch omits variants; it does not filter out parent products.
        # Explicit parent/set flags below prevent a family from becoming stock.
        rows = reader.pages("/products", "products", {"codes": ";".join(codes), "variants_yn": "false"})
        seen_parents, parents = set(), {}
        for parent in rows:
            parent_code, parent_id = _code(parent.get("code")), _native_id(parent.get("product_id"))
            _need(parent_code.casefold() in folded, "source_filter_mismatch")
            _need((parent_code, parent_id) not in seen_parents, "source_duplicate_parent")
            seen_parents.add((parent_code, parent_id))
            parents[(parent_code, parent_id)] = parent
            if parent_code not in wanted:
                continue
            _need(type(parent.get("variants_exists_yn")) is bool, "source_product_kind_unknown")
            _need(type(parent.get("set_yn", False)) is bool, "source_product_kind_unknown")
            if parent["variants_exists_yn"] or parent.get("set_yn", False):
                continue
            leaf = _leaf(parent, parent, variant=False)
            leaves[_key(leaf)] = leaf

        # The documented flat endpoint already carries leaf EAN/parameters and
        # parent identity. Never pull an entire POS family to find one leaf.
        rows = reader.pages("/products/variants", "variants", {"variant_codes": ";".join(codes)})
        seen_native, variant_items = set(), {}
        for item in rows:
            code = _code(item.get("code"))
            _need(code.casefold() in folded, "source_filter_mismatch")
            if code not in wanted:
                continue
            parent = {"code": item.get("product_code"), "product_id": item.get("product_id")}
            leaf = _leaf(parent, item, variant=True)
            key = _key(leaf)
            _need(key not in leaves, "source_duplicate_variant")
            native = leaf["external_id"]
            _need(native not in seen_native, "source_duplicate_variant_id")
            seen_native.add(native)
            leaves[key] = leaf
            variant_items[key] = item
            _need(len(leaves) <= MAX_LEAVES, "source_result_limit")
        # Flat variants carry their exact parent identity, but not its display
        # links/title. Fetch only those parents without their sibling payload.
        missing = sorted({leaf['parent_code'] for leaf in leaves.values() if leaf['is_variant']
                          and (leaf['parent_code'], leaf['parent_id']) not in parents})
        for offset in range(0, len(missing), MAX_CODES):
            batch = _codes(missing[offset:offset + MAX_CODES])
            for parent in reader.pages('/products', 'products', {'codes': ';'.join(batch), 'variants_yn': 'false'}):
                parent_code, parent_id = _code(parent.get('code')), _native_id(parent.get('product_id'))
                _need(parent_code in batch, 'source_filter_mismatch')
                _need((parent_code, parent_id) not in parents, 'source_duplicate_parent')
                parents[(parent_code, parent_id)] = parent
        for key, item in variant_items.items():
            leaf = leaves[key]
            parent = parents.get((leaf['parent_code'], leaf['parent_id']))
            _need(parent is not None, 'source_parent_changed', 409)
            _need(parent.get('variants_exists_yn') is True and parent.get('set_yn', False) is False,
                  'source_product_kind_unknown')
            leaves[key] = _leaf(parent, item, variant=True)
        return {"fingerprint": reader.fingerprint, "leaves": sorted(leaves.values(), key=_key), "requests": reader.requests}
    finally:
        reader.close()


async def discover(shop_code: str, codes: list[str], expected_fingerprint: str | None = None,
                   *, max_requests: int = MAX_REQUESTS) -> dict:
    return await asyncio.to_thread(_discover, shop_code, codes, expected_fingerprint, max_requests)


async def current_fingerprint(shop_code: str) -> str:
    """Read normal connection configuration without an API request or secret output."""
    def read():
        reader = _Reader(shop_code, None, 1)
        try:
            return reader.fingerprint
        finally:
            reader.close()
    return await asyncio.to_thread(read)


async def verify(shop_code: str, leaves: list[dict], expected_fingerprint: str,
                 *, max_requests: int = MAX_REQUESTS) -> dict:
    _need(isinstance(leaves, list) and 0 < len(leaves) <= MAX_CODES and all(isinstance(v, dict) for v in leaves),
          "source_leaves_invalid", 422)
    try:
        expected = {_key(leaf) for leaf in leaves}
        codes = sorted({leaf["code"] for leaf in leaves})
    except (KeyError, TypeError):
        raise SourceError("source_leaves_invalid", 422) from None
    _need(len(expected) == len(leaves), "source_leaves_invalid", 422)
    result = await discover(shop_code, codes, expected_fingerprint, max_requests=max_requests)
    # An explicit selection may distinguish equal codes under different
    # parents. Recheck only its exact code/kind/parent scope; never switch to
    # another parent or recreated native identity if the selected one vanished.
    scopes = {(leaf["code"], leaf["parent_code"], leaf["is_variant"]) for leaf in leaves}
    selected = [leaf for leaf in result["leaves"]
                if (leaf["code"], leaf["parent_code"], leaf["is_variant"]) in scopes]
    found = {_key(leaf) for leaf in selected}
    _need(found == expected, "source_identity_changed", 409)
    return {**result, "leaves": selected}
