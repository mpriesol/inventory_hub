"""Native accounting differs from local stock actions and reservation coverage."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from inventory_hub.services import stock_native as service, order_stock_source as source
from test_order_stock import raw_line, raw_order, raw_page


class NativeFactsTests(unittest.TestCase):
    def observed(self, *lines, status="Received"):
        raw = raw_order(*(lines or [raw_line(product_id=101, quantity="5")]))
        observed, _ = source._order(raw_page(raw), raw["order_number"])
        for line in observed["lines"]:
            line.update(classification="mapped", product_id=7, sku="SKU-A")
        local = SimpleNamespace(stock_source_uuid=raw["uuid"], external_id=raw["order_number"],
            stock_warehouse_id=2, stock_state="reserved", stock_revision=3)
        return local, observed, [{"id": 1, "type": status, "name": "Storno"}]

    def test_full_native_quantity_does_not_use_reserved_allocation(self):
        local, observed, statuses = self.observed()
        local.allocated = 2
        result = service.facts(local, observed, statuses)
        self.assertEqual(result["quantities"], {"7": "-5"})
        self.assertEqual(result["lines"][0]["native_product_id"], 101)

    def test_only_real_canceled_type_restores_not_custom_or_payment_canceled(self):
        for kind in service.STATUS_TYPES:
            with self.subTest(kind=kind):
                args = self.observed(status=kind)
                self.assertEqual(service.facts(*args)["quantities"], {"7": "0" if kind == "Canceled" else "-5"})

    def test_unknown_status_type_never_becomes_an_assumed_debit(self):
        with self.assertRaisesRegex(service.NativeError, "stock_native_status_unknown"):
            service.facts(*self.observed(status="NewVendorState"))

    def test_coded_manual_line_without_native_identity_has_no_native_effect(self):
        result = service.facts(*self.observed(raw_line(quantity="3")))
        self.assertEqual(result["quantities"], {})
        self.assertFalse(result["lines"][0]["native"])

    def test_manual_service_is_ignored_and_native_identity_cannot_be_hidden(self):
        local, observed, statuses = self.observed(raw_line(code="", unit="hod"))
        observed["lines"][0]["classification"] = "manual"
        self.assertEqual(service.facts(local, observed, statuses)["quantities"], {})
        observed["lines"][0]["has_native_identity"] = True
        with self.assertRaisesRegex(service.NativeError, "stock_native_line_identity"):
            service.facts(local, observed, statuses)

    def test_native_ids_are_preserved_separately_from_canonical_product_id(self):
        result = service.facts(*self.observed(raw_line(product_id=901, option_set_id=202)))
        row = result["lines"][0]
        self.assertEqual((row["product_id"], row["native_product_id"], row["native_option_set_id"]), (7, 901, 202))

    def test_native_gifts_sets_and_nested_lines_need_an_explicit_contract(self):
        for values in ({"kind": "gift"}, {"kind": "set"}, {"parent_uuid": "nested"}):
            with self.subTest(values=values):
                local, observed, statuses = self.observed()
                observed["lines"][0].update(values)
                with self.assertRaisesRegex(service.NativeError, "stock_native_line_unsupported"):
                    service.facts(local, observed, statuses)

    def test_malformed_quantity_and_missing_native_parent_fail_closed(self):
        for values in ({"quantity": None}, {"quantity": "NaN"}, {"quantity": "1.5"},
                       {"native_product_id": None}, {"native_product_id": True}):
            with self.subTest(values=values):
                local, observed, statuses = self.observed()
                observed["lines"][0].update(values)
                with self.assertRaises(service.NativeError):
                    service.facts(local, observed, statuses)

    def test_duplicate_lines_and_wrong_order_uuid_never_contribute(self):
        local, observed, statuses = self.observed()
        duplicate = deepcopy(observed)
        duplicate["lines"] *= 2
        with self.assertRaisesRegex(service.NativeError, "stock_native_line_identity"):
            service.facts(local, duplicate, statuses)
        observed["uuid"] = "different"
        with self.assertRaisesRegex(service.NativeError, "stock_native_order_identity"):
            service.facts(local, observed, statuses)
