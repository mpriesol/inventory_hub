"""Strict stock source observations; synthetic data and no live shop calls."""
from copy import deepcopy
from decimal import Decimal
import json
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from inventory_hub.db_models import IdentifierType, Product, ProductIdentifier, Shop
from inventory_hub.db_models_ext import ShopProduct
from inventory_hub.services import order_stock_source as source
from inventory_hub.services.product_identity import IdentityIndex
from inventory_hub.services.upgates import UpgatesClient, UpgatesError


STATUSES = {"order_statuses": [{"id": 1, "type": "Received", "mark_paid_yn": False,
    "mark_resolved_yn": False, "mark_delivered_yn": False,
    "descriptions": [{"language_id": "sk", "name": "Prijatá"}]}]}


def line(**values):
    return {"uuid": str(uuid4()), "code": "SHARED", "title": "Synthetic product", "quantity": "2.000",
            "unit": "ks", "type": "product", "ean": "", **values}


def order(*lines, **values):
    return {"order_number": "SOURCE-1", "uuid": str(uuid4()), "creation_time": "2026-09-23T10:00:00+02:00",
            "last_update_time": "2026-09-23T10:01:00+02:00", "status_id": 1, "origin": "frontend",
            "resolved_yn": False, "paid_date": None, "products": list(lines) or [line()], **values}


def envelope(raw):
    return {"orders": [raw], "current_page": 1, "number_of_pages": 1, "number_of_items": 1}


def identity_index(mapped=False):
    products = [Product(id=1, sku="SHARED", name="Canonical"), Product(id=2, sku="OTHER", name="Other")]
    mappings = [ShopProduct(id=1, shop_id=1, product_id=1, external_code="ALIAS", is_variant=False)] if mapped else []
    identifiers = [ProductIdentifier(id=1, product_id=1, value="4006381333931", identifier_type=IdentifierType.ean),
                   ProductIdentifier(id=2, product_id=2, value="5901234123457", identifier_type=IdentifierType.ean)]
    return IdentityIndex(1, products, mappings, identifiers)


