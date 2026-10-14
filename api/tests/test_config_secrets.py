"""Configuration credentials remain server-only across real HTTP-style edits."""
import asyncio
import copy
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from inventory_hub import config_io, configs
from inventory_hub.settings import settings
from inventory_hub.config_secrets import ConfigSecretError, config_patch, public_config
from inventory_hub.routers import shops, suppliers


class ConfigSecretsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for module in (config_io, suppliers):
            mocked = patch.object(module, "DATA_ROOT", self.root)
            mocked.start()
            self.addCleanup(mocked.stop)

    def supplier(self):
        return {"name": "Synthetic", "feeds": {"sources": {
            "products": {"mode": "remote", "remote": {
                "url": "https://supplier.test/private-path-token.xml",
                "urls": ["https://supplier.test/list?key=url-secret"],
                "headers": {"X-Custom-Credential": "header-secret"},
                "params": {"arbitrary": "parameter-secret"},
                "auth": {"mode": "basic", "basic_user": "account", "basic_pass": "feed-secret"}}},
            "obsolete": {"mode": "remote", "remote": {"url": "https://supplier.test/obsolete-secret"}},
        }}, "invoices": {"download": {"strategy": "web", "web": {
            "login": {"mode": "form", "username": "account", "password": "invoice-secret",
                      "login_url": "https://supplier.test/login", "user_field": "user"}}}}}

    def test_supplier_get_save_and_history_never_return_reusable_credentials(self):
        original = self.supplier()
        config_io.save_supplier("synthetic", original)
        public = suppliers.get_supplier_config("synthetic")
        serialized = json.dumps(public)
        for secret in ("private-path-token", "url-secret", "header-secret", "parameter-secret", "feed-secret", "invoice-secret"):
            self.assertNotIn(secret, serialized)
        remote = public["feeds"]["sources"]["products"]["remote"]
        self.assertEqual(remote["url"], "")
        self.assertTrue(remote["url_configured"])
        self.assertEqual(remote["urls"], [])
        self.assertTrue(remote["urls_configured"])
        self.assertTrue(remote["auth"]["basic_pass_configured"])
        self.assertEqual(public["invoices"]["download"]["web"]["login"]["username"], "account")
        public["name"] = "Changed name"
        saved = suppliers.put_supplier_config("synthetic", public)
        self.assertNotIn("invoice-secret", json.dumps(saved))
        loaded = config_io.load_supplier("synthetic")
        self.assertEqual(loaded["feeds"], original["feeds"] | {"current_key": "products"})
        self.assertEqual(loaded["invoices"]["download"], original["invoices"]["download"])
        self.assertNotIn("_configured", json.dumps(loaded))
        version = suppliers.get_supplier_history("synthetic")[0].version
        self.assertNotIn("invoice-secret", json.dumps(suppliers.get_supplier_history_version("synthetic", version)))
        self.assertEqual(stat.S_IMODE(config_io.supplier_path("synthetic").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((suppliers._history_dir("synthetic") / f"{version}.json").stat().st_mode), 0o600)

    def test_blank_or_omitted_auth_keeps_complete_saved_configuration(self):
        config_io.save_supplier("synthetic", self.supplier())
        original = config_io.load_supplier("synthetic")
        suppliers.put_supplier_config("synthetic", {"invoices": {"months_back_default": 7}})
        self.assertEqual(config_io.load_supplier("synthetic")["invoices"]["download"], original["invoices"]["download"])
        suppliers.put_supplier_config("synthetic", {"feeds": {"sources": {"products": {"remote": {"method": "GET"}}}}})
        loaded = config_io.load_supplier("synthetic")
        self.assertEqual(loaded["feeds"]["sources"]["products"]["remote"]["auth"],
                         original["feeds"]["sources"]["products"]["remote"]["auth"])
        self.assertNotIn("obsolete", loaded["feeds"]["sources"])
        public = suppliers.get_supplier_config("synthetic")
        public["invoices"]["download"]["web"]["login"]["password"] = None
        suppliers.put_supplier_config("synthetic", public)
        self.assertEqual(config_io.load_supplier("synthetic")["invoices"]["download"]["web"]["login"]["password"], "invoice-secret")

    def test_explicit_replacement_removal_and_restore_work_without_mask_corruption(self):
        config_io.save_supplier("synthetic", self.supplier())
        public = suppliers.get_supplier_config("synthetic")
        public["invoices"]["download"]["web"]["login"]["password"] = "replacement-secret"
        suppliers.put_supplier_config("synthetic", public)
        original_history = suppliers.get_supplier_history("synthetic")[0].version
        stored = config_io.load_supplier("synthetic")
        self.assertEqual(stored["invoices"]["download"]["web"]["login"]["password"], "replacement-secret")
        # Freeze history writing so the synthetic same-second restore fixture
        # cannot overwrite its own history file before loading the old version.
        with patch.object(suppliers, "_save_to_history"):
            restored = suppliers.restore_supplier_version("synthetic", original_history)
        self.assertTrue(restored["invoices"]["download"]["web"]["login"]["password_configured"])
        self.assertEqual(config_io.load_supplier("synthetic")["invoices"]["download"]["web"]["login"]["password"], "invoice-secret")
        suppliers.put_supplier_config("synthetic", {"_clear_secrets": ["/invoices/download/web/login/password", "/feeds/sources/products/remote/headers"]})
        loaded = config_io.load_supplier("synthetic")
        self.assertEqual(loaded["invoices"]["download"]["web"]["login"]["password"], "")
        self.assertEqual(loaded["invoices"]["download"]["web"]["login"]["username"], "account")
        self.assertEqual(loaded["feeds"]["sources"]["products"]["remote"]["headers"], {})
        self.assertEqual(loaded["feeds"]["sources"]["products"]["remote"]["url"], "https://supplier.test/private-path-token.xml")
        with self.assertRaises(HTTPException) as failure:
            suppliers.put_supplier_config("synthetic", {"password": "********"})
        self.assertEqual(failure.exception.status_code, 422)

    def test_shop_and_console_get_put_are_write_only_for_secrets(self):
        config_io.save_shop("synthetic", {"upgates_api_key": "upgates-secret", "upgates_login": "api-user",
                                         "upgates_full_export_url_csv": "https://shop.test/secret-export.csv"})
        returned = shops.get_shop_config("synthetic")
        self.assertEqual(returned["upgates_api_key"], "")
        self.assertTrue(returned["upgates_api_key_configured"])
        returned["verify_ssl"] = True
        self.assertNotIn("upgates-secret", json.dumps(shops.put_shop_config("synthetic", returned)))
        self.assertEqual(config_io.load_shop("synthetic")["upgates_api_key"], "upgates-secret")
        self.assertNotIn("_configured", json.dumps(config_io.load_shop("synthetic")))
        config_io.save_console({"provider": {"api_key": "provider-secret"}})
        public = configs.get_console_config()
        self.assertTrue(public["provider"]["api_key_configured"])
        self.assertNotIn("provider-secret", json.dumps(configs.post_console_config(public)))
        self.assertEqual(config_io.load_console()["provider"]["api_key"], "provider-secret")

    def test_userinfo_and_query_urls_are_redacted_and_inputs_unchanged(self):
        source = {"login_url": "https://user:private-password@host.test/login",
                  "base_url": "https://host.test/?private=query-credential", "public_url": "https://host.test/"}
        before = copy.deepcopy(source)
        public = public_config(source)
        self.assertEqual(source, before)
        self.assertNotIn("private-password", json.dumps(public))
        self.assertNotIn("query-credential", json.dumps(public))
        self.assertEqual(public["public_url"], source["public_url"])
        self.assertEqual(config_patch(source, public), source)
        for pointer in ("/name", "../../outside", "/", "/provider/__proto__/token"):
            with self.assertRaises(ConfigSecretError):
                config_patch(source, {"_clear_secrets": [pointer]})

    def test_auth_query_value_and_remote_post_body_are_write_only(self):
        original = {"feeds": {"sources": {"products": {"remote": {
            "auth": {"mode": "query", "name": "apikey", "value": "query-auth-secret"},
            "body": {"customCredential": "body-secret"},
        }}}}}
        public = public_config(original)
        self.assertNotIn("query-auth-secret", json.dumps(public))
        self.assertNotIn("body-secret", json.dumps(public))
        self.assertEqual(config_patch(original, public), original)

    def test_legacy_feed_endpoints_and_post_bodies_are_also_write_only(self):
        original = {"feed": {"url": "https://supplier.test/opaque-access-code.xml", "method": "POST",
                             "body": {"secretCredential": "legacy-body-secret"},
                             "auth": {"type": "basic", "username": "account", "password": "legacy-pass"}},
                    "price_coefficients": {"Brand": 0}, "verify_ssl": False}
        public = public_config(original)
        self.assertEqual(public["feed"]["url"], "")
        self.assertTrue(public["feed"]["url_configured"])
        self.assertEqual(public["feed"]["body"], {})
        for secret in ("opaque-access-code", "legacy-body-secret", "legacy-pass"):
            self.assertNotIn(secret, json.dumps(public))
        self.assertEqual(public["price_coefficients"], {"Brand": 0})
        self.assertIs(public["verify_ssl"], False)
        self.assertEqual(config_patch(original, public), original)

    def test_replacing_single_and_multiple_feed_endpoints_does_not_keep_hidden_old_choice(self):
        old = {"feeds": {"sources": {"products": {"remote": {"urls": ["https://old.test/feed"]}}}}}
        changed = {"feeds": {"sources": {"products": {"remote": {"url": "https://new.test/feed", "urls": []}}}}}
        remote = config_patch(old, changed)["feeds"]["sources"]["products"]["remote"]
        self.assertEqual(remote, {"url": "https://new.test/feed", "urls": []})
        next_change = {"feeds": {"sources": {"products": {"remote": {"url": "", "urls": ["https://list.test/feed"]}}}}}
        remote = config_patch(changed, next_change)["feeds"]["sources"]["products"]["remote"]
        self.assertEqual(remote, {"url": "", "urls": ["https://list.test/feed"]})

    def test_history_and_shop_identifiers_cannot_escape_configuration_root(self):
        with self.assertRaises(HTTPException):
            suppliers.get_supplier_history_version("synthetic", "../../console/config")
        for shop in ("../console", "..", "", "/etc", "a\\b"):
            with self.assertRaises(ValueError):
                config_io.shop_path(shop)

    def test_export_download_errors_do_not_echo_url_credentials(self):
        config_io.save_shop("synthetic", {"upgates_full_export_url_csv": "https://shop.test/export-private-token.csv"})
        with patch.object(shops.settings, "INVENTORY_DATA_ROOT", self.root), patch("httpx.AsyncClient") as client:
            client.return_value.__aenter__.return_value.get = AsyncMock(side_effect=RuntimeError("GET https://shop.test/export-private-token.csv failed"))
            with self.assertRaises(HTTPException) as failure:
                asyncio.run(shops.fetch_shop_export("synthetic"))
        self.assertEqual(failure.exception.status_code, 502)
        self.assertNotIn("private-token", str(failure.exception.detail))

    def test_supplier_url_warnings_do_not_echo_saved_feed_credentials(self):
        config_io.save_supplier("synthetic", self.supplier())
        with patch.object(suppliers, "_validate_url", new=AsyncMock(return_value=False)):
            result = asyncio.run(suppliers.validate_supplier_config("synthetic", check_urls=True))
        self.assertFalse(result.feed_url_reachable)
        self.assertNotIn("private-path-token", json.dumps(result.model_dump()))

    def test_http_integration_changes_require_admin_and_preserve_legacy_bearer(self):
        config_io.save_supplier("synthetic", self.supplier())
        original = config_io.supplier_path("synthetic").read_bytes()
        app = FastAPI()
        app.include_router(suppliers.router)
        app.include_router(shops.router)
        app.include_router(configs.router)

        @app.middleware("http")
        async def session_fixture(request, call_next):
            role = request.headers.get("x-test-session-role")
            if role:
                request.state.hub_user = {"role": role}
                request.state.hub_bearer = True
            return await call_next(request)

        token = "synthetic-config-admin-token-123456789"
        with patch.object(settings, "AI_CONTENT_ACCESS_TOKEN", SecretStr(token)), TestClient(app) as client:
            for method, url, payload in (
                ("POST", "/suppliers", {"code": "new", "name": "New"}),
                ("PUT", "/suppliers/synthetic/config", {"name": "changed"}),
                ("POST", "/suppliers/synthetic/config", {"name": "changed"}),
                ("POST", "/suppliers/synthetic/restore/old", {}),
                ("POST", "/suppliers/synthetic/validate?check_urls=true", {}),
                ("DELETE", "/suppliers/synthetic?confirm=true", {}),
                ("PUT", "/shops/synthetic/config", {"upgates_api_base_url": "https://other.test"}),
                ("POST", "/configs/console", {"provider": {"api_key": "changed"}}),
                ("POST", "/suppliers/synthetic/feeds/refresh?source_url=https://other.test", {}),
            ):
                with self.subTest(method=method, url=url):
                    response = client.request(method, url, json=payload, headers={"x-test-session-role": "operator"})
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(response.json()["detail"]["code"], "hub_admin_required")
            self.assertEqual(config_io.supplier_path("synthetic").read_bytes(), original)
            self.assertFalse(config_io.shop_path("synthetic").exists())
            self.assertFalse(config_io.supplier_path("new").exists())
            self.assertEqual(client.put("/shops/synthetic/config", json={"upgates_api_key": "admin-key"},
                                        headers={"x-test-session-role": "admin"}).status_code, 200)
            response = client.put("/shops/synthetic/config", json={"upgates_api_key": "token-key"},
                                  headers={"Authorization": "Bearer " + token})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["upgates_api_key"], "")
            self.assertTrue(response.json()["upgates_api_key_configured"])
            self.assertEqual(config_io.load_shop("synthetic")["upgates_api_key"], "token-key")
            self.assertEqual(client.put("/shops/synthetic/config", json={},
                                        headers={"Authorization": "Bearer wrong"}).status_code, 401)
            # A normal configured refresh remains an operational action. Stop
            # before any real request while proving this guard permits it.
            with patch.object(suppliers, "io_load_supplier", return_value={}):
                response = client.post("/suppliers/synthetic/feeds/refresh", headers={"x-test-session-role": "operator"})
            self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
