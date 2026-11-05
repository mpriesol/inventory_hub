from copy import deepcopy
import unittest
from types import SimpleNamespace

from inventory_hub.services import atomer_conflict_resolution as resolution
from inventory_hub.services import atomer_variant_repair as repair
from inventory_hub.services.atomer_review_targets import ReviewError


class GenerationObservationTests(unittest.TestCase):
    def test_reports_only_journal_ids_changes_and_raw_stock(self):
        old = {"variant_id": 23, "code": "OLD", "ean": "111", "parameters": [], "stock": 1}
        family = {"parent": {"product_id": 11, "code": "PARENT", "prices": [10], "variants": [deepcopy(old)]},
                  "variants": [deepcopy(old)]}
        original = repair.projection(family)[0]
        doc = {"identity": {"product_id": 11, "parent_code": "PARENT"}, "before_protected": original,
               "observed_ids": {"NEW": 24}, "expected_axes": {"OLD": {}, "NEW": {}}, "additions": {"NEW": {}}}
        new = {"variant_id": 24, "code": "NEW", "ean": "222", "parameters": [], "stock": None}
        family["parent"]["prices"] = [20]
        family["parent"]["variants"].append({**new, "stock": 0})
        family["variants"].append(new)
        family["variants"][0]["stock"] = 2
        result = resolution.observe_family(doc, family)
        self.assertEqual(result["parent_changes"], {"prices": {"before": [10], "current": [20]}})
        self.assertEqual(result["original_leaf_changes"], {"23": {"variant": {"stock": {"before": 1, "current": 2}}}})
        added = result["additions"]["NEW"]
        self.assertTrue(added["identity_matches"])
        self.assertEqual(added["variant_stock"], {"raw": {"stock": None}, "quantity": None})
        self.assertEqual(added["product_stock"], {"raw": {"stock": 0}, "quantity": "0"})
        self.assertEqual(resolution.native_stock({}), {"raw": {}, "quantity": None})
        doc["observed_ids"]["NEW"] = 25
        self.assertEqual(resolution.observe_family(doc, family)["additions"]["NEW"],
                         {"observed_id": 25, "identity_matches": False})

    def test_does_not_equate_missing_and_explicit_null(self):
        self.assertEqual(resolution.field_changes({"quantity": None}, {}), {"quantity": {"before": None}})
        self.assertEqual(resolution.field_changes({}, {"quantity": None}), {"quantity": {"current": None}})

    def test_reopen_requires_own_previous_machine_block_and_exact_receipt_on_retry(self):
        common = {"note": "", "instruction": "", "decision": "undecided", "scope": "both"}
        family = dict(common, status="open", revision=0)
        blocked = dict(common, status="blocked", revision=1, result="prior review")
        row = SimpleNamespace(revision=2)
        self.assertTrue(resolution.may_reopen(blocked, family, row, "review_block", "proof"))
        reopened = dict(blocked, status="open", revision=2, result="proof")
        self.assertFalse(resolution.may_reopen(reopened, family, row, "barcode_proof_reopen", "proof"))
        for saved, group, command in [(blocked, family, "block"), (dict(blocked, note="manual"), family, "review_block"),
                                      (blocked, dict(family, scope="biketrek"), "review_block"),
                                      (reopened, family, "reopen"), (dict(reopened, revision=3), family, "barcode_proof_reopen")]:
            with self.assertRaises(ReviewError):
                resolution.may_reopen(saved, group, row, command, "proof")

    def test_correction_preserves_separate_stock_cards_and_requires_distinct_documented_gtins(self):
        p = resolution.OwnerPair(subject='ITEM-KIT', revision=1, other_revision=0, before_identifier_id=14,
            observed_card=dict(id=3, sku='ITEM', link=20, product_id='8', variant_id=None, ean='4006381333931',
                               source_before='', source_parent='ITEM'),
            correct_owner=dict(id=4, sku='ITEM-KIT', link=21, product_id='12', variant_id='91', ean='9780306406157',
                               source_before='9780306406157', source_parent='ITEM-KIT'),
            manufacturer_or_supplier='https://example.org/workbook.pdf',
            evidence='A dated supplier catalog explicitly lists both SKU and the GTIN of each separate package.')
        resolution.validate_owner_pair(p)
        for field, value in [('ean', p.correct_owner.ean), ('source_before', p.correct_owner.ean), ('id', p.correct_owner.id)]:
            altered = p.model_copy(deep=True)
            setattr(altered.observed_card, field, value)
            with self.assertRaises(ReviewError):
                resolution.validate_owner_pair(altered)

    def test_reads_only_thin_parents_and_exact_skus_for_point_of_sale_variants(self):
        from unittest.mock import MagicMock, patch
        card = resolution.PairCard(id=4, sku='ITEM', link=20, product_id='9', variant_id='15', ean='9780306406157',
                                   source_before='', source_parent='PARENT')
        solo = card.model_copy(update={'id': 5, 'sku': 'SOLO', 'product_id': '11', 'variant_id': None})
        parents = [{'product_id': 9, 'code': 'PARENT', 'variants_exists_yn': True},
                   {'product_id': 11, 'code': 'SOLO', 'variants_exists_yn': False, 'stock': 1}]
        leaf = {'variant_id': 15, 'product_id': 9, 'product_code': 'PARENT', 'code': 'ITEM', 'stock': None}
        client = MagicMock()
        def read(client_arg, path, key, filters):
            self.assertIs(client_arg, client)
            if path == 'products':
                self.assertEqual((key, filters), ('products', {'product_ids': '11;9', 'variants_yn': 'false'}))
                return parents
            self.assertEqual((path, key, filters), ('products/variants', 'variants', {'variant_codes': 'ITEM'}))
            return [leaf]
        with patch.object(resolution.transport, '_client', return_value=client), patch.object(resolution.batch,
                'collect_pages', side_effect=read):
            current = resolution.native_pair_links([solo, card], 'target')
            self.assertIsNone(current[4]['stock']['quantity'])
            self.assertEqual(current[5]['stock']['quantity'], '1')
            for key, changed in [('product_code','OTHER'), ('product_id',11), ('variant_id',17), ('code','OTHER')]:
                with self.subTest(key=key), patch.dict(leaf, {key:changed}), self.assertRaises(ReviewError):
                    resolution.native_pair_links([solo, card], 'target')
            with patch.dict(parents[1], {'variants_exists_yn': True}), self.assertRaises(ReviewError):
                resolution.native_pair_links([solo, card], 'target')

    def test_thin_read_distinguishes_response_failure_and_preserves_capacity_signal(self):
        from unittest.mock import MagicMock, patch
        card = resolution.PairCard(id=4, sku='ITEM', link=20, product_id='9', variant_id=None, ean='9780306406157',
                                   source_before='', source_parent='ITEM')
        client = MagicMock()
        for failure, code in [(resolution.transport.SourceError('stock_publication_invalid_response'), 'barcode_owner_parent_response_invalid'),
                              (resolution.transport.SourceError('stock_publication_rate_limited',429,45), 'stock_publication_rate_limited')]:
            with patch.object(resolution.transport, '_client', return_value=client), patch.object(resolution.batch,
                    'collect_pages', side_effect=failure), self.assertRaises(resolution.transport.SourceError) as caught:
                resolution.native_pair_links([card], 'target')
            self.assertEqual(caught.exception.code, code)
            if failure.status == 429:
                self.assertEqual(caught.exception.retry_after, 45)
        self.assertEqual(client.session.close.call_count, 2)

    def test_other_owner_can_keep_its_exact_untouched_batch_proposal(self):
        row = {'code':'SOLO', 'state':'needs_target', 'reasons':[], 'bike_state':'linked', 'card_state':'exists'}
        group = {'scope':'default'}
        blank = {'status':'open','revision':0,'instruction':'','scope':'default'}
        check = resolution.untouched_other_work
        self.assertTrue(check(blank, group, row, 0, None))
        queued = dict(blank, status='queued', revision=1, instruction=resolution.proposals.INSTRUCTIONS['targetX'])
        self.assertTrue(check(queued, group, row, 1, 'approve_suggestions'))
        self.assertFalse(check(queued, group, row, 0, 'approve_suggestions'))
        self.assertFalse(check(queued, group, row, 1, 'save'))
        self.assertFalse(check(dict(queued, instruction='Operator'), group, row, 1, 'approve_suggestions'))
        self.assertFalse(check(dict(queued, status='working', revision=2), group, row, 2, 'claim'))

    def test_unreported_quantity_requires_explicit_null_on_exact_warehouse(self):
        from inventory_hub.services import atomer_generations as gen
        row = {'stock': None, 'stocks': [{'stock_id': 7, 'quantity': None}]}
        self.assertTrue(gen.reports_unset_stock(row, 7))
        for other in [dict(row, stock=0), {'stocks': row['stocks']}, {'stock': None}, dict(row, stocks=[]),
                      dict(row, stocks=[{'stock_id': 7}]), dict(row, stocks=[{'stock_id': 7, 'quantity': 2}]),
                      dict(row, stocks=[*row['stocks'], {'stock_id': 8, 'quantity': None}])]:
            self.assertFalse(gen.reports_unset_stock(other, 7))
        self.assertFalse(gen.reports_unset_stock(row, 8))
