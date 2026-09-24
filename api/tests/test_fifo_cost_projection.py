"""Exact purchase-cost publication proofs without network or database I/O."""
import copy
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace as Record
from uuid import uuid4

from inventory_hub.db_models import MovementType
from inventory_hub.services import fifo
from inventory_hub.services import fifo_cost_projection as service

D = Decimal
STAMP = datetime(2026, 9, 24, tzinfo=timezone.utc)


def layer(identifier, cost, remaining="1", status="known", stock_status="available"):
    return Record(id=identifier, product_id=7, warehouse_id=9, quantity_remaining=D(remaining),
        unit_cost=None if cost is None else D(cost), cost_status=status, stock_status=stock_status,
        cost_revision=0, root_cost_layer_id=identifier, physical_received_at=STAMP)


def product_evidence(layers):
    return (Record(id=7, sku="SKU-7", is_active=True), Record(id=9, is_active=True),
        Record(product_id=7, warehouse_id=9,
            qty_on_hand=sum((row.quantity_remaining for row in layers), D("0")),
            qty_quarantined=sum((row.quantity_remaining for row in layers if row.stock_status == "quarantine"), D("0"))),
        Record(product_id=7, warehouse_id=9, revision=5), layers, {row.id: row for row in layers})


def issued_evidence(costs=("10", "20", "30"), quantity=None):
    key = str(uuid4())
    amount = str(len(costs)) if quantity is None else quantity
    source = {"line_key": key, "product_id": 7, "sku": "SKU-7", "code": "SHOP-SKU-7",
        "classification": "mapped", "quantity": amount, "unit": "ks"}
    order = Record(id=11, shop_id=12, external_id="ORDER-11", stock_source_uuid=str(uuid4()),
        currency=None, stock_warehouse_id=9, stock_state="issued", stock_revision=3, stock_issued_at=STAMP)
    order.stock_snapshot = {"order_number": order.external_id, "uuid": order.stock_source_uuid, "lines": [source]}
    order.stock_issue_result = {"action": "issue", "order_id": order.id, "stock_state": "issued", "revision": 3,
        "lines": [{"line_key": key, "product_id": 7, "sku": "SKU-7", "quantity": str(D(amount).normalize())}],
        "movement_ids": [21], "movements_created": 1}
    item = Record(id=31, order_id=11, product_id=7, status="fulfilled", stock_managed=True,
        external_item_id=key, external_product_code="SHOP-SKU-7", quantity=D(amount))
    reservation = Record(id=41, shop_order_item_id=31, product_id=7, warehouse_id=9, quantity=D(amount), sale_movement_id=21)
    movement = Record(id=21, product_id=7, warehouse_id=9, quantity=-D(amount),
        movement_type=MovementType.SALE_OUT, reference_type="shop_order", reference_id="11",
        reference_source="12", idempotency_key="order:11:issue:31", unit_cost_currency="EUR", fx_rate_to_eur=D("1"))
    rows = []
    for offset, cost in enumerate(costs):
        root = layer(offset + 1, cost, remaining="0", status="unknown" if cost is None else "known")
        allocation = Record(id=51+offset, issue_movement_id=21, layer_id=root.id, sequence=offset,
            quantity=D("1"), quantity_before=D("1"), returned_quantity=D("0"),
            unit_cost_at_issue=root.unit_cost, total_cost_at_issue=root.unit_cost, cost_status_at_issue=root.cost_status,
            unit_cost_current=root.unit_cost, total_cost_current=root.unit_cost, cost_status_current=root.cost_status)
        rows.append((item, reservation, movement, allocation, root, root))
    return order, rows


