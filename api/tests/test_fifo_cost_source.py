"""FIFO cost targeting, VAT basis, preservation and single-attempt transport."""
from copy import deepcopy
from decimal import Decimal
import json
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import Mock, patch

import requests

from inventory_hub.services import fifo_cost_source as cost
from inventory_hub.services import stock_publication_source as source
from inventory_hub.services.upgates import UpgatesClient


BASE = "https://fifo-cost-test.invalid/api/v2"
FINGERPRINT = source.connection_fingerprint(BASE, "reader")
ORDER_UUID = "00000000-0000-4000-8000-000000000001"
LINE_UUID = "00000000-0000-4000-8000-000000000002"
OTHER_UUID = "00000000-0000-4000-8000-000000000003"
TARGET = {"code": "LEAF-1", "parent_code": "POS-xTrek", "variant_code": "LEAF-1", "product_id": 2, "variant_id": 3}


def product(*, gross=True, vat="23"):
    return {"identity": deepcopy(TARGET),
            "options": {"prices_with_vat": gross, "language": "sk", "currency": "EUR", "pricelist": "Default"},
            "leaf": {"product_id": 2, "variant_id": 3, "code": "LEAF-1", "product_code": "POS-xTrek",
                "stock": "5", "ean": "5901234123457", "active_yn": False,
                "prices": [{"language": "sk", "price_purchase": Decimal("12.30"), "vat": vat,
                    "price_common": Decimal("80"), "pricelists": [{"name": "Default", "price_original": Decimal("50"),
                        "product_discount": Decimal("10"), "price_sale": Decimal("40")},
                        {"name": "Wholesale", "price_original": Decimal("30")}]},
                    {"language": "cs", "price_purchase": Decimal("500"), "vat": "21"}],
                "last_update_time": "2026-09-24T08:00:00Z"}}


def order(*, gross=True):
    return {"current_page": 1, "current_page_items": 1, "number_of_pages": 1, "number_of_items": 1,
        "orders": [{"order_number": "TEST-1", "order_id": 7, "uuid": ORDER_UUID, "resolved_yn": True,
            "status_id": 8, "origin": "admin", "currency_id": "EUR", "prices_with_vat_yn": gross,
            "creation_time": "2026-09-24T08:00:00Z", "last_update_time": "2026-09-24T08:01:00Z",
            "customer": {"email": "private@example.invalid"}, "products": [
                {"uuid": LINE_UUID, "product_id": 2, "option_set_id": 3, "code": "LEAF-1", "ean": "5901234123457",
                 "type": "product", "quantity": 3, "vat": Decimal("20"), "buy_price": Decimal("12"),
                 "price_per_unit": Decimal("60"), "price": Decimal("180"), "unit": "ks"},
                {"uuid": OTHER_UUID, "code": "OTHER", "type": "product", "quantity": 1,
                 "vat": Decimal("10"), "buy_price": Decimal("11"), "price_per_unit": Decimal("30")}] }]}


def issued(**changes):
    return {"line_key": LINE_UUID, "code": "LEAF-1", "quantity": "3", "unit_cost_net": "20", **changes}


def observed_order(body=None):
    return cost._order_observation(body or order(), "TEST-1")


