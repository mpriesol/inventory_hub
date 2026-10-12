"""Combine Sloger's brand feeds without accepting a partial catalog refresh."""
from __future__ import annotations

import time
import math
import re
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

from lxml import etree


MAX_CATALOG_BYTES = 100 * 1024 * 1024


def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> int | None:
    """Parse Retry-After without retaining the header in any error or artifact."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]+", value):
        # Only the <=60s retry window matters; avoid unbounded integer parsing.
        seconds = value.lstrip("0") or "0"
        return 61 if len(seconds) > 6 else int(seconds)
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0, math.ceil((retry_at - (now or datetime.now(timezone.utc))).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        return None


class SlogerFeedError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def aggregate_catalog_sources(
    source: dict,
    destination: Path,
    download: Callable[[dict, int, int, float | None], Path],
    *,
    max_elapsed_seconds: float | None = None,
    max_size_bytes: int = MAX_CATALOG_BYTES,
) -> Path:
    """Retain individual snapshots; publish an aggregate only after every source succeeds.

    ``download`` is the regular bounded catalog downloader. It receives a single
    source, its one-based index, and the remaining byte/time budgets. Errors must
    remain sanitized by that downloader; source URLs are never included here.
    """
    remote = source.get("remote") or {}
    urls = remote.get("urls")
    if not isinstance(urls, list) or not 1 <= len(urls) <= 100:
        raise SlogerFeedError("feed_urls_invalid", "Configure between 1 and 100 Sloger listing URLs")
    for url in urls:
        if not isinstance(url, str) or url != url.strip():
            raise SlogerFeedError("feed_urls_invalid", "Each Sloger listing URL must be an HTTP or HTTPS URL")
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme in ("https", "http") and bool(parsed.hostname) and not parsed.fragment
        except ValueError:
            valid = False
        if not valid:
            raise SlogerFeedError("feed_urls_invalid", "Each Sloger listing URL must be an HTTP or HTTPS URL")
    if len(set(urls)) != len(urls):
        raise SlogerFeedError("feed_urls_invalid", "Sloger listing URLs must not be repeated")

    limit = min(max_size_bytes, MAX_CATALOG_BYTES)
    downloaded_bytes = 0
    started = time.monotonic()
    temporary = destination.with_suffix(".part")
    retry_get = str(remote.get("method") or "GET").upper() == "GET"

    def remaining_time() -> float | None:
        if max_elapsed_seconds is None:
            return None
        remaining = max_elapsed_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise SlogerFeedError("feed_timeout", "The supplier exceeded the feed download deadline", 504)
        return remaining

    def wait(seconds: int) -> None:
        remaining = remaining_time()
        if remaining is not None and seconds >= remaining:
            raise SlogerFeedError("feed_timeout", "The supplier exceeded the feed download deadline", 504)
        if seconds:
            time.sleep(seconds)
        remaining_time()

    try:
        with temporary.open("wb") as output:
            output.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<products>\n')
            for index, url in enumerate(urls, 1):
                if index > 1 and retry_get:
                    wait(1)
                if downloaded_bytes >= limit:
                    raise SlogerFeedError("feed_too_large", "Combined Sloger feeds exceed the download size limit", 413)
                child_remote = {key: value for key, value in remote.items() if key != "urls"}
                child_remote["url"] = url
                child = {**source, "remote": child_remote}
                for attempt in range(3):
                    try:
                        snapshot = download(child, index, limit - downloaded_bytes, remaining_time())
                        break
                    except Exception as error:
                        code = getattr(error, "code", None)
                        delay = getattr(error, "retry_after_seconds", None)
                        delay = delay if delay is not None else 5 * (attempt + 1)
                        if retry_get and code == "feed_rate_limited" and attempt < 2 and delay <= 60:
                            wait(delay)
                            continue
                        if code:
                            # The regular downloader has already sanitized this
                            # error; add only the source position, never its URL.
                            raise SlogerFeedError(code, f"Sloger source {index}: {error}", getattr(error, "status", 502)) from None
                        raise
                downloaded_bytes += snapshot.stat().st_size
                remaining_time()
                if downloaded_bytes > limit:
                    raise SlogerFeedError("feed_too_large", "Combined Sloger feeds exceed the download size limit", 413)
                try:
                    tree = etree.parse(str(snapshot), etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
                except (etree.XMLSyntaxError, OSError):
                    raise SlogerFeedError("feed_xml_invalid", f"Sloger source {index} did not return valid listing XML", 502) from None
                root = tree.getroot()
                items = [item for item in root if isinstance(item.tag, str)]
                if tree.docinfo.doctype or root.tag != "products" or any(item.tag != "product" for item in items):
                    raise SlogerFeedError("feed_xml_invalid", f"Sloger source {index} did not return valid listing XML", 502)
                if not items:
                    raise SlogerFeedError("feed_empty", f"Sloger source {index} returned no products", 502)
                # A relative snapshot name is enough to locate the exact source
                # later without embedding access tokens or credentials in XML.
                provenance = etree.ProcessingInstruction("sloger_snapshot", f"source={index} file={snapshot.name}")
                output.write(etree.tostring(provenance) + b"\n")
                for item in items:
                    serialized = etree.tostring(item, encoding="UTF-8", with_tail=False)
                    if output.tell() + len(serialized) + len(b"\n</products>\n") > limit:
                        raise SlogerFeedError("feed_too_large", "Combined Sloger catalog exceeds the download size limit", 413)
                    output.write(serialized + b"\n")
            output.write(b"</products>\n")
        temporary.replace(destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)
