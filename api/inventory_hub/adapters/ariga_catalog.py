"""ARIGA's observed B2B HTML, reduced to a product-only XML snapshot.

The catalogue code is supplier evidence, not a verified manufacturer MPN.
Neither a basket quantity nor the text 'Skladem' is a stock count.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import re
from urllib.parse import urljoin, urlsplit

from lxml import etree, html

from inventory_hub.adapters.paul_lange_catalog import _source_fields
from inventory_hub.catalog_types import CatalogParameter, CatalogPrices, CatalogProduct
from inventory_hub.services.catalog_html import clean_description
from inventory_hub.supplier_prefix import canonical_supplier_sku, get_supplier_prefix

ORIGIN = "https://b2b.ariga.cz"
PRODUCT_PATH = re.compile(r"^/(?:[^/?#]+/)+prod_([1-9][0-9]*)\.html$")
CATEGORY_PATH = re.compile(r"^/(?:[^/?#]+/)+cat_([1-9][0-9]*)\.html$")


def nodes(root, class_name):
    return root.xpath(
        ".//*[contains(concat(' ', normalize-space(@class), ' '), $token)]", token=f" {class_name} ")


def text(element):
    return " ".join(element.text_content().split()) if element is not None else ""


def document(raw):
    doc = html.fromstring(raw, parser=html.HTMLParser(no_network=True, encoding="utf-8"))
    title = " ".join(doc.xpath("//title/text()")).casefold()
    if any(word in title for word in ("captcha", "just a moment", "access denied", "attention required")) or doc.xpath(
        "//*[@id='challenge-form' or @id='cf-challenge-running']"
    ):
        raise ValueError("ARIGA verification challenge")
    return doc


def authenticated(doc):
    return any(urlsplit(a.get("href", "")).path == "/cart.php" and
               "act=account" in a.get("href", "") for a in doc.xpath("//a[@href]"))


def image_url(value):
    value = urljoin(ORIGIN, value or "")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.netloc != "b2b.ariga.cz" or
            not parsed.path.startswith("/images/uploads/") or parsed.query or parsed.fragment):
        return None
    return value


def money(value):
    """Only the explicitly rendered EUR amount; never currency conversion."""
    token = re.sub(r"\s+", "", value)
    if not re.fullmatch(r"\d+(?:,\d{2})?€", token):
        raise ValueError("Invalid ARIGA EUR price")
    amount = Decimal(token[:-1].replace(",", "."))
    if not 0 < amount < 10000000:
        raise ValueError("Invalid ARIGA EUR price")
    return format(amount, ".2f")


def _add(parent, name, value):
    if value is not None:
        etree.SubElement(parent, name).text = str(value)


def product_evidence(doc, url):
    """Extract all explicit variants from one page, never the recommendation list."""
    match = PRODUCT_PATH.fullmatch(urlsplit(url).path)
    details = nodes(doc, "product-detail")
    if not match or len(details) != 1 or not authenticated(doc):
        raise ValueError("Invalid ARIGA product page")
    detail = details[0]
    headings = detail.xpath(".//h1")
    if len(headings) != 1 or not 0 < len(text(headings[0])) <= 500:
        raise ValueError("Invalid ARIGA product name")
    name = text(headings[0])
    fields, codes = {}, {}
    for section in nodes(detail, "parameter-details"):
        label = (section.text or "").strip()
        for child in section:
            if child.tag == "strong":
                value = text(child)
                if "js-product-variant-code" in (child.get("class") or "").split():
                    key = child.get("data-product-variant-id")
                    if not key or key in codes:
                        raise ValueError("Ambiguous ARIGA variant code")
                    codes[key] = value
                else:
                    fields[label] = value
            elif child.tag == "br":
                label = ""
            label += child.tail or ""
            label = " ".join(label.split())
    for param in nodes(detail, "parameter"):
        labels, values = nodes(param, "parameter-label"), nodes(param, "parameter-value")
        if len(labels) == len(values) == 1:
            fields[text(labels[0])] = text(values[0])
    choices = nodes(detail, "js-prod-detail-variants-choose")
    if len(choices) > 1:
        raise ValueError("Unsupported ARIGA variant axes")
    options = {}
    if choices:
        for option in choices[0].xpath("./option"):
            key = option.get("value")
            if not key or key in options or not text(option):
                raise ValueError("Invalid ARIGA variant option")
            options[key] = text(option)
        labels = choices[0].getparent().xpath("./label")
        if len(labels) != 1 or not text(labels[0]) or set(options) != set(codes):
            raise ValueError("Incomplete ARIGA variant identities")
        axis = text(labels[0])
        if fields.get("Počet variant") != str(len(options)):
            raise ValueError("Incomplete ARIGA variant count")
    else:
        if codes or fields.get("Počet variant") not in (None, "1"):
            raise ValueError("Missing ARIGA variant selector")
        codes = {match[1]: fields.get("Kód zboží", "")}
        axis = None
    prices = nodes(detail, "product-detail-prices")
    if len(prices) != len(codes) or {p.get("data-product-variant-id") for p in prices} != set(codes):
        raise ValueError("Incomplete ARIGA variant prices")
    if any(not code or len(code) > 100 or any(ord(c) < 32 for c in code) for code in codes.values()):
        raise ValueError("Invalid ARIGA supplier code")
    articles = doc.xpath("//article[contains(concat(' ', normalize-space(@class), ' '), ' article ')]")
    if len(articles) != 1:
        raise ValueError("Missing ARIGA product description")
    article = deepcopy(articles[0])
    gallery = list(dict.fromkeys(image_url(a.get("href")) for a in nodes(article, "js-open-popup-gallery")))
    for element in nodes(article, "slideshow-wrap"):
        element.drop_tree()
    description = clean_description(html.tostring(article, encoding="unicode"))
    crumbs = nodes(doc, "breadcrumb")
    category_links = crumbs[0].xpath(".//a[@href]") if len(crumbs) == 1 else []
    category = " > ".join(text(a) for a in category_links)
    category_match = CATEGORY_PATH.fullmatch(urlsplit(category_links[-1].get("href")).path) if category_links else None
    records = []
    for block in prices:
        variant_id = block.get("data-product-variant-id")
        item = etree.Element("product", page_id=match[1], variant_id=variant_id)
        for key, value in {"code": codes[variant_id], "name": name, "brand": fields.get("Značka"),
                           "url": url, "description": description, "category": category,
                           "category_code": category_match[1] if category_match else None}.items():
            _add(item, key, value)
        if options:
            _add(item, "group_code", match[1])
            etree.SubElement(item, "variant", name=axis).text = options[variant_id]
        params = etree.SubElement(item, "parameters")
        for key, value in fields.items():
            if key not in {"Kód zboží", "Kód vybrané varianty", "Počet variant", "Značka"}:
                etree.SubElement(params, "parameter", name=key).text = value
        # These are independently labelled price blocks. Never use the prominent
        # wholesale gross amount or a category/listing price as acquisition net.
        seen = set()
        for price in nodes(block, "product-detail-price"):
            spans = price.xpath("./span")
            strong = price.xpath("./strong")
            if spans and text(spans[0]) == "Doporučená MO cena":
                key = "retail_gross"
                if len(strong) != 1 or "s DPH" not in (strong[0].tail or ""):
                    raise ValueError("Invalid ARIGA recommended price")
                amount = money(text(strong[0]))
            elif any(text(n) == "VO cena" for n in nodes(price, "product-detail-amounts")):
                key = "purchase_net"
                if len(spans) != 1 or not text(spans[0]).endswith(" bez DPH"):
                    raise ValueError("Invalid ARIGA wholesale price")
                amount = money(text(spans[0]).removesuffix(" bez DPH"))
            else:
                raise ValueError("Unknown ARIGA price block")
            if key in seen:
                raise ValueError("Ambiguous ARIGA prices")
            seen.add(key)
            _add(item, key, amount)
        availability = nodes(block, "product-detail-availability")
        if len(availability) != 1:
            raise ValueError("Ambiguous ARIGA availability")
        _add(item, "availability", text(availability[0]))
        thumbs = [image_url(a.get("href")) for a in nodes(detail, "js-product-variant-thumb")
                  if a.get("data-product-variant-id") == variant_id]
        if len(thumbs) > 1:
            raise ValueError("Ambiguous ARIGA variant image")
        images = etree.SubElement(item, "images")
        for image in dict.fromkeys(thumbs + gallery):
            if image:
                _add(images, "url", image)
        records.append(item)
    return records


def parse_catalog(path, supplier, cfg, feed_key="products"):
    tree = etree.parse(str(path), etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
    root = tree.getroot()
    if (tree.docinfo.doctype or root.tag != "ariga_catalog" or root.get("version") != "1" or
            root.get("currency") != "EUR" or root.get("complete") != "true" or
            root.get("items") != str(len(root)) or not len(root)):
        raise ValueError("Incomplete ARIGA catalogue snapshot")
    adapter = cfg.get("adapter_settings") or {}
    try:
        vat = Decimal(str(adapter.get("vat_rate", adapter.get("vat", "23"))))
        if not vat.is_finite() or not 0 <= vat <= 100:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid ARIGA destination VAT") from None
    factor, result, seen = 1 + vat / 100, [], set()
    for item in root:
        value = lambda name: item.findtext(name, "")
        code = value("code")
        if item.tag != "product" or not code or code in seen or not value("name"):
            raise ValueError("Invalid or duplicate ARIGA supplier code")
        seen.add(code)
        amounts = {}
        warnings = ["missing_ean", "source_language_cs"]
        for key in ("purchase_net", "retail_gross"):
            raw = value(key)
            if raw:
                if not re.fullmatch(r"\d+\.\d{2}", raw) or not 0 < Decimal(raw) < 10000000:
                    raise ValueError("Invalid ARIGA snapshot price")
                amounts[key] = Decimal(raw)
            else:
                warnings.append("missing_purchase_price" if key == "purchase_net" else "missing_retail_price")
        purchase, retail = amounts.get("purchase_net"), amounts.get("retail_gross")
        availability = value("availability")
        available = True if availability == "Skladem" else False if availability == "Není skladem" else None
        attrs = [CatalogParameter(name=v.get("name"), value=v.text) for v in item.findall("variant")]
        params = [CatalogParameter(name=p.get("name"), value=p.text or "") for p in item.findall("parameters/parameter")]
        images = [u for v in item.findall("images/url") if (u := image_url(v.text))]
        if not images:
            warnings.append("missing_images")
        product = CatalogProduct(supplier=supplier, feed_key=feed_key, code=code,
            shop_code=canonical_supplier_sku(get_supplier_prefix(cfg), code), name=value("name"),
            brand=value("brand") or None, url=value("url"), description=value("description"),
            category=value("category") or None, category_code=value("category_code") or None,
            images=images, parameters=params + attrs, availability=availability or None,
            supplier_available=available, supplier_stock_raw="true" if available is True else "false" if available is False else None,
            group_code=value("group_code") or None, group_name=value("name") if attrs else None,
            variant_attributes=attrs, variant_relationship="explicit" if attrs else "not_provided",
            prices=CatalogPrices(currency="EUR", vat_percent=vat, purchase_net=purchase, retail_gross=retail,
                purchase_gross=(purchase * factor).quantize(Decimal(".01"), rounding=ROUND_HALF_UP) if purchase is not None else None,
                retail_net=(retail / factor).quantize(Decimal(".01"), rounding=ROUND_HALF_UP) if retail is not None else None),
            warnings=warnings)
        xml = etree.tostring(item, encoding="unicode", with_tail=False)
        product.source_hash = hashlib.sha256(xml.encode()).hexdigest()
        result.append((product, {"xml": xml, "fields": _source_fields(item)}))
    return result
