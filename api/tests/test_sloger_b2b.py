"""Synthetic supplier pages: no account data, credentials or live network."""
import unittest
from unittest.mock import MagicMock, patch

import requests

from inventory_hub.services import sloger_b2b as b2b
from inventory_hub.services.catalog import CatalogError
from inventory_hub.sloger_probe_types import SlogerObservation


def page(body):
    return ('<html><head><meta charset="utf-8"></head><body>' + body + '</body></html>').encode()


def response(body=b"<html></html>", status=200, **headers):
    result = MagicMock(status_code=status)
    result.headers = {"Content-Type": "text/html; charset=utf-8", **headers}
    result.iter_content.return_value = [body]
    return result


AUTH = '<a href="/b2b/logout">Odhlásiť</a>'
PRODUCT_URL = b2b.ORIGIN + "/shop/tools/test-brush.html"


def product(sku="TEST-42", *, purchase="12.50€ bez DPH", stock="Skladom 7 ks", secondary="Do 5 dní &gt; 99 ks"):
    return page(AUTH + f'''<h1>Synthetic test brush</h1>
        <p class="muted">Kód: {sku} / TEST-BRUSH / <i class="fa fa-barcode"></i> 0012345678912</p>
        <p id="price-detail" class="shop-list-price"><span>{purchase}</span>
          <small>MOC 19.99€ s DPH</small><small>Vaša prirážka<strong>37%</strong></small></p>
        <div class="product-info form-horizontal" itemprop="offers">
          <meta itemprop="price" content="12345.00"><meta itemprop="priceCurrency" content="EUR">
          <h3>Dostupnosť</h3><span class="label label-success">{stock}</span>
          <span class="label label-info">{secondary}</span><button>Do košíka</button>
        </div>''')


LOGIN = page('''<div class="modal"><form method="post"><input name="username"><input name="password"></form></div>
    <form action="/sk/prihlasenie-b2b" method="post"><input type="hidden" name="redirect" value="https://www.sloger.sk/shop/">
      <input type="hidden" name="loginPage" value="1"><input type="hidden" name="login" value="1">
      <input name="username"><input name="password" type="password"><button>Prihlásiť</button></form>''')


def search(*urls):
    return page(AUTH + "".join(f'<div id="lipr_{i}"><p class="product-name"><a href="{url}">Product</a></p></div>'
                                for i, url in enumerate(urls)))


def variant_product():
    # Synthetic commercial values in the observed radio/label DOM structure.
    rows = []
    for index, sku, ean, stock in ((1, "HELMET-S", "0012345678912", "Skladom 2 ks"),
                                   (2, "HELMET-M", "0012345678929", "Skladom 7 ks")):
        checked = ' checked="checked"' if index == 1 else ''
        rows.append(f'''<div class="form-check boxOptionWithRadioAndLabel">
          <input type="radio" id="option-{index}" name="option[1]" value="{index}" price="86.10€"{checked}>
          <label for="option-{index}">Fixture size {index}<span class="dispatch"><span class="label label-success">{stock}</span></span>
            <span class="muted">({sku} / <i class="fa fa-barcode"></i> {ean})</span></label></div>''')
    return b2b._document(page(AUTH + '''<h1>Fixture helmet</h1>
        <p id="price-detail"><span>61.25€ <small>bez DPH</small></span>
          <small class="priceBckp"><i>72.00€ bez DPH</i><b class="label label-warning">Zľava 15%</b></small>
          <small>MOC 145.90€ s DPH</small></p>
        <div class="product-info" itemprop="offers"><span class="label">Skladom 999 ks</span>'''
        + "".join(rows) + '</div>'))


def row_priced_variant_product():
    document = variant_product()
    price = document.xpath("//*[@id='price-detail']/span")[0]
    price.clear()
    price.append(b2b.html.fromstring('<small><strong>21.25€ - 27.50€</strong> <small>bez DPH</small></small>'))
    for label, current, marker in zip(document.xpath('//label'), ('21.25', '27.50'), ('31.20', '36.90')):
        label.insert(0, b2b.html.fromstring('<strong>' + current + '€</strong>'))
        label.insert(1, b2b.html.fromstring('<span class="muted">bez DPH</span>'))
        label.insert(2, b2b.html.fromstring('<small class="priceBckp"><i>30.00€ bez DPH</i>'
                                         '<span class="label label-warning">Zľava 15%</span></small>'))
        document.xpath('//input[@id="' + label.get('for') + '"]')[0].set('price', marker + '€')
    return document


