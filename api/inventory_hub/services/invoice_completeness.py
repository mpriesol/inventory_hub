"""Compare an external checklist against the local invoice archive without writes."""
from collections import Counter, defaultdict
import csv
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO, StringIO
from pathlib import Path
import re
from zipfile import ZipFile

from openpyxl import load_workbook
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pypdf import PdfReader
from sqlalchemy import text

from inventory_hub.services.invoice_archive import InvoiceArchiveError
from inventory_hub.services.invoice_finance import amount_value, gross_amount, money
from inventory_hub.services.invoice_issuers import issuer_key


MAX_CHECKLIST_BYTES = 8 * 1024 * 1024
MAX_ROWS = 500
HEADERS = {
    "issuer": {"vystavitel", "vystavovatel", "dodavatel", "dodavatelka", "supplier", "issuer"},
    "number": {"cislo", "cislofaktury", "cislofa", "cislodokladu", "invoice", "invoicenumber", "number"},
    "invoice_date": {"datumvystavenia", "datumvystaveni", "datum", "invoicedate", "issuedate", "date"},
    "amount": {"celkom", "suma", "celkovasuma", "sumaeur", "sumasdph", "nauhradu", "amount", "total", "gross"},
    "currency": {"mena", "currency"},
}


class ChecklistRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row: int = Field(ge=1, le=100_000)
    issuer: str = Field(default="", max_length=255)
    number: str = Field(default="", max_length=100)
    invoice_date: date | None = None
    amount: str | None = Field(default=None, max_length=32)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    issue: str | None = Field(default=None, max_length=80)
    raw: str | None = Field(default=None, max_length=1000)


class ChecklistRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date_from: date
    date_to: date
    rows: list[ChecklistRow] = Field(min_length=1, max_length=MAX_ROWS)

    @model_validator(mode="after")
    def date_and_amount(self):
        if not 0 <= (self.date_to - self.date_from).days <= 366:
            raise ValueError("Choose an issue-date period of at most 367 days")
        if len({row.row for row in self.rows}) != len(self.rows):
            raise ValueError("Checklist row indexes must be unique")
        for row in self.rows:
            if row.amount is not None:
                amount_value(row.amount)
        return self


def worksheet_rows(filename, content):
    extension = Path(filename).suffix.casefold()
    if extension == ".csv":
        try:
            body = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            body = content.decode("cp1250")
        sample = next((line for line in body.splitlines() if line.strip()), "")
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        yield from csv.reader(StringIO(body, newline=""), dialect)
    elif extension == ".xlsx":
        with ZipFile(BytesIO(content)) as zipfile:
            if sum(entry.file_size for entry in zipfile.infolist()) > 30 * 1024 * 1024:
                raise ValueError
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=True, keep_links=False)
        try:
            yield from workbook.active.iter_rows(min_col=1, max_col=40, values_only=True)
        finally:
            workbook.close()
    elif extension == ".pdf":
        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 25:
            raise ValueError
        for page in reader.pages:
            for line in (page.extract_text(extraction_mode="layout") or "").splitlines():
                if line.strip():
                    yield re.split(r"\s{2,}", line.strip())
    else:
        raise InvoiceArchiveError("invoice_checklist_format", 422)


def columns_for(row):
    keys = [issuer_key(str(cell or "")) for cell in row]
    return {key: keys.index(candidate) for key, aliases in HEADERS.items()
            if (candidate := next((key for key in keys if key in aliases), None)) is not None}


def clean_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}", str(value)):
        return datetime.strptime(str(value), "%d.%m.%Y").date()
    return date.fromisoformat(str(value))


