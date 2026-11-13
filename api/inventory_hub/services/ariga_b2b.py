"""Bounded ARIGA catalogue reads. Only login and listing pagination may POST.

Credentials/cookies/account HTML remain in the ephemeral session. The saved
snapshot contains product facts only and is committed only after a full crawl.
"""
from __future__ import annotations

import os
import re
import time
from urllib.parse import parse_qs, urljoin, urlsplit

from lxml import etree
import requests

from inventory_hub.adapters.ariga_catalog import (
    ORIGIN, PRODUCT_PATH, CATEGORY_PATH, authenticated, document, nodes, product_evidence, text,
)
from inventory_hub.services.catalog import CatalogError

LOGIN_URL = ORIGIN + "/cart.php?act=login"
HOME_URL = ORIGIN + "/index.php"
MAX_SECONDS = 900
MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_PRODUCTS = 2000
MAX_CATEGORIES = 40
MAX_PAGES = 250


def _error(code="ariga_b2b_invalid_response", status=422):
    messages = {
        "ariga_credentials_missing": "Save the ARIGA B2B email and password in the supplier product source.",
        "ariga_auth_failed": "ARIGA did not confirm the B2B login. Check the saved credentials.",
        "ariga_b2b_invalid_response": "ARIGA returned an incomplete or changed page; the previous catalogue was preserved.",
        "ariga_b2b_unavailable": "ARIGA could not be read. No automatic retry was made.",
        "ariga_b2b_timeout": "ARIGA exceeded the catalogue read time limit; the previous catalogue was preserved.",
        "ariga_currency_invalid": "ARIGA did not confirm EUR prices.",
    }
    return CatalogError(code, messages[code], status)


def configured(source):
    auth = (source.get("remote") or {}).get("auth") or {}
    return all(isinstance(auth.get(key), str) and auth[key].strip() for key in ("username", "password"))


def _url(url, kind):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "b2b.ariga.cz" or parsed.fragment or
                any(c in url for c in ("\\", "\r", "\n")) or "%" in parsed.path):
            raise ValueError
        query = parse_qs(parsed.query, keep_blank_values=True)
        if any(len(values) != 1 for values in query.values()):
            raise ValueError
        if kind == "product":
            valid = PRODUCT_PATH.fullmatch(parsed.path) and not query
        elif kind == "category":
            valid = CATEGORY_PATH.fullmatch(parsed.path) and set(query) <= {"sort", "page"} and all(
                re.fullmatch(r"[a-zA-Z0-9_-]{1,30}", values[0]) for values in query.values())
        elif kind == "login":
            valid = parsed.path == "/cart.php" and query.get("act") == ["login"] and set(query) <= {"act", "cartredir"}
        elif kind == "currency":
            valid = parsed.path == "/switch.php" and query.get("currency") == ["EUR"] and set(query) <= {"r", "currency"}
            valid = valid and query.get("r", ["/index.php"])[0] in ("/", "/index.php")
        else:
            valid = kind == "home" and parsed.path in ("/", "/index.php") and not query
        if not valid:
            raise ValueError
    except ValueError:
        raise _error() from None
    return url


class Session:
    def __init__(self, client, deadline, size_limit):
        self.client, self.deadline, self.size_limit = client, deadline, size_limit
        self.last_request, self.bytes_read = 0.0, 0

    def read(self, url, kind, data=None):
        if data is not None and kind not in {"login", "category"}:
            raise _error()
        method = "POST" if data is not None else "GET"
        for redirect in range(4):
            _url(url, kind)
            delay = max(0, .5 - (time.monotonic() - self.last_request))
            if time.monotonic() + delay >= self.deadline:
                raise _error("ariga_b2b_timeout", 504)
            if delay:
                time.sleep(delay)
            self.last_request = time.monotonic()
            try:
                with self.client.request(method, url, data=data, allow_redirects=False, stream=True,
                        timeout=(10, min(25, max(.1, self.deadline - time.monotonic()))),
                        headers={"Origin": ORIGIN, "Referer": url} if method == "POST" else None) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        if redirect == 3 or (method == "POST" and response.status_code in (307, 308)):
                            raise _error()
                        target = urljoin(url, response.headers.get("Location", ""))
                        # Never forward a password on a redirect or follow a
                        # catalogue link to an account, basket or action URL.
                        if kind in {"login", "currency"}:
                            _url(target, "home")
                            kind = "home"
                        else:
                            _url(target, kind)
                            if urlsplit(target).path != urlsplit(url).path:
                                raise _error()
                        url, method, data = target, "GET", None
                        continue
                    if response.status_code != 200:
                        raise _error("ariga_b2b_unavailable", 502)
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    if content_type not in {"text/html", "application/xhtml+xml"}:
                        raise _error()
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        self.bytes_read += len(chunk)
                        if size > MAX_PAGE_BYTES or self.bytes_read > self.size_limit:
                            raise _error()
                        if time.monotonic() >= self.deadline:
                            raise _error("ariga_b2b_timeout", 504)
                        chunks.append(chunk)
                    try:
                        return document(b"".join(chunks)), url
                    except (ValueError, etree.ParserError):
                        raise _error() from None
            except requests.RequestException:
                # Do not expose requests' URLs, request bodies or server text.
                raise _error("ariga_b2b_unavailable", 502) from None
        raise _error()


