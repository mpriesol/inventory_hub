"""Fresh identity reads, without a live shop, database or catalog pull."""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests

from inventory_hub.services import product_link_source as source
from inventory_hub.services.upgates import connection_fingerprint
from inventory_hub.services.upgates_budget import AdmissionDenied


BASE = "https://fixture.invalid/api/v2"
FINGERPRINT = connection_fingerprint(BASE, "fixture-login")
EAN = "8585047234448"
SHOP_URL = "https://fixture.invalid/p/jacket"
ADMIN_URL = "https://fixture.invalid/admin/products/7"


def page(field, rows, *, number=1, pages=1, total=None):
    return {field: rows, "current_page": number, "number_of_pages": pages,
            "number_of_items": len(rows) if total is None else total}


def variant(code="LEAF-M", identifier=71):
    return {"code": code, "variant_id": identifier, "product_code": "PARENT", "product_id": 7, "ean": EAN}


def family(*variants):
    return {"code": "PARENT", "product_id": 7, "variants_exists_yn": True,
            "descriptions": [{"language": "sk", "title": "Jacket"}], "variants": list(variants)}


def parent_metadata(**changes):
    return {**family(), "descriptions": [{"language": "sk", "title": "Jacket", "url": SHOP_URL}],
            "admin_url": ADMIN_URL, **changes}


def detailed(code="LEAF-M", identifier=71):
    return {**variant(code, identifier), "parameters_new": [
        {"id": 1, "descriptions": [{"language": "sk", "name": "Farba"}],
         "values": [{"id": 2, "descriptions": [{"language": "sk", "value": "Modrá"}]}]},
        {"id": 3, "descriptions": [{"language": "sk", "name": "Veľkosť"}],
         "values": [{"id": 4, "descriptions": [{"language": "sk", "value": "M"}]}]},
    ]}


class Response:
    def __init__(self, data, status=200, headers=None):
        self.body = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.status_code, self.headers, self.closed = status, headers or {}, False

    def iter_content(self, size):
        for offset in range(0, len(self.body), size):
            yield self.body[offset:offset+size]

    def close(self):
        self.closed = True


class Session:
    auth = ("fixture-login", "fixture-key")

    def __init__(self, replies):
        self.replies, self.calls, self.closed = replies, [], False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        reply = self.replies[len(self.calls)-1]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def close(self):
        self.closed = True


