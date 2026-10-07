"""Expose the real default without changing configured stock destinations."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from inventory_hub.services import fifo, fifo_cost_sync, opening_stock, order_stock
from inventory_hub.services import product_editor, stock_history, stock_settings


def rows(values):
    return SimpleNamespace(all=lambda: values)


class WarehouseOptionsTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.other = SimpleNamespace(id=1, code="aaa", name="Other", is_default=False, is_active=True)
        self.default = SimpleNamespace(id=2, code="main", name="Main", is_default=True, is_active=True)
        self.warehouses = [self.other, self.default]
        self.shop = SimpleNamespace(id=7, code="biketrek", name="BIKETREK")
        self.db = SimpleNamespace(scalars=AsyncMock(return_value=rows(self.warehouses)),
                                  execute=AsyncMock(), get=AsyncMock(), commit=AsyncMock(), flush=AsyncMock())

    def assert_defaults(self, result):
        self.assertEqual([(row["code"], row["is_default"]) for row in result["warehouses"]],
                         [("aaa", False), ("main", True)])
        self.db.commit.assert_not_awaited()
        self.db.flush.assert_not_awaited()

    async def test_opening_and_fifo_expose_database_default_not_alphabetical_first(self):
        for service in (opening_stock, fifo):
            with self.subTest(service=service.__name__):
                self.assert_defaults(await service.options(self.db))

    async def test_editor_exposes_default_with_existing_shop_and_brand_options(self):
        self.db.scalars.side_effect = [rows([self.shop]), rows(self.warehouses), rows(["Shimano"])]
        result = await product_editor.options(self.db)
        self.assert_defaults(result)
        self.assertEqual(result["shops"][0]["code"], "biketrek")
        self.assertEqual(result["brands"], ["Shimano"])

    async def test_history_keeps_inactive_warehouses_and_reports_their_state(self):
        archived = SimpleNamespace(code="closed", name="Closed", is_default=False, is_active=False)
        self.db.execute.return_value = rows([*self.warehouses, archived])
        result = await stock_history.options(self.db)
        self.assertEqual([(row["code"], row["is_default"], row["is_active"]) for row in result["warehouses"]],
                         [("aaa", False, True), ("main", True, True), ("closed", False, False)])
        query = self.db.execute.await_args.args[0]
        self.assertEqual([column.name for column in query.selected_columns],
                         ["code", "name", "is_default", "is_active"])
        self.db.commit.assert_not_awaited()
        self.db.flush.assert_not_awaited()

    async def test_order_options_do_not_replace_explicit_policy_with_default(self):
        policy = SimpleNamespace(warehouse_id=self.other.id, starts_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                                 revision=3, status_actions={})
        self.db.get.side_effect = [policy, self.other]
        self.db.execute.return_value = rows([])
        result = await order_stock._options(self.db, self.shop, {"statuses": []})
        self.assert_defaults(result)
        self.assertEqual(result["policy"]["warehouse_code"], "aaa")
        self.assertEqual(result["policy"]["revision"], 3)

    async def test_settings_options_preserve_configured_warehouse(self):
        policy = SimpleNamespace(warehouse_id=self.other.id, starts_at=None, revision=2)
        with patch.object(stock_settings, "_shop", AsyncMock(return_value=self.shop)), \
                patch.object(stock_settings, "_one", AsyncMock(side_effect=[policy, None])), \
                patch.object(stock_settings, "warehouse_settings", AsyncMock(return_value={})) as selected, \
                patch.object(stock_settings, "effective", AsyncMock(return_value={})):
            result = await stock_settings.options(self.db, "biketrek")
        self.assert_defaults(result)
        self.assertEqual(result["policy"]["warehouse_code"], "aaa")
        selected.assert_awaited_once_with(self.db, "aaa")

    async def test_cost_options_preserve_resolved_warehouse(self):
        self.db.scalars.side_effect = [rows(self.warehouses), rows([])]
        with patch.object(fifo_cost_sync, "scope", AsyncMock(return_value=(self.shop, self.other, None, None, {}))):
            result = await fifo_cost_sync.options(self.db, "biketrek")
        self.assert_defaults(result)
        self.assertEqual(result["warehouse"]["code"], "aaa")
        self.assertEqual(result["settings"]["warehouse_code"], "aaa")
