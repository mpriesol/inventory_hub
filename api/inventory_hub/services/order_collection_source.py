"""Bounded, read-only order discovery without customer or product projections.

Upgates has page-number pagination, not a snapshot cursor. This page reader
cannot certify a complete scan: the caller owns overlap, cross-page validation,
checkpoint persistence and reconciliation. Discovery never authorizes stock.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from inventory_hub.services.order_stock_source import SourceError, _positive_id, _text, _timestamp, _uuid
from inventory_hub.services.upgates import UpgatesClient, UpgatesError, connection_fingerprint


MAX_PAGE_SIZE = 100
# Delta discovery must include modifications to orders predating Hub cutover.
# The last-update filter still bounds work to the current incremental window.
OLDEST_ORDER_CREATION = datetime(1900, 1, 1, tzinfo=timezone.utc)


class CollectionSourceError(SourceError):
    def __init__(self, code: str, status: int = 502, retry_after: int | None = None):
        super().__init__(code, status)
        self.retry_after = retry_after


def _invalid(reason: str) -> CollectionSourceError:
    # Callers supply fixed categories only, never source values or exceptions.
    return CollectionSourceError(f"order_collection_invalid_response_{reason}")


def _instant(value) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CollectionSourceError("order_collection_invalid_request", 422)
    return value.astimezone(timezone.utc)


def _wire_instant(value: datetime, *, upper=False) -> datetime:
    """Whole-second API bounds cover the caller's exact inclusive window.

    Only the read envelope is rounded. Stored run/cursor/cutover timestamps and
    returned source timestamps retain their precision and review semantics.
    """
    rounded = value.replace(microsecond=0)
    if upper and value.microsecond:
        try:
            rounded += timedelta(seconds=1)
        except OverflowError:
            raise CollectionSourceError("order_collection_invalid_request", 422) from None
    return rounded


def _metadata(payload, page: int) -> tuple[list[dict], int, int]:
    if not isinstance(payload, dict) or payload.get("messages"):
        raise _invalid("metadata")
    rows = payload.get("orders")
    if not isinstance(rows, list) or len(rows) > MAX_PAGE_SIZE:
        raise _invalid("metadata")
    values = {}
    for field in ("current_page", "current_page_items", "number_of_pages", "number_of_items"):
        value = payload.get(field)
        if type(value) is not int or value < 0:
            raise _invalid("metadata")
        values[field] = value
    pages, count = values["number_of_pages"], values["number_of_items"]
    if values["current_page"] != page or values["current_page_items"] != len(rows):
        raise _invalid("metadata")
    if count == 0:
        if rows or page != 1 or pages not in (0, 1):
            raise _invalid("metadata")
    else:
        if not rows or not 1 <= page <= pages:
            raise _invalid("metadata")
        # The contract promises a maximum page size, not a configurable fixed
        # size. Every other reported page must contain between 1 and 100 rows.
        if not len(rows) + pages - 1 <= count <= len(rows) + MAX_PAGE_SIZE * (pages - 1):
            raise _invalid("metadata")
    return rows, pages, count


def _entry(raw, *, created_from: datetime, changed_from: datetime, created_to: datetime, deleted: bool) -> dict:
    if not isinstance(raw, dict) or raw.get("messages"):
        raise _invalid("identity")
    try:
        number = _text(raw.get("order_number"), blank=False)
        if number != raw["order_number"] or ";" in number:
            raise _invalid("identity")
        identifier = _uuid(raw.get("uuid"))
    except SourceError:
        raise _invalid("identity") from None
    try:
        created = _timestamp(raw.get("creation_time"))
        updated = _timestamp(raw.get("last_update_time"))
    except SourceError:
        raise _invalid("timestamp") from None
    try:
        status_id = _positive_id(raw.get("status_id"))
        origin = _text(raw.get("origin"), 40, blank=False)
    except SourceError:
        raise _invalid("identity") from None
    created_at, updated_at = datetime.fromisoformat(created), datetime.fromisoformat(updated)
    if not created_from <= created_at <= created_to:
        raise _invalid("created_window")
    if updated_at < created_at:
        raise _invalid("update_before_creation")
    if updated_at < changed_from:
        raise _invalid("updated_window")
    # Deletion membership is established by the query. GET order objects do
    # not promise a deletion flag, but an explicitly contradictory flag is bad.
    for field in ("deleted_yn", "deleted"):
        flag = raw.get(field)
        if flag is not None and (type(flag) is not bool or flag != deleted):
            raise _invalid("deleted_scope")
    return {"order_number": number, "uuid": identifier, "created_at": created, "updated_at": updated,
            "deleted": deleted, "status_id": status_id, "origin": origin}


def _normalize_page(payload, *, created_from: datetime, changed_from: datetime, created_to: datetime,
                    page: int, deleted: bool) -> dict:
    rows, pages, count = _metadata(payload, page)
    entries = [_entry(row, created_from=created_from, changed_from=changed_from,
                      created_to=created_to, deleted=deleted) for row in rows]
    if len({entry["order_number"] for entry in entries}) != len(entries) or len({entry["uuid"] for entry in entries}) != len(entries):
        raise _invalid("duplicate_identity")
    times = [datetime.fromisoformat(entry["updated_at"]) for entry in entries]
    if times != sorted(times):
        raise _invalid("ordering")
    return {"entries": entries, "page": page, "number_of_pages": pages, "number_of_items": count,
            "has_more": page < pages}


def _fetch_page(shop_code: str, params: dict, expected_target_fingerprint: str | None = None) -> dict:
    client = None
    try:
        client = UpgatesClient.from_shop(shop_code)
        if expected_target_fingerprint is not None:
            auth = client.session.auth
            login = auth[0] if isinstance(auth, (tuple, list)) and len(auth) == 2 else getattr(auth, "username", None)
            actual = connection_fingerprint(client.base_url, login)
            if actual is None or actual != expected_target_fingerprint:
                raise CollectionSourceError("order_collection_target_changed", 409)
        return client.read_order_collection(params)
    except UpgatesError as error:
        if error.status_code in (401, 403):
            raise CollectionSourceError("order_collection_upgates_access") from None
        if error.status_code == 429:
            delay = getattr(error, "retry_after", None)
            retry_after = delay if type(delay) is int and 1 <= delay <= 604800 else None
            raise CollectionSourceError("order_collection_rate_limited", 429, retry_after) from None
        if error.status_code == 400:
            # Keep only the status; upstream bodies, URLs and headers can carry
            # customer or connection data and are neither parsed nor logged.
            raise CollectionSourceError("order_collection_upgates_http_400") from None
        raise CollectionSourceError("order_collection_source_unavailable") from None
    finally:
        if client is not None:
            client.session.close()


async def load_changed_page(shop_code: str, *, created_from: datetime, changed_from: datetime,
                            created_to: datetime, page: int, deleted: bool = False,
                            expected_target_fingerprint: str | None = None) -> dict:
    created_from, changed_from, created_to = map(_instant, (created_from, changed_from, created_to))
    if created_from > created_to or type(page) is not int or not 1 <= page <= 1_000_000 or type(deleted) is not bool:
        raise CollectionSourceError("order_collection_invalid_request", 422)
    wire_created_from = _wire_instant(created_from)
    wire_changed_from = _wire_instant(changed_from)
    wire_created_to = _wire_instant(created_to, upper=True)
    params = {"creation_time_from": wire_created_from.isoformat(timespec='seconds'),
              "creation_time_to": wire_created_to.isoformat(timespec='seconds'),
              "last_update_time_from": wire_changed_from.isoformat(timespec='seconds'), "page": page,
              "order_by": "last_update_time", "order_dir": "asc"}
    if deleted:
        params["deleted_yn"] = "true"
    target_guard = {"expected_target_fingerprint": expected_target_fingerprint} if expected_target_fingerprint is not None else {}
    payload = await asyncio.to_thread(_fetch_page, shop_code, params, **target_guard)
    # Validate every row against precisely the transmitted envelope. Keep its
    # boundary overlap so page/item counts and cross-page checks remain exact;
    # save_entries still compares original timestamps with the exact cutover.
    return _normalize_page(payload, created_from=wire_created_from, changed_from=wire_changed_from,
                           created_to=wire_created_to, page=page, deleted=deleted)