def login(session, auth):
    doc, url = session.read(LOGIN_URL, "login")
    forms = doc.xpath("//form[.//input[@name='username'] and .//input[@name='password']]")
    forms = [form for form in forms if any(text(button) == "Přihlásit se" for button in form.xpath(".//button"))]
    if len(forms) != 1:
        raise _error("ariga_auth_failed")
    form = forms[0]
    if form.get("method", "").lower() != "post":
        raise _error("ariga_auth_failed")
    payload = {field.get("name"): field.get("value", "") for field in form.xpath(".//input[@type='hidden' and @name]")}
    for button in form.xpath(".//button[@name]"):
        payload[button.get("name")] = button.get("value", "")
    if "act" in payload and payload["act"] != "login":
        raise _error("ariga_auth_failed")
    target = urljoin(url, form.get("action") or url)
    if target == ORIGIN + "/cart.php" and payload.get("act") == "login":
        target = LOGIN_URL
    target = _url(target, "login")
    payload.update(username=auth["username"], password=auth["password"])
    doc, _ = session.read(target, "login", payload)
    if not authenticated(doc):
        raise _error("ariga_auth_failed")
    # Currency switch is taken from the rendered site's own link. Always return
    # to home, not an arbitrary page-selected redirect destination.
    doc, _ = session.read(HOME_URL, "home")
    links = [urljoin(ORIGIN, a.get("href")) for a in doc.xpath("//a[@href]")
             if text(a) == "Euro"]
    if not links:
        raise _error("ariga_currency_invalid")
    target = _url(links[0], "currency")
    doc, _ = session.read(target, "currency")
    if not authenticated(doc) or not any(text(a) == "Měna: €" for a in doc.xpath("//a")):
        raise _error("ariga_currency_invalid")
    return doc


def _listing(doc, url):
    if not authenticated(doc):
        raise _error("ariga_auth_failed")
    links = list(dict.fromkeys(_url(urljoin(ORIGIN, a.get("href")), "product")
        for a in nodes(doc, "product-name") if a.tag == "a"))
    counts = set(re.findall(r"Zobrazeno\s+\d+\s+produktů z\s+(\d+)", text(doc)))
    if len(counts) != 1:
        raise _error()
    total = int(counts.pop())
    if total > MAX_PRODUCTS or (total and not links):
        raise _error()
    forms = nodes(doc, "js-products-list-more")
    next_page = None
    if forms:
        if len(forms) != 1 or forms[0].get("method", "").lower() != "post":
            raise _error()
        fields = {field.get("name"): field.get("value", "") for field in forms[0].xpath(".//input[@name]")}
        if set(fields) - {"page", "diplay_mode"} or not re.fullmatch(r"[1-9][0-9]*", fields.get("page", "")):
            raise _error()
        target = _url(urljoin(url, forms[0].get("action") or url), "category")
        if urlsplit(target).path != urlsplit(url).path:
            raise _error()
        next_page = target, fields
    return links, total, next_page


def collect(session, home):
    categories = []
    for nav in nodes(home, "navigation-categories"):
        for a in nav.xpath("./li/div/a[@href] | ./li/a[@href]"):
            if CATEGORY_PATH.fullmatch(urlsplit(a.get("href")).path):
                categories.append(_url(urljoin(ORIGIN, a.get("href")), "category"))
    categories = list(dict.fromkeys(categories))
    if not 1 <= len(categories) <= MAX_CATEGORIES:
        raise _error()
    products, page_count = {}, 0
    for category in categories:
        doc, current_url = session.read(category, "category")
        visited, seen_pages, expected = set(), set(), None
        while True:
            page_count += 1
            if page_count > MAX_PAGES:
                raise _error()
            links, total, next_page = _listing(doc, current_url)
            if expected is not None and total != expected:
                raise _error()
            expected = total
            new = set(links) - visited
            if (total and not new) or len(visited | set(links)) > total:
                raise _error()
            visited.update(links)
            for link in links:
                product_id = PRODUCT_PATH.fullmatch(urlsplit(link).path)[1]
                if product_id in products and products[product_id] != link:
                    raise _error()
                products[product_id] = link
            if len(products) > MAX_PRODUCTS:
                raise _error()
            if len(visited) == total:
                break
            if not next_page or next_page[1]["page"] in seen_pages:
                raise _error()
            seen_pages.add(next_page[1]["page"])
            doc, current_url = session.read(next_page[0], "category", next_page[1])
    if not products:
        raise _error()
    root = etree.Element("ariga_catalog", version="1", currency="EUR", complete="true")
    seen = set()
    for url in products.values():
        doc, final_url = session.read(url, "product")
        try:
            records = product_evidence(doc, final_url)
        except (ValueError, etree.LxmlError):
            raise _error() from None
        for record in records:
            code = record.findtext("code")
            if code in seen:
                raise _error()
            seen.add(code)
            root.append(record)
    root.set("items", str(len(root)))
    return etree.tostring(root, encoding="utf-8", xml_declaration=True)


def download(source, destination, *, max_elapsed_seconds=None, max_size_bytes=100 * 1024 * 1024):
    if not configured(source):
        raise _error("ariga_credentials_missing")
    temporary = destination.with_suffix(".part")
    auth = source["remote"]["auth"]
    try:
        with requests.Session() as client:
            client.headers.update({"User-Agent": "InventoryHub-Ariga-ReadOnly/1.0",
                "Accept": "text/html,application/xhtml+xml", "Accept-Language": "cs-CZ,cs;q=0.9"})
            duration = min(MAX_SECONDS, max_elapsed_seconds if max_elapsed_seconds is not None else MAX_SECONDS)
            session = Session(client, time.monotonic() + duration, max_size_bytes)
            snapshot = collect(session, login(session, auth))
            if len(snapshot) > max_size_bytes:
                raise _error()
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(snapshot)
        temporary.replace(destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)
