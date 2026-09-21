"""Serve Paul Lange's HTTP-only catalog photographs through the Hub origin."""
import asyncio
import re

import httpx

from inventory_hub.services.catalog import CatalogError


MAX_IMAGE_BYTES = 5 * 1024 * 1024
_downloads = asyncio.Semaphore(8)


async def paul_lange_image(filename: str) -> bytes:
    # A fixed public origin and filename format, never a caller-provided URL.
    if not re.fullmatch(r"ito5-[A-Za-z0-9]{1,60}\.jpg", filename):
        raise CatalogError("image_not_found", "Catalog image was not found", 404)
    try:
        async with _downloads, asyncio.timeout(20):
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                async with client.stream("GET", f"http://xml.paul-lange-oslany.sk:8081/{filename}",
                                         headers={"Accept": "image/jpeg", "Accept-Encoding": "identity"}) as response:
                    if response.status_code == 404:
                        raise CatalogError("image_not_found", "Catalog image was not found", 404)
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.aiter_bytes(64 * 1024):
                        content.extend(chunk)
                        if len(content) > MAX_IMAGE_BYTES:
                            raise CatalogError("image_too_large", "Catalog image exceeds the size limit", 502)
                    if not content.startswith(b"\xff\xd8\xff"):
                        raise CatalogError("invalid_image", "Supplier did not return a JPEG image", 502)
                    return bytes(content)
    except (httpx.HTTPError, TimeoutError):
        raise CatalogError("image_unavailable", "Supplier image is temporarily unavailable", 502) from None
