"""Synthetic supplier-only keeper evidence; raw barcode/history never move."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import delete, func, select, text

import test_product_consolidation_db as fixture
from inventory_hub.catalog_types import CatalogPrices, CatalogProduct
from inventory_hub.db_models import IdentifierType, Product, ProductIdentifier, Shop, Supplier, SupplierProduct
from inventory_hub.db_models_ext import ProductSupplySource, ShopProduct
from inventory_hub.product_consolidation_models import ProductCanonicalAlias, ProductConsolidation
from inventory_hub.product_consolidation_types import ProductConsolidationPreviewRequest
from inventory_hub.services import product_consolidation as service, product_links as links
from inventory_hub.services import product_consolidation_supplier_identity as proof
from inventory_hub.services import product_dependency_scope as scope
from inventory_hub.services.identifiers import ProductIdentifierService


@unittest.skipUnless(fixture.TEST_URL, 'Dedicated localhost *_catalog_test database required')
class SupplierGtinDatabaseTests(unittest.IsolatedAsyncioTestCase):
    transaction = fixture.ProductConsolidationDatabaseTests.transaction
    leaf = fixture.ProductConsolidationDatabaseTests.leaf
    discover = fixture.ProductConsolidationDatabaseTests.discover
    verify = fixture.ProductConsolidationDatabaseTests.verify
    absence = fixture.ProductConsolidationDatabaseTests.absence
    code_absence = fixture.ProductConsolidationDatabaseTests.code_absence
    preview = fixture.ProductConsolidationDatabaseTests.preview
    apply = fixture.ProductConsolidationDatabaseTests.apply
    stock = fixture.ProductConsolidationDatabaseTests.stock
    asyncTearDown = fixture.ProductConsolidationDatabaseTests.asyncTearDown

    async def asyncSetUp(self):
        await fixture.ProductConsolidationDatabaseTests.asyncSetUp(self)
        self.article = 'SYN-ARTICLE-BLUE-M'
        self.observed = datetime.now(timezone.utc) - timedelta(days=20)
        async with self.transaction() as db:
            await db.execute(delete(ProductIdentifier).where(ProductIdentifier.product_id == self.keeper))
            barcode = await db.scalar(select(ProductIdentifier).where(ProductIdentifier.product_id == self.retired))
            barcode.identifier_type, barcode.value, barcode.is_primary = IdentifierType.ean, fixture.EAN, True
            self.barcode_id = barcode.id
            for pid in (self.keeper, self.retired):
                (await db.get(Product, pid)).brand = 'Synthetic'
            supplier = Supplier(code='supplier-proof', name='Synthetic supplier proof')
            db.add(supplier); await db.flush()
            self.supplier_id = supplier.id
            product = CatalogProduct(supplier=supplier.code, code=self.article, manufacturer_code=self.article,
                shop_code='SYN-KEEP', name='Synthetic blue M retail unit', brand='Synthetic', eans=[fixture.EAN],
                variant_attributes=[{'name': 'SIZE', 'value': 'M'}], fetched_at=self.observed,
                prices=CatalogPrices(currency='EUR'))
            source = SupplierProduct(supplier_id=supplier.id, supplier_sku=self.article, manufacturer_sku=self.article,
                name=product.name, brand=product.brand, ean=fixture.EAN, last_seen_at=self.observed,
                attributes={'catalog': product.model_dump(mode='json')})
            db.add(source); await db.flush()
            self.source_id = source.id
            db.add(ProductIdentifier(product_id=self.keeper, identifier_type=IdentifierType.supplier_sku,
                supplier_id=supplier.id, value=self.article))
            relation = ProductSupplySource(product_id=self.keeper, supplier_product_id=source.id, pack_size=1)
            db.add(relation); await db.flush()
            self.relation_id = relation.id
            xtrek = await db.scalar(select(Shop).where(Shop.code == 'xtrek'))
            xtrek.platform, xtrek.is_active = 'upgates', True
            self.xtrek_id = xtrek.id
            for index, mapping_id in enumerate(self.mapping_ids):
                mapping = await db.get(ShopProduct, mapping_id)
                mapping.shop_id = self.shop_id if index == 0 else self.xtrek_id
                mapping.is_variant = True
                mapping.parent_code = 'SYN-PARENT-BT' if index == 0 else 'SYN-PARENT-XT'
                mapping.external_product_id = str(8001 + index)
                mapping.external_variant_id = str(9001 + index)
        for index, leaf in enumerate(self.remote):
            leaf.update(is_variant=True, parent_id=str(8001 + index),
                parent_code='SYN-PARENT-BT' if index == 0 else 'SYN-PARENT-XT',
                supplier_code=self.article if index == 0 else None,
                attributes=[{'name': 'Farba', 'value': 'Blue'}, {'name': 'Veľkosť', 'value': 'M'}])
        self.remote[0]['eans'], self.remote[0]['raw_barcodes'] = [], []

    def request(self, enabled=True):
        raw = {'request_id': str(uuid4()), 'keeper_product_id': self.keeper,
            'retired_product_ids': [self.retired], 'mode': 'identity_only_preserve_unconfirmed_history',
            'same_physical_retail_unit_confirmed': True, 'legacy_initial_history_preserved': True,
            proof.FLAG: enabled, 'mapping_actions': [], 'additional_sales': [],
            'identity_evidence': {'review_note': 'Synthetic exact manufacturer article and same complete physical variant.',
                'physical_unit': 'One complete synthetic retail unit', 'source_urls': ['https://manufacturer.invalid/synthetic']}}
        for index, (mapping_id, leaf) in enumerate(zip(self.mapping_ids, self.remote)):
            raw['mapping_actions'].append({'mapping_id': mapping_id, 'action': 'retain' if index == 0 else 'bind',
                'target': {'shop_code': 'biketrek' if index == 0 else 'xtrek', 'code': leaf['code'], 'is_variant': True,
                    'parent_code': leaf['parent_code'], 'native_product_id': int(leaf['parent_id']),
                    'native_variant_id': int(leaf['external_id']), 'sale_to_stock_factor': 1}})
        return ProductConsolidationPreviewRequest.model_validate(raw)

    async def preserved(self):
        result = await self.stock()
        async with self.transaction() as db:
            for table in ('product_identifiers', 'product_supply_sources', 'supplier_products', 'product_editor_overrides'):
                result[table] = await db.scalar(text(f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb) FROM {table} t"))
        return result

    async def change_source(self, **values):
        async with self.transaction() as db:
            source = await db.get(SupplierProduct, self.source_id)
            for name, value in values.items():
                setattr(source, name, value)

    async def test_sensor_shaped_apply_replay_preserves_raw_identifiers_and_history(self):
        before = await self.preserved()
        request = self.request()
        ready = await self.preview(request)
        self.assertTrue(ready['eligible'], ready['dependencies']['blockers'])
        evidence = ready[proof.PROOF_KEY]
        self.assertEqual(evidence['source']['supplier_product_id'], self.source_id)
        self.assertEqual(evidence['source']['source_attributes'], [{'name': 'SIZE', 'value': 'M'}])
        self.assertEqual(evidence['native_leaves'][0]['eans'], [])
        self.assertEqual(evidence['registered_identifiers'], [{'identifier_id': self.barcode_id,
            'product_id': self.retired, 'identifier_type': 'ean', 'value': fixture.EAN}])
        self.assertEqual(await self.preserved(), before)
        done = await self.apply(ready)
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done[proof.PROOF_KEY], evidence)
        self.assertEqual(await self.preserved(), before)
        async with self.transaction() as db:
            identifier = await db.get(ProductIdentifier, self.barcode_id)
            self.assertEqual(identifier.product_id, self.retired)
            scanner = ProductIdentifierService(db)
            self.assertEqual((await scanner.find_product_by_barcode(fixture.EAN)).id, self.keeper)
            self.assertEqual(await scanner.get_all_barcodes(self.keeper), [fixture.EAN])
            self.assertEqual((await db.get(Product, self.keeper)).sku, 'SYN-KEEP')
        reads = links.source.discover.await_count, links.source.verify.await_count
        self.assertEqual(await self.preview(request), done)
        self.assertEqual(await self.apply(ready), done)
        self.assertEqual((links.source.discover.await_count, links.source.verify.await_count), reads)

    async def test_false_omitted_and_direct_gtin_path_keep_original_contract(self):
        disabled = self.request(False)
        normalized = service._request(disabled)
        self.assertNotIn(proof.FLAG, normalized)
        self.assertEqual(service._request(ProductConsolidationPreviewRequest.model_validate(normalized)), normalized)
        with self.assertRaisesRegex(links.LinkError, 'consolidation_keeper_verified_gtin_required'):
            await self.preview(disabled)
        async with self.transaction() as db:
            db.add(ProductIdentifier(product_id=self.keeper, identifier_type=IdentifierType.upc, value=fixture.UPC))
        ready = await self.preview(self.request(False))
        self.assertTrue(ready['eligible'])
        self.assertNotIn(proof.PROOF_KEY, ready)
        with self.assertRaisesRegex(links.LinkError, 'consolidation_supplier_gtin_keeper_not_empty'):
            await self.preview(self.request())

    async def test_unlinked_missing_alias_multiple_sources_and_pack_are_rejected(self):
        async with self.transaction() as db:
            await db.execute(delete(ProductSupplySource).where(ProductSupplySource.id == self.relation_id))
        with self.assertRaisesRegex(links.LinkError, 'consolidation_supplier_source_ambiguous'):
            await self.preview()
        async with self.transaction() as db:
            db.add(ProductSupplySource(id=self.relation_id, product_id=self.keeper, supplier_product_id=self.source_id, pack_size=2))
        with self.assertRaisesRegex(links.LinkError, 'consolidation_supplier_source_inactive_or_pack'):
            await self.preview()
        async with self.transaction() as db:
            (await db.get(ProductSupplySource, self.relation_id)).pack_size = 1
            await db.execute(delete(ProductIdentifier).where(ProductIdentifier.product_id == self.keeper))
        with self.assertRaisesRegex(links.LinkError, 'consolidation_supplier_alias_unverified'):
            await self.preview()

    async def test_multiple_linked_sources_are_not_chosen_arbitrarily(self):
        async with self.transaction() as db:
            other = SupplierProduct(supplier_id=self.supplier_id, supplier_sku='SYN-OTHER', name='Other offer')
            db.add(other); await db.flush()
            extra = ProductSupplySource(product_id=self.keeper, supplier_product_id=other.id)
            db.add(extra); await db.flush()
            extra_id = extra.id
        for active in (True, False):
            async with self.transaction() as db:
                (await db.get(ProductSupplySource, extra_id)).is_active = active
            with self.subTest(active=active), self.assertRaisesRegex(links.LinkError, 'consolidation_supplier_source_ambiguous'):
                await self.preview()

    async def test_inactive_discontinued_or_wrong_article_source_is_not_evidence(self):
        for fields, error in [({'is_active': False}, 'inactive_or_pack'),
                ({'is_active': True, 'is_discontinued': True}, 'inactive_or_pack'),
                ({'is_discontinued': False, 'manufacturer_sku': 'OTHER-ARTICLE'}, 'article_changed')]:
            with self.subTest(fields=fields):
                await self.change_source(**fields)
                with self.assertRaisesRegex(links.LinkError, error):
                    await self.preview()

    async def test_false_multiple_and_stale_supplier_gtin_evidence_is_rejected(self):
        for ean in (fixture.OTHER_EAN, fixture.EAN + '/' + fixture.OTHER_EAN, fixture.EAN + '/invalid'):
            with self.subTest(ean=ean):
                await self.change_source(ean=ean)
                with self.assertRaisesRegex(links.LinkError, 'consolidation_supplier_gtin_unverified'):
                    await self.preview()
        await self.change_source(ean=fixture.EAN, last_seen_at=self.observed + timedelta(seconds=1))
        with self.assertRaisesRegex(links.LinkError, 'consolidation_supplier_snapshot_stale'):
            await self.preview()

    async def test_foreign_even_inactive_gtin_owner_is_not_absorbed(self):
        async with self.transaction() as db:
            foreign = Product(sku='SYN-FOREIGN', name='Foreign owner', is_active=False)
            db.add(foreign); await db.flush()
            db.add(ProductIdentifier(product_id=foreign.id, identifier_type=IdentifierType.upc, value=fixture.UPC))
        with self.assertRaisesRegex(links.LinkError, 'consolidation_foreign_identity_owner'):
            await self.preview()

    async def test_native_article_gtin_missing_and_true_axis_conflicts_are_rejected(self):
        initial = deepcopy(self.remote)
        cases = [(0, 'supplier_code', None, 'corroboration_missing'),
                 (0, 'supplier_code', 'OTHER-ARTICLE', 'article_conflict'),
                 (1, 'eans', [], 'corroboration_missing'),
                 (1, 'eans', [fixture.OTHER_EAN], 'gtin_conflict'),
                 (1, 'attributes', [{'name': 'SIZE', 'value': 'L'}], 'axes_conflict'),
                 (0, 'attributes', [], 'axes_conflict')]
        for index, key, value, error in cases:
            with self.subTest(case=(index, key, value)):
                self.remote = deepcopy(initial); self.remote[index][key] = value
                with self.assertRaisesRegex(links.LinkError, error):
                    await self.preview()

    async def test_only_xxxl_3xl_equivalence_is_scoped_and_raw_values_remain(self):
        async with self.transaction() as db:
            source = await db.get(SupplierProduct, self.source_id)
            attrs = deepcopy(source.attributes); attrs['catalog']['variant_attributes'] = [{'name': 'SIZE', 'value': 'XXXL'}]
            source.attributes = attrs
        for leaf in self.remote:
            leaf['attributes'][1]['value'] = '3XL'
        before = await self.preserved()
        ready = await self.preview()
        evidence = ready[proof.PROOF_KEY]
        self.assertEqual(evidence['axis_rule'], 'exact_except_size_xxxl_3xl')
        self.assertEqual(evidence['source']['source_attributes'][0]['value'], 'XXXL')
        self.assertEqual(evidence['local_axes'][0]['attributes'][0]['value'], 'XXXL')
        self.assertEqual(evidence['native_leaves'][0]['attributes'][1]['value'], '3XL')
        self.assertEqual(evidence['physical_axes']['size'], '3xl')
        self.assertEqual(links.axes([{'name': 'SIZE', 'value': 'XXXL'}]), {'size': 'xxxl'})
        await self.apply(ready)
        self.assertEqual(await self.preserved(), before)

    async def test_2xl_4xl_and_color_changes_do_not_use_size_equivalence(self):
        for source_axes in ([{'name': 'SIZE', 'value': '2XL'}], [{'name': 'SIZE', 'value': '4XL'}],
                            [{'name': 'COLOR', 'value': 'Red'}]):
            with self.subTest(source_axes=source_axes):
                async with self.transaction() as db:
                    source = await db.get(SupplierProduct, self.source_id)
                    attrs = deepcopy(source.attributes); attrs['catalog']['variant_attributes'] = source_axes
                    source.attributes = attrs
                for leaf in self.remote:
                    leaf['attributes'][1]['value'] = '3XL'
                with self.assertRaisesRegex(links.LinkError, 'consolidation_supplier_physical_axes_conflict'):
                    await self.preview()

    async def test_direct_gtin_without_optin_does_not_gain_size_equivalence(self):
        async with self.transaction() as db:
            db.add(ProductIdentifier(product_id=self.keeper, identifier_type=IdentifierType.upc, value=fixture.UPC))
            source = await db.get(SupplierProduct, self.source_id)
            attrs = deepcopy(source.attributes); attrs['catalog']['variant_attributes'] = [{'name': 'SIZE', 'value': 'XXXL'}]
            source.attributes = attrs
        for leaf in self.remote:
            leaf['attributes'][1]['value'] = '3XL'
        with self.assertRaisesRegex(links.LinkError, 'consolidation_physical_axes_conflict'):
            await self.preview(self.request(False))

    async def test_source_or_native_drift_after_preview_expires_without_partial_apply(self):
        ready = await self.preview()
        before = await self.preserved()
        self.remote[0]['supplier_code'] = 'OTHER-ARTICLE'
        with self.assertRaisesRegex(links.LinkError, 'consolidation_native_identity_changed') as failure:
            await self.apply(ready)
        self.assertTrue(failure.exception.known_unapplied)
        self.assertEqual(await self.preserved(), before)
        self.remote[0]['supplier_code'] = self.article
        ready = await self.preview()
        await self.change_source(name='Changed source after preview')
        reads = links.source.discover.await_count, links.source.verify.await_count
        with self.assertRaisesRegex(links.LinkError, 'consolidation_context_changed') as failure:
            await self.apply(ready)
        self.assertTrue(failure.exception.known_unapplied)
        self.assertEqual((links.source.discover.await_count, links.source.verify.await_count), reads)
        async with self.transaction() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductCanonicalAlias)), 0)
            self.assertEqual((await db.get(ProductIdentifier, self.barcode_id)).product_id, self.retired)

    async def test_source_change_during_native_preview_is_not_frozen_as_fresh(self):
        actual = self.discover
        changed = False
        async def racing(*args, **kwargs):
            nonlocal changed
            result = await actual(*args, **kwargs)
            if not changed:
                changed = True
                await self.change_source(name='Changed during remote observation')
            return result
        with patch.object(links.source, 'discover', side_effect=racing):
            with self.assertRaisesRegex(links.LinkError, 'consolidation_context_changed'):
                await self.preview()
        async with self.transaction() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(ProductConsolidation)), 0)

    async def test_alias_and_relation_drift_during_native_and_before_apply_are_rejected(self):
        for field, error in (('alias', 'supplier_alias_unverified'), ('relation', 'source_inactive_or_pack')):
            async def change():
                async with self.transaction() as db:
                    if field == 'alias':
                        alias = await db.scalar(select(ProductIdentifier).where(ProductIdentifier.product_id == self.keeper))
                        alias.value = self.article + '-CHANGED'
                    else:
                        (await db.get(ProductSupplySource, self.relation_id)).pack_size = 2
            async def restore():
                async with self.transaction() as db:
                    alias = await db.scalar(select(ProductIdentifier).where(ProductIdentifier.product_id == self.keeper))
                    alias.value = self.article
                    (await db.get(ProductSupplySource, self.relation_id)).pack_size = 1
            first = True
            async def racing(*args, **kwargs):
                nonlocal first
                result = await self.discover(*args, **kwargs)
                if first:
                    first = False
                    await change()
                return result
            with self.subTest(field=field, phase='during_native'), patch.object(links.source, 'discover', side_effect=racing):
                with self.assertRaisesRegex(links.LinkError, error):
                    await self.preview()
            await restore()
            ready = await self.preview()
            await change()
            reads = links.source.discover.await_count, links.source.verify.await_count
            with self.subTest(field=field, phase='before_apply'), self.assertRaisesRegex(links.LinkError, error) as failure:
                await self.apply(ready)
            self.assertTrue(failure.exception.known_unapplied)
            self.assertEqual((links.source.discover.await_count, links.source.verify.await_count), reads)
            await restore()

    async def test_existing_source_writer_gets_known_unapplied_busy_without_wait(self):
        ready = await self.preview()
        async with self.sessions() as writer:
            await writer.execute(text('UPDATE supplier_products SET name=name WHERE id=:id'), {'id': self.source_id})
            with self.assertRaisesRegex(links.LinkError, 'consolidation_supplier_identity_writer_busy') as failure:
                await asyncio.wait_for(self.apply(ready), timeout=5)
            self.assertTrue(failure.exception.known_unapplied)
            await writer.rollback()

    async def test_source_alias_and_relation_updates_after_rr_snapshot_reject_stale_rows(self):
        actual = service._local
        for field in ('source', 'alias', 'relation'):
            async def racing(db, raw, *, lock=False):
                if lock:
                    async with self.transaction() as writer:
                        if field == 'source':
                            (await writer.get(SupplierProduct, self.source_id)).name = 'Committed after preview snapshot began'
                        elif field == 'alias':
                            alias = await writer.scalar(select(ProductIdentifier).where(ProductIdentifier.product_id == self.keeper))
                            alias.value = self.article + '-CHANGED'
                        else:
                            (await writer.get(ProductSupplySource, self.relation_id)).priority = 99
                return await actual(db, raw, lock=lock)
            with self.subTest(field=field), patch.object(service, '_local', side_effect=racing):
                with self.assertRaisesRegex(links.LinkError, 'consolidation_dependency_snapshot_changed') as failure:
                    await self.preview()
            self.assertTrue(failure.exception.known_unapplied)
            async with self.transaction() as db:
                alias = await db.scalar(select(ProductIdentifier).where(ProductIdentifier.product_id == self.keeper))
                alias.value = self.article
                self.assertEqual(await db.scalar(select(func.count()).select_from(ProductConsolidation)), 0)

    async def test_source_writer_waits_at_final_proof_fence_until_alias_commit(self):
        ready = await self.preview()
        fenced, finish, writing = asyncio.Event(), asyncio.Event(), asyncio.Event()
        writer_pid = []
        actual = service._identity
        async def pause(*args, **kwargs):
            result = await actual(*args, **kwargs)
            fenced.set(); await finish.wait()
            return result
        async def writer():
            async with self.transaction() as db:
                writer_pid.append(await db.scalar(text('SELECT pg_backend_pid()')))
                writing.set()
                await db.execute(text('UPDATE supplier_products SET name=name WHERE id=:id'), {'id': self.source_id})
        with patch.object(service, '_identity', side_effect=pause):
            applying = asyncio.create_task(self.apply(ready))
            await asyncio.wait_for(fenced.wait(), timeout=5)
            waiting = asyncio.create_task(writer())
            await asyncio.wait_for(writing.wait(), timeout=5)
            try:
                blocked = False
                for _ in range(100):
                    async with self.sessions() as db:
                        blocked = await db.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=:pid AND NOT granted AND mode='RowExclusiveLock')"), {'pid': writer_pid[0]})
                    if blocked:
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(blocked, 'Competing source UPDATE must be fenced in PostgreSQL')
                self.assertFalse(waiting.done())
            finally:
                finish.set()
                result = await asyncio.wait_for(applying, timeout=10)
                await asyncio.wait_for(waiting, timeout=10)
        self.assertEqual(result['status'], 'completed')

    async def test_typed_frozen_proof_accepts_complete_lineage_and_rejects_extra_or_forged_fields(self):
        ready = await self.preview()
        async with self.transaction() as db:
            row = await db.get(ProductConsolidation, ready['id'])
            document = deepcopy(row.document)
        local = document['local']
        own = scope.Scope.build({self.keeper, self.retired}, local['products'], local['identifiers'],
                                local['mappings'], service._targets(document['request']))
        accepted = scope.Proof(own)
        scope.consolidation(accepted, {'document': document})
        self.assertEqual(accepted.result()['classification'], 'reference')
        for change in ('extra', 'extra_native', 'source_id', 'native_owner', 'no_flag', 'source_without_local', 'bool_id'):
            value = deepcopy(document)
            if change == 'extra': value['identity'][proof.PROOF_KEY]['source']['hidden_product_id'] = 987654
            if change == 'extra_native': value['identity'][proof.PROOF_KEY]['native_leaves'][0]['unreviewed_targets'] = [987654]
            if change == 'source_id': value['identity'][proof.PROOF_KEY]['source']['supplier_product_id'] += 1
            if change == 'native_owner': value['identity'][proof.PROOF_KEY]['native_leaves'][0]['original_product_id'] = self.retired
            if change == 'no_flag': value['request'].pop(proof.FLAG)
            if change == 'source_without_local': value['local'].pop(proof.LOCAL_KEY)
            if change == 'bool_id': value['identity'][proof.PROOF_KEY]['source']['supplier_product_id'] = True
            with self.subTest(change=change), self.assertRaises(scope.Unverified):
                scope.consolidation(scope.Proof(own), {'document': value})

    async def test_saved_optin_preview_does_not_poison_next_disjoint_group_scan(self):
        first = await self.preview()
        self.assertTrue(first['eligible'])
        async with self.transaction() as db:
            cards = [Product(sku=code, name='Unrelated complete synthetic retail unit')
                     for code in ('SYN-NEXT-KEEP', 'SYN-NEXT-OLD')]
            db.add_all(cards); await db.flush()
            self.keeper, self.retired = (p.id for p in cards)
            db.add_all([ProductIdentifier(product_id=self.keeper, identifier_type=IdentifierType.ean,
                                         value=fixture.OTHER_EAN),
                        ProductIdentifier(product_id=self.retired, identifier_type=IdentifierType.ean,
                                          value='0' + fixture.OTHER_EAN)])
            mappings = [ShopProduct(shop_id=self.shop_id, product_id=p.id, external_code=p.sku, is_variant=False)
                        for p in cards]
            db.add_all(mappings); await db.flush()
            self.mapping_ids = [m.id for m in mappings]
        self.remote = [self.leaf('SYN-NEXT-KEEP', 29001), self.leaf('SYN-NEXT-OLD', 29002)]
        for leaf in self.remote:
            leaf['eans'], leaf['raw_barcodes'] = [fixture.OTHER_EAN], [fixture.OTHER_EAN]
        next_request = fixture.ProductConsolidationDatabaseTests.request(self)
        later = await self.preview(next_request)
        self.assertTrue(later['eligible'], later['dependencies']['blockers'])
        self.assertNotIn(proof.PROOF_KEY, later)
