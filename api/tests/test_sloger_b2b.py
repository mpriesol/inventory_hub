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
    <form action="" method="post"><input type="hidden" name="redirect" value="/b2b">
      <input type="hidden" name="loginPage" value="1"><input type="hidden" name="login" value="1">
      <input name="username"><input name="password" type="password"><button>Prihlásiť</button></form>''')


def search(*urls):
    return page(AUTH + "".join(f'<div id="lipr_{i}"><p class="product-name"><a href="{url}">Product</a></p></div>'
                                for i, url in enumerate(urls)))


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
        client.request.side_effect = [response(LOGIN), response(page(AUTH)), response(search(other, PRODUCT_URL)),
                                      response(product("OTHER")), response(product())]
        with patch.object(b2b.requests, "Session", return_value=client), patch.object(b2b.time, "sleep"):
            result = b2b._lookup("synthetic-user", "synthetic-secret", "TEST-42")
        self.assertEqual(result["sku"], "TEST-42")
        self.assertEqual(client.request.call_count, 5)
        self.assertEqual(client.request.call_args_list[2].args[1], b2b.ORIGIN + "/sk/shop/hladaj.html?s=TEST-42")
        methods = [call.args[0] for call in client.request.call_args_list]
        self.assertEqual(methods, ["GET", "POST", "GET", "GET", "GET"])
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


class SlogerConnectionTests(unittest.TestCase):
    def test_prelogin_check_reads_only_fixed_login_page_without_credentials(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.request.return_value = response(LOGIN)
        with patch.object(b2b.requests, "Session", return_value=client):
            result = b2b._connection()
        self.assertEqual(result, {"reachable": True, "login_form_found": True, "diagnostic": None})
        client.request.assert_called_once()
        self.assertEqual(client.request.call_args.args, ("GET", b2b.LOGIN_URL))
        self.assertIsNone(client.request.call_args.kwargs["data"])

    def test_connection_failure_reports_only_stage_reason_and_http_status(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.request.return_value = response(b"private response synthetic-secret", status=403)
        with patch.object(b2b.requests, "Session", return_value=client):
            result = b2b._connection()
        self.assertEqual(result, {"reachable": False, "login_form_found": False,
                                 "diagnostic": {"stage": "login_page", "reason": "http_status", "http_status": 403}})
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
        self.assertEqual(error.diagnostic(), {"stage": "login_page", "reason": "structure", "http_status": None})


if __name__ == "__main__":
    unittest.main()