class SourceNormalizationTests(IsolatedAsyncioTestCase):
    async def load(self, raw=None, *, index=None, statuses=None, payload=None):
        raw = raw or order()
        with patch.object(source, "_fetch", return_value=(payload or envelope(raw), statuses or STATUSES)), \
             patch.object(source, "load_identity_index", AsyncMock(return_value=index or identity_index())):
            return await source.load_source(None, Shop(id=1, code="biketrek"), "SOURCE-1")

    async def test_current_shared_sku_without_ean_has_canonical_identity_and_aware_utc_times(self):
        result = await self.load()
        observed = result["order"]
        self.assertEqual(observed["created_at"], "2026-09-23T08:00:00+00:00")
        row = observed["lines"][0]
        self.assertEqual((row["classification"], row["matched_by"], row["product_id"], row["quantity"]),
                         ("identified", "shared_sku", 1, "2"))
        self.assertEqual(set(observed), {"order_number", "uuid", "created_at", "updated_at", "origin", "status_id", "paid", "resolved", "lines"})
        self.assertEqual(len(result["source_hash"]), 64)

    async def test_mapped_variant_leaf_and_shared_sku_ean_conflicts_keep_original_resolver_rules(self):
        index = identity_index()
        index.add_mapping(ShopProduct(id=2, shop_id=1, product_id=1, is_variant=True,
                                     external_code="POS-UMBRELLA", variant_code="SHARED", parent_code="POS-UMBRELLA"))
        result = await self.load(order(line(option_set_id=901, product_id=999),
                                       line(ean="5901234123457"), line(code="shared")), index=index)
        rows = result["order"]["lines"]
        self.assertEqual((rows[0]["classification"], rows[0]["product_id"]), ("mapped", 1))
        self.assertEqual([row["classification"] for row in rows[1:]], ["conflict", "conflict"])

    async def test_manual_lines_are_only_those_without_code_barcode_or_native_identity(self):
        result = await self.load(order(line(code="", unit="hod"), line(code="", ean="4006381333931"),
                                       line(code="", product_id=9), line(code="", option_set_id=10),
                                       line(code="", type="discount", quantity="-1")))
        rows = result["order"]["lines"]
        self.assertEqual([row["classification"] for row in rows], ["manual", "identified", "unresolved", "unresolved", "non_stock"])
        self.assertTrue(rows[2]["has_native_identity"])
        self.assertIn("missing_code_with_identity", rows[1]["reasons"])
        self.assertEqual(rows[1]["matched_by"], "validated_barcode")

    async def test_malformed_identity_never_becomes_manual_when_saved_projection_is_resolved_again(self):
        result = await self.load(order(line(code={"private": "not persisted"}), line(code="", product_id=True),
                                       line(ean="invalid-ean"), line(code="SHARED\n"),
                                       line(ean="4006381333931/garbage")))
        rows = result["order"]["lines"]
        self.assertTrue(all(row["classification"] == "conflict" and row["identity_invalid"] for row in rows))
        self.assertNotIn("not persisted", json.dumps(result))
        for row in rows:
            row.update(classification="manual", product_id=999, sku="invented", matched_by="forged", reasons=[])
        with patch.object(source, "load_identity_index", AsyncMock(return_value=identity_index())):
            resolved = await source.resolve_lines(None, Shop(id=1, code="biketrek"), result["order"])
        self.assertTrue(all(row["classification"] == "conflict" and row["product_id"] is None for row in resolved))

    async def test_uuid_mismatch_missing_or_duplicate_and_invalid_datetime_boolean_are_rejected(self):
        same = str(uuid4())
        bad = [order(uuid=""), order(uuid="not-a-uuid"), order(order_number="OTHER"),
               order(line(uuid="")), order(line(uuid=same), line(uuid=same)),
               order(creation_time="2026-09-23T10:00:00"), order(last_update_time="2020-01-01T00:00:00Z"),
               order(paid_date="2026-99-99"), order(resolved_yn="true"), order(resolved_yn=1),
               order(deleted_yn=0),
               order(line(parent_uuid="not-a-uuid")), order(products=[line()] * 1001)]
        for raw in bad:
            with self.subTest(keys=list(raw)), self.assertRaises(source.SourceError) as raised:
                await self.load(raw)
            self.assertEqual(raised.exception.code, "order_stock_invalid_response")

    async def test_quantity_bundles_units_and_length_are_preserved_for_stock_gates(self):
        parent = str(uuid4())
        result = await self.load(order(line(quantity="1.5", unit="m", length="2", length_unit="m"),
                                       line(quantity="NaN"), line(type="set"), line(parent_uuid=parent),
                                       line(type="gift", parent_uuid=parent), line(unit="")))
        rows = result["order"]["lines"]
        self.assertEqual((rows[0]["quantity"], rows[0]["unit"], rows[0]["length"], rows[0]["length_unit"]), ("1.5", "m", "2", "m"))
        self.assertEqual([row["classification"] for row in rows[1:]], ["conflict", "conflict", "conflict", "identified", "identified"])
        self.assertEqual(rows[-1]["unit"], "")

    async def test_source_hash_tracks_remote_changes_but_not_local_identity_or_row_order(self):
        raw = order(line(), line(code="OTHER"))
        first = await self.load(raw)
        reordered = deepcopy(raw)
        reordered["products"].reverse()
        self.assertEqual((await self.load(reordered))["source_hash"], first["source_hash"])
        remote_changes = [{"last_update_time": "2026-09-23T10:02:00+02:00"}, {"status_id": 2},
                          {"paid_date": "2026-09-23"}, {"resolved_yn": True}]
        for changes in remote_changes:
            self.assertNotEqual((await self.load({**raw, **changes}))["source_hash"], first["source_hash"])
        changed = deepcopy(raw)
        changed["products"][0]["quantity"] = "3"
        self.assertNotEqual((await self.load(changed))["source_hash"], first["source_hash"])
        changed_index = IdentityIndex(1, [Product(id=99, sku="SHARED", name="Different local product")])
        second = await self.load(raw, index=changed_index)
        self.assertEqual(second["source_hash"], first["source_hash"])
        self.assertNotEqual(second["order"]["lines"][0]["product_id"], first["order"]["lines"][0]["product_id"])

    async def test_private_and_commercial_data_never_enter_projection_or_identity_queries(self):
        private = "PRIVATE-CUSTOMER-AND-PRICE"
        raw = order(line(price=private, buy_price=private, configurations=[private]),
                    customer={"email": private}, internal_note=private, attachments=[private])
        result = await self.load(raw)
        self.assertNotIn(private, json.dumps(result))

    async def test_empty_ambiguous_or_incomplete_active_scope_cannot_be_confirmed(self):
        for payload in ({"orders": []}, {**envelope(order()), "orders": [order(), order()]},
                        {"orders": [order()]}, {**envelope(order()), "number_of_pages": 2}):
            with self.subTest(payload=payload.keys()), self.assertRaises(source.SourceError):
                await self.load(payload=payload)
        with self.assertRaises(source.SourceError) as raised:
            await self.load(order(deleted_yn=True))
        self.assertEqual(raised.exception.code, "order_stock_source_missing")

    async def test_invalid_order_number_is_rejected_before_remote_access(self):
        with patch.object(source, "_fetch") as fetch:
            for number in ("A;B", " SOURCE-1 ", "", "bad\nnumber", "bad\ud800number"):
                with self.subTest(number=repr(number)), self.assertRaises(source.SourceError):
                    await source.load_source(None, Shop(id=1, code="biketrek"), number)
        fetch.assert_not_called()

    async def test_target_binding_reaches_client_fetch_and_default_manual_signature_is_unchanged(self):
        fingerprint = source.connection_fingerprint("https://example.invalid/api/v2", "test")
        for expected in (None, fingerprint):
            with patch.object(source, "_fetch", return_value=(envelope(order()), STATUSES)) as fetch, \
                 patch.object(source, "load_identity_index", AsyncMock(return_value=identity_index())):
                await source.load_source(None, Shop(id=1, code="biketrek"), "SOURCE-1", expected_target_fingerprint=expected)
            if expected is None:
                fetch.assert_called_once_with("biketrek", "SOURCE-1")
            else:
                fetch.assert_called_once_with("biketrek", "SOURCE-1", expected_target_fingerprint=fingerprint)


