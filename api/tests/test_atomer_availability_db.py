"""PostgreSQL regression tests for local Atomer exports and retry recovery."""

from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
import asyncio
import csv
import hashlib
import io
import unittest

import asyncpg
from sqlalchemy import func, select
import test_receiving_db as fixture
from test_atomer_availability import buyers
from inventory_hub import config_io
from inventory_hub.atomer_availability_models import (
    AtomerAvailabilitySettings as Settings,
    AtomerAvailabilityRun as Run,
    AtomerAvailabilityAdminSource as AdminSource,
)
from inventory_hub.atomer_availability_types import (
    Configure,
    RunInput,
    ExportInput,
    Override,
)
from inventory_hub.db_models import ProductIdentifier, IdentifierType
from inventory_hub.db_models_ext import StockBalance, StockMovement
from inventory_hub.services import atomer_availability as service
from inventory_hub.services import atomer_availability_worker as worker


@unittest.skipUnless(
    fixture.TEST_URL,
    "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB",
)
class AtomerAvailabilityDatabaseTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixture.ReceivingDatabaseTests.asyncTearDown
    make_session = fixture.ReceivingDatabaseTests.make_session
    finalize = fixture.ReceivingDatabaseTests.finalize

    async def asyncSetUp(self):
        await fixture.ReceivingDatabaseTests.asyncSetUp(self)
        conn = await asyncpg.connect(fixture.TEST_URL)
        try:
            await conn.execute(f'SET search_path TO "{self.schema}"')
            await conn.execute(
                (
                    Path(__file__).resolve().parents[2]
                    / "infra/db-init/055_atomer_availability.sql"
                ).read_text()
            )
        finally:
            await conn.close()
        config_io.save_shop(
            "xtrek",
            {
                "upgates_full_export_url_csv": "https://upgates.example.invalid/existing.csv",
                "atomer_availability": {
                    "export_url": "https://atomer.example.invalid/buyers.xml"
                },
            },
        )

    async def configure(self, payload=None):
        payload = payload or Configure(
            request_id=uuid4(),
            expected_revision=0,
            enabled=True,
            warehouse_code="receipt-test",
        )
        async with self.sessions() as db:
            result = await service.configure(db, payload)
            await db.commit()
            return result

    async def queue(self, payload=None):
        payload = payload or RunInput(request_id=uuid4(), warehouse_code="receipt-test")
        async with self.sessions() as db:
            result = await service.enqueue(db, payload)
            await db.commit()
            return result

    async def stock(self):
        receipt = await self.make_session(
            [
                {
                    "product_id": self.product_id,
                    "ordered_qty": D(5),
                    "received_qty": D(5),
                    "unit_price": D(7),
                }
            ]
        )
        await self.finalize(receipt)
        async with self.sessions() as db:
            balance = await db.scalar(
                select(StockBalance).where(StockBalance.product_id == self.product_id)
            )
            balance.qty_reserved, balance.qty_quarantined = D(2), D(1)
            await db.commit()

    async def ready(self, raw=None):
        queued = await self.queue()
        async with self.sessions() as db:
            run = await db.get(Run, queued["id"])
            await service.prepare(
                db,
                run,
                {"export_url": raw or buyers({})},
                service.now(),
                service.digest(service.source_config()),
            )
            await db.commit()
            return service.dto(run)

    async def snapshot(self):
        async with self.sessions() as db:
            balance = await db.scalar(
                select(StockBalance).where(StockBalance.product_id == self.product_id)
            )
            return (
                (
                    str(balance.qty_on_hand),
                    str(balance.qty_reserved),
                    str(balance.qty_quarantined),
                    str(balance.total_value),
                    await db.scalar(select(func.count()).select_from(StockMovement)),
                )
                if balance
                else None
            )

    async def test_configure_replay_cas_and_secret_namespace_preserve_upgates(self):
        request = Configure(
            request_id=uuid4(),
            expected_revision=0,
            enabled=True,
            warehouse_code="receipt-test",
            feed_url="https://atomer.example.invalid/changed.xml",
        )
        first = await self.configure(request)
        replay = await self.configure(request)
        self.assertEqual(first["revision"], 1)
        self.assertEqual(replay["revision"], 1)
        self.assertTrue(first["source_configured"])
        self.assertNotIn("changed.xml", str(first))
        self.assertEqual(
            config_io.load_shop("xtrek")["upgates_full_export_url_csv"],
            "https://upgates.example.invalid/existing.csv",
        )
        with self.assertRaisesRegex(service.ExportError, "request_conflict"):
            await self.configure(request.model_copy(update={"enabled": False}))
        with self.assertRaisesRegex(service.ExportError, "settings_changed"):
            await self.configure(request.model_copy(update={"request_id": uuid4()}))
        async with self.sessions() as db:
            self.assertTrue((await db.get(Settings, 1)).enabled)
            self.assertEqual(
                await db.scalar(select(func.count()).select_from(StockMovement)), 0
            )

    async def test_enqueue_uuid_returns_same_run_and_rejects_different_scope(self):
        request = RunInput(request_id=uuid4(), warehouse_code="receipt-test")
        first = await self.queue(request)
        self.assertEqual(await self.queue(request), first)
        self.assertEqual(first["id"], str(request.request_id))
        with self.assertRaisesRegex(service.ExportError, "request_conflict"):
            await self.queue(
                request.model_copy(update={"product_ids": [self.product_id]})
            )
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(Run)), 1)

    async def test_confirmed_stock_minus_reserves_and_quarantine_exports_without_stock_changes(
        self,
    ):
        await self.stock()
        before = await self.snapshot()
        ready = await self.ready()
        async with self.sessions() as db:
            detail = await service.get_run(db, ready["id"])
            self.assertEqual(D(detail["rows"][0]["qty_available"]), D(2))
            result = await service.export(
                db, ready["id"], ExportInput(expected_revision=ready["revision"])
            )
            await db.commit()
        self.assertEqual(result["status"], "exported")
        self.assertEqual(await self.snapshot(), before)
        async with self.sessions() as db:
            content, name, _ = await service.download(db, ready["id"])
        rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig")), delimiter=";"))
        self.assertEqual(rows[1], ["RECEIPT-A", "", "1", "SKLADOM"])
        self.assertEqual(len(rows), 2)
        self.assertTrue(name.endswith(".csv"))

    async def test_export_override_retry_is_immutable_and_does_not_leak_to_next_run(
        self,
    ):
        await self.stock()
        ready = await self.ready()
        request = ExportInput(
            expected_revision=ready["revision"],
            overrides=[
                Override(
                    code="RECEIPT-A", visibility=False, availability="Na predobjednanie"
                )
            ],
        )
        before = await self.snapshot()

        async def do_export(payload):
            async with self.sessions() as db:
                result = await service.export(db, ready["id"], payload)
                await db.commit()
                return result

        first, replay = await asyncio.gather(do_export(request), do_export(request))
        self.assertEqual(first, replay)
        with self.assertRaisesRegex(service.ExportError, "immutable"):
            await do_export(ExportInput(expected_revision=ready["revision"]))
        self.assertEqual(await self.snapshot(), before)
        fresh = await self.ready()
        async with self.sessions() as db:
            old = await service.get_run(db, ready["id"])
            new = await service.get_run(db, fresh["id"])
        self.assertEqual(old["rows"][0]["proposed_availability"], "Na predobjednanie")
        self.assertFalse(old["rows"][0]["proposed_visibility"])
        self.assertEqual(new["rows"][0]["proposed_availability"], "SKLADOM")
        self.assertTrue(new["rows"][0]["proposed_visibility"])

    async def test_stock_change_after_preview_requires_new_review(self):
        await self.stock()
        ready = await self.ready()
        async with self.sessions() as db:
            balance = await db.scalar(
                select(StockBalance).where(StockBalance.product_id == self.product_id)
            )
            balance.qty_reserved = D(3)
            await db.commit()
        with self.assertRaisesRegex(service.ExportError, "stock_changed"):
            async with self.sessions() as db:
                await service.export(
                    db, ready["id"], ExportInput(expected_revision=ready["revision"])
                )
        async with self.sessions() as db:
            run = await db.get(Run, ready["id"])
            self.assertEqual(run.status, "ready")
            self.assertIsNone(run.csv_content)

    async def test_every_preview_compares_fresh_source_not_last_csv(self):
        await self.stock()
        first = await self.ready()
        async with self.sessions() as db:
            await service.export(
                db, first["id"], ExportInput(expected_revision=first["revision"])
            )
            await db.commit()
        unchanged_source = await self.ready()
        fresh_source = await self.ready(buyers({"AVAILABILITY": "SKLADOM"}))
        self.assertEqual(unchanged_source["summary"]["changed"], 1)
        self.assertEqual(fresh_source["summary"]["changed"], 0)

    async def test_retention_preserves_last_success_and_only_discards_older_csv_bytes(
        self,
    ):
        old_id, new_id, failed_id = map(str, (uuid4(), uuid4(), uuid4()))
        async with self.sessions() as db:
            db.add(
                Run(
                    id=old_id,
                    request_hash="a" * 64,
                    warehouse_id=self.warehouse_id,
                    trigger="daily",
                    status="exported",
                    completed_at=service.now() - timedelta(days=20),
                    created_at=service.now() - timedelta(days=20),
                    document={
                        "evidence": "keep",
                        "source": [{"code": "OLD"}],
                        "rows": [
                            {"code": "DELTA", "status": "changed"},
                            {"code": "SAME", "status": "unchanged"},
                        ],
                        "summary": {"changed": 1},
                        "overrides": [{"code": "DELTA", "visibility": False}],
                    },
                    csv_content=b"old",
                    csv_sha256=hashlib.sha256(b"old").hexdigest(),
                )
            )
            db.add(
                Run(
                    id=failed_id,
                    request_hash="b" * 64,
                    warehouse_id=self.warehouse_id,
                    trigger="daily",
                    status="failed",
                    completed_at=service.now(),
                    document={},
                )
            )
            await db.commit()
            await service.prune(db)
            await db.commit()
        async with self.sessions() as db:
            retained = await db.get(Run, old_id)
            self.assertEqual(retained.csv_content, b"old")
            self.assertEqual(retained.document["source"], [{"code": "OLD"}])
            db.add(
                Run(
                    id=new_id,
                    request_hash="c" * 64,
                    warehouse_id=self.warehouse_id,
                    trigger="daily",
                    status="exported",
                    completed_at=service.now(),
                    document={
                        "source": [{"code": "LATEST"}],
                        "rows": [{"code": "SAME", "status": "unchanged"}],
                    },
                    csv_content=b"new",
                    csv_sha256=hashlib.sha256(b"new").hexdigest(),
                )
            )
            await db.commit()
            await service.prune(db)
            await db.commit()
        async with self.sessions() as db:
            old = await db.get(Run, old_id)
            new = await db.get(Run, new_id)
            self.assertIsNone(old.csv_content)
            self.assertNotIn("source", old.document)
            self.assertEqual(
                old.document["rows"], [{"code": "DELTA", "status": "changed"}]
            )
            self.assertEqual(old.document["evidence"], "keep")
            self.assertEqual(old.document["summary"], {"changed": 1})
            self.assertEqual(
                old.document["overrides"], [{"code": "DELTA", "visibility": False}]
            )
            self.assertTrue(old.document["archived"])
            self.assertEqual(
                old.document["warnings"].count("history_details_compacted"), 1
            )
            self.assertEqual(new.document["source"], [{"code": "LATEST"}])
            self.assertEqual(
                new.document["rows"], [{"code": "SAME", "status": "unchanged"}]
            )
            self.assertEqual(new.csv_content, b"new")
            with self.assertRaisesRegex(service.ExportError, "download_missing"):
                await service.download(db, old_id)

    async def test_daily_worker_restarts_fetching_safely_and_finishes_one_export(self):
        await self.stock()
        await self.configure()
        async with self.sessions() as db:
            result = await service.enqueue(
                db, RunInput(request_id=uuid4()), trigger="daily"
            )
            run = await db.get(Run, result["id"])
            run.status = "fetching"
            await db.commit()

        @asynccontextmanager
        async def context():
            async with self.sessions() as db:
                try:
                    yield db
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

        before = await self.snapshot()
        with patch.object(worker, "get_session_context", context), patch.object(
            service, "fetch_source", return_value={"export_url": buyers({})}
        ) as fetch:
            await worker.process(result["id"])
            await worker.process(result["id"])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(await self.snapshot(), before)
        async with self.sessions() as db:
            run = await db.get(Run, result["id"])
            self.assertEqual(run.status, "exported")
            self.assertIsNotNone(run.csv_content)
            self.assertEqual(await db.scalar(select(func.count()).select_from(Run)), 1)

    async def test_conflicting_ean_blocks_known_sku_without_creating_second_card(self):
        await self.stock()
        async with self.sessions() as db:
            db.add(
                ProductIdentifier(
                    product_id=self.product_id,
                    identifier_type=IdentifierType.ean,
                    value="4006381333931",
                    is_primary=True,
                )
            )
            await db.commit()
        before = await self.snapshot()
        ready = await self.ready(buyers({"EAN": "5901234123457"}))
        async with self.sessions() as db:
            detail = await service.get_run(db, ready["id"])
            self.assertEqual(detail["rows"][0]["status"], "blocked")
            self.assertIn("identity_conflict", detail["rows"][0]["reasons"])
            result = await service.export(
                db, ready["id"], ExportInput(expected_revision=ready["revision"])
            )
            await db.commit()
            content, _, _ = await service.download(db, ready["id"])
        self.assertEqual(
            len(
                list(
                    csv.reader(io.StringIO(content.decode("utf-8-sig")), delimiter=";")
                )
            ),
            1,
        )
        self.assertEqual(await self.snapshot(), before)

    async def test_expired_preview_cannot_export_and_corrupt_download_is_refused(self):
        await self.stock()
        ready = await self.ready()
        async with self.sessions() as db:
            run = await db.get(Run, ready["id"])
            run.source_fetched_at = service.now() - timedelta(minutes=31)
            await db.commit()
        async with self.sessions() as db:
            with self.assertRaisesRegex(service.ExportError, "preview_expired"):
                await service.export(
                    db, ready["id"], ExportInput(expected_revision=ready["revision"])
                )
        fresh = await self.ready()
        async with self.sessions() as db:
            await service.export(
                db, fresh["id"], ExportInput(expected_revision=fresh["revision"])
            )
            await db.commit()
            run = await db.get(Run, fresh["id"])
            run.csv_content = b"damaged"
            await db.commit()
        async with self.sessions() as db:
            with self.assertRaisesRegex(service.ExportError, "download_invalid"):
                await service.download(db, fresh["id"])

    async def test_admin_snapshot_age_controls_coverage_and_fresh_remote_takes_precedence(
        self,
    ):
        await self.stock()
        raw = (
            "Kat. číslo;Kat. číslo rodiča;Interné číslo;Viditeľný;Názov tovaru;EAN kód;Dodacia doba;Obrázky\n"
            "RECEIPT-A;;1;0;Hidden item;;do 5 dní;\n"
        ).encode()
        async with self.sessions() as db:
            imported = await service.upload_admin(db, raw, True)
            replay = await service.upload_admin(db, raw, True)
            self.assertEqual(imported, replay)
            await db.commit()
        fresh = await self.ready()
        self.assertEqual(fresh["coverage"]["kind"], "admin_complete")
        self.assertEqual(fresh["coverage"]["hidden_rows"], 1)
        self.assertNotIn("hidden_catalog_coverage_unverified", fresh["warnings"])
        async with self.sessions() as db:
            uploaded = await db.get(AdminSource, 1)
            uploaded.imported_at = service.now() - timedelta(days=2)
            await db.commit()
        stale = await self.ready()
        self.assertEqual(stale["coverage"]["kind"], "buyers_only")
        self.assertIn("admin_upload_stale", stale["warnings"])
        self.assertIn("hidden_catalog_coverage_unverified", stale["warnings"])
        async with self.sessions() as db:
            detail = await service.get_run(db, stale["id"])
            self.assertTrue(detail["rows"][0]["current_visibility"])
            self.assertEqual(detail["rows"][0]["current_availability"], "overíme")
        await self.configure(
            Configure(
                request_id=uuid4(),
                expected_revision=0,
                enabled=True,
                warehouse_code="receipt-test",
                admin_complete=True,
            )
        )
        queued = await self.queue()
        async with self.sessions() as db:
            run = await db.get(Run, queued["id"])
            await service.prepare(
                db,
                run,
                {"export_url": buyers({}), "admin_export_url": raw},
                service.now(),
                service.digest(service.source_config()),
            )
            await db.commit()
            self.assertEqual(run.document["coverage"]["kind"], "admin_complete")
            self.assertNotIn("admin_upload_stale", run.document["warnings"])
            self.assertFalse(run.document["rows"][0]["current_visibility"])

    async def test_changed_source_config_invalidates_download_in_flight_and_preview(
        self,
    ):
        await self.stock()
        ready = await self.ready()
        queued = await self.queue()
        old_hash = service.digest(service.source_config())
        config_io.save_shop(
            "xtrek",
            {
                "atomer_availability": {
                    "export_url": "https://atomer.example.invalid/new-source.xml"
                }
            },
        )
        async with self.sessions() as db:
            run = await db.get(Run, queued["id"])
            with self.assertRaisesRegex(service.ExportError, "source_changed"):
                await service.prepare(
                    db, run, {"export_url": buyers({})}, service.now(), old_hash
                )
        async with self.sessions() as db:
            with self.assertRaisesRegex(service.ExportError, "source_changed"):
                await service.export(
                    db, ready["id"], ExportInput(expected_revision=ready["revision"])
                )
        async with self.sessions() as db:
            self.assertEqual((await db.get(Run, queued["id"])).status, "queued")
            exported = await db.get(Run, ready["id"])
            self.assertEqual(exported.status, "ready")
            self.assertIsNone(exported.csv_content)

    async def test_history_includes_latest_download_beyond_thirty_newer_failures(self):
        latest_id = str(uuid4())
        async with self.sessions() as db:
            db.add(
                Run(
                    id=latest_id,
                    request_hash="a" * 64,
                    warehouse_id=self.warehouse_id,
                    trigger="daily",
                    status="exported",
                    completed_at=service.now() - timedelta(days=40),
                    created_at=service.now() - timedelta(days=40),
                    document={},
                    csv_content=b"latest",
                    csv_sha256=hashlib.sha256(b"latest").hexdigest(),
                )
            )
            for age in range(31):
                db.add(
                    Run(
                        id=str(uuid4()),
                        request_hash="b" * 64,
                        warehouse_id=self.warehouse_id,
                        trigger="daily",
                        status="failed",
                        created_at=service.now() - timedelta(days=age),
                        document={},
                    )
                )
            await db.commit()
            history = await service.list_runs(db)
            self.assertEqual(len(history["items"]), 31)
            retained = [item for item in history["items"] if item["download_available"]]
            self.assertEqual([item["id"] for item in retained], [latest_id])
            self.assertEqual((await service.download(db, latest_id))[0], b"latest")

    async def test_new_receipt_export_preserves_global_and_other_receipt_history(self):
        global_id, old_receipt_id, new_receipt_id = (str(uuid4()) for _ in range(3))
        async with self.sessions() as db:
            for run_id, age, batch_id, products in (
                (global_id, 40, None, None),
                (old_receipt_id, 35, str(uuid4()), [self.product_id]),
                (new_receipt_id, 1, str(uuid4()), [self.product_id]),
            ):
                content = run_id.encode()
                db.add(
                    Run(
                        id=run_id,
                        request_hash="c" * 64,
                        warehouse_id=self.warehouse_id,
                        trigger="manual" if batch_id else "daily",
                        status="exported",
                        receiving_batch_id=batch_id,
                        product_ids=products,
                        created_at=service.now() - timedelta(days=age),
                        completed_at=service.now() - timedelta(days=age),
                        document={"source": [{"code": run_id}], "rows": []},
                        csv_content=content,
                        csv_sha256=hashlib.sha256(content).hexdigest(),
                    )
                )
            for age in range(31):
                db.add(
                    Run(
                        id=str(uuid4()),
                        request_hash="d" * 64,
                        warehouse_id=self.warehouse_id,
                        trigger="daily",
                        status="failed",
                        created_at=service.now() - timedelta(hours=age),
                        document={},
                    )
                )
            await db.commit()
            await service.prune(db)
            await db.commit()
        async with self.sessions() as db:
            for run_id in (global_id, old_receipt_id, new_receipt_id):
                run = await db.get(Run, run_id)
                self.assertEqual(run.csv_content, run_id.encode())
                self.assertEqual(run.document["source"], [{"code": run_id}])
                self.assertNotIn("archived", run.document)
            history = await service.list_runs(db)
            downloadable = {
                item["id"] for item in history["items"] if item["download_available"]
            }
            self.assertIn(global_id, downloadable)
            self.assertIn(new_receipt_id, downloadable)
