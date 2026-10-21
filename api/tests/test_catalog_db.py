"""PostgreSQL integration tests. Dedicated, ephemeral localhost test DB only."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

from catalog_fixtures import FakeUpgates, northfinder_product, northfinder_variant, xml_item
from inventory_hub import config_io
from inventory_hub.catalog_types import ShopImportPreviewRequest, CatalogProduct, CatalogPrices
from inventory_hub.db_models import Product, Shop, Supplier, SupplierFeedRun, SupplierProduct, ProductIdentifier, IdentifierType
from inventory_hub.db_models_ext import ShopProduct, ShopProductContent
from inventory_hub.services import catalog, catalog_import

TEST_URL = os.environ.get("CATALOG_TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_URL, "Set CATALOG_TEST_DATABASE_URL to an isolated localhost *_catalog_test DB")
class CatalogDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.path.endswith("_catalog_test"):
            raise RuntimeError("Catalog tests require a dedicated localhost *_catalog_test database")
        self.schema = "catalog_test_" + uuid4().hex
        connection = await asyncpg.connect(TEST_URL)
        try:
            await connection.execute(f'CREATE SCHEMA "{self.schema}"')
            await connection.execute(f'SET search_path TO "{self.schema}"')
            root = Path(__file__).resolve().parents[2]
            for name in ("001_schema.sql", "004_shop_product_content.sql", "014_supplier_availability.sql", "021_product_feed_mapping.sql", "029_sku_policy.sql"):
                await connection.execute((root / "infra" / "db-init" / name).read_text())
        finally:
            await connection.close()
        # Each test gets a fresh namespace. The CI service is discarded after the job.
        self.engine = create_async_engine(TEST_URL.replace("postgresql://", "postgresql+asyncpg://", 1), poolclass=NullPool,
                                         connect_args={"server_settings": {"search_path": self.schema}})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patcher = patch.object(config_io, "DATA_ROOT", self.root)
        self.patcher.start()
        self.feed = self.root / "suppliers" / "paul-lange" / "feeds" / "xml" / "fixture.xml"
        cfg_path = config_io.supplier_path("paul-lange")
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text(json.dumps({"name": "Test supplier", "feeds": {"sources": {"products": {"mode": "local", "local_path": str(self.feed)}}},
            "adapter_settings": {"vat": 23, "mapping": {"postprocess": {"product_code_prefix": "PL-"}}}}))
        self.feed.parent.mkdir(parents=True)
        self.write_feed(xml_item(), xml_item("A-002", "Modrá prilba", ean="00012346"), xml_item("A-003", "Červené rukavice", ean="00012347"))

    async def asyncTearDown(self):
        self.patcher.stop()
        self.temp.cleanup()
        await self.engine.dispose()

    def write_feed(self, *items):
        self.feed.write_text("<SHOP>" + "".join(items) + "</SHOP>", encoding="utf-8")

    async def refresh(self):
        async with self.sessions() as db:
            result = await catalog.refresh_catalog(db, "paul-lange")
            await db.commit()
            return result

    async def test_search_without_accents_partial_code_ean_and_all_pages(self):
        await self.refresh()
        async with self.sessions() as db:
            page = await catalog.catalog_page(db, "paul-lange", q="CERVEN", page_size=1)
            self.assertEqual((page.total_items, page.pages, len(page.items)), (2, 2, 1))
            ids = await catalog.catalog_selection(db, "paul-lange", q="CERVEN")
            self.assertEqual(len(ids.ids), 2)
            self.assertEqual((await catalog.catalog_page(db, "paul-lange", ean="00012345")).total_items, 1)
            self.assertEqual((await catalog.catalog_page(db, "paul-lange", ean="12345")).total_items, 0)
            self.assertEqual((await catalog.catalog_page(db, "paul-lange", code="m-a-002")).total_items, 1)
            self.assertEqual((await catalog.catalog_page(db, "paul-lange", q="%_")).total_items, 0)
            self.assertEqual((await catalog.catalog_page(db, "paul-lange")).total_items, 3)
            detail = await catalog.catalog_detail(db, "paul-lange", ids.ids[0])
            self.assertIn("<SHOPITEM>", detail["source_xml"])
            self.assertEqual(await db.scalar(select(func.count()).select_from(Product)), 0)
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM stock_movements")), 0)

    async def image_fallback_fixture(self, *, variant=False):
        self.write_feed(xml_item(ean='5901234123457'))
        await self.refresh()
        cfg = config_io.load_supplier('paul-lange')
        cfg['adapter_settings']['catalog_image_fallback'] = {'enabled': True, 'shop_code': 'biketrek', 'brands': ['TEST']}
        config_io.supplier_path('paul-lange').write_text(json.dumps(cfg))
        async with self.sessions() as db:
            source = await db.scalar(select(SupplierProduct))
            data = dict(source.attributes['catalog']); data['images'] = []; data['warnings'].append('missing_images')
            source.attributes = {**source.attributes, 'catalog': data}; source.images = []
            product = Product(sku='OLD-123', name='Linked product', brand='TEST', source_supplier_product_id=source.id)
            shop = await db.scalar(select(Shop).where(Shop.code == 'biketrek'))
            db.add(product); await db.flush()
            parent = 'PARENT' if variant else product.sku
            leaf = {'code': product.sku, 'product_id': 11, 'ean': '5901234123457',
                    'images': [{'url': 'https://images.example.test/exact.jpg'}]}
            if variant: leaf['variant_id'] = 22
            mapping = ShopProduct(shop_id=shop.id, product_id=product.id, external_code=parent,
                parent_code=parent if variant else None, variant_code=product.sku if variant else None,
                external_id='22' if variant else '11', is_variant=variant, is_listed=True)
            cache = ShopProductContent(shop_id=shop.id, external_code=parent,
                data={'code': parent, 'product_id': 11, 'images': [{'url': 'https://images.example.test/parent.jpg'}],
                      'variants': [leaf]} if variant else leaf)
            db.add_all([mapping, cache]); await db.commit()
            return source.id, product.id, mapping.id, cache.id

    async def test_missing_catalog_images_use_verified_shop_cache_in_all_read_paths_without_mutation(self):
        source_id, _, _, _ = await self.image_fallback_fixture()
        async with self.sessions() as db:
            for product in [(await catalog.catalog_page(db, 'paul-lange')).items[0].product,
                            (await catalog.catalog_detail(db, 'paul-lange', source_id))['product'],
                            (await catalog.selected_products(db, 'paul-lange', 'products', [source_id]))[0]]:
                self.assertEqual(product.images, ['https://images.example.test/exact.jpg'])
                self.assertEqual(product.mapping_provenance['image_fallback']['native_id'], '11')
                self.assertIn('images_from_linked_shop', product.warnings)
                self.assertNotIn('missing_images', product.warnings)
            raw = await db.get(SupplierProduct, source_id)
            self.assertEqual(raw.attributes['catalog']['images'], [])
            self.assertEqual(raw.images, [])
            self.assertEqual(await db.scalar(text('SELECT count(*) FROM stock_movements')), 0)
        await self.refresh()
        async with self.sessions() as db:
            product = (await catalog.catalog_detail(db, 'paul-lange', source_id))['product']
            self.assertEqual(product.images[0], 'https://images.example.com/A-001.jpg', 'Supplier images retain priority after refresh')

    async def test_variant_fallback_rejects_stale_native_identity_and_does_not_guess_parent_images(self):
        source_id, _, mapping_id, cache_id = await self.image_fallback_fixture(variant=True)
        async with self.sessions() as db:
            self.assertEqual((await catalog.catalog_detail(db, 'paul-lange', source_id))['product'].images,
                             ['https://images.example.test/exact.jpg'])
            mapping = await db.get(ShopProduct, mapping_id); mapping.external_id = '99'; await db.commit()
            self.assertEqual((await catalog.catalog_detail(db, 'paul-lange', source_id))['product'].images, [])
            mapping.external_id = '22'
            cache = await db.get(ShopProductContent, cache_id)
            value = dict(cache.data); value['variants'] = [{**value['variants'][0], 'images': []}]
            cache.data = value; await db.commit()
            self.assertEqual((await catalog.catalog_detail(db, 'paul-lange', source_id))['product'].images, [])
            cache.data = {**cache.data, 'add_images_into_variants_yn': True}; await db.commit()
            self.assertEqual((await catalog.catalog_detail(db, 'paul-lange', source_id))['product'].images,
                             ['https://images.example.test/parent.jpg'])

    async def test_image_fallback_requires_enabled_exact_binding_brand_and_unique_native_target(self):
        source_id, product_id, _, _ = await self.image_fallback_fixture()
        async with self.sessions() as db:
            product = await db.get(Product, product_id); product.source_supplier_product_id = None; await db.commit()
            self.assertEqual((await catalog.catalog_detail(db, 'paul-lange', source_id))['product'].images, [])
            product.source_supplier_product_id = source_id; product.brand = 'OTHER'; await db.commit()
            self.assertEqual((await catalog.catalog_detail(db, 'paul-lange', source_id))['product'].images, [])
            product.brand = 'TEST'
            second = Product(sku='OTHER-456', name='Ambiguous second listing', brand='TEST', source_supplier_product_id=source_id)
            db.add(second); await db.flush()
            shop = await db.scalar(select(Shop).where(Shop.code == 'biketrek'))
            db.add(ShopProduct(shop_id=shop.id, product_id=second.id, external_id='33', external_code=second.sku))
            db.add(ShopProductContent(shop_id=shop.id, external_code=second.sku, data={'code': second.sku, 'product_id': 33,
                'images': [{'url': 'https://images.example.test/other.jpg'}]}))
            await db.commit()
            self.assertEqual((await catalog.catalog_detail(db, 'paul-lange', source_id))['product'].images, [])

    async def test_refresh_is_atomic_keeps_ids_and_rejects_stale_selection(self):
        first = await self.refresh()
        async with self.sessions() as db:
            initial = await catalog.catalog_selection(db, "paul-lange")
        self.write_feed(xml_item(name="Aktualizovaná prilba"))
        second = await self.refresh()
        self.assertNotEqual(first["run_id"], second["run_id"])
        async with self.sessions() as db:
            page = await catalog.catalog_page(db, "paul-lange")
            self.assertEqual(page.total_items, 1)
            id = page.items[0].product.id
            self.assertIn(id, initial.ids)
            with self.assertRaises(catalog.CatalogError):
                await catalog.selected_products(db, "paul-lange", "products", [id], first["run_id"])
            self.assertEqual(await db.scalar(select(func.count()).select_from(SupplierProduct)), 3)
        self.feed.write_text("<SHOP><broken>")
        with self.assertRaises(catalog.CatalogError):
            await self.refresh()
        async with self.sessions() as db:
            page = await catalog.catalog_page(db, "paul-lange")
            self.assertEqual(page.run_id, second["run_id"])
            self.assertEqual(page.items[0].product.name, "Aktualizovaná prilba")
            self.assertEqual((await catalog.catalog_status(db, "paul-lange"))["status"], "failed")
            self.assertEqual(await db.scalar(select(func.count()).select_from(SupplierFeedRun)), 3)

    async def test_explicit_groups_expand_and_filter_without_guessing(self):
        self.write_feed(xml_item(extra="<ITEMGROUP_ID>G1</ITEMGROUP_ID>"), xml_item("A-002", "Modrá prilba", extra="<ITEMGROUP_ID>G1</ITEMGROUP_ID>", ean="00012346"))
        await self.refresh()
        async with self.sessions() as db:
            grouped = await catalog.catalog_page(db, "paul-lange")
            self.assertEqual((grouped.total, grouped.total_items), (1, 2))
            self.assertTrue(grouped.items[0].is_group)
            self.assertEqual(len(grouped.items[0].variants), 2)
            filtered = await catalog.catalog_page(db, "paul-lange", q="modra")
            self.assertEqual(filtered.items[0].variants_count, 1)
            self.assertEqual(len((await catalog.catalog_detail(db, "paul-lange", filtered.items[0].product.id))["variants"]), 2)
            self.assertEqual((await catalog.catalog_page(db, "paul-lange", grouped=False)).total, 2)

    async def test_cached_singleton_group_is_flattened_but_one_selected_real_variant_is_preserved(self):
        self.write_feed(xml_item(extra='<ITEMGROUP_ID>REAL</ITEMGROUP_ID>'),
            xml_item('A-002', extra='<ITEMGROUP_ID>REAL</ITEMGROUP_ID>', ean='00012346'),
            xml_item('A-003', extra='<ITEMGROUP_ID>SINGLE</ITEMGROUP_ID>', ean='00012347'))
        await self.refresh()
        async with self.sessions() as db:
            rows = (await db.scalars(select(SupplierProduct).order_by(SupplierProduct.id))).all()
            single = next(row for row in rows if row.supplier_sku == 'A-003')
            self.assertIsNone(single.supplier_group_code, 'Fresh ingest normalizes a whole feed')
            data = dict(single.attributes['catalog'])
            data.update(group_code='SINGLE', group_name='Old group', variant_relationship='explicit',
                        variant_attributes=[{'name': 'Veľkosť', 'value': 'M'}])
            single.attributes = {**single.attributes, 'catalog': data}
            single.supplier_group_code = 'SINGLE'
            await db.flush()
            normalized = (await catalog.selected_products(db, 'paul-lange', 'products', [single.id]))[0]
            self.assertIsNone(normalized.group_code)
            self.assertIn(('Veľkosť', 'M'), [(p.name, p.value) for p in normalized.parameters])
            real = next(row for row in rows if row.supplier_sku == 'A-001')
            selected = (await catalog.selected_products(db, 'paul-lange', 'products', [real.id]))[0]
            self.assertEqual(selected.group_code, 'REAL', 'Filtered selection is not feed cardinality')
            self.assertEqual(single.attributes['catalog']['group_code'], 'SINGLE', 'Reads do not rewrite stored source facts')

    async def test_northfinder_index_keeps_conflicting_eans_and_inherited_prices(self):
        path = config_io.supplier_path("northfinder")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"feeds": {"sources": {"products": {"mode": "local", "local_path": str(self.feed)}}},
            "adapter_settings": {"vat": 23, "product_code_prefix": "NF-"}}))
        variants = northfinder_variant(ean="000001") + northfinder_variant(ean="000002") + northfinder_variant("N-RED-L", "000003", "L", purchase="", retail="")
        self.feed.write_text("<products>" + northfinder_product(variants) + "</products>")
        async with self.sessions() as db:
            await catalog.refresh_catalog(db, "northfinder")
            await db.commit()
            page = await catalog.catalog_page(db, "northfinder", q="cervena bunda")
            self.assertEqual((page.total, page.total_items), (1, 2))
            found = [(await catalog.catalog_page(db, "northfinder", ean=ean)).items[0].product for ean in ("000001", "000002")]
            self.assertEqual(found[0].id, found[1].id)
            self.assertEqual(found[0].import_blockers, ["duplicate_supplier_code"])
            row = await db.get(SupplierProduct, found[0].id)
            self.assertIsNone(row.ean, "No conflicting EAN is chosen as the canonical database identity")
            detail = await catalog.catalog_detail(db, "northfinder", found[0].id)
            self.assertIn("<conflicting_items>", detail["source_xml"])
            inherited = (await catalog.catalog_page(db, "northfinder", ean="000003")).items[0].product
            self.assertEqual(inherited.prices.purchase_net, 10)
            self.assertIn("inherited_retail_price", inherited.warnings)
            first_ids = set((await catalog.catalog_selection(db, "northfinder")).ids)
            await catalog.refresh_catalog(db, "northfinder")
            await db.commit()
            self.assertEqual(set((await catalog.catalog_selection(db, "northfinder")).ids), first_ids)
            self.assertEqual(await db.scalar(select(func.count()).select_from(SupplierProduct)), 2)
            self.assertEqual(await db.scalar(select(func.count()).select_from(Product)), 0)
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM stock_movements")), 0)

    async def test_sloger_combined_catalog_keeps_ids_unknown_costs_and_duplicate_evidence(self):
        path = config_io.supplier_path("sloger")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"name": "Sloger test", "feeds": {"sources": {"products": {
            "mode": "local", "local_path": str(self.feed), "catalog_parser": "sloger"}}},
            "adapter_settings": {"vat_rate": 20, "vat": 23, "product_code_prefix": "SL-"}}))

        def combined_feed(first_price="24.60"):
            return f"""<products>
              <product><id>100</id><code>SHIRT</code><name>Test shirt</name><manufacturer>Brand A</manufacturer>
                <description>Parent context</description><price_msrp>24.60</price_msrp>
                <images><url>https://supplier.example/image.jpg</url></images><options>
                  <option><id>1001</id><code>SHIRT-M</code><name>Veľkosť M</name><ean>00000001</ean><price_msrp>{first_price}</price_msrp></option>
                  <option><id>1002</id><code>SHIRT-L</code><name>Veľkosť L</name><ean>00000002</ean><price_msrp>24.60</price_msrp></option>
                </options></product>
              <product><id>200</id><code>DUP</code><name>Different brand accessory</name><manufacturer>Brand A</manufacturer>
                <ean>00000003</ean><price_msrp>12.30</price_msrp><options/></product>
              <product><id>300</id><code>DUP</code><name>Unrelated accessory</name><manufacturer>Brand B</manufacturer>
                <ean>00000004</ean><price_msrp>36.90</price_msrp><options/></product>
            </products>"""

        self.feed.write_text(combined_feed(), encoding="utf-8")
        async with self.sessions() as db:
            first = await catalog.refresh_catalog(db, "sloger")
            await db.commit()
            self.assertEqual(first["items"], 3)
            self.assertEqual((await catalog.catalog_status(db, "sloger"))["defaults"]["vat_percent"], "20")
            initial = await catalog.catalog_selection(db, "sloger")
            page = await catalog.catalog_page(db, "sloger", grouped=False)
            original_ids = {row.product.code: row.product.id for row in page.items}
            for ean in ("00000003", "00000004"):
                duplicate = (await catalog.catalog_page(db, "sloger", ean=ean)).items[0].product
                self.assertEqual(duplicate.id, original_ids["DUP"])
                self.assertEqual(duplicate.import_blockers, ["duplicate_supplier_code"])
                self.assertIsNone((await db.get(SupplierProduct, duplicate.id)).ean)
                self.assertIsNone(duplicate.prices.retail_gross)
            detail = await catalog.catalog_detail(db, "sloger", original_ids["DUP"])
            self.assertIn("<conflicting_items>", detail["source_xml"])
            self.assertIn("Unrelated accessory", detail["source_xml"])
            self.assertIn("Different brand accessory", detail["source_xml"])
            for row in (await db.scalars(select(SupplierProduct))).all():
                self.assertIsNone(row.purchase_price, "Unknown acquisition cost must remain NULL, not zero or DMOC")
            self.feed.write_text(combined_feed("36.90"), encoding="utf-8")
            second = await catalog.refresh_catalog(db, "sloger")
            await db.commit()
            current = await catalog.catalog_selection(db, "sloger")
            self.assertEqual(set(current.ids), set(initial.ids))
            self.assertNotEqual(first["run_id"], second["run_id"])
            changed = (await catalog.selected_products(db, "sloger", "products", [original_ids["SHIRT-M"]]))[0]
            self.assertEqual(str(changed.prices.retail_gross), "36.90")
            self.assertEqual(changed.prices.vat_percent, 20)
            self.assertIsNone(changed.prices.purchase_net)
            self.assertIsNone(changed.supplier_stock)
            self.assertEqual(await db.scalar(select(func.count()).select_from(Product)), 0)
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM stock_movements")), 0)
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM stock_balances")), 0)

        self.feed.write_text("<products><broken>", encoding="utf-8")
        async with self.sessions() as db:
            with self.assertRaises(catalog.CatalogError):
                await catalog.refresh_catalog(db, "sloger")
        async with self.sessions() as db:
            selection = await catalog.catalog_selection(db, "sloger")
            self.assertEqual(selection.run_id, second["run_id"])
            self.assertEqual(set(selection.ids), set(initial.ids))
            self.assertEqual((await catalog.catalog_status(db, "sloger"))["status"], "failed")
            self.assertEqual(await db.scalar(select(func.count()).select_from(Product)), 0)
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM stock_movements")), 0)

    async def test_cached_ean_status_filters_detail_and_variant_order_agree(self):
        cfg = config_io.supplier_path("northfinder")
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({"feeds": {"sources": {"products": {"mode": "local", "local_path": str(self.feed)}}},
                                   "adapter_settings": {"vat": 23, "product_code_prefix": "NF-"}}))
        variants = northfinder_variant("N-RED-L", "000001", "L") + northfinder_variant("N-RED-S", "000002", "S") + northfinder_variant("N-RED-M", "000003", "M")
        self.feed.write_text("<products>" + northfinder_product(variants) + "</products>")
        path = config_io.shop_path("test-shop")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"upgates_api_base_url": "https://shop.example.test/api/v2", "upgates_login": "fixture", "upgates_api_key": "fixture"}))
        client = FakeUpgates()
        client.products = {"LEGACY": {"product_id": 42, "code": "LEGACY", "variants": [{"code": "OLD-L", "ean": "000001"}]}}
        catalog_import.checked_remote_identities("test-shop", client)
        async with self.sessions() as db:
            await catalog.refresh_catalog(db, "northfinder")
            await db.commit()
            with patch.object(catalog_import.UpgatesClient, "from_shop", side_effect=AssertionError("Browsing must not call Upgates")):
                page = await catalog.catalog_page(db, "northfinder", shop="test-shop")
                self.assertIsNotNone(page.shop_checked_at)
                self.assertEqual([p.shop_code for p in page.items[0].variants], ["NF-N-RED-S", "NF-N-RED-M", "NF-N-RED-L"])
                self.assertEqual([p.listed for p in page.items[0].variants], [False, False, True])
                listed = await catalog.catalog_page(db, "northfinder", shop="test-shop", listing="listed")
                self.assertEqual(listed.total_items, 1)
                self.assertEqual(listed.items[0].product.shop_matches[0].code, "OLD-L")
                remaining = await catalog.catalog_selection(db, "northfinder", shop="test-shop", listing="unlisted")
                self.assertEqual(remaining.total, 2)
                detail = await catalog.catalog_detail(db, "northfinder", listed.items[0].product.id, shop="test-shop")
                self.assertEqual([p.id for p in detail["variants"]], [p.id for p in page.items[0].variants])
                self.assertTrue(detail["product"].listed)
            client.products = {"NF-G-N": {"code": "NF-G-N", "variants": []}}
            catalog_import.checked_remote_identities("test-shop", client, refresh=True)
            self.assertEqual((await catalog.catalog_selection(db, "northfinder", shop="test-shop", listing="listed")).total, 3)
            self.assertEqual((await catalog.catalog_selection(db, "northfinder", shop="test-shop", listing="unlisted")).total, 0)

    async def test_preview_and_local_registration_do_not_change_stock(self):
        await self.refresh()
        path = config_io.shop_path("test-shop")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"upgates_api_base_url": "https://test.example.com/api/v2", "upgates_login": "fixture", "upgates_api_key": "fixture"}))
        client = FakeUpgates()
        async with self.sessions() as db:
            db.add(Shop(code="test-shop", name="Test shop", platform="upgates"))
            await db.commit()
            page = await catalog.catalog_page(db, "paul-lange")
            id = page.items[0].product.id
            with patch.object(catalog_import.UpgatesClient, "from_shop", return_value=client):
                preview = await catalog_import.create_preview(db, "test-shop", ShopImportPreviewRequest(supplier="paul-lange", product_ids=[id], run_id=page.run_id))
            self.assertEqual(preview.items[0].errors, ['sku_identity_required'])
            source = (await catalog.selected_products(db, 'paul-lange', 'products', [id]))[0]
            db.add(Product(sku=source.shop_code, name=source.name, brand=source.brand))
            await db.flush()
            with patch.object(catalog_import.UpgatesClient, "from_shop", return_value=client):
                preview = await catalog_import.create_preview(db, "test-shop", ShopImportPreviewRequest(supplier="paul-lange", product_ids=[id], run_id=page.run_id))
            self.assertEqual(preview.items[0].status, "ready")
            self.assertEqual(client.sent, [])
            product = (await catalog.selected_products(db, "paul-lange", "products", [id]))[0]
            item = preview.items[0].model_dump()
            remote = {**item["payload"], "product_id": 101, "admin_url": "https://admin.example.test/product/101",
                      "descriptions": [{"language": "en", "url": "https://shop.example.test/en/product"},
                                       {"language": "sk", "url": "https://shop.example.test/p/produkt"}]}
            await catalog_import.register_created(db, "test-shop", item, {id: product}, remote)
            await db.commit()
            await catalog_import.register_created(db, "test-shop", item, {id: product}, remote)
            await db.commit()
            self.assertEqual(await db.scalar(select(func.count()).select_from(Product)), 1)
            self.assertEqual(await db.scalar(select(func.count()).select_from(ShopProduct)), 1)
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM stock_movements")), 0)
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM stock_balances")), 0)
            self.assertTrue((await catalog.catalog_page(db, "paul-lange", shop="test-shop", listing="listed")).items[0].product.listed)
            listed = (await catalog.catalog_page(db, "paul-lange", shop="test-shop", listing="listed")).items[0].product
            self.assertEqual(listed.shop_url, "https://shop.example.test/p/produkt")
            self.assertEqual(listed.shop_admin_url, "https://admin.example.test/product/101")
            self.assertFalse(listed.shop_active)
            self.assertEqual((await catalog.catalog_detail(db, "paul-lange", id, shop="test-shop"))["product"].shop_url, listed.shop_url)
            self.assertIsNone((await catalog.catalog_detail(db, "paul-lange", id))["product"].shop_url)
            self.assertIsNone((await catalog.catalog_page(db, "paul-lange", shop="other-shop")).items[0].product.shop_url)

    async def test_variant_links_use_parent_content_only_in_selected_shop(self):
        self.write_feed(xml_item(extra="<ITEMGROUP_ID>GROUP1</ITEMGROUP_ID>"),
                        xml_item('A-002', extra="<ITEMGROUP_ID>GROUP1</ITEMGROUP_ID>", ean='00012346'))
        await self.refresh()
        async with self.sessions() as db:
            item = (await catalog.catalog_page(db, "paul-lange")).items[0].product
            product = Product(sku=item.shop_code, name=item.name)
            shops = [Shop(code="first-shop", name="First", platform="upgates"), Shop(code="second-shop", name="Second", platform="upgates")]
            db.add_all([product, *shops])
            await db.flush()
            for shop in shops:
                db.add(ShopProduct(shop_id=shop.id, product_id=product.id, external_code=item.shop_code,
                                   variant_code=item.shop_code, parent_code="PARENT", is_listed=True))
                db.add(ShopProductContent(shop_id=shop.id, external_code="PARENT", data={
                    "descriptions": [{"language": "sk", "url": f"https://{shop.code}.example.test/p/parent"}],
                    "admin_url": f"https://{shop.code}.example.test/admin/parent", "active_yn": True}))
            await db.commit()
            with patch.object(catalog_import.UpgatesClient, "from_shop", side_effect=AssertionError("Links must not call Upgates")):
                for shop in shops:
                    row = (await catalog.catalog_page(db, "paul-lange", shop=shop.code)).items[0]
                    expected = f"https://{shop.code}.example.test/p/parent"
                    self.assertEqual(len(row.variants), 2, 'Parent-link coverage uses a real variant family')
                    self.assertEqual(row.product.shop_url, expected)
                    self.assertEqual(row.variants[0].shop_url, expected)
                    detail = await catalog.catalog_detail(db, "paul-lange", item.id, shop=shop.code)
                    self.assertEqual(detail["variants"][0].shop_url, expected)
                    self.assertTrue(detail["product"].shop_active)

    async def test_local_mixed_case_match_agrees_with_listing_filter_without_remote_cache(self):
        self.write_feed(xml_item("STRAẞE"), xml_item("A-002", ean="0002"), xml_item("A-003", ean="0003"))
        await self.refresh()
        async with self.sessions() as db:
            item = (await catalog.catalog_page(db, "paul-lange", code="STRAẞE")).items[0].product
            product = Product(sku=item.shop_code.casefold(), name=item.name)
            shop = Shop(code="case-shop", name="Case shop", platform="upgates")
            db.add_all([product, shop])
            await db.flush()
            db.add(ShopProduct(shop_id=shop.id, product_id=product.id, external_code=item.shop_code.casefold(), is_listed=True))
            db.add(ShopProductContent(shop_id=shop.id, external_code=item.shop_code.casefold(), data={
                "descriptions": [{"language": "sk", "url": "https://shop.example.test/p/mixed-case"}]}))
            await db.commit()
            page = await catalog.catalog_page(db, "paul-lange", shop="case-shop", listing="listed")
            self.assertEqual(page.total_items, 1)
            self.assertTrue(page.items[0].product.listed)
            self.assertEqual(page.items[0].product.shop_url, "https://shop.example.test/p/mixed-case")
            self.assertTrue((await catalog.catalog_detail(db, "paul-lange", item.id, shop="case-shop"))["product"].listed)
            self.assertEqual((await catalog.catalog_selection(db, "paul-lange", shop="case-shop", listing="unlisted")).total, 2)


    async def test_catalog_identity_reuses_shared_sku_and_blocks_disjoint_ean(self):
        async with self.sessions() as db:
            product = Product(sku="COMMON", name="Physical item")
            db.add(product)
            await db.flush()
            db.add(ProductIdentifier(product_id=product.id, identifier_type=IdentifierType.ean, value="4006381333931"))
            await db.flush()
            source = CatalogProduct(id=5, supplier="paul-lange", code="RAW", shop_code="COMMON",
                name="Item", prices=CatalogPrices(currency="EUR"))
            self.assertEqual(await catalog_import.local_identities(db, [source]), ({5: product.id}, set()))
            source.eans = ["5901234123457"]
            self.assertEqual(await catalog_import.local_identities(db, [source]), ({}, {5}))
            source.eans = ["4006381333931"]
            self.assertEqual(await catalog_import.local_identities(db, [source]), ({5: product.id}, set()))

    async def test_catalog_identity_checks_supplier_alias_case_collision_and_duplicate_leaves(self):
        async with self.sessions() as db:
            supplier = Supplier(code="test-local", name="Supplier")
            first, second = Product(sku="COMMON", name="First"), Product(sku="OTHER", name="Second")
            db.add_all([supplier, first, second])
            await db.flush()
            db.add(ProductIdentifier(product_id=second.id, identifier_type=IdentifierType.supplier_sku,
                supplier_id=supplier.id, value="RAW"))
            db.add(ProductIdentifier(product_id=first.id, identifier_type=IdentifierType.ean, value="4006381333931"))
            await db.flush()
            source = CatalogProduct(id=5, supplier="test-local", code="RAW", shop_code="COMMON",
                name="Item", prices=CatalogPrices(currency="EUR"))
            self.assertEqual(await catalog_import.local_identities(db, [source]), ({}, {5}))
            source.code = "DIFFERENT"
            source.shop_code = "common"
            self.assertEqual(await catalog_import.local_identities(db, [source]), ({}, {5}))
            source.shop_code = "COMMON"
            source.eans = ["4006381333931"]
            alias = source.model_copy(update={"id": 6, "shop_code": "ALIAS"})
            matches, conflicts = await catalog_import.local_identities(db, [source, alias])
            self.assertEqual(matches, {5: first.id, 6: first.id})
            self.assertEqual(conflicts, {5, 6})

    async def test_catalog_never_matches_a_shared_unverified_short_barcode(self):
        async with self.sessions() as db:
            first, second = Product(sku="A", name="First"), Product(sku="B", name="Second")
            db.add_all([first, second])
            await db.flush()
            for product in (first, second):
                db.add(ProductIdentifier(product_id=product.id, identifier_type=IdentifierType.unverified_barcode,
                    value="12345"))
            await db.flush()
            source = CatalogProduct(id=5, supplier="paul-lange", code="RAW", shop_code="NEW", eans=["12345"],
                name="Item", prices=CatalogPrices(currency="EUR"))
            self.assertEqual(await catalog_import.local_identities(db, [source]), ({}, set()))
