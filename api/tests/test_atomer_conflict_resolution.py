from copy import deepcopy
import unittest

from inventory_hub.services import atomer_conflict_resolution as resolution
from inventory_hub.services import atomer_variant_repair as repair


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
