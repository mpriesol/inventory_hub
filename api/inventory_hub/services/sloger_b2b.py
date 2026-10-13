"""Ephemeral, read-only Sloger B2B access for a single exact supplier SKU.

The session, credentials and HTML never leave this adapter. Only scoped product
facts are returned; supplier quotes are not inventory acquisition costs.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
import time
import unicodedata
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from lxml import etree, html
import requests

from inventory_hub.services.catalog import CatalogError


ORIGIN = "https://www.sloger.sk"
LOGIN_URL = ORIGIN + "/sk/prihlasenie-b2b"
SEARCH_URL = ORIGIN + "/sk/shop/hladaj.html"
MAX_HTML_BYTES = 2_000_000
MAX_SECONDS = 100
MAX_CANDIDATES = 5
_PRODUCT_PATH = re.compile(r"^/(?:sk/)?shop/(?:[^/.]+/)+[^/]+\.html$")
_HEADERS = {"User-Agent": "InventoryHub-Sloger-ReadOnly/1.0", "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "sk-SK,sk;q=0.9"}


class SlogerB2BError(CatalogError):
    """Only these fixed labels may be recorded outside the ephemeral session."""
    stage = "login_page"

    def diagnostic(self) -> dict:
        return {"stage": self.stage if self.stage in {"login_page", "login_submit", "search", "product"} else "login_page",
                "reason": self.reason if self.reason in {
                    "http_status", "network", "redirect", "content_type", "challenge", "size", "deadline", "url", "structure", "authentication"
                } else "structure",
                "http_status": self.http_status if type(self.http_status) is int and 100 <= self.http_status <= 599 else None}


def _error(code="sloger_b2b_unavailable", *, reason="structure", http_status=None):
    messages = {
        "sloger_b2b_unavailable": "Sloger B2B could not be read safely; no lookup was retried",
        "sloger_auth_failed": "Sloger did not confirm the B2B login",
        "sloger_product_not_found": "Sloger did not return the exact requested supplier SKU",
        "sloger_product_ambiguous": "Sloger returned ambiguous product evidence",
    }
    error = SlogerB2BError(code, messages[code], 422 if code != "sloger_b2b_unavailable" else 502)
    error.reason, error.http_status = reason, http_status
    return error


def _text(element) -> str:
    return " ".join(element.text_content().split()) if element is not None else ""


def _fold(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c)).casefold()


def _safe_url(url: str) -> str:
    """Never send credentials, cookies or requests to a page-selected host."""
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname == "www.sloger.sk"
                 and parsed.port in (None, 443) and not parsed.username and not parsed.password
                 and not parsed.fragment and "\\" not in url and not any(ord(c) < 32 for c in url))
    except ValueError:
        valid = False
    if not valid:
        raise _error(reason="url")
    return url


def _product_url(url: str) -> str:
    parsed = urlsplit(_safe_url(url))
    if not _PRODUCT_PATH.fullmatch(parsed.path) or any(part in (".", "..") for part in parsed.path.split("/")):
        raise _error(reason="url")
    # The observed VOC view is the only permitted product query. In particular,
    # no page-controlled query action or token is ever forwarded or returned.
    if parsed.query not in ("", "voc"):
        raise _error(reason="url")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "voc", ""))


def _document(raw: bytes):
    try:
        document = html.fromstring(raw, parser=html.HTMLParser(no_network=True))
    except (etree.ParserError, ValueError):
        raise _error() from None
    title = _fold(" ".join(document.xpath("//title/text()")))
    if (any(marker in title for marker in ("just a moment", "access denied", "attention required", "captcha"))
            or document.xpath("//*[@id='challenge-form' or @id='cf-challenge-running']")):
        raise _error(reason="challenge")
    return document


class _Session:
    def __init__(self, client):
        self.client = client
        self.started = time.monotonic()
        self.last_request = None
        self.stage = "login_page"

    def read(self, url: str, *, data=None):
        """One bounded attempt; redirects are explicit and never replay a POST."""
        method = "POST" if data is not None else "GET"
        for redirect in range(4):
            _safe_url(url)
            remaining = MAX_SECONDS - (time.monotonic() - self.started)
            if remaining <= 0:
                raise _error(reason="deadline")
            if self.last_request is not None:
                time.sleep(max(0, 1 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            response = None
            try:
                response = self.client.request(method, url, data=data, allow_redirects=False,
                                               timeout=(10, min(20, remaining)), stream=True,
                                               headers={"Origin": ORIGIN, "Referer": url} if method == "POST" else None)
                if response.status_code in (301, 302, 303, 307, 308):
                    if redirect == 3 or (method == "POST" and response.status_code in (307, 308)):
                        raise _error(reason="redirect")
                    target = urljoin(url, response.headers.get("Location", ""))
                    if target == url:
                        raise _error(reason="redirect")
                    _safe_url(target)
                    # Login redirects can return the B2B landing page or home;
                    # product/search redirects may only stay on their same path.
                    source, destination = urlsplit(url), urlsplit(target)
                    source_path, target_path = source.path, destination.path
                    # Only the observed Slovak prefix may be added/removed from
                    # the same page. Other locales would change parser meaning.
                    same_slovak_page = (method == "GET" and source.query == destination.query
                        and (source_path.startswith("/shop/") and target_path == "/sk" + source_path
                             or target_path.startswith("/shop/") and source_path == "/sk" + target_path))
                    # The observed login form returns to this exact shop root.
                    # Permit only the credential-dropping redirect of that POST,
                    # never a search/product redirect or a credential replay.
                    login_return = (self.stage == "login_submit" and method == "POST"
                                    and url == LOGIN_URL and target == ORIGIN + "/shop/")
                    if (target_path not in (source_path, "/", "/b2b", "/b2b/", "/sk/b2b", "/sk/b2b/")
                            and not same_slovak_page and not login_return):
                        raise _error(reason="redirect")
                    url, method, data = target, "GET", None
                    continue
                if response.status_code != 200:
                    raise _error("sloger_auth_failed" if response.status_code == 401 else "sloger_b2b_unavailable",
                                 reason="http_status", http_status=response.status_code)
                if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() not in ("text/html", "application/xhtml+xml"):
                    raise _error(reason="content_type")
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_HTML_BYTES:
                        raise _error(reason="size")
                    if time.monotonic() - self.started > MAX_SECONDS:
                        raise _error(reason="deadline")
                    chunks.append(chunk)
                return _document(b"".join(chunks)), url
            except requests.RequestException:
                # requests exceptions may contain URLs, request bodies or server
                # fragments. Never propagate or log those values.
                raise _error(reason="network") from None
            finally:
                if response is not None:
                    response.close()
        raise _error(reason="redirect")


def _authenticated(document) -> bool:
    for link in document.xpath("//a"):
        if _fold(_text(link)) == "odhlasit":
            return True
        # The observed Slovak B2B navigation uses an icon-only logout link.
        # Its exact same-origin destination is evidence; it is never followed.
        try:
            target = urlsplit(_safe_url(urljoin(ORIGIN, link.get("href") or "")))
        except SlogerB2BError:
            continue
        if target.path == "/prihlasenie" and target.query == "logout":
            return True
    return False


def _decimal(value: str) -> str | None:
    value = re.sub(r"\s+", "", value).replace(",", ".")
    if not re.fullmatch(r"\d{1,9}(?:\.\d{1,4})?", value):
        return None
    try:
        return format(Decimal(value).normalize(), "f")
    except InvalidOperation:
        return None


def _login(session: _Session, username: str, password: str):
    session.stage = "login_page"
    document, landing = session.read(LOGIN_URL)
    forms = document.xpath("//form[.//input[@name='username'] and .//input[@name='password']]"
                           "[not(ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' modal ')])]")
    if len(forms) != 1:
        raise _error("sloger_auth_failed")
    form = forms[0]
    action = urljoin(landing, form.get("action") or landing)
    # This integration implements the observed same-page POST form, not a
    # configurable browser or a generic credential submission capability.
    if action != landing or (form.get("method") or "get").lower() != "post":
        raise _error("sloger_auth_failed")
    payload = {}
    for node in form.xpath(".//input[@type='hidden']"):
        name = node.get("name")
        if name not in ("redirect", "loginPage", "login") or name in payload:
            raise _error("sloger_auth_failed")
        payload[name] = node.get("value") or ""
    if payload.get("redirect"):
        _safe_url(urljoin(landing, payload["redirect"]))
    payload.update(username=username, password=password)
    try:
        session.stage = "login_submit"
        authenticated, _ = session.read(action, data=payload)
    finally:
        payload.clear()
    if not _authenticated(authenticated):
        raise _error("sloger_auth_failed", reason="authentication")


def _candidate_urls(document, base: str) -> list[str]:
    urls = []
    for card in document.xpath("//*[starts-with(@id, 'lipr_')]"):
        links = card.xpath(".//p[contains(concat(' ', normalize-space(@class), ' '), ' product-name ')]/a[@href]")
        if len(links) != 1:
            raise _error()
        url = _product_url(urljoin(base, links[0].get("href")))
        if url not in urls:
            urls.append(url)
        if len(urls) > MAX_CANDIDATES:
            raise _error("sloger_product_ambiguous")
    return urls


def _price(value: str) -> tuple[str | None, str]:
    found = re.fullmatch(r"\s*([\d\s.,]+)\s*€\s*(bez|s)\s+DPH\s*", value, re.IGNORECASE)
    if not found:
        return None, "unknown"
    amount = _decimal(found[1])
    return amount, ("net" if found[2].casefold() == "bez" else "gross") if amount is not None else "unknown"


def _tier(label: str) -> dict:
    quantity, kind, relation, lead = None, "unknown", "unknown", None
    found = re.search(r"(>=|≥|>)?\s*(\d{1,9}(?:[.,]\d{1,3})?)\s*(\+)?\s*ks\b", label)
    if found:
        quantity = _decimal(found[2])
        relation = "greater_than" if found[1] == ">" else "at_least" if found[1] or found[3] else "equal"
        kind = "exact" if relation == "equal" else "minimum"
    days = re.match(r"do\s+(\d{1,3})\s+dni\b", _fold(label))
    if days:
        lead = int(days[1])
    return {"label": label, "quantity": quantity, "quantity_kind": kind,
            "quantity_relation": relation, "lead_days": lead}


def _parse_product(document, sku: str, url: str) -> dict | None:
    code_nodes = [node for node in document.xpath("//p[contains(concat(' ', normalize-space(@class), ' '), ' muted ')]")
                  if _fold(_text(node)).startswith("kod:")]
    if len(code_nodes) != 1:
        raise _error()
    code_node = code_nodes[0]
    code_parts = _text(code_node).split(":", 1)[1].strip().split("/")
    if code_parts[0].strip() != sku:
        return None
    names = document.xpath("//h1")
    price_nodes = document.xpath("//*[@id='price-detail']")
    offer_nodes = document.xpath("//div[@itemprop='offers'][contains(concat(' ', normalize-space(@class), ' '), ' product-info ')]")
    if len(names) != 1 or len(price_nodes) != 1 or len(offer_nodes) != 1:
        raise _error()
    purchase, purchase_basis, retail, retail_basis = None, "unknown", None, "unknown"
    main_prices = price_nodes[0].xpath("./span")
    if len(main_prices) == 1:
        purchase, purchase_basis = _price(_text(main_prices[0]))
        # VOC was requested. A gross-only main price may be an unauthenticated
        # MOC view, so it must never become an alleged supplier purchase quote.
        if purchase_basis == "gross":
            raise _error("sloger_auth_failed")
    retail_nodes = [node for node in price_nodes[0].xpath("./small") if _text(node).startswith("MOC ")]
    if len(retail_nodes) > 1:
        raise _error()
    if retail_nodes:
        retail, retail_basis = _price(_text(retail_nodes[0])[4:])
    eans = []
    barcode = code_node.xpath(".//i[contains(concat(' ', normalize-space(@class), ' '), ' fa-barcode ')]")
    if len(barcode) > 1:
        raise _error()
    if barcode:
        candidate = (barcode[0].tail or "").strip()
        if re.fullmatch(r"\d{1,32}", candidate) and set(candidate) != {"0"}:
            eans.append(candidate)
    manufacturer = code_parts[1].strip() if len(code_parts) == 3 and barcode else None
    tiers = [_tier(_text(node)) for node in offer_nodes[0].xpath(
        ".//span[contains(concat(' ', normalize-space(@class), ' '), ' label ')]")]
    if any(not tier["label"] or len(tier["label"]) > 200 for tier in tiers) or len(tiers) > 10:
        raise _error()
    own_tiers = [tier for tier in tiers if re.match(r"skladom\b", _fold(tier["label"]))]
    if len(own_tiers) > 1:
        raise _error("sloger_product_ambiguous")
    own = own_tiers[0] if own_tiers else None
    available = None
    if own:
        # Only an explicit exact zero says there is no on-hand stock. A lower
        # bound such as > 0 must not be turned into an out-of-stock observation.
        available = not (own["quantity_relation"] == "equal" and own["quantity"] is not None
                         and Decimal(own["quantity"]) == 0)
    if own is None and any(_fold(tier["label"]) in ("nie je skladom", "nedostupne", "vypredane") for tier in tiers):
        available = False
    parsed = urlsplit(_product_url(url))
    return {"sku": sku, "product_name": _text(names[0]) or None, "manufacturer_code": manufacturer,
            "eans": eans, "purchase_price": purchase, "currency": "EUR" if purchase is not None or retail is not None else None,
            "price_basis": purchase_basis, "retail_price": retail, "retail_price_basis": retail_basis,
            "availability": own["label"] if own else None, "available": available,
            "quantity": own["quantity"] if own else None, "quantity_kind": own["quantity_kind"] if own else "unknown",
            "quantity_relation": own["quantity_relation"] if own else "unknown", "availability_tiers": tiers,
            "source_url": urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")),
            "observed_at": datetime.now(timezone.utc).isoformat()}


def _lookup(username: str, password: str, sku: str) -> dict:
    with requests.Session() as client:
        client.headers.update(_HEADERS)
        session = _Session(client)
        try:
            _login(session, username, password)
            session.stage = "search"
            search_url = SEARCH_URL + "?" + urlencode({"s": sku})
            document, final_url = session.read(search_url)
            if not _authenticated(document):
                raise _error("sloger_auth_failed", reason="authentication")
            found = []
            for product_url in _candidate_urls(document, final_url):
                session.stage = "product"
                product, final_product_url = session.read(product_url)
                if not _authenticated(product):
                    raise _error("sloger_auth_failed", reason="authentication")
                observation = _parse_product(product, sku, final_product_url)
                if observation is not None:
                    found.append(observation)
            if not found:
                raise _error("sloger_product_not_found")
            if len(found) != 1:
                raise _error("sloger_product_ambiguous")
            return found[0]
        except SlogerB2BError as error:
            error.stage = session.stage
            raise


async def lookup_product(username: str, password: str, sku: str) -> dict:
    return await asyncio.to_thread(_lookup, username, password, sku)


def _connection() -> dict:
    """Check the fixed login page without credentials, a POST or a model call."""
    with requests.Session() as client:
        client.headers.update(_HEADERS)
        session = _Session(client)
        try:
            document, landing = session.read(LOGIN_URL)
            forms = document.xpath("//form[.//input[@name='username'] and .//input[@name='password']]"
                                   "[not(ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' modal ')])]")
            found = (len(forms) == 1 and (forms[0].get("method") or "get").lower() == "post"
                     and urljoin(landing, forms[0].get("action") or landing) == landing)
            return {"reachable": True, "login_form_found": found,
                    "diagnostic": None if found else _error().diagnostic()}
        except SlogerB2BError as error:
            error.stage = session.stage
            return {"reachable": False, "login_form_found": False, "diagnostic": error.diagnostic()}


async def connection() -> dict:
    return await asyncio.to_thread(_connection)
