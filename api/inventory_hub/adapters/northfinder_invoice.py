"""Northfinder Money S5 invoices with original financial and net receipt lines."""
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import io
import re

from pypdf import PdfReader

from inventory_hub.adapters.sk_profi_bike_invoice import Invoice, InvoiceLine, InvoiceParseError


_CENT = Decimal('0.01')
_INTEGER = r'(?:[0-9]{1,3}(?: [0-9]{3})+|[0-9]+)'
_AMOUNT = rf'-? *{_INTEGER},[0-9]{{2}}'
_PRICE = rf'-? *{_INTEGER},[0-9]{{2,4}}'
_PERCENT = r'[0-9]{1,3}(?:,[0-9]{1,2})?'
_NUMBER = re.compile(r'^ *FAKTÚRA VYSTAVENÁ č\.(?: - INVOICE no\.:)? +([0-9]{1,30}) *$', re.M)
_HEADER = re.compile(
    r'^ *Kód +(?:Popis produktu|Označenie dodávky) +Množstvo +MJ +(?:Jedn\. cena|Cena) +Zľava +'
    r'Cena po zľave +Sadzba +Základ +Celkom s DPH *$', re.M)
_EN_HEADER = re.compile(r'^ *Code +Product description +Quantity +CU +Price +Discou +Final Price +VAT Rat +Total NET +Total with VAT *$')
_TABLE_TOTAL = re.compile(r'^ *Celkom(?: / Total)?:.*$', re.M)
_PRINT_FOOTER = re.compile(r'^ *(?:Vytlačil\(a\):[^\n]*?)?Process by +Money S5(?: +Vystavil:[^\n]*)? *$', re.M)
_PAGE_NUMBER = re.compile(r'^ *(?:Vytlačil\(a\):[^\n]*? +)?([0-9]+)/([0-9]+)(?: +Vystavil:[^\n]*)? *$', re.M)
_TOTAL = re.compile(rf'^ *CELKOM(?: / TOTAL)? +({_AMOUNT}) +({_AMOUNT}) +({_AMOUNT})(?: +[^\n]*)?$', re.M)
_EUR_TOTAL = re.compile(rf'Celková čiastka(?: / Total amount)? +({_AMOUNT}) +EUR *(?:\n|$)')
_ROW = re.compile(
    rf'^ *(?P<code>[A-Za-z0-9-]{{1,100}}) {{2,}}(?P<title>\S.*?) {{2,}}'
    rf'(?P<quantity>{_INTEGER}(?:,[0-9]{{1,3}})?) +(?:(?:ks|pcs) +)?'
    rf'(?P<price>{_PRICE}) +(?P<discount>{_PERCENT}) *% +(?P<final>{_PRICE}) +'
    rf'(?P<vat>{_PERCENT}) *% +(?P<net>{_AMOUNT}) +(?P<gross>{_AMOUNT}) +EUR *$')
_GOODS_CODE = re.compile(r'[0-9]+-[0-9]+-[0-9]+')
_SHIPPING = 'ART00784'
_DISCOUNT = 'ART00930'


class NorthfinderInvoice(Invoice):
    @property
    def receiving_lines(self):
        return tuple(line for line in self.lines if line.sku not in (_SHIPPING, _DISCOUNT))

    def fingerprint(self):
        # Bind the full printed invoice and its physical receiving projection.
        return hashlib.sha256(super().fingerprint().encode('ascii') + self.csv_bytes()).hexdigest()


def _decimal(value):
    return Decimal(value.replace(' ', '').replace(',', '.'))


def _money(value):
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _date(header, label):
    values = re.findall(rf'{label}: *(\d{{2}}\.\d{{2}}\.\d{{4}})(?![0-9])', header)
    if len(values) != 1:
        raise InvoiceParseError('Northfinder: chýba jednoznačný dátum faktúry.')
    try:
        return datetime.strptime(values[0], '%d.%m.%Y').date()
    except ValueError:
        raise InvoiceParseError('Northfinder: neplatný dátum faktúry.') from None


