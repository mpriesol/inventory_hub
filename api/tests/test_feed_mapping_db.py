"""Mapping persistence, explicit remapping and scope isolation in a guarded database."""
import json
import unittest
from io import BytesIO
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import func, select, text

import test_catalog_db as catalog_db
from inventory_hub import config_io
from inventory_hub.db_models import Product, Shop, SupplierFeed, SupplierProduct
from inventory_hub.feed_mapping_models import ProductFeedMappingRevision
from inventory_hub.feed_mapping_types import MappingDefinition, MappingPreview, MappingSave
from inventory_hub.services import catalog, feed_mapping as mapping


@unittest.skipUnless(catalog_db.TEST_URL, "Dedicated localhost *_catalog_test database required")
class FeedMappingDatabaseTests(unittest.IsolatedAsyncioTestCase):
    write_feed = catalog_db.CatalogDatabaseTests.write_feed

    async def asyncSetUp(self):
        await catalog_db.CatalogDatabaseTests.asyncSetUp(self)
        async with self.engine.begin() as connection:
            raw = await connection.get_raw_connection()
            await raw.driver_connection.execute((Path(__file__).resolve().parents[2] / "infra/db-init/021_product_feed_mapping.sql").read_text())

    async def asyncTearDown(self):
        await catalog_db.CatalogDatabaseTests.asyncTearDown(self)

    async def test_save_revision_conflicts_do_not_rewrite_catalog_and_reapply_is_explicit(self):
        async with self.sessions() as db:
            first = await catalog.refresh_catalog(db, "paul-lange")
            page = await catalog.catalog_page(db, "paul-lange")
            product_id = page.items[0].product.id
            original_name = page.items[0].product.name
            definition = MappingDefinition.model_validate({"bindings": [{"target": "name", "source": "PRODUCT",
                "transforms": [{"op": "prefix", "value": "Mapped "}]}]})
            saved = await mapping.save_mapping(db, "paul-lange", MappingSave(expected_revision=0, definition=definition))
            self.assertEqual(saved["revision"], 1)
            self.assertEqual((await catalog.selected_products(db, "paul-lange", "products", [product_id]))[0].name, original_name)
            with self.assertRaises(catalog.CatalogError) as stale:
                await mapping.apply_shop_mapping(db, (await catalog.selected_products(db, "paul-lange", "products", [product_id]))[0], "biketrek")
            self.assertEqual(stale.exception.code, "feed_mapping_stale")
            with self.assertRaises(catalog.CatalogError) as conflict:
                await mapping.save_mapping(db, "paul-lange", MappingSave(expected_revision=0, definition=definition))
            self.assertEqual(conflict.exception.status, 409)
            self.assertEqual((await mapping.save_mapping(db, "paul-lange", MappingSave(expected_revision=1, definition=definition)))["revision"], 1)
            path = await mapping.source_path(db, "paul-lange", "products")
            remapped = await catalog.refresh_catalog(db, "paul-lange", source_path=path, expected_mapping_revision=1)
            self.assertNotEqual(first["run_id"], remapped["run_id"])
            current = (await catalog.selected_products(db, "paul-lange", "products", [product_id]))[0]
            self.assertEqual(current.name, "Mapped " + original_name)
            self.assertEqual(current.mapping_revision, 1)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductFeedMappingRevision)), 1)
            self.assertEqual(await db.scalar(select(func.count()).select_from(Product)), 0)
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM stock_movements")), 0)
            await db.commit()

    async def test_uploaded_generic_supplier_is_ingested_and_searchable_without_registered_parser(self):
        cfg_path = config_io.supplier_path("generic-test")
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text(json.dumps({"name": "Generic feed", "adapter_settings": {"vat": 23,
            "mapping": {"postprocess": {"product_code_prefix": "GEN-"}}}}))
        definition = MappingDefinition.model_validate({"bindings": [{"target": "code", "source": "SKU"},
            {"target": "name", "source": "Name"}, {"target": "eans", "source": "EAN"},
            {"target": "prices.purchase_net", "source": "Purchase"}, {"target": "prices.retail_gross", "source": "Retail"},
            {"target": "parameter:Valve", "source": "Valve"}]})
        async with self.sessions() as db:
            sample = await mapping.upload_sample(db, "generic-test", "products",
                UploadFile(BytesIO(b"SKU;Name;EAN;Purchase;Retail;Valve\n0001;Tube;001234;10;24.60;SV\n"), filename="feed.csv"), definition)
            self.assertEqual(sample["total_records"], 1)
            saved = await mapping.save_mapping(db, "generic-test", MappingSave(expected_revision=0, definition=definition))
            path = await mapping.source_path(db, "generic-test", "products", sample["sample_id"])
            await catalog.refresh_catalog(db, "generic-test", source_path=path, expected_mapping_revision=saved["revision"])
            status = await catalog.catalog_status(db, "generic-test")
            self.assertTrue(status["sources"][0]["supported"])
            self.assertTrue(status["sources"][0]["configured"])
            page = await catalog.catalog_page(db, "generic-test", q="Tube")
            self.assertEqual(page.total_items, 1)
            result = (await catalog.selected_products(db, "generic-test", "products", [page.items[0].product.id]))[0]
            self.assertEqual(result.shop_code, "GEN-0001")
            self.assertEqual(result.raw_fields["Valve"], "SV")
            self.assertEqual(result.seo_title, "Tube")
            # Subsequent normal catalog refresh reuses the mapped uploaded source.
            await catalog.refresh_catalog(db, "generic-test")
            self.assertEqual(await db.scalar(select(func.count()).select_from(Product)), 0)
            await db.commit()

    async def test_shop_category_and_fields_do_not_change_shared_supplier_rows(self):
        self.write_feed(catalog_db.xml_item(extra="<CATEGORYTEXT>Tubes</CATEGORYTEXT>"))
        async with self.sessions() as db:
            self.assertIsNotNone(await db.scalar(select(Shop.id).where(Shop.code == "biketrek", Shop.is_active.is_(True))))
            await catalog.refresh_catalog(db, "paul-lange")
            definition = MappingDefinition.model_validate({"category_rules": [{"source": "Tubes", "target_code": "TUBE-LEAF"}],
                "bindings": [{"target": "short_description", "constant": "Manual mapping constant"}]})
            await mapping.save_mapping(db, "paul-lange", MappingSave(shop="biketrek", expected_revision=0, definition=definition))
            page = await catalog.catalog_page(db, "paul-lange")
            source = (await catalog.selected_products(db, "paul-lange", "products", [page.items[0].product.id]))[0]
            target = await mapping.apply_shop_mapping(db, source, "biketrek")
            other = await mapping.apply_shop_mapping(db, source, "xtrek")
            self.assertEqual(target.target_category_code, "TUBE-LEAF")
            self.assertEqual(target.short_description, "Manual mapping constant")
            self.assertIsNone(other.target_category_code)
            self.assertEqual(other.short_description, "")
            unchanged = (await catalog.selected_products(db, "paul-lange", "products", [source.id]))[0]
            self.assertIsNone(unchanged.target_category_code)
            self.assertEqual(unchanged.short_description, "")
            await db.commit()

    async def test_preview_is_read_only_and_reports_missing_identifiers(self):
        async with self.sessions() as db:
            report = await mapping.preview_mapping(db, "paul-lange", MappingPreview(definition=MappingDefinition()))
            self.assertEqual(len(report["items"]), 3)
            self.assertEqual(report["errors"], [])
            self.assertEqual(await db.scalar(select(func.count()).select_from(SupplierFeed)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(SupplierProduct)), 0)
            sample = await mapping.upload_sample(db, "paul-lange", "products", UploadFile(BytesIO(b'SKU;Name\n;Missing ID\n'), filename="feed.csv"), MappingDefinition())
            definition = MappingDefinition.model_validate({"format": "csv", "bindings": [
                {"target": "code", "source": "SKU"}, {"target": "name", "source": "Name"}]})
            report = await mapping.preview_mapping(db, "paul-lange", MappingPreview(definition=definition, sample_id=sample["sample_id"]))
            self.assertEqual(len(report["errors"]), 1)
            self.assertEqual(report["items"], [])
