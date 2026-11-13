"""Bounded, read-only ASP.NET catalogue traversal. No order controls are posted."""
from dataclasses import dataclass
from decimal import Decimal
import re
import time
from urllib.parse import urljoin, urlsplit

from lxml import etree, html
import requests

from inventory_hub.adapters.husky_catalog import money


ORIGIN = "https://b2b.huskysk.sk"
LOGIN = ORIGIN + "/login"
LISTING = ORIGIN + "/objednavky"
GRID = "ctl00$MainContent$zbozi$grdList"
SEARCH = "ctl00$TopSiteBoxContent$txtNazevQuery"
SEARCH_BUTTON = "ctl00$TopSiteBoxContent$btnOk"
BRANCH = "ctl00$loginInfo$lvLoginView$cboPobocka"
LISTING_STATE = "ctl00$ctl33"
LOGIN_PREFIX = "ctl00$ctl00$MainContent$MainContent$loginPanel$lgLogin$"
MAX_HTML_BYTES = 2_000_000
MAX_PAGES = 250


class HuskyError(Exception):
    def __init__(self, code="husky_b2b_structure_changed"):
        super().__init__(code)
        self.code = code


def validate_options(options):
    if (not isinstance(options, dict) or type(options.get("b2b_enabled", False)) is not bool
            or options.get("purchase_price_basis", "unknown") not in ("unknown", "net", "gross")):
        raise HuskyError("husky_price_basis_invalid")
    for key, limit in (("username", 200), ("password", 500)):
        value = options.get(key, "")
        if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
            raise HuskyError("husky_credentials_missing")
        if options.get("b2b_enabled") and not value.strip():
            raise HuskyError("husky_credentials_missing")


@dataclass(frozen=True)
class Quote:
    code: str
    stock: str
    available: bool | None
    quantity: Decimal | None
    kind: str
    base_price: Decimal | None
    purchase_price: Decimal | None


def stock_value(raw):
    if raw in ("", "-", "—"):
        return None, None, "unknown"
    match = re.fullmatch(r"(>)?\s*(\d{1,8})\s*ks", raw)
    if not match:
        raise HuskyError("husky_b2b_invalid_stock")
    number = Decimal(match[2])
    # Whole pieces: >10 proves at least 11, never an exact quantity.
    if match[1]:
        return True, number + 1, "minimum"
    return number > 0, number, "exact"


def text(element):
    return " ".join(element.text_content().split())


def safe_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "b2b.huskysk.sk"
            or parsed.path not in ("/", "/login", "/objednavky")
            or parsed.query or parsed.fragment):
        raise HuskyError("husky_b2b_unsafe_redirect")
    return url


def document(raw):
    try:
        doc = html.fromstring(raw, parser=html.HTMLParser(no_network=True))
    except (ValueError, etree.ParserError):
        raise HuskyError() from None
    if doc.xpath("//*[@id='challenge-form' or @id='cf-challenge-running']"):
        raise HuskyError("husky_b2b_auth_failed")
    return doc


def form_data(doc, url):
    forms = doc.xpath("//form")
    if len(forms) != 1 or urljoin(url, forms[0].get("action", "")) != url:
        raise HuskyError()
    data = {}
    for node in forms[0].xpath(".//input[@type='hidden']"):
        name = node.get("name", "")
        if (re.fullmatch(r"__(?:VIEWSTATE\d*|VIEWSTATEFIELDCOUNT|VIEWSTATEGENERATOR|VIEWSTATEENCRYPTED|EVENTVALIDATION|CSRFTOKEN|EVENTTARGET|EVENTARGUMENT|LASTFOCUS)", name)
                or (url == LISTING and name == LISTING_STATE)):
            if name in data:
                raise HuskyError()
            data[name] = node.get("value", "")
    if "__VIEWSTATE" not in data:
        raise HuskyError()
    data.update(__EVENTTARGET="", __EVENTARGUMENT="", __LASTFOCUS="")
    return data


def listing_post(doc):
    data = form_data(doc, LISTING)
    # Match the browser's read-only request context, including the empty
    # placeholder. Never pick another branch or copy cart/order controls.
    branches = doc.xpath("//select[@name=$name]", name=BRANCH)
    if len(branches) != 1:
        raise HuskyError()
    options = branches[0].xpath("./option")
    selected = [option for option in options if option.get("selected") is not None] or options[:1]
    if len(selected) != 1 or selected[0].get("value") is None:
        raise HuskyError()
    data[BRANCH] = selected[0].get("value")
    # Preserve only the catalogue tree. Never copy quantity, cart or order inputs.
    for node in doc.xpath("//input[@type='checkbox']"):
        name = node.get("name", "")
        if re.fullmatch(r"ctl00\$LeftSiteBoxContent\$catalog\$trvCatalogTreen\d+CheckBox", name):
            data[name] = node.get("value", "on")
    data[SEARCH] = ""
    return data


