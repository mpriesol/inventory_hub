from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import requests
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from husky_fixtures import item, stock, login, page
from inventory_hub.adapters import husky_catalog as parser, husky_b2b as b2b
from inventory_hub.config_secrets import public_config, config_patch
from inventory_hub.services import catalog, husky_sync, supplier_availability as availability
from inventory_hub.services.supplier_availability_source import AvailabilityError
from inventory_hub.services.catalog_merchandising import apply_availability


class HuskyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = {'adapter_settings': {'vat_rate': 23, 'currency': 'EUR', 'husky': {}}}

    def xml(self, name, content):
        path = self.root / name
        path.write_text('<SHOP>' + content + '</SHOP>')
        return path

    def records(self, *items):
        return parser.parse_catalog(self.xml('products.xml', ''.join(items or [item()])), 'husky', self.cfg)

    def test_catalog_identity_images_prices_and_explicit_variants(self):
        records = self.records(item(), item('PUBLIC-2', 'MPN-2', '4006381333931', color='modrá'))
        p = records[0][0]
        self.assertEqual((p.code, p.manufacturer_code, p.eans), ('PUBLIC-1', 'MPN-1', ['5901234123457']))
        self.assertEqual(p.images, ['https://example.test/original.jpg'])
        self.assertEqual((p.group_code, p.variant_attributes[0].value), ('42', 'zelená'))
        self.assertEqual((p.prices.retail_gross, p.prices.discount_gross, p.prices.purchase_net), (Decimal('123'), Decimal('99.9'), None))
        self.assertEqual(p.prices.vat_percent, Decimal(23))
        self.assertEqual(p.parameters[0].value, '3 kg')

    def test_invalid_and_duplicate_barcodes_are_not_silently_adopted(self):
        p = self.records(item(ean='hsk1093'))[0][0]
        self.assertEqual(p.eans, [])
        self.assertIn('invalid_ean', p.warnings)
        rows = self.records(item(), item('PUBLIC-2', 'MPN-2'))
        self.assertTrue(all('duplicate_supplier_ean' in p.import_blockers for p,_ in rows))

    def test_damaged_xml_duplicate_sku_and_bad_stock_abort(self):
        for content in [item()+item(), item().replace('<CODE>PUBLIC-1</CODE>', '')]:
            with self.assertRaises(ValueError):
                self.records(content)
        for content in [stock()+stock(), stock(flag='maybe')]:
            with self.assertRaises(ValueError):
                parser.read_stock(self.xml('stock.xml', content))
        dtd = self.root / 'bad.xml'
        dtd.write_text('<!DOCTYPE SHOP [<!ENTITY secret SYSTEM "file:///etc/passwd">]><SHOP>'+item()+'</SHOP>')
        with self.assertRaises(ValueError):
            parser.parse_catalog(dtd, 'husky', self.cfg)

    def test_public_boolean_never_manufactures_count_or_purchase_price(self):
        for flag in (True, False):
            snapshot = husky_sync.combine(self.records(), {'PUBLIC-1': flag}, None, self.cfg)
            p, raw = snapshot.catalog_records[0]
            self.assertEqual((p.supplier_stock, p.supplier_stock_min, p.availability, p.prices.purchase_net), (None, None, 'overíme', None))
            self.assertIsNone(p.supplier_available)
            self.assertIsNone(p.supplier_stock_raw)
            self.assertEqual((snapshot[0].available, snapshot[0].quantity_kind), (None, 'unknown'))
            self.assertIs(raw['public_stock'], flag)
            self.assertIs(snapshot[0].raw['public_stock'], flag)
            self.assertIsNone(p.group_code)  # singleton family becomes a fixed attribute
        with self.assertRaises(AvailabilityError):
            husky_sync.combine(self.records(), {}, None, self.cfg)

    def test_legacy_public_flags_remain_evidence_but_cannot_drive_b2b_stock_or_import(self):
        at = datetime.now(timezone.utc)
        for flag in (True, False):
            product = self.records()[0][0]
            product.warnings = ['husky_public_stock_only']
            product.supplier_available = flag
            product.supplier_stock_raw = str(flag)
            product.supplier_observation_expires_at = at + timedelta(hours=1)
            stored = product.model_dump(mode='json')
            row = SimpleNamespace(id=123, attributes={'catalog': stored})
            displayed = catalog.public_product(row, display=True)
            self.assertIsNone(displayed.supplier_available)
            self.assertIs(stored['supplier_available'], flag)
            self.assertEqual(displayed.supplier_stock_raw, str(flag))
            observation = SimpleNamespace(available=flag, quantity=None, quantity_kind='boolean',
                observed_at=at, expires_at=at+timedelta(hours=1), raw={'source':'husky_public_stock'})
            projected = availability.project_observation(observation, 'do 2-4 dní', 'husky', at)
            self.assertEqual((projected['available'],projected['quantity'],projected['quantity_kind'],projected['availability_state']),
                (None,None,'unknown','unknown'))
            payload = {}
            apply_availability(payload, [product], {'orderable':'do 2-4 dní','unavailable':'nedostupné','unknown':'overíme'})
            self.assertEqual(payload['availability'], 'overíme')
            self.assertTrue(payload['can_add_to_basket_yn'])

    def test_b2b_reg_number_join_lower_bound_price_basis_and_unknown(self):
        quotes = {q.code:q for q in b2b.parse_page(b2b.document(page().encode()), 1)[0]}
        snapshot = husky_sync.combine(self.records(), {'PUBLIC-1': False}, quotes, self.cfg)
        p, raw = snapshot.catalog_records[0]
        self.assertEqual((p.supplier_stock, p.supplier_stock_min, p.availability), (None, Decimal(11), 'do 2-4 dní'))
        self.assertIsNone(p.prices.purchase_net)
        self.assertEqual(raw['b2b']['price'], '55.00')
        self.cfg['adapter_settings']['husky']['purchase_price_basis'] = 'net'
        p = husky_sync.combine(self.records(), {'PUBLIC-1': True}, quotes, self.cfg).catalog_records[0][0]
        self.assertEqual((p.prices.purchase_net, p.prices.purchase_gross), (Decimal('55'), Decimal('67.65')))
        self.cfg['adapter_settings']['husky']['purchase_price_basis'] = 'gross'
        p = husky_sync.combine(self.records(), {'PUBLIC-1': True}, quotes, self.cfg).catalog_records[0][0]
        self.assertEqual(p.prices.purchase_net, Decimal('44.72'))
        quotes = {q.code:q for q in b2b.parse_page(b2b.document(page(stock_value='-').encode()), 1)[0]}
        p = husky_sync.combine(self.records(), {'PUBLIC-1': True}, quotes, self.cfg).catalog_records[0][0]
        self.assertIsNone(p.supplier_available)  # public True cannot override unknown wholesale

    def test_stock_export_lag_retains_catalog_code_with_unknown_availability(self):
        snapshot=husky_sync.combine(self.records(item(),item('PUBLIC-2','MPN-2','4006381333931')),
            {'PUBLIC-1':True},None,self.cfg)
        product,raw=snapshot.catalog_records[1]
        self.assertIsNone(product.supplier_available)
        self.assertEqual(product.availability,'overíme')
        self.assertIsNone(raw['public_stock'])
        self.assertIsNone(snapshot[1].raw['stock'])
        self.assertEqual(snapshot[1].quantity_kind,'unknown')
        self.assertEqual((snapshot.report()['total'],snapshot.report()['accepted'],snapshot.report()['unknown']),(2,0,2))
        self.assertIn('husky_public_stock_unmatched',snapshot.warnings)
        self.assertEqual(snapshot.stock_count,1)

    def test_b2b_evidence_remains_usable_for_code_missing_from_public_stock(self):
        quotes={q.code:q for q in b2b.parse_page(b2b.document(page().encode()),1)[0]}
        snapshot=husky_sync.combine(self.records(item(),item('PUBLIC-2','MPN-2','4006381333931')),
            {'PUBLIC-2':True},quotes,self.cfg)
        self.assertIsNone(snapshot.catalog_records[0][1]['public_stock'])
        self.assertTrue(snapshot[0].available)
        self.assertEqual((snapshot[0].quantity,snapshot[0].quantity_kind),(Decimal(11),'minimum'))
        self.assertIsNone(snapshot[1].available)

    def test_duplicate_mpn_not_used_for_b2b_join(self):
        quotes = {q.code:q for q in b2b.parse_page(b2b.document(page().encode()), 1)[0]}
        with self.assertRaises(AvailabilityError) as caught:
            husky_sync.combine(self.records(item(),item('PUBLIC-2',ean='4006381333931')), {'PUBLIC-1': True,'PUBLIC-2': True}, quotes, self.cfg)
        self.assertEqual(caught.exception.code,'husky_b2b_no_matches')

    def test_stock_semantics_and_pagination_validation(self):
        self.assertEqual(b2b.stock_value('0 ks'),(False,Decimal(0),'exact'))
        self.assertEqual(b2b.stock_value('1 ks'),(True,Decimal(1),'exact'))
        self.assertEqual(b2b.stock_value('-'),(None,None,'unknown'))
        for value in ('in stock','-1 ks','1.5 ks'):
            with self.assertRaises(b2b.HuskyError): b2b.stock_value(value)
        for html in (page(current=1,next_page=3),page(current=1,next_page=2).replace('Skladom','Warehouse')):
            with self.assertRaises(b2b.HuskyError): b2b.parse_page(b2b.document(html.encode()),1)
        with self.assertRaises(b2b.HuskyError): b2b.parse_page(b2b.document(page(current=1,next_page=2).encode()),2)

    def test_only_read_controls_posted_and_no_auth_or_html_persisted(self):
        documents = [login(),page(),page(),page(next_page=2),page(('MPN-2',),current=2)]
        posts = []
        def read(_session,url,data=None):
            posts.append((url,deepcopy(data)))
            return b2b.document(documents.pop(0).encode())
        with patch.object(b2b.Session, 'read', read):
            quotes = b2b.fetch_quotes('synthetic-user','synthetic-password')
        self.assertEqual(set(quotes),{'MPN-1','MPN-2'})
        self.assertEqual(posts[-1][1]['__EVENTARGUMENT'],'Page$2')
        for url, data in posts[2:]:
            if data:
                self.assertEqual(data[b2b.SEARCH], '')
                self.assertEqual(data[b2b.BRANCH], '')
                self.assertFalse(any('Objednat' in key or 'Cart' in key or 'PouzeSkladem' in key for key in data))
        self.assertNotIn('synthetic-password', repr(quotes))
        documents[:] = [login(),page(),page(),page(next_page=2),page(current=2)]
        with patch.object(b2b.Session,'read',read), self.assertRaises(b2b.HuskyError) as caught:
            b2b.fetch_quotes('synthetic-user','synthetic-password')
        self.assertEqual(caught.exception.code,'husky_b2b_duplicate_identity')

    def test_branch_context_survives_filter_reset_and_grid_postback(self):
        for branch in ('', 'synthetic-branch-2'):
            posts = []
            def read(_session, url, data=None):
                if url == b2b.LOGIN:
                    return b2b.document((login() if data is None else page(branch=branch)).encode())
                if data is not None:
                    posts.append(deepcopy(data))
                    # Missing dropdown data resets the selection/page during
                    # ASP.NET post-data processing before the grid event.
                    if data.get(b2b.BRANCH) == branch and data.get('__EVENTARGUMENT') == 'Page$2':
                        return b2b.document(page(('MPN-2',), current=2, branch=branch).encode())
                return b2b.document(page(next_page=2, branch=branch).encode())
            with patch.object(b2b.Session, 'read', read):
                quotes = b2b.fetch_quotes('synthetic-user', 'synthetic-password')
            self.assertEqual(set(quotes), {'MPN-1', 'MPN-2'})
            self.assertEqual([p[b2b.BRANCH] for p in posts], [branch, branch])
            self.assertEqual(posts[0][b2b.SEARCH_BUTTON], 'OK')
            self.assertEqual(posts[1]['__EVENTARGUMENT'], 'Page$2')

    def test_missing_or_ambiguous_branch_selection_is_rejected(self):
        for content in (
            page().replace(f'name="{b2b.BRANCH}"', 'name="unexpected"'),
            page(branch='synthetic-branch-2').replace('<option value="">', '<option value="" selected>'),
        ):
            with self.assertRaises(b2b.HuskyError):
                b2b.listing_post(b2b.document(content.encode()))
        implicit = page().replace(' selected>', '>')
        self.assertEqual(b2b.listing_post(b2b.document(implicit.encode()))[b2b.BRANCH], '')

    def test_fixed_https_destinations(self):
        for url in ['https://evil.test/login', b2b.ORIGIN+'/kosik', b2b.LISTING+'?order=1', 'http://b2b.huskysk.sk/login']:
            with self.assertRaises(b2b.HuskyError): b2b.safe_url(url)
        self.assertEqual(b2b.safe_url(b2b.LOGIN), b2b.LOGIN)

    def test_write_only_credentials_and_validated_configuration(self):
        cfg = {'adapter_settings':{'husky':{'b2b_enabled':True,'username':'synthetic-user','password':'synthetic-password'}}}
        public = public_config(cfg)
        self.assertEqual(public['adapter_settings']['husky']['password'], '')
        self.assertTrue(public['adapter_settings']['husky']['password_configured'])
        self.assertEqual(config_patch(cfg, public)['adapter_settings']['husky']['password'], 'synthetic-password')
        for options in [{'b2b_enabled':'true'}, {'b2b_enabled':True}, {'purchase_price_basis':'guess'}]:
            with self.assertRaises(b2b.HuskyError): b2b.validate_options(options)

    def test_schedule_and_freshness_do_not_claim_two_pieces(self):
        self.assertEqual(availability.schedule_values(True, 86400, 172800, 'husky'), (86400,172800))
        self.assertEqual(availability.schedule_values(True, 3600, 7200, 'husky'), (3600,7200))
        self.assertEqual(availability.schedule_values(True, 3600, 7200, 'sloger'), (86400,172800))
        p=self.records()[0][0]
        policy={'orderable':'do 2-4 dní','unknown':'overíme'}
        for qty,fresh,label in [(None,True,'overíme'),(1,True,'overíme'),(2,True,'do 2-4 dní'),(2,False,'overíme')]:
            p.supplier_stock=Decimal(qty) if qty is not None else None
            p.supplier_available=True
            p.supplier_observation_expires_at=datetime.now(timezone.utc)+timedelta(hours=1 if fresh else -1)
            payload={}
            apply_availability(payload,[p],policy)
            self.assertEqual(payload['availability'],label)

    def test_network_errors_and_redirects_never_retry_credentials(self):
        class Response:
            def __init__(self, status=200, headers=None, raw=b''):
                self.status_code, self.headers, self.raw = status, headers or {'Content-Type':'text/html'}, raw
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def iter_content(self,*args): yield self.raw
        class Client:
            def __init__(self,response): self.response,self.calls=response,[]
            def request(self,*args,**kwargs):
                self.calls.append((args,kwargs))
                if isinstance(self.response,Exception): raise self.response
                return self.response
        cases=[(Response(302,{'Location':'https://evil.test/login'}),'husky_b2b_unsafe_redirect'),
            (Response(429),'husky_b2b_rate_limited'),
            (requests.ConnectionError('synthetic-private-password'),'husky_b2b_unavailable'),
            (Response(raw=b'x'*(b2b.MAX_HTML_BYTES+1)),'husky_b2b_too_large')]
        for response,code in cases:
            client=Client(response)
            with self.assertRaises(b2b.HuskyError) as caught:
                b2b.Session(client, b2b.time.monotonic()+10).read(b2b.LOGIN, {'password':'synthetic-private-password'})
            self.assertEqual(str(caught.exception),code)
            self.assertEqual(len(client.calls),1)
            self.assertFalse(client.calls[0][1]['allow_redirects'])
