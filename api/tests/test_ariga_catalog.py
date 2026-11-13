"""Synthetic copies of observed ARIGA markup; no customer data or credentials."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lxml import etree

from inventory_hub.adapters import ariga_catalog as adapter
from inventory_hub.services import ariga_b2b, catalog, supplier_availability as availability
from inventory_hub.services.supplier_availability_source import parse as parse_availability

AUTH = '<a href="/cart.php?act=account">Fixture account</a>'
URL = adapter.ORIGIN + '/fixture/prod_100.html'
CFG = {'adapter_settings': {'vat_rate': 23}}


def page(variants=False, stock='Skladem', currency='€'):
    ids = ['101', '102'] if variants else ['100']
    codes = ('Počet variant <strong>2</strong><br>Kód vybrané varianty '
             '<strong class="js-product-variant-code" data-product-variant-id="101">SUP-A</strong>'
             '<strong class="js-product-variant-code is-hidden" data-product-variant-id="102">SUP-B</strong>'
             if variants else 'Kód zboží <strong>SUP-A</strong>')
    prices = ''.join(f'''<div class="product-detail-prices" data-product-variant-id="{i}">
        <div class="product-detail-price"><span>Doporučená MO cena</span><strong>159,90 {currency}</strong> s DPH
          <div class="product-detail-availability">{stock}</div></div>
        <div class="product-detail-price"><span>92,50 {currency} bez DPH</span><strong>111,93 {currency}</strong> s DPH
          <div class="product-detail-amounts">VO cena</div></div></div>''' for i in ids)
    selector = '''<div><label>Barva</label><select class="js-prod-detail-variants-choose">
        <option value="101">černá</option><option value="102">zlatá</option></select></div>''' if variants else ''
    thumbs = ''.join(f'<a class="js-product-variant-thumb" data-product-variant-id="{i}" href="/images/uploads/fixture/{i}.jpg">Photo</a>' for i in ids)
    return f'''<html><body>{AUTH}<div class="breadcrumb"><a href="/fixture/cat_10.html">Fixture</a></div>
      <div class="product-detail"><h1>Fixture torch</h1><div class="parameter-details">
      Značka <strong>Fenix</strong><br>Hmotnost <strong>120 g</strong><br>{codes}<br></div>
      {thumbs}{prices}{selector}<form name="addtobasket"><input name="quan" value="999"></form></div>
      <article class="article"><div class="slideshow-wrap"><a class="js-open-popup-gallery" href="/images/uploads/fixture/shared.jpg">Photo</a></div>
      <p>Product facts <strong>only</strong>.</p><script>bad()</script><iframe src="https://example.test"></iframe></article>
      <div class="recommendations">Other price 1,00 €</div></body></html>'''


def snapshot(records):
    root = etree.Element('ariga_catalog', version='1', currency='EUR', complete='true', items=str(len(records)))
    root.extend(records)
    return etree.tostring(root)


class ArigaParserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'catalog.xml'

    def parse(self, html=None, **kwargs):
        self.path.write_bytes(snapshot(adapter.product_evidence(adapter.document(html or page(**kwargs)), URL)))
        return adapter.parse_catalog(self.path, 'ariga', CFG)

    def test_gross_moc_is_preserved_and_czech_wholesale_gross_is_ignored(self):
        product, raw = self.parse()[0]
        self.assertEqual(product.prices.purchase_net, Decimal('92.50'))
        self.assertEqual(product.prices.purchase_gross, Decimal('113.78'))
        self.assertEqual(product.prices.retail_gross, Decimal('159.90'))
        self.assertEqual(product.prices.retail_net, Decimal('130.00'))
        self.assertEqual(product.code, 'SUP-A')
        self.assertIsNone(product.manufacturer_code)
        self.assertEqual(product.eans, [])
        self.assertIsNone(product.supplier_stock)
        self.assertTrue(product.supplier_available)
        self.assertNotIn('Fixture account', raw['xml'])
        self.assertNotIn('999', raw['xml'])
        self.assertNotIn('bad()', product.description)
        self.assertNotIn('iframe', product.description)
        self.assertEqual(product.images, [adapter.ORIGIN + '/images/uploads/fixture/100.jpg',
                                          adapter.ORIGIN + '/images/uploads/fixture/shared.jpg'])

    def test_all_explicit_variants_have_their_own_identity_and_main_image(self):
        records = self.parse(variants=True)
        self.assertEqual([p.code for p, _ in records], ['SUP-A', 'SUP-B'])
        self.assertEqual([p.group_code for p, _ in records], ['100', '100'])
        self.assertEqual([p.variant_attributes[0].value for p, _ in records], ['černá', 'zlatá'])
        self.assertTrue(records[1][0].images[0].endswith('/102.jpg'))

    def test_shop_price_payload_respects_both_vat_modes(self):
        from inventory_hub.catalog_types import ShopImportOptions
        from inventory_hub.services.catalog_import import _prices
        product = self.parse()[0][0]
        for with_vat, moc, purchase in ((True, 159.90, 113.78), (False, 130.00, 92.50)):
            price = _prices(product, ShopImportOptions(), CFG, with_vat)[0]
            self.assertEqual(price['price_common'], moc)
            self.assertEqual(price['pricelists'][0]['price_original'], moc)
            self.assertEqual(price['price_purchase'], purchase)

    def test_missing_images_do_not_turn_into_invented_photos(self):
        doc = adapter.document(page())
        for node in adapter.nodes(doc, 'js-product-variant-thumb') + adapter.nodes(doc, 'slideshow-wrap'):
            node.drop_tree()
        self.path.write_bytes(snapshot(adapter.product_evidence(doc, URL)))
        product = adapter.parse_catalog(self.path, 'ariga', CFG)[0][0]
        self.assertEqual(product.images, [])
        self.assertIn('missing_images', product.warnings)

    def test_missing_price_is_unknown_and_not_zero(self):
        raw = page().replace('<span>92,50 € bez DPH</span>', '<span>bad price</span>')
        with self.assertRaises(ValueError):
            self.parse(html=raw)
        items = adapter.product_evidence(adapter.document(page()), URL)
        items[0].remove(items[0].find('purchase_net'))
        self.path.write_bytes(snapshot(items))
        product = adapter.parse_catalog(self.path, 'ariga', CFG)[0][0]
        self.assertIsNone(product.prices.purchase_net)
        self.assertIn('missing_purchase_price', product.warnings)

    def test_changed_currency_or_variant_structure_rejects_the_snapshot(self):
        for raw in (page(currency='Kč'), page(variants=True).replace('>SUP-B<', '><'),
                    page(variants=True).replace('Počet variant <strong>2', 'Počet variant <strong>3'),
                    page().replace(AUTH, ''), page().replace('data-product-variant-id="100"', 'data-product-variant-id="9"')):
            with self.subTest(raw=raw[:30]), self.assertRaises(ValueError):
                self.parse(html=raw)

    def test_unknown_availability_expires_instead_of_becoming_zero(self):
        for label, expected in [('Skladem', True), ('Není skladem', False), ('Na dotaz', None), ('Na cestě', None)]:
            product = self.parse(stock=label)[0][0]
            self.assertIs(product.supplier_available, expected)
            facts = parse_availability(self.path, 'ariga', {'feeds': {'sources': {'products': {}}}, **CFG}, 'products', 'ariga')
            if expected is None:
                self.assertEqual(len(facts), 0)
                self.assertEqual(facts.issues[0]['kind'], 'unknown')
            else:
                self.assertIs(facts[0].available, expected)
                self.assertIsNone(facts[0].quantity)
                self.assertEqual(facts[0].quantity_kind, 'boolean')

    def test_duplicate_or_incomplete_snapshot_is_rejected(self):
        items = adapter.product_evidence(adapter.document(page()), URL)
        for raw in (snapshot(items + deepcopy(items)), snapshot(items).replace(b'complete="true"', b'complete="false"'),
                    snapshot(items).replace(b'items="1"', b'items="2"'), b'<!DOCTYPE x><ariga_catalog/>'):
            self.path.write_bytes(raw)
            with self.assertRaises(ValueError):
                adapter.parse_catalog(self.path, 'ariga', CFG)


class Response:
    def __init__(self, body='', status=200, location=None):
        self.body, self.status_code = body.encode(), status
        self.headers = {'Content-Type': 'text/html; charset=utf-8', **({'Location': location} if location else {})}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, size): yield self.body


class Client:
    def __init__(self, responses): self.responses, self.calls, self.headers = list(responses), [], {}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses: raise AssertionError('Unexpected supplier request')
        return self.responses.pop(0)


HOME = AUTH + '''<a href="/switch.php?r=%2Findex.php&amp;currency=EUR">Euro</a><a>Měna: €</a>
  <ul class="navigation-categories"><li><div><a href="/fixture/cat_10.html">Fixture</a></div></li></ul>'''
LOGIN = '''<form action="/cart.php?act=login" method="post"><input name="username"><input name="password" type="password">
    <button name="submit" type="submit">Přihlásit se</button></form>'''


def listing(product_id='100', total=1, next_page=None):
    more = f'''<form class="js-products-list-more" method="post" action="/fixture/cat_10.html?sort=pricedesc">
        <input name="page" value="{next_page}"><input name="diplay_mode" value="grid"></form>''' if next_page else ''
    return AUTH + f'<p>Zobrazeno 1 produktů z {total}</p><a class="product-name" href="/fixture/prod_{product_id}.html">Fixture</a>{more}'


class ArigaCrawlerTests(unittest.TestCase):
    def source(self):
        return {'mode': 'ariga-b2b', 'remote': {'auth': {'username': 'fixture@example.test', 'password': 'synthetic-password'}}}

    def test_login_currency_full_catalogue_and_product_only_snapshot(self):
        client = Client([Response(LOGIN), Response(HOME), Response(HOME), Response(HOME), Response(listing()), Response(page())])
        with tempfile.TemporaryDirectory() as tmp, patch.object(ariga_b2b.requests, 'Session', return_value=client), patch.object(ariga_b2b.time, 'sleep'):
            path = Path(tmp) / 'catalog.xml'
            ariga_b2b.download(self.source(), path)
            self.assertEqual(len(adapter.parse_catalog(path, 'ariga', CFG)), 1)
            self.assertNotIn(b'synthetic-password', path.read_bytes())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        posts = [(method, url) for method, url, _ in client.calls if method == 'POST']
        self.assertEqual(posts, [('POST', ariga_b2b.LOGIN_URL)])
        self.assertTrue(all(call[2]['allow_redirects'] is False for call in client.calls))

    def test_pagination_reads_all_pages_and_deduplicates_repeated_category_links(self):
        second_product = page().replace('SUP-A', 'SUP-B').replace('"100"', '"200"')
        client = Client([Response(listing(total=2, next_page=2)), Response(listing('200', total=2)), Response(page()), Response(second_product)])
        session = ariga_b2b.Session(client, ariga_b2b.time.monotonic() + 60, 100000)
        with patch.object(ariga_b2b.time, 'sleep'):
            raw = ariga_b2b.collect(session, adapter.document(HOME))
        self.assertEqual(len(etree.fromstring(raw)), 2)
        self.assertEqual(client.calls[1][0], 'POST')
        self.assertEqual(client.calls[1][2]['data']['page'], '2')

    def test_incomplete_repeated_or_changed_listing_stops_before_indexing(self):
        for second in (listing(total=2), listing('200', total=3)):
            client = Client([Response(listing(total=2, next_page=2)), Response(second)])
            session = ariga_b2b.Session(client, ariga_b2b.time.monotonic() + 60, 100000)
            with patch.object(ariga_b2b.time, 'sleep'), self.assertRaises(catalog.CatalogError):
                ariga_b2b.collect(session, adapter.document(HOME))
            self.assertEqual(len(client.calls), 2)

    def test_redirect_credentials_basket_urls_and_challenges_fail_closed(self):
        for target in ('https://example.test/', adapter.ORIGIN + '/cart.php?act=cart', adapter.ORIGIN + '/index.php?logout=1'):
            client = Client([Response(status=302, location=target)])
            session = ariga_b2b.Session(client, ariga_b2b.time.monotonic() + 60, 100000)
            with self.assertRaises(catalog.CatalogError):
                session.read(ariga_b2b.LOGIN_URL, 'login', {'password': 'synthetic-password'})
            self.assertEqual(len(client.calls), 1)
        client = Client([Response('<title>Just a moment</title>')])
        with self.assertRaises(catalog.CatalogError):
            ariga_b2b.Session(client, ariga_b2b.time.monotonic() + 60, 100000).read(ariga_b2b.HOME_URL, 'home')

    def test_failed_read_leaves_previous_snapshot_and_no_partial_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'existing.xml'
            path.write_bytes(b'previous-good-snapshot')
            with patch.object(ariga_b2b.requests, 'Session', return_value=Client([Response(status=403)])):
                with self.assertRaises(catalog.CatalogError): ariga_b2b.download(self.source(), path)
            self.assertEqual(path.read_bytes(), b'previous-good-snapshot')
            self.assertFalse(path.with_suffix('.part').exists())

    def test_expired_login_limits_and_product_posts_are_rejected(self):
        client = Client([Response(LOGIN), Response(LOGIN)])
        session = ariga_b2b.Session(client, ariga_b2b.time.monotonic() + 60, 100000)
        with patch.object(ariga_b2b.time, 'sleep'), self.assertRaises(catalog.CatalogError) as error:
            ariga_b2b.login(session, self.source()['remote']['auth'])
        self.assertEqual(error.exception.code, 'ariga_auth_failed')
        self.assertNotIn('synthetic-password', str(error.exception))
        for deadline, size, payload in ((0, 100000, None), (ariga_b2b.time.monotonic() + 60, 1, None),
                                        (ariga_b2b.time.monotonic() + 60, 100000, {'quan': '1'})):
            client = Client([Response(page())])
            session = ariga_b2b.Session(client, deadline, size)
            with self.assertRaises(catalog.CatalogError): session.read(URL, 'product', payload)
            if payload: self.assertEqual(client.calls, [])

    def test_login_form_cannot_override_action(self):
        bad = LOGIN.replace('<input name="username">', '<input type="hidden" name="act" value="basket"><input name="username">')
        client = Client([Response(bad)])
        with self.assertRaises(catalog.CatalogError):
            ariga_b2b.login(ariga_b2b.Session(client, ariga_b2b.time.monotonic() + 60, 100000), self.source()['remote']['auth'])
        self.assertEqual(len(client.calls), 1)

    def test_existing_catalogue_and_availability_recognize_configured_source(self):
        source = self.source()
        cfg = {'feeds': {'sources': {'products': source}}, **CFG}
        self.assertEqual(catalog.source_config('ariga', cfg, 'products')[1], 'ariga')
        self.assertTrue(catalog._source_configured(source))
        self.assertEqual(availability._feed_keys(cfg), ['products'])
        self.assertEqual(availability.public('ariga', cfg)['interval_seconds'], 86400)
        source['remote']['auth']['password'] = ''
        self.assertFalse(catalog._source_configured(source))
        with self.assertRaises(catalog.CatalogError): catalog.source_config('other', cfg, 'products')
        source['catalog_parser'] = 'sloger'
        with self.assertRaises(catalog.CatalogError): catalog.source_config('ariga', cfg, 'products')


if __name__ == '__main__':
    unittest.main()