def parse_page(doc, page):
    if doc.xpath("//input[@type='password']") or not doc.xpath("//*[@id='loginInfo_lvLoginView_lsLoginStatusLoggedIn']"):
        raise HuskyError("husky_b2b_auth_failed")
    tables = doc.xpath("//table[@id='MainContent_zbozi_grdList']")
    if len(tables) != 1:
        raise HuskyError()
    table = tables[0]
    headers = [text(node) for node in table.xpath("./tr/th|./tbody/tr/th")]
    required = ("Názov tovaru", "Reg. číslo", "Skladom", "Cena (EUR)", "Cena so zľavou (EUR)", "Objednať")
    if tuple(headers[-6:]) != required or len(headers) not in (6, 7):
        raise HuskyError()
    next_pages = set()
    for anchor in table.xpath(".//a[contains(@href, 'Page$')]"):
        match = re.fullmatch(r"javascript:__doPostBack\('ctl00\$MainContent\$zbozi\$grdList','Page\$(\d+)'\)", anchor.get("href", ""))
        if not match:
            raise HuskyError()
        next_pages.add(int(match[1]))
    if next_pages or page > 1:
        current = table.xpath("./tr[td/table]//span/text()|./tbody/tr[td/table]//span/text()")
        if set(value.strip() for value in current if value.strip()) != {str(page)}:
            raise HuskyError("husky_b2b_incomplete")
    rows = []
    for row in table.xpath("./tr|./tbody/tr"):
        cells = row.findall("td")
        if len(cells) <= 1 or row.findall("th"):
            continue
        if len(cells) != len(headers):
            raise HuskyError()
        values = dict(zip(headers, (text(cell) for cell in cells)))
        code, raw = values["Reg. číslo"], values["Skladom"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/+-]{0,99}", code):
            raise HuskyError("husky_b2b_invalid_identity")
        available, number, kind = stock_value(raw)
        try:
            base, purchase = money(values["Cena (EUR)"]), money(values["Cena so zľavou (EUR)"])
        except ValueError:
            raise HuskyError("husky_b2b_invalid_price") from None
        rows.append(Quote(code, raw, available, number, kind, base, purchase))
    if not 0 < len(rows) <= 60 or (any(p > page for p in next_pages) and page + 1 not in next_pages):
        raise HuskyError("husky_b2b_incomplete")
    return rows, page + 1 in next_pages


class Session:
    def __init__(self, client, deadline):
        self.client, self.deadline, self.last_request = client, deadline, 0

    def read(self, url, data=None):
        method = "POST" if data is not None else "GET"
        for redirect in range(4):
            safe_url(url)
            delay = max(0, 1 - (time.monotonic() - self.last_request))
            remaining = self.deadline - time.monotonic() - delay
            if remaining <= 0:
                raise HuskyError("husky_b2b_timeout")
            time.sleep(delay)
            self.last_request = time.monotonic()
            try:
                with self.client.request(method, url, data=data, allow_redirects=False, stream=True,
                        timeout=(min(10, remaining), min(20, remaining)),
                        headers={"Origin": ORIGIN, "Referer": url} if method == "POST" else None) as response:
                    if response.status_code in (301, 302, 303):
                        if redirect == 3:
                            raise HuskyError("husky_b2b_unsafe_redirect")
                        url = safe_url(urljoin(url, response.headers.get("Location", "")))
                        method, data = "GET", None
                        continue
                    if response.status_code != 200:
                        raise HuskyError("husky_b2b_auth_failed" if response.status_code in (401, 403) else
                                         "husky_b2b_rate_limited" if response.status_code == 429 else "husky_b2b_unavailable")
                    if "text/html" not in response.headers.get("Content-Type", "").lower():
                        raise HuskyError()
                    raw = bytearray()
                    for chunk in response.iter_content(65536):
                        if time.monotonic() > self.deadline:
                            raise HuskyError("husky_b2b_timeout")
                        raw.extend(chunk)
                        if len(raw) > MAX_HTML_BYTES:
                            raise HuskyError("husky_b2b_too_large")
                    return document(bytes(raw))
            except requests.RequestException:
                raise HuskyError("husky_b2b_unavailable") from None
        raise HuskyError()


def fetch_quotes(username, password, *, deadline=None):
    if not username or not password:
        raise HuskyError("husky_credentials_missing")
    with requests.Session() as client:
        client.headers.update({"User-Agent": "InventoryHub-Husky-ReadOnly/1.0", "Accept-Language": "sk-SK,sk;q=0.9"})
        session = Session(client, deadline or time.monotonic() + 480)
        login = session.read(LOGIN)
        data = form_data(login, LOGIN)
        for field in ("UserName", "Password", "LoginButton"):
            if len(login.xpath("//input[@name=$name]", name=LOGIN_PREFIX + field)) != 1:
                raise HuskyError()
        data.update({LOGIN_PREFIX + "UserName": username, LOGIN_PREFIX + "Password": password,
                     LOGIN_PREFIX + "LoginButton": login.xpath("//input[@name=$name]", name=LOGIN_PREFIX + "LoginButton")[0].get("value", "")})
        signed_in = session.read(LOGIN, data)
        if signed_in.xpath("//input[@type='password']"):
            raise HuskyError("husky_b2b_auth_failed")
        doc = session.read(LISTING)
        # Explicitly clear search and both filters; a persisted filter is not a full catalog.
        if not doc.xpath("//*[@id='loginInfo_lvLoginView_lsLoginStatusLoggedIn']"):
            raise HuskyError("husky_b2b_auth_failed")
        data = listing_post(doc)
        data[SEARCH_BUTTON] = "OK"
        doc = session.read(LISTING, data)
        output = {}
        for page in range(1, MAX_PAGES + 1):
            rows, has_next = parse_page(doc, page)
            for quote in rows:
                if quote.code in output:
                    raise HuskyError("husky_b2b_duplicate_identity")
                # Never persist a supplier response containing the submitted credentials.
                if username in quote.code or password in quote.code or username in quote.stock or password in quote.stock:
                    raise HuskyError()
                output[quote.code] = quote
            if not has_next:
                return output
            data = listing_post(doc)
            data.update(__EVENTTARGET=GRID, __EVENTARGUMENT=f"Page${page + 1}")
            doc = session.read(LISTING, data)
        raise HuskyError("husky_b2b_incomplete")