class SlogerVariantTests(unittest.TestCase):
    def test_explicit_row_quotes_ignore_range_backup_and_untyped_radio_amounts(self):
        document = row_priced_variant_product()
        for sku, ean, amount, quantity in (("HELMET-S", "0012345678912", "21.25", "2"),
                                           ("HELMET-M", "0012345678929", "27.5", "7")):
            with self.subTest(sku=sku):
                result = b2b._parse_product(document, sku, PRODUCT_URL)
                self.assertEqual(result['eans'], [ean])
                self.assertEqual((result['purchase_price'], result['price_basis']), (amount, 'net'))
                self.assertIsNone(result['retail_price'])
                self.assertEqual(result['quantity'], quantity)
                self.assertEqual([tier['label'] for tier in result['availability_tiers']], [f'Skladom {quantity} ks'])
                SlogerObservation.model_validate(result)

    def test_missing_or_untyped_row_quote_never_inherits_family_or_sibling_price(self):
        for change in (lambda label: label.remove(label.xpath('./strong')[0]),
                       lambda label: label.remove(label.xpath('./span[@class="muted"]')[0]),
                       lambda label: setattr(label.xpath('./strong')[0], 'text', 'neznáma cena'),
                       lambda label: setattr(label.xpath('./span[@class="muted"]')[0], 'text', 'DPH neznáma'),
                       lambda label: [label.remove(node) for node in label.xpath('./strong | ./span[@class="muted"][not(i)]')]):
            document = row_priced_variant_product()
            # Even equal option markers cannot replace an absent row quote.
            for radio in document.xpath('//input'):
                radio.set('price', '86.10€')
            change(document.xpath('//label')[1])
            result = b2b._parse_product(document, 'HELMET-M', PRODUCT_URL)
            self.assertIsNone(result['purchase_price'])
            self.assertEqual(result['price_basis'], 'unknown')
            self.assertIsNone(result['retail_price'])
            self.assertEqual(result['quantity'], '7')
            self.assertEqual(b2b._parse_product(document, 'HELMET-S', PRODUCT_URL)['purchase_price'], '21.25')

    def test_gross_row_current_price_is_not_a_b2b_purchase_quote(self):
        document = row_priced_variant_product()
        document.xpath('//label')[1].xpath('./span[@class="muted"]')[0].text = 's DPH'
        with self.assertRaises(b2b.SlogerB2BError) as caught:
            b2b._parse_product(document, 'HELMET-M', PRODUCT_URL)
        self.assertEqual(caught.exception.code, 'sloger_auth_failed')

    def test_duplicate_row_quotes_or_barcode_identities_fail_closed(self):
        for extra in ('<strong>99.99€</strong>',
                      '<span class="muted">(OTHER / <i class="fa fa-barcode"></i> 0012345678936)</span>',
                      '<span><i class="fa fa-barcode"></i> 0012345678936</span>'):
            document = row_priced_variant_product()
            document.xpath('//label')[1].append(b2b.html.fromstring(extra))
            with self.subTest(extra=extra), self.assertRaises(b2b.SlogerB2BError):
                b2b._parse_product(document, 'HELMET-M', PRODUCT_URL)

    def test_variant_stock_outside_dispatch_is_not_availability_evidence(self):
        document = row_priced_variant_product()
        label = document.xpath('//label')[1]
        label.remove(label.xpath('./span[@class="dispatch"]')[0])
        label.append(b2b.html.fromstring('<span class="label">Skladom 888 ks</span>'))
        result = b2b._parse_product(document, 'HELMET-M', PRODUCT_URL)
        self.assertEqual(result['availability_tiers'], [])
        self.assertIsNone(result['available'])
        self.assertIsNone(result['quantity'])
        self.assertEqual(result['purchase_price'], '27.5')

    def test_exact_radio_label_sku_gets_own_ean_quantity_and_shared_current_price(self):
        document = variant_product()
        for sku, ean, quantity in (("HELMET-S", "0012345678912", "2"), ("HELMET-M", "0012345678929", "7")):
            with self.subTest(sku=sku):
                result = b2b._parse_product(document, sku, PRODUCT_URL)
                self.assertEqual(result["eans"], [ean])
                self.assertEqual(result["quantity"], quantity)
                self.assertEqual(result["availability_tiers"], [{"label": f"Skladom {quantity} ks", "quantity": quantity,
                    "quantity_kind": "exact", "quantity_relation": "equal", "lead_days": None}])
                self.assertEqual(result["purchase_price"], "61.25")
                self.assertEqual(result["price_basis"], "net")
                self.assertEqual(result["retail_price"], "145.9")
                self.assertIsNone(result["manufacturer_code"])
                SlogerObservation.model_validate(result)

    def test_mixed_missing_untyped_prices_or_nonunique_selection_leave_prices_unknown(self):
        for change in (lambda radios: radios[1].set("price", "96.10€"),
                       lambda radios: radios[1].attrib.pop("price"),
                       lambda radios: radios[1].set("price", "unreadable"),
                       lambda radios: radios[0].attrib.pop("checked"),
                       lambda radios: radios[1].set("checked", "checked")):
            document = variant_product()
            change(document.xpath("//input[@type='radio']"))
            result = b2b._parse_product(document, "HELMET-M", PRODUCT_URL)
            self.assertIsNone(result["purchase_price"])
            self.assertIsNone(result["retail_price"])
            self.assertEqual(result["price_basis"], "unknown")
            self.assertEqual(result["quantity"], "7")
            self.assertEqual(result["eans"], ["0012345678929"])

    def test_unknown_and_zero_stock_are_scoped_to_requested_variant(self):
        for text, available, quantity in (("Dostupnosť overíme", None, None), ("Skladom 0 ks", False, "0")):
            document = variant_product()
            label = document.xpath("//label[@for='option-2']//span[contains(@class,'label-success')]")[0]
            label.text = text
            result = b2b._parse_product(document, "HELMET-M", PRODUCT_URL)
            self.assertEqual(result["available"], available)
            self.assertEqual(result["quantity"], quantity)
            self.assertEqual(b2b._parse_product(document, "HELMET-S", PRODUCT_URL)["quantity"], "2")

    def test_missing_sku_never_uses_selected_or_first_variant(self):
        self.assertIsNone(b2b._parse_product(variant_product(), "HELMET-OTHER", PRODUCT_URL))

    def test_unboxed_choice_cannot_be_ignored_when_claiming_a_common_quote(self):
        document = variant_product()
        extra = b2b.html.fromstring('<input type="radio" name="option[1]" id="option-3" value="3" price="99.10€" checked="checked">')
        document.xpath("//div[@itemprop='offers']")[0].append(extra)
        result = b2b._parse_product(document, "HELMET-M", PRODUCT_URL)
        self.assertIsNone(result["purchase_price"])
        self.assertIsNone(result["retail_price"])
        self.assertEqual(result["quantity"], "7")

    def test_malformed_label_identity_and_duplicate_variants_are_rejected(self):
        for change in (lambda doc: doc.xpath("//label")[1].set("for", "option-1"),
                       lambda doc: doc.xpath("//label//span[@class='muted']")[1].__setattr__("text", "(HELMET-S / "),
                       lambda doc: doc.xpath("//input")[1].set("id", "option-1"),
                       lambda doc: doc.xpath("//input")[1].set("name", "option[2]"),
                       lambda doc: doc.xpath("//label//i")[1].__setattr__("tail", " no-barcode)")):
            document = variant_product()
            change(document)
            with self.assertRaises(b2b.SlogerB2BError):
                b2b._parse_product(document, "HELMET-S", PRODUCT_URL)

    def test_variant_boxes_outside_offer_are_not_product_evidence(self):
        document = b2b._document(product())
        for box in variant_product().xpath("//div[contains(@class,'boxOptionWithRadioAndLabel')]"):
            document.append(box)
        result = b2b._parse_product(document, "TEST-42", PRODUCT_URL)
        self.assertEqual(result["quantity"], "7")
        self.assertEqual(result["purchase_price"], "12.5")
        self.assertIsNone(b2b._parse_product(document, "HELMET-S", PRODUCT_URL))

    def test_nonmatching_standalone_candidate_does_not_require_price_or_offer_structure(self):
        document = b2b._document(page('<p class="muted">Kód: OTHER / OTHER-CODE</p>'))
        self.assertIsNone(b2b._parse_product(document, "TEST-42", PRODUCT_URL))


