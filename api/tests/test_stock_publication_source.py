"""Synthetic single-leaf transport, identity and uncertain-write regressions."""
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock, patch

import requests

from inventory_hub.services import stock_publication_source as source
from inventory_hub.services.upgates import UpgatesClient


BASE = "https://stock-test.invalid/api/v2"
FINGERPRINT = source.connection_fingerprint(BASE, "reader")
SIMPLE = {"code": "SKU-1", "parent_code": "SKU-1", "variant_code": None}
VARIANT = {"code": "SKU-1", "parent_code": "POS-UMBRELLA", "variant_code": "SKU-1"}


def identity(variant=False):
    return {**(VARIANT if variant else SIMPLE), "product_id": 41, "variant_id": 71 if variant else None}


def observation(variant=False, stock=3):
    item = {"code": "SKU-1", "product_id": 41, "stock": stock}
    if variant:
        item.update(variant_id=71, product_code="POS-UMBRELLA")
    else:
        item.update(variants_exists_yn=False, variants=[], set_yn=False)
    return {"current_page": 1, "current_page_items": 1, "number_of_items": 1, "number_of_pages": 1,
            "variants" if variant else "products": [item]}


def acknowledgement(variant=False):
    item = {"code": "POS-UMBRELLA" if variant else "SKU-1", "product_id": 41, "updated_yn": True,
            "messages": [{"level": "info", "message": "private upstream success"}]}
    if variant:
        item["variants"] = [{"code": "SKU-1", "variant_id": 71, "updated_yn": True, "messages": []}]
    return {"products": [item]}


