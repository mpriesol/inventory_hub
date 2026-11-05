import hashlib
import io
from types import SimpleNamespace
import unittest
import zlib
from lxml import etree

from inventory_hub.services import atomer_new_source as src
from inventory_hub.services.atomer_review_targets import ReviewError

EAN = '4006381333931'


def row(code, internal, parent='0', title=None, *, active=True, ean='', price='10', vat='23',
        image=None, flags=(), other=()):
    return tuple([
        ('Kat. číslo',code),('Interné číslo',internal),('Kat. číslo rodiča',parent),
        ('Názov tovaru',title if title is not None else code),('Viditeľný',str(int(active))),
        ('Výrobca','Brand'),('Hlavna kategória','Catalog'),('Dodacia doba','Available'),
        ('Bežná cena',price),('Stará cena','0'),('DPH',vat),('Váha','0,125'),
        ('Min. obj. množstvo','1'),('Počet kusov','18'),('EAN kód',ean),
        ('Obrázky',image or ''), *[(flag,str(int(flag in flags))) for flag in src.FLAGS], *other,
    ])


def node(code, parent, **values):
    child = etree.Element('PRODUCT')
    for tag,value in {'PRODUCT_ID':code,'PARENT_ID':parent,'VISIBILITY':'1','PRICE_VAT':'10','VAT':'23',**values}.items():
        if tag == 'AXES':
            axes = etree.SubElement(child,'VARIANT_PARAMETERS')
            for key,v in value.items():
                etree.SubElement(axes,key).text=v
        elif tag == 'PROPS':
            props = etree.SubElement(child,'PROPERTIES')
            for key,v in value.items():
                etree.SubElement(props,key).text=v
        else:
            etree.SubElement(child,tag).text=value
    return child


def document(index, root, *, options=None, names=None):
    csv, xml = index.family(root)
    active = src.source.flag(src.named(csv[root],'Viditeľný'))
    details = {c:{'ean':src.named(csv[c],'EAN kód').strip(),'source_state':{
        'product':active,'variant':src.source.flag(src.named(csv[c],'Viditeľný')),
        'product_origin':'admin_csv','variant_origin':'admin_csv'}} for c in index.leaves(root)}
    conf = dict(availability={'available':2}, categories={'Catalog':17},labels={'Akcia':7},pricelist='Default')
    return src.build(SimpleNamespace(parent=root,brand='Brand',axis_names=names or {}),details,csv,xml,
                     {**conf,**(options or {})},{'Remote'})


def with_xml(index, *selected):
    x=etree.Element('PRODUCTS')
    x.extend(selected)
    index.index_xml(etree.tostring(x))
    return index


