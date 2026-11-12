"""ARIGA's EUR text PDF: verified net goods costs, separate non-stock postage."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import io
import json
import re

from pypdf import PdfReader

from inventory_hub.adapters.sk_profi_bike_invoice import Invoice, InvoiceLine, InvoiceParseError


_AMOUNT = r"(?:[0-9]{1,3}(?: [0-9]{3})+|[0-9]+),[0-9]{2,4}"
_HEADER = re.compile(
    r"^[ \t]*Zboží[ \t]+Kód[ \t]+Cena/Ks[ \t]+Kusů[ \t]+Celkem[ \t]+DPH[ \t]+Celkem[ \t]*\n"
    r"[ \t]*bez DPH[ \t]+bez DPH[ \t]+s DPH[ \t]*$", re.M)
_ROW = re.compile(
    rf"^\s*(?P<title>.+?)\s+(?P<sku>[A-Za-z0-9][A-Za-z0-9._/\-]{{0,95}})\s+"
    rf"(?P<price>{_AMOUNT})\s*€\s+(?P<qty>[0-9]+(?:,[0-9]{{1,3}})?)\s+"
    rf"(?P<net>{_AMOUNT})\s*€\s+(?P<vat>[0-9]+(?:,[0-9]{{1,2}})?)\s*%\s+"
    rf"(?P<gross>{_AMOUNT})\s*€\s*$")
_POSTAGE = re.compile(
    rf"^\s*Poštovné\s+(?P<price>{_AMOUNT})\s*€\s+(?P<net>{_AMOUNT})\s*€\s+"
    rf"(?P<vat>[0-9]+(?:,[0-9]{{1,2}})?)\s*%\s+(?P<gross>{_AMOUNT})\s*€\s*$")
_TOTAL = re.compile(rf"^[ \t]*Celkem[ \t]+({_AMOUNT})[ \t]*€[ \t]+({_AMOUNT})[ \t]*€[ \t]+({_AMOUNT})[ \t]*€[ \t]*$", re.M)


def _number(value):
    return Decimal(value.replace(" ", "").replace(",", "."))


def _money(value):
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Postage:
    price: Decimal
    net: Decimal
    vat: Decimal
    gross: Decimal


@dataclass(frozen=True)
class ArigaInvoice(Invoice):
    # No supplier SKU, EAN or quantity is invented for the uncoded charge.
    # The original PDF and invoice totals retain it; receiving CSV contains goods only.
    postage: Postage | None = None

    def fingerprint(self):
        facts = [super().fingerprint(), None if self.postage is None else
                 [str(getattr(self.postage, field)) for field in ("price", "net", "vat", "gross")]]
        return hashlib.sha256(json.dumps(facts).encode("utf-8")).hexdigest()


def parse_invoice_text(text):
    text = text.replace("\xa0", " ").replace("\u202f", " ")
    if (not re.search(r"\bARIGA\s+s\.r\.o\.", text)
            or not re.search(r"\bIČ\s+25130293\b", text)
            or not re.search(r"\bDIČ\s+CZ25130293\b", text)):
        raise InvoiceParseError("PDF nie je rozpoznaná faktúra ARIGA.")
    numbers = re.findall(r"Faktura číslo \(variabilní symbol\):[ \t]*([0-9]{1,40})(?![0-9])", text)
    headers, totals = list(_HEADER.finditer(text)), list(_TOTAL.finditer(text))
    dates = re.findall(r"Datum vystavení:[ \t]*(\d{2}\.\d{2}\.\d{4})[^\n]*?Splatnost:[ \t]*(\d{2}\.\d{2}\.\d{4})", text)
    if len(numbers) != 1 or len(headers) != 1 or len(totals) != 1 or len(dates) != 1 or totals[0].start() <= headers[0].end():
        raise InvoiceParseError("ARIGA: chýba jednoznačná hlavička, tabuľka alebo súhrn faktúry v EUR.")
    try:
        issued, due = [datetime.strptime(value, "%d.%m.%Y").date() for value in dates[0]]
    except ValueError:
        raise InvoiceParseError("ARIGA: neplatný dátum faktúry.") from None
    code_column = headers[0].group().splitlines()[0].index("Kód")
    lines, postage = [], None
    for raw in text[headers[0].end():totals[0].start()].splitlines():
        if not raw.strip():
            continue
        row, charge = _ROW.fullmatch(raw), _POSTAGE.fullmatch(raw)
        if row is None and charge is None:
            raise InvoiceParseError("ARIGA: niektorý riadok faktúry sa nepodarilo bezpečne prečítať.")
        values = (row or charge).groupdict()
        price, net, vat, gross = [_number(values[key]) for key in ("price", "net", "vat", "gross")]
        if (price >= Decimal("100000000") or max(net, gross) >= Decimal("1000000000000")
                or not 0 <= vat <= 100 or _money(net) != net or _money(gross) != gross
                or _money(net * (1 + vat / 100)) != gross):
            raise InvoiceParseError("ARIGA: nesúhlasí cena alebo DPH položky.")
        if row:
            # The layout aligns codes under Kód (the printed label is up to
            # three positions right of the cell). Missing codes must not turn
            # the final word of a title into a supplier identity.
            if abs(row.start("sku") - code_column) > 3 or len(values["title"]) > 2000:
                raise InvoiceParseError("ARIGA: kód položky nie je v stĺpci Kód.")
            qty = _number(values["qty"])
            if not 0 < qty < Decimal("1000000000") or _money(qty * price) != net:
                raise InvoiceParseError("ARIGA: nesúhlasí množstvo alebo nákupná cena položky.")
            lines.append(InvoiceLine(len(lines) + 1, values["sku"], " ".join(values["title"].split()),
                                     qty, price, vat, net, gross))
        else:
            if postage is not None or price != net:
                raise InvoiceParseError("ARIGA: nejednoznačné poštovné.")
            postage = Postage(price, net, vat, gross)
    net, vat, gross = map(_number, totals[0].groups())
    goods_net = sum((line.net for line in lines), Decimal(0))
    goods_gross = sum((line.gross for line in lines), Decimal(0))
    if (not lines or len(lines) > 10000
            or _money(sum((line.qty * line.price for line in lines), Decimal(0))) != goods_net
            or goods_net + (postage.net if postage else 0) != net
            or goods_gross + (postage.gross if postage else 0) != gross or net + vat != gross):
        raise InvoiceParseError("ARIGA: súčty tovaru, poštovného, DPH a celkovej sumy nesúhlasia.")
    # Fail closed on another table/page after the total, while accepting the
    # supplier's printed zero-VAT legal note (which is source data, not policy).
    footer = text[totals[0].end():].strip()
    if footer and not re.fullmatch(r"Dodavatel dodává zboží osvobozené od DPH[^\n]*", footer):
        raise InvoiceParseError("ARIGA: nerozpoznaný obsah za súhrnom faktúry.")
    return ArigaInvoice(numbers[0], issued, due, net, vat, gross, tuple(lines), postage)


def parse_invoice_pdf(content):
    if not content.startswith(b"%PDF-") or len(content) > 20 * 1024 * 1024:
        raise InvoiceParseError("Očakáva sa pôvodné textové PDF ARIGA do 20 MB.")
    try:
        reader = PdfReader(io.BytesIO(content))
        # Only the supplied one-page layout has been verified. Never accept a
        # partially readable multi-page document as a complete receipt source.
        if reader.is_encrypted or len(reader.pages) != 1:
            raise ValueError()
        text = reader.pages[0].extract_text(extraction_mode="layout")
        if not text or not text.strip():
            raise ValueError()
    except Exception:
        raise InvoiceParseError("ARIGA: podporované je čitateľné jednostranové textové PDF; sken alebo iný formát treba preveriť.") from None
    return parse_invoice_text(text)
