"""Units preserve invoice values and explicit services never become stock."""
from decimal import Decimal
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from pydantic import ValidationError

from inventory_hub.db_models import Product, Shop
from inventory_hub.routers.receiving_db import SetUnitRequest
from inventory_hub.services.item_units import ItemUnitError, receipt_values, validate_stock_quantity
from inventory_hub.services.product_identity import IdentityIndex
from inventory_hub.services import order_stock_source, stock_native
from inventory_hub.services.order_stock_ledger import _source_lines
from inventory_hub.services.opening_stock import parse_csv


class UnitConversionTests(TestCase):
    def test_purchase_package_converts_quantity_and_cost_without_guessing(self):
        self.assertEqual(receipt_values(Decimal("2"), Decimal("50"), Decimal("10")),
                         (Decimal("20"), Decimal("5.0000")))
        self.assertEqual(receipt_values(Decimal("2"), None, Decimal("10")), (Decimal("20"), None))
        self.assertEqual(receipt_values(Decimal("3"), Decimal("1"), Decimal("3")),
                         (Decimal("9"), Decimal("0.3333")))
        self.assertEqual(receipt_values(Decimal("1.5"), Decimal("10")),
                         (Decimal("1.5"), Decimal("10.0000")))

    def test_unrepresentable_conversion_is_rejected_instead_of_rounding_quantity_or_zeroing_cost(self):
        for values in (("1", "1", "0"), ("1", "1", "NaN"), ("0.001", "1", "0.001"),
                       ("1", "0.0001", "1000"), ("1000", "1", "3"), ("999999999", "1", "10")):
            with self.subTest(values=values), self.assertRaises(ItemUnitError):
                receipt_values(*(Decimal(value) for value in values))
        for value in (10, True, "0", "NaN", "1e1", "0.0001"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                SetUnitRequest(purchase_unit="pack", purchase_to_stock_factor=value)

    def test_pairs_and_packages_are_whole_sale_units_meters_allow_three_decimals(self):
        for unit in ("pcs", "pair", "pack"):
            self.assertEqual(validate_stock_quantity(Decimal(2), unit), Decimal(2))
            with self.assertRaisesRegex(ItemUnitError, "stock_quantity_unit_mismatch"):
                validate_stock_quantity(Decimal("1.5"), unit)
        self.assertEqual(validate_stock_quantity(Decimal("1.125"), "m"), Decimal("1.125"))
        with self.assertRaises(ItemUnitError):
            validate_stock_quantity(Decimal("1.0001"), "m")

    def test_opening_csv_preserves_units_and_does_not_truncate_meters(self):
        rows, errors, _ = parse_csv("sku;quantity;unit_cost;unit\nCABLE;1.125;2;m\nGLOVES;2;10;pair\n")
        self.assertEqual(errors, [])
        self.assertEqual([(row["unit"], row["quantity"]) for row in rows],
                         [("m", Decimal("1.125")), ("pair", Decimal(2))])
        _, errors, _ = parse_csv("sku;quantity;unit_cost;unit\nGLOVES;1.125;2;pair\n")
        self.assertTrue(errors)


class ServiceIdentityTests(IsolatedAsyncioTestCase):
    async def test_resolved_service_with_native_shop_identity_is_excluded_but_unknown_code_is_an_error(self):
        product = Product(id=5, sku="WORK", name="Service work", item_type="service", stock_unit="pcs")
        index = IdentityIndex(1, [product])
        lines = [order_stock_source._line({"uuid": str(uuid4()), "code": code, "type": "product",
                    "quantity": "1.5", "unit": "hod", "product_id": 77}) for code in ("WORK", "UNKNOWN")]
        with patch.object(order_stock_source, "load_identity_index", AsyncMock(return_value=index)):
            resolved = await order_stock_source.resolve_lines(None, Shop(id=1, code="biketrek"), {"lines": lines})
        self.assertEqual(resolved[0]["classification"], "non_stock")
        self.assertEqual(resolved[0]["product_id"], 5)
        self.assertEqual(resolved[1]["classification"], "unresolved")
        planned, excluded, errors = _source_lines({"lines": resolved})
        self.assertEqual(planned, [])
        self.assertEqual(len(excluded), 1)
        self.assertEqual(len(errors), 1)
        order = SimpleNamespace(stock_source_uuid="source", external_id="ORDER", stock_warehouse_id=1,
                                stock_state="pending", stock_revision=0)
        facts = stock_native.facts(order, {"uuid": "source", "order_number": "ORDER", "status_id": 1,
            "updated_at": "2026-10-22T12:00:00+00:00", "lines": resolved[:1]}, [{"id": 1, "type": "Received"}])
        self.assertEqual(facts["quantities"], {})
        self.assertEqual(facts["lines"], [])

    async def test_saved_service_flag_never_hides_conflicting_identity(self):
        raw = order_stock_source._line({"uuid": str(uuid4()), "code": "WORK", "ean": "not-a-barcode",
                                      "type": "product", "quantity": "1", "unit": "ks"})
        raw.update(classification="non_stock", item_type="service", stock_unit="pcs", product_id=5)
        product = Product(id=5, sku="WORK", name="Service work", item_type="service", stock_unit="pcs")
        with patch.object(order_stock_source, "load_identity_index", AsyncMock(return_value=IdentityIndex(1, [product]))):
            resolved = await order_stock_source.resolve_lines(None, Shop(id=1, code="biketrek"), {"lines": [raw]})
        self.assertEqual(resolved[0]["classification"], "conflict")
        self.assertIsNone(resolved[0]["item_type"])
