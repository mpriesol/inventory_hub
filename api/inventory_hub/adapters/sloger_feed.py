"""Combine Sloger's brand feeds without accepting a partial catalog refresh."""
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from lxml import etree


MAX_CATALOG_BYTES = 100 * 1024 * 1024


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

    def remaining_time() -> float | None:
        if max_elapsed_seconds is None:
            return None
        remaining = max_elapsed_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise SlogerFeedError("feed_timeout", "The supplier exceeded the feed download deadline", 504)
        return remaining

    try:
        with temporary.open("wb") as output:
            output.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<products>\n')
            for index, url in enumerate(urls, 1):
                if downloaded_bytes >= limit:
                    raise SlogerFeedError("feed_too_large", "Combined Sloger feeds exceed the download size limit", 413)
                child_remote = {key: value for key, value in remote.items() if key != "urls"}
                child_remote["url"] = url
                child = {**source, "remote": child_remote}
                snapshot = download(child, index, limit - downloaded_bytes, remaining_time())
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