class FifoCostPreparationTests(TestCase):
    def test_product_uses_exact_leaf_and_only_purchase_field_not_pricelists(self):
        remote = cost._product_observation(product())
        intent = cost.prepare_product(remote, "40")
        self.assertEqual(intent["payload"], {"products": [{"code": "POS-xTrek", "variants": [
            {"code": "LEAF-1", "prices": [{"language": "sk", "price_purchase": 49.2}]}]}]})
        self.assertEqual(intent["after"]["values"], {"price_purchase": "49.2"})
        self.assertTrue(cost.matches_before(intent, remote))
        self.assertFalse(cost.matches_after(intent, remote))
        changed = product(); changed["leaf"]["prices"][0]["price_purchase"] = Decimal("49.2000")
        changed["leaf"]["last_update_time"] = "2026-09-24T10:00:00Z"
        self.assertTrue(cost.matches_after(intent, cost._product_observation(changed)))

    def test_product_gross_and_net_and_explicit_zero(self):
        for gross, expected in ((True, "12.3"), (False, "10")):
            remote = cost._product_observation(product(gross=gross))
            self.assertEqual(cost.prepare_product(remote, "10")["after"]["values"]["price_purchase"], expected)
            self.assertEqual(cost.prepare_product(remote, "0")["after"]["values"]["price_purchase"], "0")
        raw = product(); raw["leaf"]["prices"][0]["price_purchase"] = None
        self.assertIsNone(cost.prepare_product(cost._product_observation(raw), "10")["before"]["values"]["price_purchase"])

    def test_unknown_currency_language_vat_or_duplicate_language_is_rejected(self):
        raws = []
        for options in ({"currency": "CZK"}, {"language": "cs"}, {"prices_with_vat": "true"}):
            raw = product(); raw["options"].update(options); raws.append(raw)
        for vat in (None, True, "101", "NaN"):
            raw = product(vat=vat); raws.append(raw)
        raw = product(); raw["leaf"]["prices"].append(deepcopy(raw["leaf"]["prices"][0])); raws.append(raw)
        for raw in raws:
            with self.subTest(raw=raw), self.assertRaises(cost.SourceError):
                cost._product_observation(raw)

    def test_live_product_options_require_one_active_slovak_eur_language_and_boolean_basis(self):
        valid = {"language_id": "sk", "active_yn": True, "currency_id": "EUR"}
        cases = [[], [dict(valid, currency_id="CZK")], [dict(valid, active_yn=False)],
                 [dict(valid, language_id="cs")], [valid, valid], None]
        for languages in cases:
            with self.subTest(languages=languages), patch.object(source, "_request", side_effect=[
                    {"config": {"prices_with_vat_yn": True}}, {"languages": languages}]), \
                    self.assertRaisesRegex(cost.SourceError, "price_basis_unknown"):
                cost._price_options(object())
        with patch.object(source, "_request", return_value={"config": {"prices_with_vat_yn": "false"}}) as request, \
             self.assertRaisesRegex(cost.SourceError, "price_basis_unknown"):
            cost._price_options(object())
        request.assert_called_once()

    def test_product_guard_detects_identity_settings_cost_and_unrelated_price_changes(self):
        intent = cost.prepare_product(cost._product_observation(product()), "20")
        changed = product(); changed["leaf"]["prices"][0]["price_purchase"] = "24.6"
        for mutate in (lambda r: r["identity"].update(variant_id=99),
                       lambda r: r["leaf"].update(code="OTHER"),
                       lambda r: r["options"].update(prices_with_vat=False),
                       lambda r: r["leaf"]["prices"][0].update(price_purchase="25"),
                       lambda r: r["leaf"]["prices"][0].update(vat="20"),
                       lambda r: r["leaf"]["prices"][0]["pricelists"][0].update(price_sale="35"),
                       lambda r: r["leaf"]["prices"][1].update(price_purchase="450")):
            raw = deepcopy(changed); mutate(raw)
            self.assertFalse(cost.matches_after(intent, cost._product_observation(raw)))

    def test_regular_stock_changes_do_not_invalidate_product_preflight_or_cost_readback(self):
        initial = product()
        initial["leaf"].update(stock=5, availability_id=1, availability="SKLADOM", availability_type="InStock",
            can_add_to_basket_yn=True, stocks=[{"stock_id": 1, "quantity": 5}], limit_orders="0")
        intent = cost.prepare_product(cost._product_observation(initial), "20")
        concurrent = deepcopy(initial)
        concurrent["leaf"].update(stock=0, availability_id=8, availability="overíme", availability_type="OnRequest",
            can_add_to_basket_yn=False, stocks=[{"stock_id": 1, "quantity": 0}], limit_orders="1",
            last_update_time="2026-09-24T12:00:00Z")
        self.assertTrue(cost.matches_before(intent, cost._product_observation(concurrent)))
        self.assertFalse(cost.matches_after(intent, cost._product_observation(concurrent)))
        concurrent["leaf"]["prices"][0]["price_purchase"] = "24.6"
        self.assertTrue(cost.matches_after(intent, cost._product_observation(concurrent)))
        self.assertFalse(cost.matches_before(intent, cost._product_observation(concurrent)))

    def test_order_uses_order_vat_and_basis_not_current_product_rate_and_sends_only_costs(self):
        remote = observed_order()
        intent = cost.prepare_order(remote, [issued()])
        self.assertEqual(intent["payload"], {"orders": [{"order_number": "TEST-1", "prices_with_vat_yn": True,
            "products": [{"code": "LEAF-1", "buy_price": 24.0}]}], "send_emails_yn": False,
            "send_sms_yn": False, "delete_missing_products_yn": False})
        # Product current VAT is 23%, this historical order line is 20%.
        self.assertEqual(cost.prepare_product(cost._product_observation(product()), "20")["after"]["values"]["price_purchase"], "24.6")
        self.assertEqual(intent["after"]["values"][LINE_UUID], "24")
        self.assertEqual(intent["after"]["values"][OTHER_UUID], "11")
        net = cost.prepare_order(observed_order(order(gross=False)), [issued()])
        self.assertEqual(net["after"]["values"][LINE_UUID], "20")
        self.assertIs(net["payload"]["orders"][0]["prices_with_vat_yn"], False)
        self.assertNotIn("private@example", json.dumps(remote))

    def test_repeating_weighted_average_is_rounded_four_places_not_to_cents(self):
        intent = cost.prepare_order(observed_order(order(gross=False)), [issued(unit_cost_net="3.333333333333333333")])
        self.assertEqual(intent["after"]["values"][LINE_UUID], "3.3333")
        raw = order(gross=False); raw["orders"][0]["products"][0]["buy_price"] = Decimal("3.33")
        self.assertFalse(cost.matches_after(intent, observed_order(raw)))
        raw["orders"][0]["products"][0]["buy_price"] = Decimal("3.3333")
        self.assertTrue(cost.matches_after(intent, observed_order(raw)))

    def test_duplicate_codes_including_case_collision_never_guess_order_line(self):
        for code in ("LEAF-1", "leaf-1"):
            raw = order(); raw["orders"][0]["products"][1]["code"] = code
            with self.assertRaisesRegex(cost.SourceError, "duplicate_order_code"):
                cost.prepare_order(observed_order(raw), [issued()])

    def test_replaced_uuid_quantity_code_or_unsupported_line_type_blocks_order_patch(self):
        for changes in ({"uuid": "00000000-0000-4000-8000-000000000004"}, {"quantity": 2},
                        {"code": "REPLACED"}, {"type": "set"}, {"ean": "invalid"}):
            raw = order(); raw["orders"][0]["products"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(cost.SourceError, "order_lines_changed"):
                cost.prepare_order(observed_order(raw), [issued()])

    def test_order_guard_preserves_state_currency_vat_quantities_other_costs_and_customer(self):
        intent = cost.prepare_order(observed_order(), [issued()])
        changed = order(); changed["orders"][0]["products"][0]["buy_price"] = Decimal("24")
        changed["orders"][0]["last_update_time"] = "2026-09-24T10:00:00Z"
        self.assertTrue(cost.matches_after(intent, observed_order(changed)))
        for mutate in (lambda r: r.update(status_id=9), lambda r: r.update(resolved_yn=False),
                       lambda r: r.update(prices_with_vat_yn=False), lambda r: r["products"][0].update(quantity=2),
                       lambda r: r["products"][0].update(vat=23), lambda r: r["products"][1].update(buy_price=12),
                       lambda r: r["customer"].update(email="changed@example.invalid")):
            raw = deepcopy(changed); mutate(raw["orders"][0])
            self.assertFalse(cost.matches_after(intent, observed_order(raw)))
        for currency in (None, "CZK", "eur"):
            raw = deepcopy(changed); raw["orders"][0]["currency_id"] = currency
            with self.assertRaisesRegex(cost.SourceError, "currency_unsupported"):
                observed_order(raw)

    def test_issued_cost_must_be_known_nonnegative_finite_and_selected_once(self):
        for value in (None, True, "-1", "NaN", "Infinity", "1e10000", {}, "1_000"):
            with self.subTest(value=value), self.assertRaises(cost.SourceError):
                cost.prepare_order(observed_order(), [issued(unit_cost_net=value)])
        with self.assertRaises(cost.SourceError):
            cost.prepare_order(observed_order(), [issued(), issued()])


class FifoCostTransportTests(IsolatedAsyncioTestCase):
    def client(self, body=None, *, status=200):
        client = UpgatesClient(BASE, "reader", "synthetic", verify_ssl=False)
        client.session = Mock(auth=("reader", "synthetic"))
        response = Mock(status_code=status, headers={})
        response.iter_content.return_value = [json.dumps(body, default=str).encode()]
        client.session.get.return_value = response
        client.session.put.return_value = response
        return client, response

    async def test_order_read_is_bounded_private_https_no_redirect_no_writes(self):
        client, response = self.client(order())
        with patch.object(source.UpgatesClient, "from_shop", return_value=client), patch.object(client, "_log") as log:
            remote = await cost.read_order("biketrek", "TEST-1", FINGERPRINT)
        self.assertEqual(remote["identity"]["uuid"], ORDER_UUID)
        client.session.get.assert_called_once_with(BASE + "/orders", params={"order_numbers": "TEST-1", "page": 1},
            timeout=(5, 25), verify=True, allow_redirects=False, stream=True)
        client.session.put.assert_not_called(); log.assert_not_called()
        client.session.close.assert_called_once(); response.close.assert_called_once()

    async def test_product_preview_preflight_write_readback_uses_ten_calls_and_no_pricelist_reads(self):
        def response(body):
            result = Mock(status_code=200, headers={})
            result.iter_content.return_value = [json.dumps(body, default=str).encode()]
            return result
        leaf = product()["leaf"]
        config = {"config": {"prices_with_vat_yn": True}}
        languages = {"languages": [{"language_id": "sk", "active_yn": True, "currency_id": "EUR"}]}
        def page(row):
            return {"current_page": 1, "current_page_items": 1, "number_of_pages": 1, "number_of_items": 1,
                    "variants": [row]}
        changed = deepcopy(leaf); changed["prices"][0]["price_purchase"] = "24.6"
        client, _ = self.client({"products": [{"code": "POS-xTrek", "product_id": 2, "updated_yn": True,
                             "variants": [{"code": "LEAF-1", "variant_id": 3, "updated_yn": True}]}]})
        client.session.get.side_effect = [response(body) for body in
            (page(leaf), config, languages, page(leaf), config, languages, page(changed), config, languages)]
        with patch.object(source.UpgatesClient, "from_shop", return_value=client):
            preview = await cost.read_product("biketrek", TARGET, FINGERPRINT)
            intent = cost.prepare_product(preview, "20")
            self.assertTrue(cost.matches_before(intent, await cost.read_product("biketrek", TARGET, FINGERPRINT)))
            await cost.write_once("biketrek", intent, FINGERPRINT)
            self.assertTrue(cost.matches_after(intent, await cost.read_product("biketrek", TARGET, FINGERPRINT)))
        calls = [(call[0], call.args[0]) for call in client.session.mock_calls if call[0] in ("get", "put")]
        observation_calls = [("get", BASE + path) for path in ("/products/variants", "/config", "/languages")]
        self.assertEqual(calls, observation_calls * 2 + [("put", BASE + "/products")] + observation_calls)
        self.assertEqual(preview["options"], {"language": "sk", "currency": "EUR", "prices_with_vat": True})
        for call in client.session.get.call_args_list[::3]:
            self.assertEqual(call.kwargs["params"]["variant_codes"], "LEAF-1")

    async def test_product_price_basis_is_not_cached_and_rate_limit_delay_is_preserved(self):
        client, response = self.client({}, status=429)
        response.headers = {"Retry-After": "37"}
        with patch.object(source.UpgatesClient, "from_shop", return_value=client), self.assertRaises(cost.SourceError) as raised:
            await cost.read_product("biketrek", TARGET, FINGERPRINT)
        self.assertEqual((raised.exception.status, raised.exception.retry_after, raised.exception.uncertain), (429, 37, False))
        client.session.get.assert_called_once(); client.session.put.assert_not_called()
        # Changing the actual config on a subsequent observation changes the
        # guard and converted purchase price; a previous observation is not reused.
        leaf = product()
        with patch.object(source, "_client", return_value=client), patch.object(cost.products, "_read", return_value=leaf), \
             patch.object(source, "_request", side_effect=[
                 {"config": {"prices_with_vat_yn": True}},
                 {"languages": [{"language_id": "sk", "active_yn": True, "currency_id": "EUR"}]},
                 {"config": {"prices_with_vat_yn": False}},
                 {"languages": [{"language_id": "sk", "active_yn": True, "currency_id": "EUR"}]}]):
            before = await cost.read_product("biketrek", TARGET, FINGERPRINT)
            after = await cost.read_product("biketrek", TARGET, FINGERPRINT)
        self.assertFalse(cost.matches_before(cost.prepare_product(before, "20"), after))
        self.assertEqual(cost.prepare_product(after, "20")["after"]["values"]["price_purchase"], "20")

    async def test_order_put_accepts_documented_singleton_object_or_array_ack_then_requires_readback(self):
        intent = cost.prepare_order(observed_order(), [issued()])
        for rows in ({"order_number": "TEST-1", "updated_yn": True}, [{"order_number": "TEST-1", "updated_yn": True}]):
            client, response = self.client({"orders": rows})
            with patch.object(source.UpgatesClient, "from_shop", return_value=client):
                result = await cost.write_once("biketrek", intent, FINGERPRINT)
            self.assertEqual(result, {"acknowledged": True})
            client.session.put.assert_called_once_with(BASE + "/orders", json=intent["payload"],
                timeout=(5, 25), verify=True, allow_redirects=False, stream=True)
            client.session.get.assert_not_called(); response.close.assert_called_once()

    async def test_minimal_variant_put_validates_exact_parent_and_leaf_ack(self):
        intent = cost.prepare_product(cost._product_observation(product()), "40")
        ack = {"products": [{"code": "POS-xTrek", "product_id": 2, "updated_yn": True,
                             "variants": [{"code": "LEAF-1", "variant_id": 3, "updated_yn": True}]}]}
        client, _ = self.client(ack)
        with patch.object(source.UpgatesClient, "from_shop", return_value=client):
            await cost.write_once("biketrek", intent, FINGERPRINT)
        self.assertEqual(client.session.put.call_args.kwargs["json"], intent["payload"])
        ack["products"][0]["variants"][0]["code"] = "SIBLING"
        client, _ = self.client(ack)
        with patch.object(source.UpgatesClient, "from_shop", return_value=client), self.assertRaises(cost.SourceError) as raised:
            await cost.write_once("biketrek", intent, FINGERPRINT)
        self.assertTrue(raised.exception.uncertain)
        client.session.put.assert_called_once()

    async def test_timeout_server_failure_or_incomplete_ack_is_uncertain_never_retried(self):
        intent = cost.prepare_order(observed_order(), [issued()])
        cases = [(200, {}), (500, {}), (302, {}), (200, {"orders": {"order_number": "OTHER", "updated_yn": True}}),
            (200, {"orders": {"order_number": "TEST-1", "updated_yn": False}}),
            (200, {"orders": {"order_number": "TEST-1", "updated_yn": True, "messages": [{"level": "warning", "message": "PRIVATE"}]}})]
        for status, body in cases:
            client, _ = self.client(body, status=status)
            with self.subTest(status=status, body=body), patch.object(source.UpgatesClient, "from_shop", return_value=client), \
                 self.assertRaises(cost.SourceError) as raised:
                await cost.write_once("biketrek", intent, FINGERPRINT)
            self.assertTrue(raised.exception.uncertain)
            self.assertNotIn("PRIVATE", str(raised.exception))
            client.session.put.assert_called_once(); client.session.get.assert_not_called()
        client, _ = self.client({})
        client.session.put.side_effect = requests.Timeout("PRIVATE")
        with patch.object(source.UpgatesClient, "from_shop", return_value=client), self.assertRaises(cost.SourceError) as raised:
            await cost.write_once("biketrek", intent, FINGERPRINT)
        self.assertTrue(raised.exception.uncertain); client.session.put.assert_called_once()

    async def test_changed_connection_or_added_write_fields_block_before_network(self):
        intent = cost.prepare_order(observed_order(), [issued()])
        for mutate in (lambda p: p["payload"]["orders"][0].update(status_id=99),
                       lambda p: p["payload"]["orders"][0]["products"][0].update(quantity=7),
                       lambda p: p["payload"].update(delete_missing_products_yn=True),
                       lambda p: p["after"]["values"].update({OTHER_UUID: "999"})):
            bad = deepcopy(intent); mutate(bad)
            with patch.object(source.UpgatesClient, "from_shop") as factory, self.assertRaises(cost.SourceError):
                await cost.write_once("biketrek", bad, FINGERPRINT)
            factory.assert_not_called()
        client, _ = self.client({}); client.base_url = "https://other.invalid/api/v2"
        with patch.object(source.UpgatesClient, "from_shop", return_value=client), self.assertRaises(cost.SourceError):
            await cost.write_once("biketrek", intent, FINGERPRINT)
        client.session.put.assert_not_called(); client.session.get.assert_not_called()