def parse(filename, content):
    if not content or len(content) > MAX_CHECKLIST_BYTES:
        raise InvoiceArchiveError("invoice_checklist_file_size", 422)
    result, columns = [], None
    try:
        for index, raw in enumerate(worksheet_rows(filename, content), 1):
            if index > MAX_ROWS + 25:
                raise InvoiceArchiveError("invoice_checklist_too_many_rows", 422)
            if not any(cell not in (None, "") for cell in raw):
                continue
            candidate = columns_for(raw)
            if {"issuer", "number"} <= candidate.keys():
                columns = candidate
                continue
            if columns is None:
                continue
            def cell(key):
                column = columns.get(key, len(raw))
                return raw[column] if column < len(raw) else None
            row = ChecklistRow(row=index, issuer=str(cell("issuer") or "").strip()[:255],
                               number=str(cell("number") or "").strip()[:100],
                               raw=" | ".join(str(value or "") for value in raw)[:1000])
            if row.issuer and row.number:
                try:
                    currency = str(cell("currency") or "").strip().upper()
                    amount = cell("amount")
                    row.invoice_date = clean_date(cell("invoice_date"))
                    row.currency = currency or None
                    if currency and not re.fullmatch(r"[A-Z]{3}", currency):
                        raise ValueError
                    if amount not in (None, ""):
                        row.amount = money(amount_value(str(amount).replace(" ", "").replace("\xa0", "").replace(",", ".")))
                except (ValueError, ArithmeticError):
                    row.issue = "unreadable"
                    row.amount = row.currency = None
            else:
                row.issue = "incomplete"
            result.append(row.model_dump(mode="json"))
    except InvoiceArchiveError:
        raise
    except Exception:
        raise InvoiceArchiveError("invoice_checklist_unreadable", 422) from None
    if len(result) > MAX_ROWS:
        raise InvoiceArchiveError("invoice_checklist_too_many_rows", 422)
    if not result:
        raise InvoiceArchiveError("invoice_checklist_no_rows", 422)
    return {"rows": result}


def invoice_number_key(value):
    return value.strip().casefold()


async def compare(db, request: ChecklistRequest):
    numbers = {invoice_number_key(row.number) for row in request.rows if row.number}
    rows = (await db.execute(text("""SELECT ui.id,ui.invoice_number,ui.issuer_name,ui.issuer_key,ui.invoice_date,
        ui.total_amount,ui.total_with_vat,ui.currency,ui.vat_rate,ui.vat_included,ui.document_type,
        s.name AS supplier_name,s.code AS supplier_code
        FROM uploaded_invoices ui LEFT JOIN suppliers s ON s.id=ui.supplier_id
        WHERE ui.direction='purchase' AND ui.archived_at IS NULL AND
          lower(btrim(ui.invoice_number))=ANY(CAST(:numbers AS text[])) AND
          (ui.invoice_date BETWEEN :start AND :finish OR ui.invoice_date IS NULL)"""),
        {"numbers": sorted(numbers), "start": request.date_from, "finish": request.date_to})).mappings().all()
    index = defaultdict(list)
    for row in rows:
        names = {row["issuer_key"]} if row["issuer_key"] else {
            issuer_key(name) for name in (row["issuer_name"], row["supplier_name"], row["supplier_code"]) if name}
        for name in names:
            index[(name, invoice_number_key(row["invoice_number"]))].append(row)

    key = lambda row: (issuer_key(row.issuer), invoice_number_key(row.number))
    duplicates = Counter(key(row) for row in request.rows if not row.issue)
    result = {"found": [], "missing": [], "review": []}
    for row in request.rows:
        matched = index[key(row)]
        issue, target = row.issue, None
        if issue is None and not all(key(row)):
            issue = "incomplete"
        elif issue is None and duplicates[key(row)] > 1:
            issue = "duplicate"
        elif issue is None and row.invoice_date and not request.date_from <= row.invoice_date <= request.date_to:
            issue = "outside_period"
        elif issue is None and len(matched) > 1:
            issue = "ambiguous"
        elif issue is None and matched:
            target = matched[0]
            if target["invoice_date"] is None or (row.invoice_date and row.invoice_date != target["invoice_date"]):
                issue = "date"
            elif row.currency and target["currency"] != row.currency:
                issue = "currency"
            elif row.amount is not None and (not row.currency or gross_amount(target) != Decimal(row.amount)):
                issue = "amount"
        group = "review" if issue else "found" if target else "missing"
        result[group].append({**row.model_dump(mode="json"), "issue": issue,
                              "invoice_id": target["id"] if target else None})
    return result