class SlogerTransportTests(unittest.TestCase):
    def setUp(self):
        sleeping = patch.object(b2b.time, "sleep")
        sleeping.start()
        self.addCleanup(sleeping.stop)
        self.client = MagicMock()

    def test_blocked_supplier_is_not_retried_and_does_not_disclose_response(self):
        for status in (403, 429, 500):
            self.client.reset_mock()
            result = response(b"private account response", status)
            self.client.request.return_value = result
            with self.subTest(status=status), self.assertRaises(CatalogError) as caught:
                b2b._Session(self.client).read(b2b.LOGIN_URL)
            self.assertEqual(caught.exception.code, "sloger_b2b_unavailable")
            self.assertNotIn("private account", str(caught.exception))
            self.client.request.assert_called_once()
            result.close.assert_called_once()

    def test_credentials_are_not_forwarded_to_redirect_or_replayed(self):
        for status, location in ((302, "https://example.test/collect"), (307, b2b.LOGIN_URL),
                                 (302, b2b.ORIGIN + "/shop/kosik.html"),
                                 (302, "https://www.sloger.sk@evil.test/collect")):
            self.client.reset_mock()
            self.client.request.return_value = response(status=status, Location=location)
            with self.subTest(status=status, location=location), self.assertRaises(CatalogError):
                b2b._Session(self.client).read(b2b.LOGIN_URL, data={"username": "synthetic-user", "password": "synthetic-secret"})
            self.client.request.assert_called_once()

    def test_normal_login_redirect_drops_credentials_before_get(self):
        self.client.request.side_effect = [response(status=302, Location="/b2b"), response(page("Ready"))]
        b2b._Session(self.client).read(b2b.LOGIN_URL, data={"password": "synthetic-secret"})
        first, second = self.client.request.call_args_list
        self.assertEqual(first.args[0], "POST")
        self.assertEqual(second.args, ("GET", b2b.ORIGIN + "/b2b"))
        self.assertIsNone(second.kwargs["data"])
        self.assertFalse(first.kwargs["allow_redirects"])

    def test_shop_return_is_limited_to_login_post_without_query_or_credential_replay(self):
        for stage, method, status, target in (
            ("login_page", "POST", 302, "/shop/"),
            ("search", "GET", 302, "/shop/"),
            ("product", "GET", 302, "/shop/"),
            ("login_submit", "GET", 302, "/shop/"),
            ("login_submit", "POST", 302, "/shop/?add_to_cart=1"),
            ("login_submit", "POST", 302, "/en/shop/"),
            ("login_submit", "POST", 302, "/sk/shop/"),
            ("login_submit", "POST", 302, "https://example.test/shop/"),
            ("login_submit", "POST", 307, "/shop/"),
            ("login_submit", "POST", 308, "/shop/"),
        ):
            self.client.reset_mock()
            self.client.request.return_value = response(status=status, Location=target)
            session = b2b._Session(self.client)
            session.stage = stage
            data = {"password": "synthetic-secret"} if method == "POST" else None
            with self.subTest(stage=stage, method=method, status=status, target=target), self.assertRaises(CatalogError):
                session.read(b2b.LOGIN_URL, data=data)
            self.client.request.assert_called_once()

    def test_only_exact_post_login_shop_get_404_uses_one_fixed_b2b_get(self):
        for status in (301, 302, 303):
            self.client.reset_mock()
            self.client.request.side_effect = [response(status=status, Location="/shop/"),
                                              response(page("Page not found"), 404), response(page(AUTH))]
            session = b2b._Session(self.client)
            session.stage = "login_submit"
            document, final = session.read(b2b.LOGIN_URL, data={"password": "synthetic-secret"})
            with self.subTest(status=status):
                self.assertTrue(b2b._authenticated(document))
                self.assertEqual(final, b2b.B2B_URL)
                calls = self.client.request.call_args_list
                self.assertEqual([call.args for call in calls], [
                    ("POST", b2b.LOGIN_URL), ("GET", b2b.ORIGIN + "/shop/"), ("GET", b2b.B2B_URL)])
                self.assertTrue(all(call.kwargs["data"] is None for call in calls[1:]))

    def test_post_failures_never_use_landing_recovery_and_identify_the_actual_request(self):
        for status in (401, 403, 404, 429, 500):
            self.client.reset_mock()
            self.client.request.return_value = response(status=status)
            session = b2b._Session(self.client)
            session.stage = "login_submit"
            with self.subTest(status=status), self.assertRaises(b2b.SlogerB2BError) as caught:
                session.read(b2b.LOGIN_URL, data={"password": "synthetic-secret"})
            self.client.request.assert_called_once()
            self.assertEqual(caught.exception.diagnostic()["method"], "POST")
            self.assertEqual(caught.exception.diagnostic()["path_class"], "login")

    def test_landing_recovery_does_not_retry_blocks_challenges_or_network_failures(self):
        for failure in [response(status=status) for status in (401, 403, 429, 500)] + [
            response(b"<html><title>Just a moment...</title></html>"),
            response(b"<html><title>Just a moment...</title></html>", 404),
            response(b"x" * (b2b.MAX_HTML_BYTES + 1), 404),
            response(b"not html", 404, **{"Content-Type": "application/json"}),
            requests.RequestException("synthetic-secret"),
        ]:
            self.client.reset_mock()
            self.client.request.side_effect = [response(status=302, Location="/shop/"), failure]
            session = b2b._Session(self.client)
            session.stage = "login_submit"
            with self.subTest(failure=failure), self.assertRaises(b2b.SlogerB2BError) as caught:
                session.read(b2b.LOGIN_URL, data={"password": "synthetic-secret"})
            self.assertEqual(self.client.request.call_count, 2)
            diagnostic = caught.exception.diagnostic()
            self.assertEqual((diagnostic["method"], diagnostic["path_class"]), ("GET", "shop_return"))
            self.assertNotIn("synthetic-secret", str(diagnostic))

    def test_direct_shop_get_and_failed_b2b_fallback_are_not_recovered(self):
        self.client.request.return_value = response(page("Not found"), 404)
        session = b2b._Session(self.client)
        session.stage = "login_submit"
        with self.assertRaises(b2b.SlogerB2BError):
            session.read(b2b.ORIGIN + "/shop/")
        self.client.request.assert_called_once()

        self.client.reset_mock()
        self.client.request.side_effect = [response(status=302, Location="/shop/"),
                                          response(page("Not found"), 404), response(status=404)]
        with self.assertRaises(b2b.SlogerB2BError) as caught:
            session.read(b2b.LOGIN_URL, data={"password": "synthetic-secret"})
        self.assertEqual(self.client.request.call_count, 3)
        diagnostic = caught.exception.diagnostic()
        self.assertEqual((diagnostic["method"], diagnostic["path_class"], diagnostic["http_status"]),
                         ("GET", "b2b", 404))

    def test_network_errors_and_challenges_are_sanitized(self):
        self.client.request.side_effect = requests.RequestException("synthetic-secret in request")
        with self.assertRaises(CatalogError) as caught:
            b2b._Session(self.client).read(b2b.LOGIN_URL)
        self.assertNotIn("synthetic-secret", str(caught.exception))
        for body in (b"<html><title>Just a moment...</title></html>", b'<form id="challenge-form"></form>'):
            with self.subTest(body=body), self.assertRaises(CatalogError):
                b2b._document(body)

    def test_response_bytes_and_elapsed_time_are_bounded(self):
        self.client.request.return_value = response(b"x" * (b2b.MAX_HTML_BYTES + 1))
        with self.assertRaises(CatalogError):
            b2b._Session(self.client).read(b2b.LOGIN_URL)
        with patch.object(b2b.time, "monotonic", side_effect=[0, b2b.MAX_SECONDS + 1]):
            with self.assertRaises(CatalogError):
                b2b._Session(self.client).read(b2b.LOGIN_URL)

    def test_product_urls_allow_only_observed_read_view(self):
        safe = b2b.ORIGIN + "/shop/tools/brush.html"
        self.assertEqual(b2b._product_url(safe), safe + "?voc")
        self.assertEqual(b2b._product_url(b2b.ORIGIN + "/sk/shop/tools/brush.html"), b2b.ORIGIN + "/sk/shop/tools/brush.html?voc")
        for url in ("https://example.test/shop/tools/brush.html", safe + "?add_to_cart=1",
                    b2b.ORIGIN + "/shop/kosik.html", "http://www.sloger.sk/shop/tools/brush.html",
                    "https://user:secret@www.sloger.sk/shop/tools/brush.html", safe + "#private",
                    b2b.ORIGIN + "/en/shop/tools/brush.html", b2b.ORIGIN + "/sk/sk/shop/tools/brush.html"):
            with self.subTest(url=url), self.assertRaises(CatalogError):
                b2b._product_url(url)

    def test_only_slovak_same_page_get_redirects_preserve_search_and_read_view(self):
        for path in ("/shop/hladaj.html?s=TEST-42", "/shop/tools/test-brush.html?voc"):
            for source, target in ((path, "/sk" + path), ("/sk" + path, path)):
                self.client.request.side_effect = [response(status=302, Location=target), response(page("Ready"))]
                _, final = b2b._Session(self.client).read(b2b.ORIGIN + source)
                self.assertEqual(final, b2b.ORIGIN + target)
        source = b2b.SEARCH_URL + "?s=TEST-42"
        for target in ("/en/shop/hladaj.html?s=TEST-42", "/hu/shop/hladaj.html?s=TEST-42",
                       "/shop/hladaj.html?s=OTHER", "/sk/shop/kosik.html?s=TEST-42", "/sk/sk/shop/hladaj.html?s=TEST-42"):
            self.client.reset_mock()
            self.client.request.side_effect = [response(status=302, Location=target)]
            with self.subTest(target=target), self.assertRaises(b2b.SlogerB2BError) as caught:
                b2b._Session(self.client).read(source)
            self.assertEqual(caught.exception.diagnostic()["reason"], "redirect")
            self.client.request.assert_called_once()


