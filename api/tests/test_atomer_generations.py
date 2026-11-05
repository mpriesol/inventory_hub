import unittest
from copy import deepcopy
from types import SimpleNamespace

from inventory_hub.services import atomer_generations as gen
from inventory_hub.services.atomer_review_targets import ReviewError


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.family=gen.Family(parent="HELMET",product_id=52,family_revision=0,evidence="https://example.com/helmet",
            existing=[dict(code="OLD-M",variant_id=29,ean="001",axes={"Size":"M"},generation="Earlier")],
            rows=[dict(code="NEW-M",ean="002",revision=1,names={"PARAM_1":"Size"},generation="Recent")])
        self.leaf=dict(product_id=52,variant_id=29,code="OLD-M",ean="001",stock=5,
                       parameters=[dict(name={"sk":"Size"},values=[{"sk":"M"}])])
        self.obs={"parent":{"code":"HELMET","variants":[deepcopy(self.leaf)]},"variants":[self.leaf]}

    def test_label_only_and_incomplete_family(self):
        axes,patches=gen.plan_existing(self.family,self.obs,"label")
        self.assertEqual(set(patches[0]),{"code","parameters"})
        self.assertEqual(axes["OLD-M"],{"Size":"M","Generácia":"Earlier"})
        with self.assertRaises(ReviewError):
            gen.plan_existing(self.family,self.obs,"extend")
        self.obs["variants"].append(dict(self.leaf,variant_id=30))
        with self.assertRaises(ReviewError):
            gen.plan_existing(self.family,self.obs,"label")

    def test_verify_protects_original_inventory(self):
        protected,_=gen.repair.projection(self.obs)
        doc=dict(before_protected=protected,family=self.family.model_dump(mode="json"),
                 expected_axes={"OLD-M":{"Size":"M","Generácia":"Earlier"}},additions={})
        self.leaf["parameters"].append(dict(name={"sk":"Generácia"},values=[{"sk":"Earlier"}]))
        self.assertEqual(gen.observe(doc,self.obs),({},[]))
        self.leaf["stock"]=4
        with self.assertRaises(ReviewError):
            gen.observe(doc,self.obs)

    def test_source_fields_and_verification(self):
        raw=('<PRODUCT><VISIBILITY>1</VISIBILITY><VAT>23</VAT><PRICE_VAT>20</PRICE_VAT>'
             '<PRICE_OLD>24</PRICE_OLD><WEIGHT>0.3</WEIGHT><AVAILABILITY>Available</AVAILABILITY>'
             '<VARIANT_PARAMETERS><PARAM_1>M</PARAM_1></VARIANT_PARAMETERS></PRODUCT>')
        evidence=dict(source_state=dict(product_origin="admin_csv",variant_origin="admin_csv",product=True,variant=True,
                                        product_flags={"Novinka":"0"},variant_flags={"Novinka":"0"}))
        item=self.family.rows[0]
        row=dict(ean=item.ean,parent_code="HELMET",manufacturer="Brand",raw_xml=raw)
        patch,expected=gen.new_leaf(row,item,evidence,dict(code="HELMET",manufacturer="Brand",active_yn=True),
            dict(prices=[dict(currency="EUR",language="sk",vat=23)]),{"Size":"M","Generácia":"Earlier"},{"pricelist":"Default"})
        self.assertNotIn("stock",patch)
        self.assertEqual(patch["weight"],300)
        self.assertNotIn("descriptions",patch)
        leaf=dict(ean=item.ean,active_yn=True,stock=0,availability="Available",weight=300,
            prices=[dict(currency="EUR",language="sk",price_common=24,pricelists=[dict(name="Default",price_with_vat=20)])],
            parameters=[dict(name={"sk":a},values=[{"sk":b}]) for a,b in expected["axes"].items()])
        self.assertEqual(gen.inspect_new(leaf,expected),"")
        self.assertTrue(gen.inspect_new(dict(leaf,stock=1),expected))
        evidence["source_state"]["variant_flags"]["Novinka"]="1"
        with self.assertRaises(ReviewError):
            gen.new_leaf(row,item,evidence,dict(code="HELMET",manufacturer="Brand",active_yn=True),{}, {}, {})

    def test_generation_digest_and_untrusted_image(self):
        selection=SimpleNamespace(phase="label",run_id=1,source_sha="a"*64)
        self.assertEqual(len(gen.doc_id(selection,self.family)),64)
        node=gen.etree.fromstring(b"<PRODUCT><MAIN_IMAGE>https://example.com/a.png</MAIN_IMAGE></PRODUCT>")
        with self.assertRaises(ReviewError):
            gen.image_paths(node)

    def test_hub_backfill_preserves_preexisting_variant_axes(self):
        from inventory_hub.services.atomer_generation_cards import missing_axes
        desired={"Size":"M","Generácia":"Recent"}
        self.assertEqual(missing_axes(desired,[("Size","M")]),[("Generácia","Recent")])
        self.assertEqual(missing_axes(desired,list(desired.items())),[])
        with self.assertRaises(ReviewError):
            missing_axes(desired,[("Size","XL")])

    def test_restore_only_observed_parent_price_side_effect(self):
        from inventory_hub.services.atomer_generation_recovery import check_prices
        rows=[dict(language="sk",currency="EUR",vat=23,price_common=0,
            pricelists=[dict(name="Default",price_original=10,price_with_vat=10,price_without_vat="8.13")])]
        changed=deepcopy(rows)
        changed[0]["price_common"]=30
        changed[0]["pricelists"][0].update(price_original=20,price_with_vat=20,price_without_vat="16.26")
        result=check_prices(rows,changed,{"NEW":{"gross":"20","common":"30"}})
        self.assertEqual(result[0]["pricelists"][0]["price_original"],10)
        changed[0]["vat"]=5
        with self.assertRaises(ReviewError):
            check_prices(rows,changed,{"NEW":{"gross":"20","common":"30"}})

    def test_matching_jpeg_renames_keeps_the_complete_source_stem(self):
        self.assertTrue(gen.same_image("https://cdn.example/abcd-12345.jpeg","https://store.example/12345.jpg"))
        self.assertFalse(gen.same_image("https://cdn.example/abcd-012345.jpeg","https://store.example/12345.jpg"))
        self.assertFalse(gen.same_image("https://cdn.example/abcd-12345.png","https://store.example/12345.jpg"))