class StockPublicationSourceTests(IsolatedAsyncioTestCase):
    def client(self, body, *, status=200, headers=None, raw=None):
        client = UpgatesClient(BASE, "reader", "synthetic", verify_ssl=False)
        client.session = Mock(auth=("reader", "synthetic"))
        response = Mock(status_code=status, headers=headers or {})
        data = raw if raw is not None else json.dumps(body).encode()
        response.iter_content.return_value = [data]
        client.session.get.return_value = response
        client.session.put.return_value = response
        return client, response

    async def read(self, body=None, *, target=None, **kwargs):
        client, response = self.client(body if body is not None else observation(), **kwargs)
        with patch.object(source.UpgatesClient, "from_shop", return_value=client), patch.object(client, "_log") as log:
            result = await source.read_stock("biketrek", target or SIMPLE, FINGERPRINT)
        log.assert_not_called()
        client.session.close.assert_called_once()
        response.close.assert_called_once()
        client.session.put.assert_not_called()
        client.session.post.assert_not_called()
        return result, client

    async def write(self, body=None, *, frozen=None, quantity="3", **kwargs):
        client, response = self.client(body if body is not None else acknowledgement(), **kwargs)
        with patch.object(source.UpgatesClient, "from_shop", return_value=client), patch.object(client, "_log") as log:
            result = await source.write_stock_once("biketrek", frozen or identity(), quantity, FINGERPRINT)
        log.assert_not_called()
        client.session.close.assert_called_once()
        response.close.assert_called_once()
        client.session.get.assert_not_called()
        client.session.post.assert_not_called()
        return result, client

    async def test_standalone_read_uses_exact_simple_detail_and_forces_safe_transport(self):
        result, client = await self.read()
        self.assertEqual(result, {"identity": identity(), "quantity": "3"})
        client.session.get.assert_called_once_with(BASE + "/products/SKU-1/simple", params={}, timeout=(5, 25),
            verify=True, allow_redirects=False, stream=True)

    async def test_variant_read_uses_only_leaf_filter_without_fetching_parent_or_thousands_of_siblings(self):
        result, client = await self.read(observation(True), target=VARIANT)
        self.assertEqual(result, {"identity": identity(True), "quantity": "3"})
        client.session.get.assert_called_once_with(BASE + "/products/variants",
            params={"variant_codes": "SKU-1", "page": 1, "current_page_items": 100}, timeout=(5, 25),
            verify=True, allow_redirects=False, stream=True)
        body = observation()
        body["products"][0].update(variants_exists_yn=True,
            variants=[{"code": f"SIBLING-{number}"} for number in range(5000)])
        with self.assertRaises(source.SourceError) as raised:
            await self.read(body)
        self.assertEqual(raised.exception.code, "stock_publication_identity_changed")

    async def test_standalone_code_is_url_encoded_as_one_component(self):
        target = {**SIMPLE, "code": "SKU/A ?ž#", "parent_code": "SKU/A ?ž#"}
        body = observation()
        body["products"][0]["code"] = target["code"]
        _, client = await self.read(body, target=target)
        self.assertEqual(client.session.get.call_args.args[0], BASE + "/products/SKU%2FA%20%3F%C5%BE%23/simple")

    async def test_existing_negative_stock_and_decimal_whole_number_are_observed_without_clamping(self):
        for stock, expected in ((-3, "-3"), (0, "0"), ("3.0000", "3"), ("1e3", "1000")):
            with self.subTest(stock=stock):
                result, _ = await self.read(observation(stock=stock))
                self.assertEqual(result["quantity"], expected)
        result, _ = await self.read(raw=json.dumps(observation()).replace('"stock": 3', '"stock": 3.0').encode())
        self.assertEqual(result["quantity"], "3")

    async def test_unknown_fractional_or_malformed_remote_stock_never_becomes_zero(self):
        for stock in (None, True, False, "NaN", "Infinity", "1.1", " 3", "3 ", "1_000", "1,000", "1e9999999", {}, []):
            with self.subTest(stock=stock), self.assertRaises(source.SourceError) as raised:
                await self.read(observation(stock=stock))
            self.assertEqual(raised.exception.code, "stock_publication_stock_unknown")
            self.assertFalse(raised.exception.uncertain)

    async def test_remote_typed_ids_parent_and_standalone_flags_are_required(self):
        for variant, changes in ((False, {"product_id": True}), (False, {"product_id": "41"}),
                (False, {"variants_exists_yn": None}), (False, {"variants_exists_yn": "false"}),
                (False, {"set_yn": True}), (False, {"set_yn": "true"}), (False, {"code": "sku-1"}),
                (True, {"product_code": "OTHER-PARENT"}), (True, {"variant_id": "71"}),
                (True, {"variant_id": True}), (True, {"product_id": 0})):
            body = observation(variant)
            body["variants" if variant else "products"][0].update(changes)
            with self.subTest(variant=variant, changes=changes), self.assertRaises(source.SourceError) as raised:
                await self.read(body, target=VARIANT if variant else SIMPLE)
            self.assertEqual(raised.exception.code, "stock_publication_identity_changed")
        for variant in (False, True):
            with self.assertRaises(source.SourceError):
                await self.read(observation(variant), target={**identity(variant), "product_id": 999})

    async def test_empty_ambiguous_extra_or_inconsistent_pages_fail_without_following_pagination(self):
        good = observation(True)
        bodies = [dict(good, variants=good["variants"] * 2), dict(good, number_of_items=2),
                  dict(good, number_of_pages=2), dict(good, current_page=2), dict(good, current_page_items=True),
                  {key: value for key, value in good.items() if key != "number_of_items"}]
        for body in bodies:
            client, _ = self.client(body)
            with self.subTest(body=body), patch.object(source.UpgatesClient, "from_shop", return_value=client), \
                 self.assertRaises(source.SourceError):
                await source.read_stock("biketrek", VARIANT, FINGERPRINT)
            client.session.get.assert_called_once()
            client.session.put.assert_not_called()
        for pages in (0, 1):
            with self.assertRaises(source.SourceError) as raised:
                await self.read(dict(good, variants=[], current_page_items=0, number_of_items=0, number_of_pages=pages), target=VARIANT)
            self.assertEqual(raised.exception.code, "stock_publication_source_missing")

    async def test_simple_and_variant_put_contain_only_absolute_selected_leaf_stock(self):
        for variant in (False, True):
            result, client = await self.write(acknowledgement(variant), frozen=identity(variant), quantity="0")
            expected = {"code": "SKU-1", "stock": 0}
            if variant:
                expected = {"code": "POS-UMBRELLA", "variants": [expected]}
            self.assertEqual(result, {"acknowledged": True})
            client.session.put.assert_called_once_with(BASE + "/products", json={"products": [expected]},
                timeout=(5, 25), verify=True, allow_redirects=False, stream=True)

    async def test_frozen_identity_and_desired_quantity_invalid_before_factory_or_network(self):
        invalid_identities = [{**identity(), "stock_increment": 1}, {**identity(), "variant_id": 71},
            {**identity(True), "variant_id": None}, {**identity(), "product_id": True}, SIMPLE,
            {**identity(), "parent_code": "OTHER"}, {**identity(True), "variant_code": "OTHER"}]
        for frozen in invalid_identities:
            with self.subTest(frozen=frozen), patch.object(source.UpgatesClient, "from_shop") as factory, \
                 self.assertRaises(source.SourceError) as raised:
                await source.write_stock_once("biketrek", frozen, "1", FINGERPRINT)
            factory.assert_not_called()
            self.assertFalse(raised.exception.uncertain)
        for quantity in (True, 1, None, "-1", "+1", "01", "1.0", "1e3", "1,000", "NaN", "9007199254740992"):
            with self.subTest(quantity=quantity), patch.object(source.UpgatesClient, "from_shop") as factory, \
                 self.assertRaises(source.SourceError) as raised:
                await source.write_stock_once("biketrek", identity(), quantity, FINGERPRINT)
            factory.assert_not_called()
            self.assertEqual(raised.exception.code, "stock_publication_invalid_quantity")

    async def test_unsafe_selectors_and_absent_expected_connection_never_construct_client(self):
        for code in ("A;B", "..", ".", " leading", "trailing ", "A\x00B", "\ud800", "A" * 101):
            with self.subTest(code=repr(code)), patch.object(source.UpgatesClient, "from_shop") as factory, \
                 self.assertRaises(source.SourceError):
                await source.read_stock("biketrek", {**SIMPLE, "code": code, "parent_code": code}, FINGERPRINT)
            factory.assert_not_called()
        for fingerprint in (None, "", "a" * 63, "A" * 64):
            with patch.object(source.UpgatesClient, "from_shop") as factory, self.assertRaises(source.SourceError):
                await source.read_stock("biketrek", SIMPLE, fingerprint)
            factory.assert_not_called()
        with patch.object(source.UpgatesClient, "from_shop") as factory, self.assertRaises(source.SourceError):
            await source.read_stock("../biketrek", SIMPLE, FINGERPRINT)
        factory.assert_not_called()

    async def test_actual_client_target_mismatch_prevents_both_get_and_put(self):
        for writing in (False, True):
            for base, login in (("http://stock-test.invalid/api/v2", "reader"),
                                ("https://other.invalid/api/v2", "reader"), (BASE, "another")):
                client, _ = self.client({})
                client.base_url, client.session.auth = base, (login, "secret")
                with self.subTest(writing=writing, base=base, login=login), \
                     patch.object(source.UpgatesClient, "from_shop", return_value=client), self.assertRaises(source.SourceError) as raised:
                    if writing:
                        await source.write_stock_once("biketrek", identity(), "3", FINGERPRINT)
                    else:
                        await source.read_stock("biketrek", SIMPLE, FINGERPRINT)
                self.assertEqual(raised.exception.code, "stock_publication_target_changed")
                self.assertFalse(raised.exception.uncertain)
                client.session.get.assert_not_called()
                client.session.put.assert_not_called()
                client.session.close.assert_called_once()

    async def test_parent_acknowledgement_alone_wrong_ids_or_sibling_ack_remain_uncertain(self):
        valid = acknowledgement(True)
        cases = []
        for changes in ({"updated_yn": False}, {"updated_yn": "true"}, {"product_id": 99}, {"product_id": True},
                        {"variants": []}, {"variants": valid["products"][0]["variants"] * 2}):
            body = deepcopy(valid)
            body["products"][0].update(changes)
            cases.append(body)
        for changes in ({"updated_yn": False}, {"updated_yn": 1}, {"variant_id": 99}, {"variant_id": True}, {"code": "SIBLING"}):
            body = deepcopy(valid)
            body["products"][0]["variants"][0].update(changes)
            cases.append(body)
        cases += [{}, {"products": []}, {"products": valid["products"] * 2}]
        for body in cases:
            client, response = self.client(body)
            with self.subTest(body=body), patch.object(source.UpgatesClient, "from_shop", return_value=client), \
                 self.assertRaises(source.SourceError) as raised:
                await source.write_stock_once("biketrek", identity(True), "3", FINGERPRINT)
            self.assertTrue(raised.exception.uncertain)
            client.session.put.assert_called_once()
            client.session.get.assert_not_called()
            response.close.assert_called_once()

    async def test_warnings_errors_and_unknown_message_shapes_at_any_ack_level_are_uncertain_without_leak(self):
        for location in ("root", "parent", "variant"):
            for message in ([{"level": "warning", "message": "PRIVATE"}], [{"level": "error", "message": "PRIVATE"}],
                            [{"level": "fatal_error", "message": "PRIVATE"}], [{"level": "unknown"}], "PRIVATE"):
                body = acknowledgement(True)
                node = body if location == "root" else body["products"][0]
                if location == "variant":
                    node = node["variants"][0]
                node["messages"] = message
                with self.subTest(location=location, message=message), self.assertRaises(source.SourceError) as raised:
                    await self.write(body, frozen=identity(True))
                self.assertTrue(raised.exception.uncertain)
                self.assertNotIn("PRIVATE", str(raised.exception))

    async def test_malformed_duplicate_key_nonfinite_and_oversized_bodies_never_acknowledge_write(self):
        for raw in (b"PRIVATE invalid JSON", b'{"products": [], "products": []}', b'{"stock": NaN}',
                    b"x" * (source.MAX_RESPONSE_BYTES + 1)):
            for writing in (False, True):
                client, response = self.client({}, raw=raw)
                with self.subTest(writing=writing, length=len(raw)), patch.object(source.UpgatesClient, "from_shop", return_value=client), \
                     self.assertRaises(source.SourceError) as raised:
                    if writing:
                        await source.write_stock_once("biketrek", identity(), "3", FINGERPRINT)
                    else:
                        await source.read_stock("biketrek", SIMPLE, FINGERPRINT)
                self.assertEqual(raised.exception.uncertain, writing)
                self.assertNotIn("PRIVATE", str(raised.exception))
                response.close.assert_called_once()
                client.session.close.assert_called_once()

    async def test_transport_failures_never_repeat_put_or_attempt_automatic_readback(self):
        for error in (requests.Timeout("PRIVATE"), requests.ConnectionError("PRIVATE")):
            client, _ = self.client({})
            client.session.put.side_effect = error
            with patch.object(source.UpgatesClient, "from_shop", return_value=client), self.assertRaises(source.SourceError) as raised:
                await source.write_stock_once("biketrek", identity(), "3", FINGERPRINT)
            self.assertTrue(raised.exception.uncertain)
            self.assertNotIn("PRIVATE", str(raised.exception))
            client.session.put.assert_called_once()
            client.session.get.assert_not_called()
            client.session.close.assert_called_once()

    async def test_http_redirect_rejections_rate_limits_and_failures_preserve_uncertainty_boundaries(self):
        for status in (301, 302, 400, 401, 403, 404, 422, 429, 500, 503):
            client, response = self.client({}, status=status, headers={"Retry-After": "900"})
            with self.subTest(status=status), patch.object(source.UpgatesClient, "from_shop", return_value=client), \
                 self.assertRaises(source.SourceError) as raised:
                await source.write_stock_once("biketrek", identity(), "3", FINGERPRINT)
            self.assertEqual(raised.exception.uncertain, status not in (401, 403, 429))
            self.assertEqual(raised.exception.retry_after, 900 if status == 429 else None)
            response.iter_content.assert_not_called()
            client.session.put.assert_called_once()
            client.session.get.assert_not_called()

    async def test_rate_limit_http_date_is_sanitized_for_both_reads_and_writes(self):
        deadline = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=900), usegmt=True)
        for writing in (False, True):
            client, _ = self.client({}, status=429, headers={"Retry-After": deadline})
            with patch.object(source.UpgatesClient, "from_shop", return_value=client), self.assertRaises(source.SourceError) as raised:
                if writing:
                    await source.write_stock_once("biketrek", identity(), "3", FINGERPRINT)
                else:
                    await source.read_stock("biketrek", SIMPLE, FINGERPRINT)
            self.assertEqual(raised.exception.code, "stock_publication_rate_limited")
            self.assertTrue(898 <= raised.exception.retry_after <= 900)
            self.assertFalse(raised.exception.uncertain)
            self.assertNotIn(deadline, str(raised.exception))

    async def test_slow_or_interrupted_stream_closes_and_leaves_a_write_uncertain(self):
        client, response = self.client(acknowledgement())
        with patch.object(source.UpgatesClient, "from_shop", return_value=client), \
             patch.object(source, "monotonic", side_effect=[0, 1, 31]), self.assertRaises(source.SourceError) as raised:
            await source.write_stock_once("biketrek", identity(), "3", FINGERPRINT)
        self.assertTrue(raised.exception.uncertain)
        client.session.put.assert_called_once()
        response.close.assert_called_once()
        for writing in (False, True):
            client, response = self.client({})
            response.iter_content.side_effect = requests.ConnectionError("PRIVATE STREAM")
            with patch.object(source.UpgatesClient, "from_shop", return_value=client), self.assertRaises(source.SourceError) as raised:
                if writing:
                    await source.write_stock_once("biketrek", identity(), "3", FINGERPRINT)
                else:
                    await source.read_stock("biketrek", SIMPLE, FINGERPRINT)
            self.assertEqual(raised.exception.uncertain, writing)
            self.assertNotIn("PRIVATE", str(raised.exception))
            response.close.assert_called_once()
            client.session.close.assert_called_once()