class SlogerProductTests(unittest.TestCase):
    def test_icon_only_logout_is_authenticated_only_for_exact_observed_origin_and_action(self):
        for target in ("/prihlasenie?logout", b2b.ORIGIN + "/prihlasenie?logout"):
            self.assertTrue(b2b._authenticated(b2b._document(page(f'<a href="{target}"><i class="fa fa-sign-out"></i></a>'))))
        for target in ("https://example.test/prihlasenie?logout", "/prihlasenie?logout=1", "/prihlasenie?login",
                       "http://www.sloger.sk/prihlasenie?logout", "/en/prihlasenie?logout"):
            self.assertFalse(b2b._authenticated(b2b._document(page(f'<a href="{target}"><i class="fa fa-sign-out"></i></a>'))))

    def test_voc_price_vat_ean_and_separate_stock_tiers_come_from_scoped_product(self):
        result = b2b._parse_product(b2b._document(product()), "TEST-42", PRODUCT_URL + "?voc")
        self.assertEqual(result["purchase_price"], "12.5")
        self.assertEqual(result["price_basis"], "net")
        self.assertEqual(result["retail_price"], "19.99")
        self.assertEqual(result["retail_price_basis"], "gross")
        self.assertEqual(result["eans"], ["0012345678912"])
        self.assertEqual(result["manufacturer_code"], "TEST-BRUSH")
        self.assertEqual(result["source_url"], PRODUCT_URL)
        self.assertEqual((result["quantity"], result["quantity_kind"], result["quantity_relation"]), ("7", "exact", "equal"))
        secondary = result["availability_tiers"][1]
        self.assertEqual((secondary["quantity"], secondary["quantity_relation"], secondary["lead_days"]), ("99", "greater_than", 5))
        self.assertTrue(result["available"])
        SlogerObservation.model_validate(result)

    def test_unknown_values_stay_unknown_and_secondary_stock_is_not_own_stock(self):
        raw = product(purchase="Na dopyt", stock="Na objednávku")
        result = b2b._parse_product(b2b._document(raw), "TEST-42", PRODUCT_URL)
        self.assertIsNone(result["purchase_price"])
        self.assertEqual(result["price_basis"], "unknown")
        self.assertIsNone(result["quantity"])
        self.assertIsNone(result["available"])
        self.assertEqual(result["quantity_kind"], "unknown")
        self.assertEqual(result["availability_tiers"][1]["quantity"], "99")
        SlogerObservation.model_validate(result)

    def test_gross_moc_view_is_not_misreported_as_purchase_quote(self):
        with self.assertRaises(CatalogError) as caught:
            b2b._parse_product(b2b._document(product(purchase="19.99€ s DPH")), "TEST-42", PRODUCT_URL)
        self.assertEqual(caught.exception.code, "sloger_auth_failed")

    def test_exact_zero_and_strict_positive_lower_bound_have_different_availability(self):
        for label, expected in (("Skladom 0 ks", False), ("Skladom &gt; 0 ks", True), ("Skladom 5+ ks", True)):
            with self.subTest(label=label):
                result = b2b._parse_product(b2b._document(product(stock=label)), "TEST-42", PRODUCT_URL)
                self.assertIs(result["available"], expected)

    def test_exact_sku_rejects_description_search_match_and_missing_identity(self):
        self.assertIsNone(b2b._parse_product(b2b._document(product("OTHER")), "TEST-42", PRODUCT_URL))
        with self.assertRaises(CatalogError):
            b2b._parse_product(b2b._document(page(AUTH + "<p>TEST-42 in description only</p>")), "TEST-42", PRODUCT_URL)

    def test_candidate_set_is_bounded_and_cross_origin_links_are_refused(self):
        with self.assertRaises(CatalogError) as caught:
            b2b._candidate_urls(b2b._document(search(*[b2b.ORIGIN + f"/shop/tools/item-{i}.html" for i in range(6)])), b2b.ORIGIN)
        self.assertEqual(caught.exception.code, "sloger_product_ambiguous")
        with self.assertRaises(CatalogError):
            b2b._candidate_urls(b2b._document(search("https://example.test/shop/tools/item.html")), b2b.ORIGIN)

    def test_one_ephemeral_login_then_exact_match_without_cart_or_order_requests(self):
        other = b2b.ORIGIN + "/shop/tools/other.html"
        client = MagicMock()
        client.__enter__.return_value = client
        client.request.side_effect = [response(LOGIN), response(status=302, Location=b2b.ORIGIN + "/shop/"),
                                      response(page(AUTH)), response(search(other, PRODUCT_URL)),
                                      response(product("OTHER")), response(product())]
        with patch.object(b2b.requests, "Session", return_value=client), patch.object(b2b.time, "sleep"):
            result = b2b._lookup("synthetic-user", "synthetic-secret", "TEST-42")
        self.assertEqual(result["sku"], "TEST-42")
        self.assertEqual(client.request.call_count, 6)
        self.assertEqual(client.request.call_args_list[0].args, ("GET", "https://www.sloger.sk/sk/prihlasenie-b2b"))
        self.assertEqual(client.request.call_args_list[1].args, ("POST", "https://www.sloger.sk/sk/prihlasenie-b2b"))
        self.assertEqual(client.request.call_args_list[2].args, ("GET", b2b.ORIGIN + "/shop/"))
        self.assertIsNone(client.request.call_args_list[2].kwargs["data"])
        self.assertEqual(client.request.call_args_list[3].args[1], b2b.ORIGIN + "/sk/shop/hladaj.html?s=TEST-42")
        methods = [call.args[0] for call in client.request.call_args_list]
        self.assertEqual(methods, ["GET", "POST", "GET", "GET", "GET", "GET"])
        # The only submitted dict was cleared once authentication completed.
        self.assertEqual(client.request.call_args_list[1].kwargs["data"], {})
        self.assertNotIn("synthetic-user", str(result))
        self.assertNotIn("synthetic-secret", str(result))
        client.__exit__.assert_called_once()

    def test_bad_login_does_not_search_and_duplicate_identity_does_not_choose_first(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.request.side_effect = [response(LOGIN), response(LOGIN)]
        with patch.object(b2b.requests, "Session", return_value=client), patch.object(b2b.time, "sleep"):
            with self.assertRaises(CatalogError) as caught:
                b2b._lookup("synthetic-user", "synthetic-secret", "TEST-42")
        self.assertEqual(caught.exception.code, "sloger_auth_failed")
        self.assertEqual(client.request.call_count, 2)
        client.request.side_effect = [response(LOGIN), response(page(AUTH)),
                                      response(search(PRODUCT_URL, b2b.ORIGIN + "/shop/tools/duplicate.html")),
                                      response(product()), response(product())]
        with patch.object(b2b.requests, "Session", return_value=client), patch.object(b2b.time, "sleep"):
            with self.assertRaises(CatalogError) as caught:
                b2b._lookup("synthetic-user", "synthetic-secret", "TEST-42")
        self.assertEqual(caught.exception.code, "sloger_product_ambiguous")

    def test_login_return_recovery_still_requires_authentication_before_search(self):
        for authenticated in (False, True):
            client = MagicMock()
            client.__enter__.return_value = client
            client.request.side_effect = [response(LOGIN), response(status=302, Location="/shop/"),
                                          response(page("Not found"), 404),
                                          response(page(AUTH) if authenticated else LOGIN),
                                          response(search(PRODUCT_URL)), response(product())]
            with self.subTest(authenticated=authenticated), patch.object(b2b.requests, "Session", return_value=client), patch.object(b2b.time, "sleep"):
                if authenticated:
                    self.assertEqual(b2b._lookup("synthetic-user", "synthetic-secret", "TEST-42")["sku"], "TEST-42")
                    self.assertEqual(client.request.call_count, 6)
                else:
                    with self.assertRaises(b2b.SlogerB2BError) as caught:
                        b2b._lookup("synthetic-user", "synthetic-secret", "TEST-42")
                    self.assertEqual(caught.exception.code, "sloger_auth_failed")
                    self.assertEqual(client.request.call_count, 4)
            calls = client.request.call_args_list
            self.assertEqual(sum(call.args[0] == "POST" for call in calls), 1)
            self.assertEqual(calls[3].args, ("GET", b2b.B2B_URL))
            self.assertTrue(all(call.kwargs["data"] is None for call in calls[2:]))


class SlogerConnectionTests(unittest.TestCase):
    def test_prelogin_check_reads_only_fixed_login_page_without_credentials(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.request.return_value = response(LOGIN)
        with patch.object(b2b.requests, "Session", return_value=client):
            result = b2b._connection()
        self.assertEqual(result, {"reachable": True, "login_form_found": True, "diagnostic": None})
        client.request.assert_called_once()
        self.assertEqual(client.request.call_args.args, ("GET", "https://www.sloger.sk/sk/prihlasenie-b2b"))
        self.assertIsNone(client.request.call_args.kwargs["data"])

    def test_connection_failure_reports_only_stage_reason_and_http_status(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.request.return_value = response(b"private response synthetic-secret", status=403)
        with patch.object(b2b.requests, "Session", return_value=client):
            result = b2b._connection()
        self.assertEqual(result, {"reachable": False, "login_form_found": False,
                                 "diagnostic": {"stage": "login_page", "reason": "http_status", "http_status": 403,
                                                "method": "GET", "path_class": "login"}})
        self.assertNotIn("synthetic-secret", str(result))
        client.request.assert_called_once()

    def test_authenticated_failure_reports_stage_and_never_echoes_bad_url(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.request.side_effect = [response(LOGIN), response(page(AUTH)),
                                      response(search("https://synthetic-secret.example.test/shop/tools/item.html"))]
        with patch.object(b2b.requests, "Session", return_value=client), patch.object(b2b.time, "sleep"):
            with self.assertRaises(b2b.SlogerB2BError) as caught:
                b2b._lookup("synthetic-user", "synthetic-password", "TEST-42")
        self.assertEqual(caught.exception.diagnostic(), {"stage": "search", "reason": "url", "http_status": None})
        self.assertNotIn("synthetic-secret", str(caught.exception.diagnostic()))

    def test_diagnostic_values_are_allowlisted_even_if_error_attributes_are_changed(self):
        error = b2b._error(reason="synthetic-secret", http_status="synthetic-secret")
        error.stage = "synthetic-secret"
        error.method = "synthetic-secret"
        error.path_class = "synthetic-secret"
        self.assertEqual(error.diagnostic(), {"stage": "login_page", "reason": "structure", "http_status": None})


if __name__ == "__main__":
    unittest.main()
