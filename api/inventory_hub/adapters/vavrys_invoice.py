"""Validate the separate Czech and Slovak Vavrys invoice layouts."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
import re

from lxml import etree
from pypdf import PdfReader

from inventory_hub.adapters.sk_profi_bike_invoice import Invoice, InvoiceLine, InvoiceParseError


AMOUNT = r"\d+(?:[ \u00a0]\d{3})*,\d{2}"
DATE = r"\d{1,2}\.\d{1,2}\.\d{4}"
SKU = r"[A-Z][A-Z0-9/-]*\d[A-Z0-9/-]*"
CENT = Decimal(".01")
NAMESPACE = "{http://isdoc.cz/namespace/invoice}"


def decimal(value):
    return Decimal(value.replace(" ", "").replace("\xa0", "").replace(",", "."))


def money(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def required(pattern, value, *, flags=0):
    found = re.search(pattern, value, flags)
    if not found:
        raise InvoiceParseError("invoice_vavrys_pdf_unrecognized")
    return found


def header_date(label, value):
    return datetime.strptime(required(rf"{label}\s+({DATE})", value).group(1), "%d.%m.%Y").date()


@dataclass(frozen=True)
class VavrysInvoice(Invoice):
    issuer_country: str = ''

    @property
    def issuer(self):
        return f"VAVRYS {self.issuer_country} s.r.o."

    @property
    def key(self):
        return f"{self.issuer_country.lower()}-{self.number}"

    @property
    def receiving_lines(self):
        return tuple(line for line in self.lines if line.sku)


def czech_lines(value):
    summary = re.compile(rf"^({SKU})\s+ks\s*(\d+)\s+({AMOUNT})\d{{1,2}},\d{{2}}\n([^\n]+)$", re.M)
    details = re.compile(rf"^({SKU})\n[^\n]+\n(\d+)\nCena\n({AMOUNT})$", re.M)
    row_summary = list(summary.finditer(value))
    row_details = list(details.finditer(value))
    if len(row_summary) != len(row_details):
        raise InvoiceParseError("invoice_vavrys_lines_mismatch")
    rows = []
    for index, (summary_row, detail) in enumerate(zip(row_summary, row_details), 1):
        sku, quantity, total, title = summary_row.groups()
        if detail.group(1, 2) != (sku, quantity):
            raise InvoiceParseError("invoice_vavrys_lines_mismatch")
        qty, net, price = Decimal(quantity), decimal(total), decimal(detail[3])
        if qty <= 0 or abs(price * qty - net) > qty * CENT:
            raise InvoiceParseError("invoice_vavrys_lines_mismatch")
        rows.append(InvoiceLine(index, sku, title, qty, price, Decimal(0), net, net))
    return tuple(rows)


def slovak_lines(value):
    rows = []
    detail_pattern = rf"({SKU})\n([^\n]+)\n([^\n]+)\n\1\n[^\n]+\n(\d+)\ncena\n({AMOUNT})\nCena s DPH\n({AMOUNT})"
    delivery_pattern = rf"(Poštovné|Doprava)\n([^\n]+)\n[^\n]+\n(\d+)\ncena\n({AMOUNT})\nCena s DPH\n({AMOUNT})"
    for match in re.finditer(rf"^(?:{detail_pattern}|{delivery_pattern})$", value, re.M | re.I):
        if match[1]:
            sku, name, summary, quantity, price_text, with_tax = match.groups()[:6]
        else:
            name, summary, quantity, price_text, with_tax = match.groups()[6:]
            sku = ""
        rate, line_net = required(rf"^(\d{{1,2}}){quantity}\s+({AMOUNT})\s*ks", summary).groups()
        qty, price, net, gross = Decimal(quantity), decimal(price_text), decimal(line_net), decimal(with_tax)
        if qty <= 0 or price * qty != net:
            raise InvoiceParseError("invoice_vavrys_lines_mismatch")
        rows.append(InvoiceLine(len(rows) + 1, sku, name, qty, price, Decimal(rate), net, qty * gross))
    return tuple(rows)


def parse_invoice_text(value, header=None):
    text = "\n".join(line.strip() for line in value.splitlines() if line.strip())
    header = header or text
    # Identify the issuer above the delivery addresses and line descriptions.
    supplier = required(r"^\s*VAVRYS (CZ|SK) s\.r\.o\.", header[:5000], flags=re.M | re.I)[1].upper()
    number = required(r"Variabiln[ýí] symbol:\s*(\d{5,30})", header)[1]
    due = header_date(r"Dátum splatnosti:?", header)
    if supplier == "SK":
        issue = header_date(r"Dátum vystavenia:", header)
        rows = slovak_lines(text)
        count = int(required(r"Po[cč]et položiek:\s*(\d+)", text, flags=re.I)[1])
        gross = decimal(required(rf"({AMOUNT})\s*Celkom za faktúru zaokrúhlené EUR", text)[1])
        net = sum((row.net for row in rows), Decimal(0))
        # Validate the printed tax recap, allowing its compact PDF column order.
        recap = required(r"^Základná sadzba dane\s+(\d{1,2})\s+(.+)$", text, flags=re.M)
        vat = money(net * Decimal(recap[1]) / 100)
        numeric = recap[2].replace(" ", "")
        printed = lambda amount: f"{amount:.2f}".replace(".", ",")
        if (count != len(rows) or any(row.vat != Decimal(recap[1]) for row in rows)
                or net + vat != gross or not all(printed(amount) in numeric for amount in (net, vat, gross))):
            raise InvoiceParseError("invoice_vavrys_totals_mismatch")
    else:
        issue = header_date(r"\bDátum", header)
        rows = czech_lines(text)
        net = gross = money(decimal(required(r"CELKOM\[EUR\]\s*(\d+,\d{2,4})", text)[1]))
        vat = Decimal(0)
        displayed = sum((row.net for row in rows), Decimal(0))
        # Czech PDF prints rounded item values and a four-decimal document total.
        if abs(displayed - net) > len(rows) * CENT:
            raise InvoiceParseError("invoice_vavrys_totals_mismatch")
    if not rows:
        raise InvoiceParseError("invoice_vavrys_lines_mismatch")
    return VavrysInvoice(number, issue, due, net, vat, gross, rows, supplier)


def parse_invoice_pdf(content):
    if not content.startswith(b"%PDF-") or len(content) > 20 * 1024 * 1024:
        raise InvoiceParseError("invoice_vavrys_pdf_unrecognized")
    try:
        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 50:
            raise InvoiceParseError("invoice_vavrys_pdf_unrecognized")
        pages = [page.extract_text() for page in reader.pages]
        if any(not page.strip() for page in pages):
            raise InvoiceParseError("invoice_vavrys_pdf_unrecognized")
        return parse_invoice_text("\n".join(pages), reader.pages[0].extract_text(extraction_mode="layout"))
    except InvoiceParseError:
        raise
    except Exception:
        raise InvoiceParseError("invoice_vavrys_pdf_unrecognized") from None


def parse_isdoc(content):
    """Read supplementary original facts, prioritizing the tax recap."""
    if len(content) > 5 * 1024 * 1024:
        raise InvoiceParseError("invoice_vavrys_isdoc_invalid")
    root = etree.fromstring(content, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    if root.tag != NAMESPACE + "Invoice" or root.getroottree().docinfo.doctype:
        raise InvoiceParseError("invoice_vavrys_isdoc_invalid")
    find = lambda node, path: node.findtext("/".join(NAMESPACE + key for key in path.split("/")))
    party = root.find(NAMESPACE + "AccountingSupplierParty")
    issuer = required(r"^VAVRYS (CZ|SK) s\.r\.o\.", find(party, "Party/PartyName/Name"), flags=re.I)[1].upper()
    number = find(root, "ID")
    if not number or not re.fullmatch(r"\d{5,30}", number) or find(root, "DocumentType") != "1":
        raise InvoiceParseError("invoice_vavrys_isdoc_invalid")
    currency = find(root, "ForeignCurrencyCode") or find(root, "LocalCurrencyCode")
    if currency != "EUR":
        raise InvoiceParseError("invoice_vavrys_isdoc_invalid")
    totals = tuple(money(Decimal(find(root, "LegalMonetaryTotal/" + field))) for field in ("TaxExclusiveAmount", "TaxInclusiveAmount"))
    recaps = root.findall(f"{NAMESPACE}TaxTotal/{NAMESPACE}TaxSubTotal")
    vat = sum((Decimal(find(row, "TaxAmount")) for row in recaps), Decimal(0))
    if not recaps or totals[0] + vat != totals[1]:
        raise InvoiceParseError("invoice_vavrys_isdoc_invalid")
    lines = []
    for row in root.findall(f"{NAMESPACE}InvoiceLines/{NAMESPACE}InvoiceLine"):
        sku = find(row, "Item/SecondarySellersItemIdentification/ID") or find(row, "Item/SellersItemIdentification/ID") or ""
        if (find(row, "Item/Description") or "").strip().casefold() in ("poštovné", "doprava"):
            sku = ""
        lines.append((sku, Decimal(find(row, "InvoicedQuantity")), Decimal(find(row, "LineExtensionAmount"))))
    return {"issuer_country": issuer, "invoice_number": number, "currency": currency,
            "total_without_vat": totals[0], "total_with_vat": totals[1], "vat_amount": vat, "lines": lines}
