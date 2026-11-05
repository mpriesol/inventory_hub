from io import BytesIO
import json
import unittest
from inventory_hub.services.atomer_progress import parse_source
from inventory_hub.services.atomer_source_state import add_states, parse_admin
from inventory_hub.services.atomer_review_targets import check_targets, parse_targets, ReviewError


class AtomerReviewSourceTests(unittest.TestCase):
    def test_hidden_variant_does_not_deactivate_parent_and_preserves_explicit_zero_flags(self):
        xml = b"<PRODUCTS><PRODUCT><PRODUCT_ID>V</PRODUCT_ID><PARENT_ID>P</PARENT_ID><VISIBILITY>0</VISIBILITY></PRODUCT></PRODUCTS>"
        _,_,src = parse_source(BytesIO(xml))
        rows=[dict(code="V",parent="P",kind="variant")]
        add_states(src,rows,{})
        self.assertIsNone(rows[0]["source_state"]["product"])
        self.assertIs(rows[0]["source_state"]["variant"],False)
        admin=parse_admin("Katalógové číslo;Viditeľný;Akcia;Novinka\nP;1;0;1\nV;0;0;0\n".encode())
        add_states(src,rows,admin)
        self.assertIs(rows[0]["source_state"]["product"],True)
        self.assertEqual(rows[0]["source_state"]["variant_flags"],{"Akcia":"0","Novinka":"0"})
        admin["V"]["visibility"]=True
        add_states(src,rows,admin)
        self.assertEqual((rows[0]["source_state"]["variant"],rows[0]["source_state"]["variant_origin"]),(None,"conflict"))

    def test_review_requires_exact_leaf_and_detects_reintroduced_source_or_parent_change(self):
        pin = dict(product_id="2",variant_id="3",code="002",parent="FP",source_parent="F",ean="",axes={"Size":["M"]})
        pins=parse_targets(BytesIO(json.dumps([pin]).encode()))
        remote=[dict(pid="2",vid="3",code="002",parent="FP",ean="",axes={"size":["m"]},stock=0)]
        source=[dict(code="other",ean="")]
        rows=[dict(code="other",family_key="v:F",family_name="Family",product_id="2",variant_id="4",target_parent="FP",match="code_or_ean",reasons=[])]
        def check(admin=None):
            return check_targets(pins,source,remote,rows,[],{},admin)[0]
        self.assertEqual(check()["check"],"eligible")
        self.assertIn("admin_absence_unknown",check()["warnings"])
        self.assertEqual(check({"other":{},"002":{}})["check"],"in_source")
        remote[0]["code"]="002-red"
        self.assertEqual(check()["check"],"changed")
        remote.clear()
        self.assertEqual(check()["check"],"missing")
        with self.assertRaises(ReviewError):
            parse_targets(BytesIO(json.dumps([pin,pin]).encode()))
