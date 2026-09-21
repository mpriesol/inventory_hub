import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from inventory_hub.routers.catalog import router
from inventory_hub.services.catalog import CatalogError
from inventory_hub.services.catalog_images import paul_lange_image


JPEG = b"\xff\xd8\xff\xe0test image\xff\xd9"


class CatalogImageTests(unittest.IsolatedAsyncioTestCase):
    def mock_client(self, handler):
        client_class = httpx.AsyncClient
        def factory(**kwargs):
            return client_class(transport=httpx.MockTransport(handler), **kwargs)
        return patch("inventory_hub.services.catalog_images.httpx.AsyncClient", side_effect=factory)

    async def test_fetches_only_fixed_supplier_origin_without_credentials(self):
        def respond(request):
            self.assertEqual(str(request.url), "http://xml.paul-lange-oslany.sk:8081/ito5-S123.jpg")
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("cookie", request.headers)
            return httpx.Response(200, content=JPEG)
        with self.mock_client(respond):
            self.assertEqual(await paul_lange_image("ito5-S123.jpg"), JPEG)

    async def test_rejects_paths_and_urls_before_network_access(self):
        with self.mock_client(lambda request: self.fail("Unexpected network access")):
            for name in ("../secret.jpg", "http://127.0.0.1/a.jpg", "ito5-X.jpg?url=x", "ito5-X.svg", "ito5-X.jpg\n"):
                with self.subTest(name=name), self.assertRaises(CatalogError) as error:
                    await paul_lange_image(name)
                self.assertEqual(error.exception.status, 404)

    async def test_never_follows_redirects(self):
        requests = []
        def redirect(request):
            requests.append(request)
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})
        with self.mock_client(redirect), self.assertRaises(CatalogError) as error:
            await paul_lange_image("ito5-S123.jpg")
        self.assertEqual(len(requests), 1)
        self.assertEqual(error.exception.status, 502)

    async def test_rejects_non_images_and_oversized_responses(self):
        for content, expected in ((b"<html>not an image</html>", "invalid_image"), (JPEG * 100, "image_too_large")):
            with self.subTest(expected=expected), self.mock_client(lambda request: httpx.Response(200, content=content)), \
                    patch("inventory_hub.services.catalog_images.MAX_IMAGE_BYTES", 128), self.assertRaises(CatalogError) as error:
                await paul_lange_image("ito5-S123.jpg")
            self.assertEqual(error.exception.code, expected)

    async def test_upstream_failure_is_controlled(self):
        def timeout(request):
            raise httpx.ReadTimeout("upstream details", request=request)
        with self.mock_client(timeout), self.assertRaises(CatalogError) as error:
            await paul_lange_image("ito5-S123.jpg")
        self.assertEqual(error.exception.code, "image_unavailable")
        self.assertNotIn("upstream details", str(error.exception))

    def test_route_returns_cacheable_jpeg_with_no_sniffing(self):
        app = FastAPI()
        app.include_router(router)
        with patch("inventory_hub.routers.catalog.paul_lange_image", return_value=JPEG):
            response = TestClient(app).get("/suppliers/paul-lange/catalog/images/ito5-S123.jpg")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, JPEG)
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("max-age=86400", response.headers["cache-control"])
