"""Bounded catalog-only API snapshots using the configured shop connection."""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import tempfile
from typing import Literal
import zipfile

import requests

from inventory_hub.services.migration_export import json_line
from inventory_hub.services.upgates import UpgatesClient, UpgatesError
from inventory_hub.services.upgates_budget import AdmissionDenied


Dataset = Literal["products", "parameters", "labels", "files", "related", "variants",
                  "categories", "parameter_registry", "availabilities", "redirections", "config"]
# Neither a caller-supplied URL nor an arbitrary upstream path is accepted.
SOURCES = {
    "products": ("products", "products", "product_id"),
    "parameters": ("products/parameters", "products", "product_id"),
    "labels": ("products/labels", "products", "product_id"),
    "files": ("products/files", "products", "product_id"),
    "related": ("products/related", "products", "product_id"),
    "variants": ("products/variants", "variants", "variant_id"),
    "categories": ("categories", "categories", "category_id"),
    "parameter_registry": ("parameters", "parameters", "id"),
    "availabilities": ("availabilities", "availabilities", "id"),
    "redirections": ("redirections", "redirections", "id"),
    "config": ("config", "config", None),
}
MAX_PAGES = 500
MAX_BYTES = 250_000_000


class ExportError(ValueError):
    """Safe error code only; never include upstream bodies or credentials."""


def _read(client, path, params):
    try:
        response = client.session.get(f"{client.base_url}/{path}", params=params,
            timeout=(5, 25), verify=client.verify_ssl, allow_redirects=False)
    except AdmissionDenied:
        raise ExportError("upgates_export_budget_deferred") from None
    except requests.RequestException:
        raise ExportError("upgates_export_connection_failed") from None
    try:
        if response.status_code != 200:
            raise ExportError("upgates_export_http_" + str(response.status_code))
        if len(response.content) > 16_000_000:
            raise ExportError("upgates_export_page_too_large")
        try:
            data = response.json(parse_float=Decimal)
        except ValueError:
            raise ExportError("upgates_export_invalid_json") from None
        if not isinstance(data, dict) or data.get("messages"):
            raise ExportError("upgates_export_response_needs_review")
        return data
    finally:
        response.close()


def _integer(data, key):
    value = data.get(key)
    if type(value) is not int or value < 0:
        raise ExportError("upgates_export_invalid_pagination")
    return value


def build_remote_export(shop_code, dataset, *, client=None):
    """One complete dataset or a safe error, never a successful partial ZIP.

    Pages and collections have independent observation times, not a global freeze.
    BudgetSession remains responsible for shared API admission. No automatic retry.
    """
    if shop_code not in ("biketrek", "xtrek") or dataset not in SOURCES:
        raise ExportError("upgates_export_invalid_source")
    owned = client is None
    if owned:
        try:
            client = UpgatesClient.from_shop(shop_code)
        except UpgatesError:
            raise ExportError("upgates_export_connection_unavailable") from None
    output = tempfile.TemporaryFile(mode="w+b")
    path, key, identity = SOURCES[dataset]
    started = datetime.now(timezone.utc)
    count, byte_count, pages, seen = 0, 0, [], set()
    try:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            # Upgates documents InStock as the default filter: request every type.
            filters = [{"type": value} for value in ("OnRequest", "NotAvailable", "InStock", "Custom")] if dataset == "availabilities" else [{}]
            for filters_index, filters_value in enumerate(filters):
                expected, subtotal, page = None, 0, 1
                while True:
                    params = {**filters_value, "page": page}
                    if path.startswith("products"):
                        params["current_page_items"] = 100
                    data = _read(client, path, params)
                    if dataset == "config":
                        if not isinstance(data.get("config"), dict):
                            raise ExportError("upgates_export_invalid_config")
                        # Only documented catalog settings. Never export arbitrary configuration.
                        data = {"config": {k: data["config"][k] for k in
                            ("prices_with_vat_yn", "vat_payer_yn", "oss_yn", "limit_orders") if k in data["config"]}}
                        rows, last = [data["config"]], True
                    else:
                        rows = data.get(key)
                        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                            raise ExportError("upgates_export_invalid_rows")
                        current = _integer(data, "current_page")
                        items = _integer(data, "number_of_items")
                        total_pages = _integer(data, "number_of_pages")
                        page_items = _integer(data, "current_page_items")
                        if current != page or page_items != len(rows) or total_pages > MAX_PAGES:
                            raise ExportError("upgates_export_invalid_pagination")
                        if total_pages == 0 and (items or rows or page != 1):
                            raise ExportError("upgates_export_invalid_pagination")
                        totals = (items, total_pages)
                        if expected is not None and expected != totals:
                            raise ExportError("upgates_export_catalog_changed")
                        expected = totals
                        last = page >= total_pages
                        if not last and not rows:
                            raise ExportError("upgates_export_incomplete_page")
                        for row in rows:
                            identifier = row.get(identity)
                            if isinstance(identifier, bool) or not isinstance(identifier, (str, int)) or str(identifier) == "":
                                raise ExportError("upgates_export_missing_identity")
                            identifier = str(identifier)
                            if identifier in seen:
                                raise ExportError("upgates_export_duplicate_identity")
                            seen.add(identifier)
                        subtotal += len(rows)
                        if last and subtotal != items:
                            raise ExportError("upgates_export_incomplete_count")
                    body = json_line(data)
                    byte_count += len(body)
                    if byte_count > MAX_BYTES:
                        raise ExportError("upgates_export_too_large")
                    member = f"pages/{filters_index:02d}-{page:05d}.json"
                    archive.writestr(member, body)
                    pages.append({"file": member, "page": page, "filters": filters_value,
                        "observed_at": datetime.now(timezone.utc), "count": len(rows),
                        "sha256": hashlib.sha256(body).hexdigest()})
                    count += len(rows)
                    if last:
                        break
                    page += 1
            archive.writestr("manifest.json", json_line({"schema_version": 1, "shop": shop_code,
                "dataset": dataset, "endpoint": path, "started_at": started,
                "completed_at": datetime.now(timezone.utc), "count": count, "pages": pages,
                "complete": True, "read_only": True, "decimal_encoding": "string",
                "notes": ["Separate GET observations; not a global freeze.",
                    "No upstream or local catalog, stock or settings writes.",
                    "Page files preserve the returned collection; decimals are exact strings."]}))
        output.seek(0)
        return output
    except BaseException:
        output.close()
        raise
    finally:
        if owned:
            client.session.close()
