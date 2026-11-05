"""The tracker records uncertainty and does not authorize catalog changes."""
from collections import defaultdict
from io import BytesIO
from types import SimpleNamespace as Row
import unittest

from inventory_hub.adapters.atomer_content import AtomerSourceError
from inventory_hub.services import atomer_progress as p
from inventory_hub.services.product_identity import IdentityIndex


def source(rows):
    return p.parse_source(BytesIO(("<PRODUCTS>" + "".join(
        f"<PRODUCT><PRODUCT_ID>{code}</PRODUCT_ID><PARENT_ID>{parent}</PARENT_ID>"
        f"<PRODUCT_NAME>{name}</PRODUCT_NAME><EAN>{ean}</EAN></PRODUCT>"
        for code,parent,name,ean in rows) + "</PRODUCTS>").encode()))


class ProgressTests(unittest.TestCase):
    def test_duplicate_and_conflicting_source_rows_and_dtd(self):
        rows = [("001","G","One",""),("001","G","One",""),("S","S","First",""),("S","S","Other","")]
        _, total, result = source(rows)
        self.assertEqual((total,len(result)), (4,2))
        self.assertEqual([r["conflict"] for r in result], [False,True])
        with self.assertRaises(AtomerSourceError):
            p.parse_source(BytesIO(b'<!DOCTYPE x><PRODUCTS/>'))

    def test_card_links_in_independent_shops_and_wrong_native_identity(self):
        _, _, sources = source([("001","G","Name blue",""),("002","G","Name red",""),("S","S","Standalone","")])
        parents = [{"product_id":19,"code":"G","admin_url":"https://admin.example/p/19",
                    "descriptions":[{"language":"sk","url":"https://shop.example/p/name","title":"Name"}],
                    "variants":[{"variant_id":20,"code":"001"}]}]
        targets = p.flatten_catalog(parents,[{"variant_id":20,"product_id":19,"code":"001"}])
        card = Row(id=72,sku="001",brand="")
        mapping = Row(id=40,product_id=72,external_product_id="19",external_variant_id="20",is_variant=True,external_code="001",parent_code="G")
        bike = Row(id=50,product_id=72,external_product_id="77",external_variant_id=None,is_variant=False,external_code="old",parent_code=None)
        bike_targets = p.bike_catalog([{"product_id":77,"code":"old","url":"https://bike.example/old","admin_url":"javascript:unsafe"}])
        def run():
            return p.reconcile(sources,targets,IdentityIndex(2,[card]),[mapping],{}, {72:[bike]},bike_targets)
        verified, missing, single = run()
        self.assertEqual((verified["state"],verified["card_state"],verified["bike_state"]), ("linked","exists","linked"))
        self.assertEqual(verified["bike"]["shop_url"],"https://bike.example/old")
        self.assertIsNone(verified["bike"]["shop_admin_url"])
        self.assertEqual((missing["card_state"],missing["state"],single["kind"]),("missing","needs_both","standalone"))
        self.assertEqual((verified["family_name"],missing["family_key"],single["family_key"]),("Name","v:G","s:S"))
        bike_targets.clear()
        review = run()[0]
        self.assertEqual((review["state"],review["bike_state"]),("linked","review"))
        self.assertIsNone(review["bike"].get("shop_url"))
        with self.assertRaises(p.ExportError):
            p.flatten_catalog(parents,[])

    def test_disagreeing_barcode_never_counts_as_linked(self):
        _, _, rows = source([("code","","Name","4006381333931")])
        targets = p.flatten_catalog([{"product_id":1,"code":"code","ean":"5901234123457"}],[])
        out, = p.reconcile(rows,targets,IdentityIndex(2),[],{},defaultdict(list),{})
        self.assertEqual(out["state"],"review")
        self.assertIn("ean_differs",out["reasons"])