class CompleteAtomerSourceTest(unittest.TestCase):
    def test_mixed_family_keeps_hidden_variant_and_uses_observed_title_order(self):
        index = src.Catalog([
            row('Family','100',title='Model',flags=('Akcia',)),
            row('One','101','100','Model S Blue',ean=EAN,other=[('Char. Width','40')]),
            row('Two','102','100','Model XL Blue',active=False),
        ])
        original = '<p style="color: #223344"><strong>Text</strong></p>'
        with_xml(index,node('One','Family',EAN=EAN,DESCRIPTION_LONG=original,AXES={'PARAM_2':'Blue','PARAM_1':'S'}))
        d=document(index,'Family',names={'PARAM_1':'Size','PARAM_2':'Colour'})
        parent=d['payload']
        self.assertEqual(index.parent_for('101'),'Family')
        self.assertEqual(index.category_paths,{'Catalog'})
        self.assertEqual(parent['descriptions'][0]['long_description'],original)
        self.assertTrue(parent['active_yn'])
        self.assertEqual(parent['labels'],[{'label_id':7,'active_yn':True}])
        new=parent['variants'][1]
        self.assertEqual(new['labels'],[{'label_id':7,'active_yn':False}])
        self.assertFalse(new['active_yn'])
        self.assertNotIn('ean',new)
        self.assertEqual(new['weight'],125)
        self.assertNotIn('stock',parent)
        self.assertFalse(any('stock' in child for child in parent['variants']))
        self.assertEqual([a['values'][0]['descriptions'][0]['value'] for a in new['parameters']],['XL','Blue'])

    def test_hidden_csv_family_uses_literal_source_name_and_saleable_vat(self):
        index=src.Catalog([row('Family','100',title='Model',active=False,vat='0'),
                           row('One','101','Family','Model Sesame',active=False,ean=EAN)])
        d=document(index,'Family')
        self.assertFalse(d['payload']['active_yn'])
        self.assertEqual(d['payload']['vats'],{'sk':23})
        self.assertEqual(d['variant_axis_origins'],{'One':'admin_csv_variant_name'})
        self.assertEqual(d['payload']['variants'][0]['parameters'],src.gen.parameters({'Variant':'Sesame'}))

    def test_shared_barcode_and_commercial_fields_merge_both_verified_duplicate_assets(self):
        first='https://www.xtrek.sk/user/12/upload/stuff/variants/1.jpg'
        second='https://www.xtrek.sk/user/12/upload/stuff/variants/2.jpg'
        index=src.Catalog([row('Family','100'),row('One','101','Family',ean=EAN,image=first),
                           row('One','102','Family',ean=EAN,image=second)])
        d=document(index,'Family')
        self.assertEqual(index.leaves('Family'),('One',))
        self.assertEqual(index.parent_for('102'),'Family')
        self.assertEqual([r['url'] for r in d['payload']['variants'][0]['images']],[first,second])
        collision=src.Catalog([row('Family','100'),row('One','101','Family',ean=EAN),
                               row('One','102','Family',ean=EAN,price='12')])
        with self.assertRaisesRegex(ReviewError,'new_parent_identity_ambiguous'):
            collision.family('Family')

    def test_only_literal_referenced_parent_can_own_variants(self):
        i=src.Catalog([row('Family','100'),row('\tFamily','101',active=False),row('One','103','Family')])
        self.assertEqual(src.named(i.family('Family')[0]['Family'],'Interné číslo'),'100')
        i=src.Catalog([row('Family','100'),row('\tFamily','101'),row('One','103','Family')])
        with self.assertRaises(ReviewError):
            i.family('Family')

    def test_units_related_and_full_gallery_are_preserved(self):
        i=src.Catalog([row('Item','100',ean=EAN,other=(
            ('Char. Width','40'),('Súvisiace tovary','Old,Future'),('Ďalšia kategória 1','Other'),
            ('SEO kľúčové slová','private original')))])
        with_xml(i,node('Item','',EAN=EAN,PROPS={'PROPERTY_8':'40 cm'}))
        d=document(i,'Item',options={'property_names':{'PROPERTY_8':'Width'},'reference_codes':{'Old':'Remote'}})
        self.assertEqual(d['payload']['parameters'],src.gen.parameters({'Width':'40 cm'}))
        self.assertEqual(d['payload']['related'],['Remote'])
        self.assertEqual(d['missing_related'],['Future'])
        self.assertEqual(d['missing_categories'],['Other'])
        self.assertEqual(d['seo_keywords'],'private original')

    def test_malformed_ean_needs_exact_supplier_proof_and_valid_target(self):
        i=src.Catalog([row('Item','100',ean='bad')])
        with self.assertRaisesRegex(ReviewError,'new_leaf_ean_unverified'):
            document(i,'Item')
        proof={'parent':'Item','before':'bad','ean':EAN,'manufacturer_or_supplier':'https://brand.example/product',
               'evidence':'Manufacturer records the exact SKU and the printed GTIN.'}
        d=document(i,'Item',options={'barcode_corrections':{'Item':proof}})
        self.assertEqual(d['payload']['ean'],EAN)
        self.assertEqual(d['barcode_corrections']['Item'],proof)
        with self.assertRaises(ReviewError):
            document(i,'Item',options={'barcode_corrections':{'Item':{**proof,'parent':'Other'}}})

    def test_attachment_and_active_markup_do_not_reach_remote(self):
        i=src.Catalog([row('Item','100',ean=EAN)])
        with_xml(i,node('Item','',EAN=EAN,DESCRIPTION_LONG='<img src=x onerror=alert(1)>'))
        with self.assertRaisesRegex(ReviewError,'new_active_html_requires_review'):
            document(i,'Item')
        self.assertEqual(src.safe_html('<iframe src="https://www.youtube.com/embed/abcdefghijk"></iframe>'),
                         '<iframe src="https://www.youtube.com/embed/abcdefghijk"></iframe>')
        with self.assertRaises(ReviewError):
            src.safe_html('<iframe src="https://example.com/untrusted"></iframe>')

    def test_archive_integrity_checked_before_use(self):
        d=b'data'
        self.assertEqual(src.hash_source(zlib.compress(d),hashlib.sha256(d).hexdigest()),d)
        with self.assertRaises(ReviewError):
            src.hash_source(zlib.compress(d),'a'*64)