class ProductLinkSourceTests(unittest.IsolatedAsyncioTestCase):
    def setup_client(self, *data):
        replies = [r if isinstance(r, (Response, Exception)) else Response(r) for r in data]
        self.session = Session(replies)
        self.responses = replies
        client = SimpleNamespace(base_url=BASE, session=self.session, log_dir=None)
        patcher = patch.object(source.UpgatesClient, "from_shop", return_value=client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def discovery(self, *variants):
        details = [{**detailed(v["code"], v["variant_id"]), **v} for v in variants]
        self.setup_client(page("products", []), page("variants", details), page("products", [parent_metadata()]))

    async def test_deleted_variant_is_verified_by_native_parent_not_old_code(self):
        self.setup_client(page("products", [family(variant("RENAMED-SIBLING", 72))]), page("products", []))
        result = await source.verify_deleted_variants("biketrek", [{"is_variant": True,
            "external_product_id": "7", "external_variant_id": "71"}], FINGERPRINT)
        self.assertEqual(result["requests"], 2)
        self.assertEqual(self.session.calls[0][0], BASE + "/products/simple")
        self.assertEqual(self.session.calls[0][1]["params"],
                         {"product_ids": "7", "variants_yn": "true", "archived_yn": "false", "page": 1, "current_page_items": 100})

    async def test_renamed_or_inactive_original_variant_still_blocks_replacement(self):
        self.setup_client(page("products", []), page("products", [family({**variant("RENAMED", 71), "active_yn": False})]))
        with self.assertRaisesRegex(source.SourceError, "product_links_variant_still_exists"):
            await source.verify_deleted_variants("biketrek", [{"is_variant": True,
                "external_product_id": "7", "external_variant_id": "71"}], FINGERPRINT)

    async def test_missing_variant_array_does_not_prove_deletion(self):
        parent = family()
        parent.pop("variants")
        self.setup_client(page("products", [parent]), page("products", []))
        with self.assertRaisesRegex(source.SourceError, "source_rows_invalid"):
            await source.verify_deleted_variants("biketrek", [{"is_variant": True,
                "external_product_id": "7", "external_variant_id": "71"}], FINGERPRINT)

    async def test_absence_from_both_archive_partitions_allows_deleted_parent(self):
        self.setup_client(page("products", []), page("products", []))
        result = await source.verify_deleted_variants("biketrek", [{"is_variant": True,
            "external_product_id": "7", "external_variant_id": "71"}], FINGERPRINT)
        self.assertEqual(result["requests"], 2)
        self.assertEqual([kwargs["params"]["archived_yn"] for _, kwargs in self.session.calls], ["false", "true"])

    async def test_parent_in_both_archive_partitions_cannot_prove_deletion(self):
        self.setup_client(page("products", [family(variant("SIBLING", 72))]),
                          page("products", [family(variant("SIBLING", 72))]))
        with self.assertRaisesRegex(source.SourceError, "source_filter_mismatch"):
            await source.verify_deleted_variants("biketrek", [{"is_variant": True,
                "external_product_id": "7", "external_variant_id": "71"}], FINGERPRINT)

    async def test_exact_variant_filter_and_separate_documented_axes(self):
        self.discovery(variant())
        result = await source.discover("xtrek", ["LEAF-M"])
        self.assertEqual(result["requests"], 3)
        self.assertEqual(result["fingerprint"], FINGERPRINT)
        self.assertEqual(len(result["leaves"]), 1)
        leaf = result["leaves"][0]
        self.assertEqual((leaf["code"], leaf["parent_code"], leaf["external_id"], leaf["parent_id"]),
                         ("LEAF-M", "PARENT", "71", "7"))
        self.assertEqual(leaf["attributes"], [{"name": "Farba", "value": "Modrá"}, {"name": "Veľkosť", "value": "M"}])
        self.assertEqual(leaf["warnings"], [])
        self.assertEqual((leaf["name"], leaf["shop_url"], leaf["shop_admin_url"]),
                         ("Jacket", SHOP_URL, ADMIN_URL))
        for url, kwargs in self.session.calls:
            self.assertTrue(url.startswith(BASE + "/products"))
            self.assertTrue(kwargs["verify"])
            self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual([call[1]["params"] for call in self.session.calls], [
            {"codes": "LEAF-M", "variants_yn": "false", "page": 1, "current_page_items": 100},
            {"variant_codes": "LEAF-M", "page": 1, "current_page_items": 100},
            {"codes": "PARENT", "variants_yn": "false", "page": 1, "current_page_items": 100},
        ])
        self.assertTrue(self.session.calls[1][0].endswith("/products/variants"))
        self.assertTrue(self.session.closed and all(r.closed for r in self.responses))
        for field in ("price", "stock", "shop_price", "shop_stock", "api_key"):
            self.assertNotIn(field, leaf)

    async def test_missing_axes_are_unknown_without_parent_fallback(self):
        self.setup_client(page("products", []), page("variants", [variant()]), page("products", [parent_metadata()]))
        result = await source.discover("xtrek", ["LEAF-M"])
        self.assertEqual(result["leaves"][0]["attributes"], [])
        self.assertIn("variant_axes_unknown", result["leaves"][0]["warnings"])

    async def test_large_pos_parent_never_requires_siblings(self):
        self.discovery(variant())
        result = await source.discover("xtrek", ["LEAF-M"])
        self.assertEqual(len(result["leaves"]), 1)
        self.assertTrue(all(call[0].endswith("/products/variants") or call[1]["params"]["variants_yn"] == "false"
                            for call in self.session.calls))
        self.assertEqual(result["leaves"][0]["eans"], [EAN])

    async def test_standalone_and_variant_native_id_namespaces_are_distinct(self):
        single = {"code": "LEAF-M", "product_id": 71, "ean": EAN, "stock": 99, "variants": [], "variants_exists_yn": False}
        self.setup_client(page("products", [single]), page("variants", [detailed()]), page("products", [parent_metadata()]))
        result = await source.discover("xtrek", ["LEAF-M"])
        self.assertEqual(len(result["leaves"]), 2)
        self.assertEqual({(v["is_variant"], v["external_id"]) for v in result["leaves"]}, {(False, "71"), (True, "71")})

    async def test_family_parent_never_becomes_leaf_even_when_variants_absent(self):
        for parent in (family(), {"code": "PARENT", "product_id": 7, "variants_exists_yn": True}):
            self.setup_client(page("products", [parent]), page("variants", []))
            result = await source.discover("xtrek", ["PARENT"])
            self.assertEqual(result["leaves"], [])

    async def test_code_inspection_keeps_non_stock_parent_and_set_collisions(self):
        parent = parent_metadata()
        bundle = {"code": "TP-SET", "product_id": 8, "variants_exists_yn": False, "set_yn": True}
        self.setup_client(page("products", [parent, bundle]), page("variants", []))
        result = await source.inspect_codes("xtrek", ["PARENT", "TP-SET", "TP-FREE"])
        self.assertEqual(result["leaves"], [])
        self.assertEqual(result["occupied_codes"], [
            {"code": "PARENT", "kind": "parent", "native_id": "7", "parent_id": "7", "parent_code": "PARENT"},
            {"code": "TP-SET", "kind": "set", "native_id": "8", "parent_id": "8", "parent_code": "TP-SET"},
        ])
        self.assertEqual(result["requests"], 2, "Code occupancy uses the existing batched GETs")

    async def test_code_inspection_retains_case_only_product_and_variant_occupancy(self):
        single = {"code": "tp-Single", "product_id": 9, "variants_exists_yn": False}
        self.setup_client(page("products", [single]), page("variants", [variant("tp-Leaf")]))
        result = await source.inspect_codes("xtrek", ["TP-SINGLE", "TP-LEAF"])
        self.assertEqual(result["leaves"], [], "Case-only matches are not silently promoted to exact leaves")
        self.assertEqual(result["occupied_codes"], [
            {"code": "tp-Leaf", "kind": "variant", "native_id": "71", "parent_id": "7", "parent_code": "PARENT"},
            {"code": "tp-Single", "kind": "product", "native_id": "9", "parent_id": "9", "parent_code": "tp-Single"},
        ])
        self.assertEqual(result["requests"], 2)

    async def test_code_inspection_preserves_exact_native_leaf_and_parent_evidence(self):
        self.discovery(variant())
        result = await source.inspect_codes("xtrek", ["LEAF-M", "TP-NEW"], FINGERPRINT)
        self.assertEqual(result["occupied_codes"], [
            {"code": "LEAF-M", "kind": "variant", "native_id": "71", "parent_id": "7", "parent_code": "PARENT"},
        ])
        leaf = result["leaves"][0]
        self.assertEqual((leaf["external_id"], leaf["parent_id"], leaf["parent_code"], leaf["eans"]),
                         ("71", "7", "PARENT", [EAN]))
        self.assertEqual(result["requests"], 3)
        self.assertEqual(self.session.calls[0][1]["params"]["codes"], "LEAF-M;TP-NEW")
        self.assertEqual(self.session.calls[1][1]["params"]["variant_codes"], "LEAF-M;TP-NEW")

    async def test_code_inspection_rejects_invalid_native_case_only_occupancy(self):
        self.setup_client(page("products", []), page("variants", [variant("leaf-m", None)]))
        with self.assertRaises(source.SourceError) as raised:
            await source.inspect_codes("xtrek", ["LEAF-M"])
        self.assertEqual(raised.exception.code, "source_native_id_invalid")

    async def test_code_inspection_honors_request_allowance_and_quota_without_retry(self):
        self.discovery(variant())
        with self.assertRaises(source.SourceError) as raised:
            await source.inspect_codes("xtrek", ["LEAF-M"], max_requests=2)
        self.assertEqual(raised.exception.code, "source_request_limit")
        self.assertEqual(len(self.session.calls), 2)
        self.setup_client(Response({}, 429, {"Retry-After": "120"}))
        with self.assertRaises(source.SourceError) as raised:
            await source.inspect_codes("xtrek", ["LEAF-M"])
        self.assertEqual((raised.exception.code, raised.exception.status, raised.exception.retry_after),
                         ("source_rate_limited", 429, 120))
        self.assertEqual(len(self.session.calls), 1)
        self.assertTrue(self.session.closed)

    async def test_hundred_variant_batch_reads_parent_metadata_once_not_per_product(self):
        variants = [variant("SKU-"+str(i), i+1) for i in range(100)]
        details = [detailed(v["code"], v["variant_id"]) for v in variants]
        self.setup_client(page("products", []), page("variants", details), page("products", [parent_metadata()]))
        result = await source.discover("xtrek", [v["code"] for v in variants])
        self.assertEqual((len(result["leaves"]), result["requests"]), (100, 3))
        self.assertEqual(self.session.calls[-1][1]["params"]["codes"], "PARENT")

    async def test_empty_or_injected_filters_never_contact_shop(self):
        self.setup_client()
        for codes in ([], ["X;Y"], ["X\n"], [" X"], ["X\x7f"], ["X"]*101):
            with self.subTest(codes=codes), self.assertRaises(source.SourceError) as raised:
                await source.discover("xtrek", codes)
            self.assertEqual(raised.exception.code, "source_codes_invalid")
        self.assertEqual(self.session.calls, [])

    async def test_fingerprint_changed_before_network(self):
        self.setup_client()
        with self.assertRaises(source.SourceError) as raised:
            await source.discover("xtrek", ["X"], "different")
        self.assertEqual(raised.exception.code, "source_connection_changed")
        self.assertEqual(self.session.calls, [])
        self.assertTrue(self.session.closed)

    async def test_request_allowance_includes_axes_reads(self):
        self.discovery(variant())
        with self.assertRaises(source.SourceError) as raised:
            await source.discover("xtrek", ["LEAF-M"], max_requests=1)
        self.assertEqual(raised.exception.code, "source_request_limit")
        self.assertEqual(len(self.session.calls), 1)

    async def test_parent_metadata_read_uses_the_same_request_allowance(self):
        self.discovery(variant())
        with self.assertRaises(source.SourceError) as raised:
            await source.discover("xtrek", ["LEAF-M"], max_requests=2)
        self.assertEqual(raised.exception.code, "source_request_limit")
        self.assertEqual(len(self.session.calls), 2, "The metadata repair must not bypass the API admission allowance")

    async def test_filtered_response_cannot_silently_be_full_catalog(self):
        self.setup_client(page("products", [{"code": "OTHER", "product_id": 9, "variants": []}]))
        with self.assertRaises(source.SourceError) as raised:
            await source.discover("xtrek", ["X"])
        self.assertEqual(raised.exception.code, "source_filter_mismatch")

    async def test_duplicate_leaf_or_reused_id_is_rejected(self):
        for second, error in ((variant(), "source_duplicate_variant"),
                              (variant("LEAF-S", 71), "source_duplicate_variant_id")):
            with self.subTest(error=error):
                self.discovery(variant(), second)
                with self.assertRaises(source.SourceError) as raised:
                    await source.discover("xtrek", ["LEAF-M", "LEAF-S"])
                self.assertEqual(raised.exception.code, error)

    async def test_native_parent_id_missing_and_invalid_axis_shapes_block(self):
        for changes, code in (({"variant_id": None}, "source_native_id_invalid"),
                              ({"product_id": None}, "source_native_id_invalid"),
                              ({"product_code": None}, "source_code_invalid"),
                              ({"parameters_new": "wrong"}, "source_parameters_invalid")):
            with self.subTest(changes=changes):
                self.setup_client(page("products", []), page("variants", [{**detailed(), **changes}]))
                with self.assertRaises(source.SourceError) as raised:
                    await source.discover("xtrek", ["LEAF-M"])
                self.assertEqual(raised.exception.code, code)

    async def test_standalone_requires_explicit_kind_and_excludes_sets(self):
        for row, error in (({"code": "A", "product_id": 1}, "source_product_kind_unknown"),
                            ({"code": "A", "product_id": 1, "variants_exists_yn": "false"}, "source_product_kind_unknown")):
            self.setup_client(page("products", [row]))
            with self.assertRaises(source.SourceError) as raised:
                await source.discover("xtrek", ["A"])
            self.assertEqual(raised.exception.code, error)
        self.setup_client(page("products", [{"code": "A", "product_id": 1, "variants_exists_yn": False, "set_yn": True}]),
                          page("variants", []))
        self.assertEqual((await source.discover("xtrek", ["A"]))["leaves"], [])

    async def test_pagination_consumes_all_filtered_pages_and_checks_totals(self):
        a = {"code": "A", "product_id": 1, "variants_exists_yn": False}; b = {"code": "B", "product_id": 2, "variants_exists_yn": False}
        self.setup_client(page("products", [a], pages=2, total=2),
                          page("products", [b], number=2, pages=2, total=2), page("variants", []))
        result = await source.discover("xtrek", ["A", "B"])
        self.assertEqual(result["requests"], 3)
        self.assertEqual([v["code"] for v in result["leaves"]], ["A", "B"])
        self.setup_client(page("products", [a], total=2))
        with self.assertRaises(source.SourceError) as raised:
            await source.discover("xtrek", ["A"])
        self.assertEqual(raised.exception.code, "source_pagination_incomplete")

    async def test_malformed_json_and_bounded_body(self):
        for body, code in ((b'{"products":[],"products":[]}', "source_duplicate_json_key"),
                           (b'{"x":NaN}', "source_nonfinite_json"),
                           (b'invalid', "source_json_invalid"),
                           (b' '*(source.MAX_BYTES+1), "source_response_limit")):
            self.setup_client(Response(body))
            with self.subTest(code=code), self.assertRaises(source.SourceError) as raised:
                await source.discover("xtrek", ["X"])
            self.assertEqual(raised.exception.code, code)
            self.assertTrue(self.responses[0].closed)

    async def test_quota_auth_network_failures_are_sanitized_and_not_retried(self):
        for reply, code, status, delay in ((Response({}, 429, {"Retry-After": "120"}), "source_rate_limited", 429, 120),
                                           (Response({}, 401), "source_authentication_failed", 502, None),
                                           (AdmissionDenied(60), "source_rate_limited", 429, 60),
                                           (requests.ConnectionError("secret URL"), "source_unavailable", 502, None)):
            self.setup_client(reply)
            with self.subTest(code=code), self.assertRaises(source.SourceError) as raised:
                await source.discover("xtrek", ["X"])
            self.assertEqual((raised.exception.code, raised.exception.status, raised.exception.retry_after), (code, status, delay))
            self.assertNotIn("secret", str(raised.exception))
            self.assertEqual(len(self.session.calls), 1)

    async def test_verify_checks_selected_kind_parent_and_native_id(self):
        self.discovery(variant())
        result = await source.discover("xtrek", ["LEAF-M"])
        self.discovery(variant())
        checked = await source.verify("xtrek", result["leaves"], FINGERPRINT)
        self.assertEqual(checked["leaves"], result["leaves"])
        for key, value in (("parent_id", "99"), ("parent_code", "OTHER"), ("external_id", "99"), ("is_variant", False)):
            self.discovery(variant())
            expected = copy.deepcopy(result["leaves"]); expected[0][key] = value
            with self.subTest(key=key), self.assertRaises(source.SourceError) as raised:
                await source.verify("xtrek", expected, FINGERPRINT)
            self.assertEqual(raised.exception.code, "source_identity_changed")

    async def test_manual_verify_ignores_other_parent_but_never_switches(self):
        self.discovery(variant())
        expected = (await source.discover("xtrek", ["LEAF-M"]))["leaves"]
        other = {**detailed(), "product_code": "OTHER", "product_id": 8, "variant_id": 81}
        other_parent = parent_metadata(code="OTHER", product_id=8)
        self.setup_client(page("products", []), page("variants", [detailed(), other]),
                          page("products", [parent_metadata(), other_parent]))
        result = await source.verify("xtrek", expected, FINGERPRINT)
        self.assertEqual(result["leaves"], expected)
        self.setup_client(page("products", []), page("variants", [other]), page("products", [other_parent]))
        with self.assertRaises(source.SourceError) as raised:
            await source.verify("xtrek", expected, FINGERPRINT)
        self.assertEqual(raised.exception.code, "source_identity_changed")

    async def test_parent_metadata_already_in_first_filtered_page_is_not_requested_twice(self):
        self.setup_client(page("products", [parent_metadata()]), page("variants", [detailed()]))
        result = await source.discover("xtrek", ["PARENT", "LEAF-M"])
        self.assertEqual(result["requests"], 2)
        self.assertEqual(len(result["leaves"]), 1, "A family parent is never a physical leaf")
        self.assertEqual(result["leaves"][0]["shop_url"], SHOP_URL)

    async def test_parent_metadata_must_belong_to_selected_native_parent(self):
        for parent in (parent_metadata(product_id=99), parent_metadata(code="OTHER"),
                       parent_metadata(variants_exists_yn=False)):
            with self.subTest(parent=parent):
                self.setup_client(page("products", []), page("variants", [detailed()]), page("products", [parent]))
                with self.assertRaises(source.SourceError):
                    await source.discover("xtrek", ["LEAF-M"])

    async def test_missing_or_unsafe_urls_are_unknown_without_invented_admin_target(self):
        for url in (None, "javascript:alert(1)", "/relative-product", "https://user:password@fixture.invalid/p/7",
                    "https://fixture.invalid/\nunsafe"):
            with self.subTest(url=url):
                parent = parent_metadata(descriptions=[{"language": "sk", "title": "Jacket", "url": url}], admin_url=url)
                self.setup_client(page("products", []), page("variants", [detailed()]), page("products", [parent]))
                leaf = (await source.discover("xtrek", ["LEAF-M"]))["leaves"][0]
                self.assertIsNone(leaf["shop_url"])
                self.assertIsNone(leaf["shop_admin_url"])
                self.assertEqual(leaf["parent_id"], "7", "Native parent ID alone does not invent a navigation URL")

    async def test_explicit_verified_leaf_navigation_takes_precedence_over_parent_navigation(self):
        leaf_url = "https://fixture.invalid/p/jacket?variant=71"
        leaf_admin = "https://fixture.invalid/admin/products/7?variant=71"
        self.discovery({**variant(), "descriptions": [{"language": "sk", "url": leaf_url}],
                        "admin_url": leaf_admin})
        leaf = (await source.discover("xtrek", ["LEAF-M"]))["leaves"][0]
        self.assertEqual((leaf["shop_url"], leaf["shop_admin_url"]), (leaf_url, leaf_admin))

    async def test_raw_invalid_barcodes_remain_visible_but_never_become_verified_identity(self):
        raw = [" 8585047234448/legacy-note ", "8585047234449", "00000000"]
        self.discovery({**variant(), "ean": raw})
        leaf = (await source.discover("xtrek", ["LEAF-M"]))["leaves"][0]
        self.assertEqual(leaf["raw_barcodes"], raw)
        self.assertEqual(leaf["eans"], [EAN])
        self.assertCountEqual(leaf["invalid_barcodes"], ["legacy-note", "8585047234449", "00000000"])
        self.assertIn("unverified_barcode", leaf["warnings"])
        self.discovery({**variant(), "ean": 0})
        zero = (await source.discover("xtrek", ["LEAF-M"]))["leaves"][0]
        self.assertEqual((zero["raw_barcodes"], zero["invalid_barcodes"], zero["eans"]), (["0"], ["0"], []))
        self.assertIn("unverified_barcode", zero["warnings"])

    async def test_current_connection_fingerprint_reads_configuration_without_contacting_shop(self):
        self.setup_client()
        self.assertEqual(await source.current_fingerprint("xtrek"), FINGERPRINT)
        self.assertEqual(self.session.calls, [])
        self.assertTrue(self.session.closed)


if __name__ == "__main__":
    unittest.main()
