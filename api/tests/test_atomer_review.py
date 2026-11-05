from io import BytesIO
import json
import unittest
from inventory_hub.services.atomer_progress import parse_source
from inventory_hub.services.atomer_source_state import add_states, catalog_with_admin, parse_admin
from inventory_hub.services.atomer_review_targets import check_targets, parse_targets, ReviewError


class AtomerReviewSourceTests(unittest.TestCase):
    def test_only_exact_native_parent_spelling_resolves_two_roots(self):
        csv = ('Interné číslo;Kat. číslo rodiča;Kat. číslo;Viditeľný\n'
               '1;0;P;1\n2;0;\tP;0\n3;P;A;1\n4;\tP;B;0\n')
        admin = parse_admin(csv.encode('cp1250'))
        result = catalog_with_admin([],admin,True)
        self.assertTrue(admin['P']['conflict'])
        self.assertFalse(any(x['conflict'] for x in result))
        rows = [dict(code=x['code'],parent='P',kind='variant') for x in result]
        add_states(result,rows,admin)
        self.assertEqual({x['code']:x['source_state']['product'] for x in rows},{'A':True,'B':False})
        duplicate = parse_admin((csv+'5;0;P;0\n').encode('cp1250'))
        self.assertTrue(next(r for r in catalog_with_admin([],duplicate,True) if r['code']=='A')['conflict'])

    def test_native_csv_adds_hidden_variants_and_retains_parent_flags(self):
        csv = ('Interné číslo;Kat. číslo rodiča;Kat. číslo;Viditeľný;EAN kód;Názov tovaru;Odporúčaný;Novinka;Char. Typ;Char. Typ\n'
               '11;0;P;1;;Kabát;1;0;A;B\n'
               '12;P;001;0;;"Kabát; ""modrý""\nS";0;1;C;D\n'
               '13;P;002;1;;Kabát biely;0;0;;\n'
               '14;0;S;0;;Iný;0;0;;\n')
        admin = parse_admin(csv.encode('cp1250'))
        _, _, buyers = parse_source(BytesIO(b'<PRODUCTS><PRODUCT><PRODUCT_ID>002</PRODUCT_ID><PARENT_ID>P</PARENT_ID></PRODUCT></PRODUCTS>'))
        catalog = catalog_with_admin(buyers, admin)
        self.assertEqual([r['code'] for r in catalog], ['002', '001'])
        self.assertEqual(catalog[1]['name'], 'Kabát; "modrý"\nS')
        rows = [dict(code='001', parent='P', kind='variant')]
        add_states(catalog, rows, admin)
        state = rows[0]['source_state']
        self.assertEqual((state['product'], state['variant'], state['variant_origin']), (True, False, 'admin_csv'))
        self.assertEqual((state['product_flags']['Odporúčaný'], state['variant_flags']['Novinka']), ('1', '1'))
        self.assertEqual([r['code'] for r in catalog_with_admin(buyers, admin, complete=True)], ['002', '001', 'S'])
        with self.assertRaisesRegex(ReviewError, 'incomplete_admin_export'):
            catalog_with_admin(buyers, {k:v for k,v in admin.items() if k != 'P'}, complete=True)
        admin['002']['parent'] = 'S'
        self.assertTrue(catalog_with_admin(buyers, admin)[0]['conflict'])
        self.assertFalse(buyers[0]['conflict'])

    def test_duplicate_mapped_columns_or_truncated_native_rows_are_rejected(self):
        for raw in ('Kat. číslo;Viditeľný;Viditelny\nS;1;0\n',
                    'Kat. číslo;Viditeľný;Char. Typ;Char. Typ\nS;1;A\n',
                    'Kat. číslo;Viditeľný\nS;"0'):
            with self.subTest(raw=raw), self.assertRaises(ReviewError):
                parse_admin(raw.encode())

    def test_native_parent_alias_preserves_existing_family_and_hidden_record(self):
        admin = parse_admin(('Interné číslo;Kat. číslo rodiča;Kat. číslo;Viditeľný\n'
                             '17;0;P;1\n18;P;V;1\n19;17;H;0\n').encode())
        _, _, buyers = parse_source(BytesIO(b'<PRODUCTS><PRODUCT><PRODUCT_ID>V</PRODUCT_ID>'
                                            b'<PARENT_ID>17</PARENT_ID></PRODUCT></PRODUCTS>'))
        catalog = catalog_with_admin(buyers, admin, complete=True)
        self.assertEqual([(r['code'], r['parent'], r['conflict']) for r in catalog],
                         [('V', '17', False), ('H', '17', False)])
        rows = [dict(code='H', parent='17', kind='variant')]
        add_states(catalog, rows, admin)
        self.assertEqual((rows[0]['source_state']['product'], rows[0]['source_state']['variant']), (True, False))

    def test_internal_only_root_requires_a_referencing_child_for_complete_scope(self):
        raw = 'Interné číslo;Kat. číslo rodiča;Kat. číslo;Viditeľný\n17;0;;1\n'
        with self.assertRaisesRegex(ReviewError, 'incomplete_admin_export'):
            catalog_with_admin([], parse_admin(raw.encode()), complete=True)
        catalog = catalog_with_admin([], parse_admin((raw+'18;17;V;0\n').encode()), complete=True)
        self.assertEqual([(r['code'], r['parent']) for r in catalog], [('V', '17')])

    def test_conflicting_parent_and_damaged_identity_are_localized(self):
        raw = ('Interné číslo;Kat. číslo rodiča;Kat. číslo;Viditeľný;EAN;Akcia\n'
               '11;0;P;1;4006381333931;1\n12;0;P;0;5901234123457;0\n13;P;V;0;;0\n'
               '14;0;S;1;;0\n15;P;BROKEN\x00SKU;0;12345670;0\n')
        admin = parse_admin(raw.encode())
        catalog = catalog_with_admin([], admin, complete=True)
        self.assertEqual([(r['code'], r['conflict']) for r in catalog], [('V', True), ('S', False)])
        self.assertEqual(admin.quarantined, ['15'])
        rows = [dict(code='V', parent='P', kind='variant')]
        add_states(catalog, rows, admin)
        state = rows[0]['source_state']
        self.assertEqual((state['product'], state['product_origin'], state['product_flags']), (None, 'conflict', {}))
        self.assertIs(state['variant'], False)
        pin = dict(product_id='1', variant_id='2', code='other', parent='FP', source_parent='P',
                   ean='5901234123457', axes={'size':['m']})
        pins = parse_targets(BytesIO(json.dumps([pin]).encode()))
        target = dict(pid='1', vid='2', code='other', parent='FP', ean=pin['ean'], axes={'size':['m']}, stock=0)
        witness = dict(code='W', family_key='v:P', family_name='Family', product_id='1', variant_id='3',
                       target_parent='FP', match='code_or_ean', reasons=[])
        check = check_targets(pins, [], [target], [witness], [], {}, admin, admin_complete=True)[0]
        self.assertEqual(check['check'], 'in_source')
        self.assertIn('admin_absence_unknown', check['warnings'])

    def test_ean_visibility_requires_a_unique_variant_in_the_same_family(self):
        ean = '4006381333931'
        pin = dict(product_id='1', variant_id='2', code='shop-sku', parent='FP', source_parent='P', ean=ean, axes={'size':['m']})
        pins = parse_targets(BytesIO(json.dumps([pin]).encode()))
        target = dict(pid='1', vid='2', code='shop-sku', parent='FP', ean=ean, axes={'size':['m']}, stock=0)
        leaf = dict(code='csv-sku', family_key='v:P', family_name='Family', product_id='1', variant_id='2',
                    target_parent='FP', match='code_or_ean', reasons=[], source_state={'variant':False,'variant_origin':'admin_csv'})
        source = [dict(code='csv-sku', ean=ean)]
        def visibility():
            return check_targets(pins, source, [target], [leaf], [], {})[0]['source_state']['variant']
        self.assertIs(visibility(), False)
        leaf['family_key'] = 'v:other'
        self.assertIsNone(visibility())
        leaf['family_key'] = 'v:P'
        source.append(dict(code='another', ean=ean))
        self.assertIsNone(visibility())

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
        self.assertIn("admin_absence_unknown",check({"other":{}})["warnings"])
        self.assertEqual(check({"other":{},"002":{}})["check"],"in_source")
        remote[0]["code"]="002-red"
        self.assertEqual(check()["check"],"changed")
        remote.clear()
        self.assertEqual(check()["check"],"missing")
        with self.assertRaises(ReviewError):
            parse_targets(BytesIO(json.dumps([pin,pin]).encode()))
