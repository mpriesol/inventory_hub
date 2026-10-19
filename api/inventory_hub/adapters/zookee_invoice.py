"""Zookee PDF invoices: validate embedded ISDOC, retain EUR net costs and EANs."""
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import io
import re

from lxml import etree
from pypdf import PdfReader

from inventory_hub.adapters.sk_profi_bike_invoice import Invoice, InvoiceLine, InvoiceParseError

NS = {"i": "http://isdoc.cz/namespace/2013"}


def parse_isdoc(content):
    try:
        tree = etree.parse(io.BytesIO(content), etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
        root = tree.getroot()
        if tree.docinfo.doctype or root.tag != "{" + NS["i"] + "}Invoice":
            raise ValueError("Unsupported ISDOC")

        def text(node, path, *, optional=False):
            nodes = node.findall("/".join("i:" + part for part in path.split("/")), NS)
            if not nodes and optional:
                return ""
            if len(nodes) != 1 or not (nodes[0].text or "").strip():
                raise ValueError("Missing or ambiguous field: " + path)
            return nodes[0].text.strip()

        def number(node, path):
            value = Decimal(text(node, path))
            if not value.is_finite() or value < 0 or value >= Decimal("1000000000"):
                raise ValueError("Invalid amount")
            return value

        if (text(root, "DocumentType") != "1"
                or text(root, "AccountingSupplierParty/Party/PartyIdentification/ID") != "26001217"):
            raise ValueError("Not a Zookee sales invoice")
        foreign = text(root, "ForeignCurrencyCode", optional=True)
        local = text(root, "LocalCurrencyCode")
        if (foreign or local) != "EUR":
            raise ValueError("Receiving supports EUR invoice costs only")
        suffix = "Curr" if foreign else ""
        invoice_no = text(root, "ID")
        if not re.fullmatch(r"[0-9]{1,40}", invoice_no):
            raise ValueError("Unsupported invoice number")
        issued = date.fromisoformat(text(root, "IssueDate"))
        due = date.fromisoformat(text(root, "PaymentMeans/Payment/Details/PaymentDueDate"))
        lines = []
        for index, node in enumerate(root.findall("i:InvoiceLines/i:InvoiceLine", NS), 1):
            if text(node, "ID") != str(index):
                raise ValueError("Missing or duplicate invoice line")
            sku = text(node, "Item/SellersItemIdentification/ID")
            title = text(node, "Item/Description")
            ean = text(node, "Item/CatalogueItemIdentification/ID", optional=True)
            if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/\-]{0,99}", sku)
                    or len(title) > 2000 or (ean and not re.fullmatch(r"\d{8,14}", ean))):
                raise ValueError("Invalid product identity")
            qty = number(node, "InvoicedQuantity")
            if not qty or qty != qty.quantize(Decimal("0.001")):
                raise ValueError("Unsupported quantity")
            net = number(node, "LineExtensionAmount" + suffix)
            gross = number(node, "LineExtensionAmountTaxInclusive" + suffix)
            vat = number(node, "ClassifiedTaxCategory/Percent")
            price = (net / qty).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            money = lambda value: value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if (vat > 100 or money(qty * price) != net or money(net * (1 + vat / 100)) != gross):
                raise ValueError("Line amounts or precision disagree")
            lines.append(InvoiceLine(index, sku, title, qty, price, vat, net, gross, ean))
        if not 0 < len(lines) <= 10000:
            raise ValueError("Missing or oversized invoice lines")
        net = number(root, "LegalMonetaryTotal/TaxExclusiveAmount" + suffix)
        gross = number(root, "LegalMonetaryTotal/TaxInclusiveAmount" + suffix)
        # Receiving values use exact 4dp unit costs. Their aggregate must still
        # round to the original billed net amount, including discounted lines.
        if (sum(line.net for line in lines) != net or sum(line.gross for line in lines) != gross
                or money(sum(line.qty * line.price for line in lines)) != net):
            raise ValueError("Invoice totals disagree")
        return Invoice(invoice_no, issued, due, net, gross - net, gross, tuple(lines))
    except (ValueError, InvalidOperation, etree.LxmlError, OverflowError):
        raise InvoiceParseError("ISDOC faktúry Zookee má nepodporované alebo nesúhlasné údaje. Očakáva sa úplná EUR faktúra s položkami a nákupnými cenami bez DPH.") from None


def parse_invoice_pdf(content):
    if not content.startswith(b"%PDF-") or len(content) > 20 * 1024 * 1024:
        raise InvoiceParseError("Očakáva sa pôvodné PDF Zookee s vloženým ISDOC do 20 MB.")
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 50:
            raise ValueError("Unsupported PDF")
        attachments = [data for name, values in reader.attachments.items()
                       if name.lower().endswith(".isdoc") for data in values]
        if len(attachments) != 1 or len(attachments[0]) > 10 * 1024 * 1024:
            raise ValueError("One ISDOC required")
    except Exception:
        raise InvoiceParseError("PDF Zookee neobsahuje jeden čitateľný ISDOC. Nahrajte pôvodnú faktúru; obrazový sken nie je podporovaný.") from None
    return parse_isdoc(attachments[0])