class FifoCostProjectionTests(unittest.TestCase):
    def assertBlocked(self, code, callback, *args):
        with self.assertRaises(service.FifoCostProjectionError) as raised:
            callback(*args)
        self.assertEqual(raised.exception.code, "fifo_cost_" + code)

    def test_five_prices_next_cost_and_order_average_use_different_evidence(self):
        layers = [layer(index, str(index * 10)) for index in range(1, 6)]
        before = service.project_product(*product_evidence(layers))
        order, rows = issued_evidence()
        sale = service.project_order(order, rows)
        for row in layers[:3]:
            row.quantity_remaining = D("0")
        after = service.project_product(*product_evidence(layers))
        self.assertEqual((before["unit_cost"], sale["lines"][0]["unit_cost"], sale["total_cost"], after["unit_cost"]),
                         ("10", "20", "60", "40"))
        self.assertEqual(fifo.summarize(layers)["avg_cost"], "45")

    def test_reservations_do_not_move_product_cost_to_later_layer(self):
        evidence = product_evidence([layer(1, "10"), layer(2, "20")])
        before = service.project_product(*evidence)
        evidence[2].qty_reserved = D("2")
        self.assertEqual(service.project_product(*evidence), before)

    def test_quarantine_skipped_unknown_first_saleable_layer_not_skipped(self):
        self.assertEqual(service.project_product(*product_evidence([
            layer(1, "10", stock_status="quarantine"), layer(2, "20")]))["unit_cost"], "20")
        for price, status in ((None, "unknown"), ("10", "provisional")):
            self.assertBlocked("cost_incomplete", service.project_product,
                *product_evidence([layer(1, price, status=status), layer(2, "20")]))

    def test_zero_empty_and_quarantine_only_preserve_remote_price_by_skipping(self):
        for layers in ([], [layer(1, "10", "0")], [layer(1, "10", stock_status="quarantine")]):
            self.assertBlocked("no_available_layer", service.project_product, *product_evidence(layers))
        self.assertEqual(service.project_product(*product_evidence([layer(1, "0")]))["unit_cost"], "0")

    def test_missing_legacy_or_inconsistent_balance_is_blocked(self):
        records = list(product_evidence([layer(1, "10")]))
        for index, code in ((2, "balance_missing"), (3, "fifo_required")):
            missing = records.copy()
            missing[index] = None
            self.assertBlocked(code, service.project_product, *missing)
        records[2].qty_on_hand = D("9")
        self.assertBlocked("layer_balance_mismatch", service.project_product, *records)

    def test_order_original_quantity_is_not_reduced_after_return(self):
        order, rows = issued_evidence()
        before = service.project_order(order, rows)
        rows[-1][3].returned_quantity = D("1")
        after = service.project_order(order, rows)
        self.assertEqual(before, after)
        self.assertEqual((after["lines"][0]["quantity"], after["lines"][0]["unit_cost"]), ("3", "20"))

    def test_documented_cost_revision_changes_signature_and_current_cost_not_issue_snapshot(self):
        order, rows = issued_evidence()
        before = service.project_order(order, rows)
        rows[0][3].unit_cost_current = rows[0][3].total_cost_current = D("40")
        rows[0][4].unit_cost, rows[0][4].cost_revision = D("40"), 1
        after = service.project_order(order, rows)
        self.assertEqual((after["total_cost"], after["lines"][0]["unit_cost"]), ("90", "30"))
        self.assertNotEqual(after["source_hash"], before["source_hash"])
        self.assertEqual(after["lines"][0]["allocations"][0]["unit_cost_at_issue"], "10")
        self.assertEqual(after["issue_result_hash"], before["issue_result_hash"])

    def test_unknown_provisional_missing_allocations_and_wrong_quantities_block_order(self):
        order, rows = issued_evidence(costs=(None, "20", "30"))
        self.assertBlocked("cost_incomplete", service.project_order, order, rows)
        order, rows = issued_evidence()
        rows[0][3].cost_status_current = "provisional"
        self.assertBlocked("cost_incomplete", service.project_order, order, rows)
        order, rows = issued_evidence()
        self.assertBlocked("allocations_missing", service.project_order, order, [(*rows[0][:3], None, None, None)])
        self.assertBlocked("allocation_quantity_mismatch", service.project_order, order, rows[:2])

    def test_decimal_quantity_format_and_current_currency_guard(self):
        order, rows = issued_evidence(quantity="3.000")
        self.assertEqual(service.project_order(order, rows)["total_cost"], "60")
        order.currency = "CZK"
        self.assertBlocked("currency_unsupported", service.project_order, order, rows)

    def test_rounding_occurs_after_summing_exact_allocation_totals(self):
        order, rows = issued_evidence(costs=("10", "10", "11"))
        value = service.project_order(order, rows)
        self.assertEqual((value["total_cost"], value["lines"][0]["unit_cost"]), ("31", "10.3333"))

    def test_duplicate_sku_lines_keep_their_own_allocations(self):
        order, first = issued_evidence(costs=("10",))
        other, second = issued_evidence(costs=("20", "30"))
        order.stock_snapshot["lines"] += other.stock_snapshot["lines"]
        order.stock_issue_result["lines"] += other.stock_issue_result["lines"]
        order.stock_issue_result.update(movement_ids=[21, 22], movements_created=2)
        item, reservation, movement = second[0][:3]
        item.id = 32
        reservation.id, reservation.shop_order_item_id, reservation.sale_movement_id = 42, 32, 22
        movement.id, movement.idempotency_key = 22, "order:11:issue:32"
        for row in second:
            row[3].id += 10
            row[3].issue_movement_id = 22
        value = service.project_order(order, first + second)
        self.assertEqual(sorted(line["unit_cost"] for line in value["lines"]), ["10", "25"])
        self.assertEqual(value["total_cost"], "60")

    def test_projection_does_not_change_any_evidence(self):
        order, rows = issued_evidence()
        before = copy.deepcopy((order, rows))
        service.project_order(order, rows)
        self.assertEqual((order, rows), before)
        order.stock_issue_result["revision"] = 2
        self.assertBlocked("issue_evidence_missing", service.project_order, order, rows)

    def test_remote_content_proof_keeps_manual_lines_and_changed_physical_identity_blocks(self):
        order, rows = issued_evidence()
        manual_key = str(uuid4())
        order.stock_snapshot["lines"].append({"line_key": manual_key, "classification": "manual",
            "code": "SERVICE", "quantity": "1.000", "kind": "product", "ean": "", "parent_uuid": ""})
        result = service.project_order(order, rows)
        self.assertEqual(len(result["lines"]), 1)
        manual = next(row for row in result["source_lines"] if row["line_key"] == manual_key)
        self.assertEqual((manual["code"], manual["quantity"]), ("SERVICE", "1"))
        rows[0][0].external_product_code = "ANOTHER-PRODUCT"
        self.assertBlocked("evidence_mismatch", service.project_order, order, rows)
