import unittest
from copy import deepcopy

from inventory_hub.services.manufacturer_references import validate_references, verified_manufacturer_item


class VerifiedOfferTests(unittest.TestCase):
    def setUp(self):
        self.item = {"code": "SUP-52", "brand": "EXAMPLE", "feed_key": "products", "eans": ["4006381333931"]}
        self.ref = {"brand": "Example", "gtin": "4006381333931", "manufacturer_code": "0005-G",
                    "source_ref": "https://parts.example.com/products/0005-G"}
        self.references = {"products": {"SUP-52": self.ref}}

    def test_exact_offer_and_package_keep_manufacturers_part_number(self):
        validate_references(self.references)
        proof = verified_manufacturer_item(self.item, self.references)
        self.assertEqual((proof["source_code"], proof["brand"], proof["source_ref"]),
                         ("0005-G", self.item["brand"], self.ref["source_ref"]))
        for edit in ({"code": "OTHER"}, {"feed_key": "spring"}, {"brand": "Another"},
                     {"eans": []}, {"eans": ["4006381333931", "invalid"]},
                     {"eans": ["4006381333931", "5901234123457"]}):
            with self.subTest(edit=edit):
                self.assertIsNone(verified_manufacturer_item({**self.item, **edit}, self.references))

    def test_incomplete_or_malformed_proof_is_never_used(self):
        for change in ({"gtin": "4006381333930"}, {"source_ref": ""}, {"source_ref": "file:///tmp/data"},
                       {"source_ref": "https://user:secret@example.org"}, {"manufacturer_code": " "},
                       {"source_ref": "https://parts.example.com/sheet?access_token=private"},
                       {"manufacturer_code": "12;34"}, {"brand": ""}):
            reference = deepcopy(self.references)
            reference["products"]["SUP-52"].update(change)
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    validate_references(reference)
                self.assertIsNone(verified_manufacturer_item(self.item, reference))