def _row(raw, number):
    match = _ROW.fullmatch(raw) if len(raw) <= 4000 else None
    if not match:
        raise InvoiceParseError('Northfinder: neúplný alebo neznámy riadok faktúry.')
    code, title = match['code'], ' '.join(match['title'].split())
    qty, price, discount, final, vat, net, gross = (
        _decimal(match[name]) for name in ('quantity', 'price', 'discount', 'final', 'vat', 'net', 'gross'))
    if (not 0 < qty < 10**9 or max(abs(price), abs(final)) >= 10**8 or max(abs(net), abs(gross)) >= 10**12
            or len(title) > 2000 or not 0 <= discount <= 100 or not 0 <= vat <= 100
            or _money(qty * final) != net or _money(net * (1 + vat / 100)) != gross):
        raise InvoiceParseError('Northfinder: nesúhlasí množstvo, cena po zľave alebo DPH riadka.')
    if code == _DISCOUNT:
        valid = title.casefold() in ('zľava', 'zľava - discount', 'discount') and net < 0 and price < 0
    elif code == _SHIPPING:
        valid = title.casefold() in ('doprava', 'doprava - shipping', 'doprava - transport') and 0 <= final <= price
    else:
        valid = _GOODS_CODE.fullmatch(code) and 0 <= final <= price
    if not valid:
        raise InvoiceParseError('Northfinder: neznámy druh položky alebo neplatná cena.')
    return InvoiceLine(number, code, title, qty, final, vat, net, gross)


def parse_invoice_pages(pages):
    if not 1 <= len(pages) <= 100:
        raise InvoiceParseError('Northfinder: podporované sú čitateľné PDF do 100 strán.')
    lines, number = [], None
    issued = due = None
    for page_no, page in enumerate(pages, 1):
        page = page.replace('\xa0', ' ').replace('\u202f', ' ').replace('−', '-')
        headers, numbers = list(_HEADER.finditer(page)), _NUMBER.findall(page)
        marker = _PAGE_NUMBER.findall(page)
        last = page_no == len(pages)
        endings = list((_TABLE_TOTAL if last else _PRINT_FOOTER).finditer(page))
        if (len(headers) != 1 or len(numbers) != 1 or len(endings) != 1
                or marker != [(str(page_no), str(len(pages)))] or endings[0].start() <= headers[0].end()
                or (number is not None and numbers[0] != number)):
            raise InvoiceParseError('Northfinder: chýba strana, tabuľka alebo číslo dokladu nesúhlasí.')
        if page_no == 1:
            header = page[:headers[0].start()]
            if not re.search(r'^ *NORTHFINDER +a\.s\.', header, re.M) or not re.search(r'\b35933011\b', header):
                raise InvoiceParseError('PDF nie je rozpoznaná faktúra Northfinder.')
            number = numbers[0]
            issued = _date(header, r'Dátum vystavenia(?: / Issuing)?')
            due = _date(header, r'Dátum splatnosti(?: / Maturity)?')
        body = page[headers[0].end():endings[0].start()].strip('\n')
        table = [row for row in body.splitlines() if row.strip()]
        if table and _EN_HEADER.fullmatch(table[0]):
            table.pop(0)
        if not table or len(lines) + len(table) > 10000:
            raise InvoiceParseError('Northfinder: tabuľka neobsahuje podporovaný počet riadkov.')
        for row in table:
            lines.append(_row(row, len(lines) + 1))
    summary = page[endings[0].end():]
    totals, euro_totals = _TOTAL.findall(summary), _EUR_TOTAL.findall(summary)
    if len(totals) != 1 or len(euro_totals) != 1:
        raise InvoiceParseError('Northfinder: chýba jednoznačný súhrn faktúry v EUR.')
    net, vat, gross = map(_decimal, totals[0])
    bases = defaultdict(Decimal)
    for line in lines:
        bases[line.vat] += line.net
    # Money S5 can round VAT once per rate instead of summing printed line VAT.
    rate_vat = sum(_money(base * rate / 100) for rate, base in bases.items())
    line_vat = sum(line.gross - line.net for line in lines)
    if (not 0 <= net <= gross < 10**12 or net + vat != gross or sum(bases.values()) != net
            or vat not in (rate_vat, line_vat) or _decimal(euro_totals[0]) != gross):
        raise InvoiceParseError('Northfinder: súčty faktúry a DPH nesúhlasia s položkami.')
    invoice = NorthfinderInvoice(number, issued, due, net, vat, gross, tuple(lines))
    if not invoice.receiving_lines:
        raise InvoiceParseError('Northfinder: faktúra nemá tovarové položky.')
    return invoice


def parse_invoice_pdf(content):
    if not content.startswith(b'%PDF-') or len(content) > 20 * 1024 * 1024:
        raise InvoiceParseError('Očakáva sa pôvodné textové PDF Northfinder do 20 MB.')
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 100:
            raise ValueError()
        pages = [page.extract_text(extraction_mode='layout') for page in reader.pages]
    except Exception:
        raise InvoiceParseError('Northfinder: PDF nie je čitateľná textová faktúra.') from None
    return parse_invoice_pages(pages)
