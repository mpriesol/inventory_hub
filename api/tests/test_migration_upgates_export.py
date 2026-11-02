"""A failed/partial read must never become a usable migration snapshot."""
import io
import json
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch
import zipfile

import test_migration_export as base_tests
from inventory_hub.services import migration_upgates_export as service
from inventory_hub.routers import migration_export as routes


def page(rows, n=1, pages=1, count=None, key="products"):
    return {"current_page": n, "current_page_items": len(rows),
            "number_of_items": len(rows) if count is None else count,
            "number_of_pages": pages, key: rows}


class RemoteExportTests(unittest.TestCase):
    def build(self, responses, dataset="parameters"):
        client = SimpleNamespace(session=Mock())
        with patch.object(service, "_read", side_effect=responses) as read:
            result = service.build_remote_export("xtrek", dataset, client=client)
        client.session.post.assert_not_called()
        client.session.put.assert_not_called()
        client.session.delete.assert_not_called()
        return result, read

    def test_complete_pages_preserve_ids_null_zero_html_and_decimals(self):
        responses = [page([{"product_id": 1, "code": "00042", "parameters": [],
                           "stock": None, "price": Decimal("19.123456789"), "html": "<b>text</b>"}], pages=2, count=2),
                     page([{"product_id": 2, "stock": 0}], n=2, pages=2, count=2)]
        result, read = self.build(responses)
        with result, zipfile.ZipFile(result) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["count"], 2)
            self.assertTrue(manifest["complete"])
            row = json.loads(archive.read(manifest["pages"][0]["file"]))["products"][0]
            self.assertEqual(row["code"], "00042")
            self.assertEqual(row["price"], "19.123456789")
            self.assertEqual(row["html"], "<b>text</b>")
            self.assertIsNone(row["stock"])
        self.assertEqual(read.call_args_list[1].args[2], {"page": 2, "current_page_items": 100})

    def test_incomplete_duplicate_or_changing_pages_fail_closed(self):
        cases = [
            [page([{"product_id": 1}], count=2)],
            [page([{"product_id": 1}], pages=2, count=2), page([], n=2, pages=2, count=2)],
            [page([{"product_id": 1}], pages=2, count=2), page([{"product_id": 1}], n=2, pages=2, count=2)],
            [page([{"product_id": 1}], pages=2, count=2), page([{"product_id": 2}], n=2, pages=2, count=3)],
            [page([{"code": "no-id"}])],
            [page([{"product_id": True}])],
            [{"products": []}],
        ]
        for response in cases:
            with self.subTest(response=response), self.assertRaises(service.ExportError):
                self.build(response)

    def test_empty_catalog_is_explicit_not_missing(self):
        output, _ = self.build([page([], pages=0)])
        with output, zipfile.ZipFile(output) as archive:
            self.assertEqual(json.loads(archive.read("manifest.json"))["count"], 0)

    def test_all_availability_types_requested(self):
        values = ("OnRequest", "NotAvailable", "InStock", "Custom")
        output, read = self.build([page([{"id": n, "type": v}], key="availabilities")
                                   for n, v in enumerate(values)], "availabilities")
        output.close()
        self.assertEqual([c.args[2]["type"] for c in read.call_args_list], list(values))

    def test_identical_availability_filter_overlap_is_preserved_but_counted(self):
        result, _ = self.build([page([{"id": 1, "type": "InStock"}], key="availabilities")] * 4, "availabilities")
        with result, zipfile.ZipFile(result) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["count"], 4)
            self.assertEqual(manifest["unique_count"], 1)
            self.assertEqual(len(manifest["pages"]), 4)
        with self.assertRaisesRegex(service.ExportError, "identity_changed_between_filters"):
            self.build([page([{"id": 1, "type": "InStock"}], key="availabilities"),
                        page([{"id": 1, "type": "Custom"}], key="availabilities")], "availabilities")

    def test_config_allowlist_excludes_secrets(self):
        output, _ = self.build([{"config": {"prices_with_vat_yn": True, "password": "MUST-NOT-EXPORT"},
                                "unrelated": "MUST-NOT-EXPORT"}], "config")
        with output, zipfile.ZipFile(output) as archive:
            for name in archive.namelist():
                self.assertNotIn(b"MUST-NOT-EXPORT", archive.read(name))

    def test_invalid_source_never_constructs_client(self):
        with patch.object(service.UpgatesClient, "from_shop") as factory:
            for shop, dataset in [("https://evil.test", "products"), ("xtrek", "orders")]:
                with self.assertRaises(service.ExportError):
                    service.build_remote_export(shop, dataset)
            factory.assert_not_called()

    def test_transport_does_not_follow_redirects_retry_or_echo_error(self):
        response = Mock(status_code=401, content=b"SECRET", text="SECRET")
        client = SimpleNamespace(base_url="https://example.test/api/v2", verify_ssl=True, session=Mock())
        client.session.get.return_value = response
        with self.assertRaisesRegex(service.ExportError, "^upgates_export_http_401$"):
            service._read(client, "products", {"page": 1})
        self.assertEqual(client.session.get.call_count, 1)
        self.assertFalse(client.session.get.call_args.kwargs["allow_redirects"])
        response.close.assert_called_once()

    def test_transport_decimal_parser_and_budget_rejection(self):
        response = Mock(status_code=200, content=b"{}")
        response.json.return_value = {"products": []}
        client = SimpleNamespace(base_url="https://example.test/api/v2", verify_ssl=True, session=Mock())
        client.session.get.return_value = response
        service._read(client, "products", {})
        response.json.assert_called_once_with(parse_float=Decimal)
        client.session.get.side_effect = service.AdmissionDenied()
        with self.assertRaisesRegex(service.ExportError, "budget_deferred"):
            service._read(client, "products", {})


class RemoteExportRouteTests(base_tests.ExportRoutesTests):
    def test_supplement_auth_and_allowlist_before_remote_read(self):
        with patch.object(routes, "build_remote_export") as build:
            self.assertEqual(self.client.get("/migration/upgates-export?dataset=products").status_code, 401)
            self.assertEqual(self.client.get("/migration/upgates-export?dataset=orders", headers=self.headers).status_code, 422)
            self.assertEqual(self.client.get("/migration/upgates-export?dataset=products&shop_code=other", headers=self.headers).status_code, 422)
            build.assert_not_called()
        self.assertEqual(self.sessions, [])

    def test_supplement_download_closes_output_without_business_db_session(self):
        output = io.BytesIO(b"complete-archive")
        with patch.object(routes, "build_remote_export", return_value=output) as build:
            response = self.client.get("/migration/upgates-export?dataset=parameters&shop_code=xtrek", headers=self.headers)
        self.assertEqual(response.content, b"complete-archive")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertTrue(output.closed)
        self.assertEqual(self.sessions, [])
        build.assert_called_once_with("xtrek", "parameters")

    def test_supplement_failed_read_is_not_a_zip(self):
        with patch.object(routes, "build_remote_export", side_effect=service.ExportError("upgates_export_incomplete_count")):
            response = self.client.get("/migration/upgates-export?dataset=parameters", headers=self.headers)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {"detail": {"code": "upgates_export_incomplete_count"}})