class StatusTests(IsolatedAsyncioTestCase):
    async def test_status_hash_covers_automation_metadata_without_deriving_actions(self):
        with patch.object(source, "_fetch", return_value=STATUSES):
            first = await source.load_statuses("biketrek")
        changed = deepcopy(STATUSES)
        changed["order_statuses"][0]["mark_paid_yn"] = True
        with patch.object(source, "_fetch", return_value=changed):
            second = await source.load_statuses("biketrek")
        self.assertEqual(first["statuses"], [{"id": 1, "name": "Prijatá", "type": "Received"}])
        self.assertEqual(first["statuses"], second["statuses"])
        self.assertNotEqual(first["status_hash"], second["status_hash"])

    async def test_malformed_and_duplicate_statuses_are_rejected(self):
        invalid = [[], {"order_statuses": [STATUSES["order_statuses"][0]] * 2},
                   {"order_statuses": [{**STATUSES["order_statuses"][0], "mark_paid_yn": "true"}]}]
        for payload in invalid:
            with patch.object(source, "_fetch", return_value=payload), self.assertRaises(source.SourceError):
                await source.load_statuses("biketrek")


class SourceTransportTests(TestCase):
    def response(self, body, status=200):
        response = Mock(status_code=status, content=b"{}")
        response.json.return_value = body
        return response

    def test_active_order_filter_and_status_reads_are_bounded_without_logging_or_remote_writes(self):
        client = UpgatesClient("https://example.invalid/api/v2", "synthetic", "synthetic")
        client.session = Mock()
        client.session.get.side_effect = [self.response(envelope(order())), self.response(STATUSES)]
        with patch.object(source.UpgatesClient, "from_shop", return_value=client), patch.object(client, "_log") as log:
            source._fetch("biketrek", "SOURCE-1")
        self.assertEqual(client.session.get.call_count, 2)
        first = client.session.get.call_args_list[0]
        self.assertTrue(first.args[0].endswith("/orders"))
        self.assertEqual(first.kwargs["params"], {"order_numbers": "SOURCE-1", "page": 1})
        self.assertFalse(first.kwargs["allow_redirects"])
        self.assertLessEqual(first.kwargs["timeout"][1], 25)
        client.session.post.assert_not_called()
        client.session.put.assert_not_called()
        log.assert_not_called()
        client.session.close.assert_called_once()

    def test_upstream_error_statuses_are_safe_without_retry_or_raw_body(self):
        for status, code in ((401, "order_stock_upgates_access"), (429, "order_stock_rate_limited"),
                             (404, "order_stock_source_missing"), (500, "order_stock_source_unavailable")):
            client = Mock()
            client.read_order_audit.side_effect = UpgatesError("PRIVATE BODY", status_code=status)
            with self.subTest(status=status), patch.object(source.UpgatesClient, "from_shop", return_value=client), \
                 self.assertRaises(source.SourceError) as raised:
                source._fetch("biketrek", "SOURCE-1")
            self.assertEqual(raised.exception.code, code)
            self.assertNotIn("PRIVATE", str(raised.exception))
            client.read_order_audit.assert_called_once()
            client.session.close.assert_called_once()

    def test_changed_actual_target_never_gets_orders_or_statuses(self):
        fingerprint = source.connection_fingerprint("https://example.invalid/api/v2", "test")
        for base, login in (("https://wrong.invalid/api/v2", "test"), ("https://example.invalid/other", "test"),
                            ("https://example.invalid/api/v2", "wrong-login"), ("http://example.invalid/api/v2", "test")):
            client = UpgatesClient(base, login, "secret-not-hashed")
            client.session = Mock(auth=(login, "secret-not-hashed"))
            with self.subTest(base=base, login=login), patch.object(source.UpgatesClient, "from_shop", return_value=client), \
                 self.assertRaises(source.SourceError) as raised:
                source._fetch("biketrek", "SOURCE-1", expected_target_fingerprint=fingerprint)
            self.assertEqual((raised.exception.code, raised.exception.status), ("order_stock_target_changed", 409))
            client.session.get.assert_not_called()
            client.session.close.assert_called_once()

    def test_rate_limit_preserves_only_bounded_integer_retry_delay(self):
        for delay, expected in ((90, 90), (604800, 604800), (604801, None), (0, None),
                                (-1, None), (True, None), ("90", None), (None, None)):
            client = Mock()
            error = UpgatesError("PRIVATE BODY", status_code=429)
            error.retry_after = delay
            client.read_order_audit.side_effect = error
            with self.subTest(delay=delay), patch.object(source.UpgatesClient, "from_shop", return_value=client), \
                 self.assertRaises(source.SourceError) as raised:
                source._fetch("biketrek", "SOURCE-1")
            self.assertEqual(raised.exception.retry_after, expected)
            self.assertEqual(raised.exception.code, "order_stock_rate_limited")
            self.assertNotIn("PRIVATE", str(raised.exception))
            client.read_order_audit.assert_called_once()
            client.session.close.assert_called_once()

    def test_same_target_with_rotated_key_fetches_both_fresh_order_and_status(self):
        fingerprint = source.connection_fingerprint("https://example.invalid/api/v2", "test")
        client = UpgatesClient("https://example.invalid/api/v2", "test", "rotated-key")
        client.session = Mock(auth=("test", "rotated-key"))
        client.session.get.side_effect = [self.response(envelope(order())), self.response(STATUSES)]
        with patch.object(source.UpgatesClient, "from_shop", return_value=client):
            source._fetch("biketrek", "SOURCE-1", expected_target_fingerprint=fingerprint)
        self.assertEqual(client.session.get.call_count, 2)
        client.session.close.assert_called_once()
