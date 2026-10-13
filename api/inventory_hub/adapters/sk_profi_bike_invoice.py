"""Validated SK Profi Bike text-PDF invoices for the existing receiving workflow."""
from __future__ import annotations

import csv
import fcntl
import hashlib
import io
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from pypdf import PdfReader


class InvoiceParseError(ValueError):
    """An unsupported or inconsistent invoice must never become a stock receipt."""


class InvoiceConflictError(ValueError):
    """An existing invoice number must not silently acquire different line items."""


_AMOUNT = r"\d+(?:[ .]\d{3})*,\d{2,4}"
_ROW = re.compile(
    rf"^\s*(?P<line>\d+)\s+(?P<sku>[A-Za-z0-9][A-Za-z0-9._/\-]*)\s+"
    rf"(?P<title>.+?)\s+(?P<qty>{_AMOUNT})\s+Ks\s+"
    rf"(?P<price>{_AMOUNT})\s+(?P<vat>\d+(?:,\d+)?)\s*%\s+"
    rf"(?P<net>{_AMOUNT})\s*€\s+(?P<gross>{_AMOUNT})\s*€\s*$"
)
_CENT = Decimal("0.01")


def _decimal(value: str) -> Decimal:
    return Decimal(value.replace(" ", "").replace(".", "").replace(",", "."))


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class InvoiceLine:
    number: int
    sku: str
    title: str
    qty: Decimal
    price: Decimal
    vat: Decimal
    net: Decimal
    gross: Decimal


@dataclass(frozen=True)
class Invoice:
    number: str
    issue_date: date
    due_date: date
    net: Decimal
    vat: Decimal
    gross: Decimal
    lines: tuple[InvoiceLine, ...]

    @property
    def key(self) -> str:
        # The accepted number grammar contains digits and slashes only.
        return self.number.replace("/", "_")

    def csv_bytes(self) -> bytes:
        output = io.StringIO(newline="")
        writer = csv.writer(output, delimiter=";", lineterminator="\n")
        writer.writerow(["SCM", "TITLE", "QTY", "PRICE", "EAN"])
        for line in self.lines:
            writer.writerow([line.sku, line.title, str(line.qty), str(line.price), ""])
        return output.getvalue().encode("utf-8")

    def fingerprint(self) -> str:
        facts = [self.number, self.issue_date.isoformat(), self.due_date.isoformat(),
                 str(self.net), str(self.vat), str(self.gross),
                 [[line.number, line.sku, line.title, str(line.qty), str(line.price),
                   str(line.vat), str(line.net), str(line.gross)] for line in self.lines]]
        return hashlib.sha256(json.dumps(facts, ensure_ascii=False).encode("utf-8")).hexdigest()


def parse_invoice_text(text: str) -> Invoice:
    text = text.replace("\xa0", " ").replace("\u202f", " ")
    if not re.search(r"SK\s*-\s*PROFI\s+BIKE,?\s+s\.?r\.?o\.?", text, re.I):
        raise InvoiceParseError("PDF nie je rozpoznaná faktúra SK Profi Bike.")
    numbers = re.findall(r"Faktúra\s+(\d{1,4}/\d{2,4}/\d{1,12})(?![\d/])", text)
    if len(set(numbers)) != 1:
        raise InvoiceParseError("Chýba jednoznačné číslo faktúry SK Profi Bike.")
    dates = re.search(
        r"Dátum vystavenia[^\n]*Dátum splatnosti[^\n]*\n\s*"
        r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}\.\d{2}\.\d{4})\s+(\d{2}\.\d{2}\.\d{4})", text
    )
    try:
        if dates is None:
            raise ValueError()
        issue_date, _, due_date = [datetime.strptime(value, "%d.%m.%Y").date() for value in dates.groups()]
    except ValueError:
        raise InvoiceParseError("Chýbajú platné dátumy faktúry.") from None
    header = re.search(r"Interná referencia[^\n]*Množstvo[^\n]*Jednotková cena[^\n]*Celkom bez DPH[^\n]*Celkom s DPH", text)
    totals = re.search(rf"Suma bez DPH\s+({_AMOUNT})\s*€", text)
    if not header or not totals or totals.start() <= header.end():
        raise InvoiceParseError("PDF nemá podporovanú tabuľku položiek a súhrn v EUR.")
    table = text[header.end():totals.start()]
    lines = []
    for raw in table.splitlines():
        if not re.match(r"^\s*\d+\s+", raw):
            continue
        row = _ROW.fullmatch(raw)
        if not row:
            raise InvoiceParseError("Niektorý riadok faktúry sa nepodarilo bezpečne prečítať.")
        values = row.groupdict()
        line = InvoiceLine(
            int(values["line"]), values["sku"], " ".join(values["title"].split()),
            *[_decimal(values[key]) for key in ("qty", "price", "vat", "net", "gross")],
        )
        if (line.number != len(lines) + 1 or not 0 < line.qty < Decimal("1000000000")
                or line.qty != line.qty.quantize(Decimal("0.001"))
                or len(line.sku) > 96 or line.price >= Decimal("100000000") or not 0 <= line.vat <= 100
                or _money(line.qty * line.price) != line.net
                or _money(line.net * (1 + line.vat / 100)) != line.gross):
            raise InvoiceParseError("Nesúhlasí poradie, množstvo, cena alebo DPH položiek faktúry.")
        lines.append(line)
    if not lines:
        raise InvoiceParseError("PDF neobsahuje žiadne rozpoznané položky.")
    summary = text[totals.end():]
    vat_rows = re.findall(rf"\bDPH\s+(\d+(?:,\d+)?)\s*%\s+({_AMOUNT})\s*€", summary)
    gross_match = re.search(rf"\bCelkom\s+({_AMOUNT})\s*€", summary)
    if not vat_rows or not gross_match:
        raise InvoiceParseError("Chýba súhrn DPH alebo celková suma v EUR.")
    net = _decimal(totals.group(1))
    vat_by_rate = {_decimal(rate): _decimal(value) for rate, value in vat_rows}
    expected_vat = {
        rate: _money(sum((line.net for line in lines if line.vat == rate), Decimal(0)) * rate / 100)
        for rate in {line.vat for line in lines}
    }
    vat = sum(vat_by_rate.values(), Decimal(0))
    gross = _decimal(gross_match.group(1))
    # Invoice-level tax rounding need not equal the sum of rounded gross lines.
    if (len(vat_rows) != len(vat_by_rate) or vat_by_rate != expected_vat
            or sum((line.net for line in lines), Decimal(0)) != net or net + vat != gross):
        raise InvoiceParseError("Súčty položiek, DPH a celkovej sumy faktúry nesúhlasia.")
    return Invoice(numbers[0], issue_date, due_date, net, vat, gross, tuple(lines))


def parse_invoice_pdf(content: bytes) -> Invoice:
    if not content.startswith(b"%PDF-") or len(content) > 20 * 1024 * 1024:
        raise InvoiceParseError("Očakáva sa textové PDF faktúry do 20 MB.")
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 50:
            raise InvoiceParseError("Šifrované PDF alebo faktúra s viac ako 50 stranami nie je podporovaná.")
        text = "\n".join(page.extract_text(extraction_mode="layout") for page in reader.pages)
    except InvoiceParseError:
        raise
    except Exception:
        raise InvoiceParseError("PDF sa nepodarilo prečítať. Nahrajte pôvodné textové PDF faktúry.") from None
    return parse_invoice_text(text)


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temp = Path(handle.name)
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def save_receiving_invoice(content: bytes, data_root: Path, supplier: str, override: str | None = None) -> dict:
    """Upload only: canonical CSV/index, never a receiving session or stock movement."""
    invoice = parse_invoice_pdf(content)
    if override and override.strip() not in (invoice.number, invoice.key):
        raise InvoiceParseError("Zadané číslo faktúry nesúhlasí s číslom v PDF.")
    root = data_root / "suppliers" / supplier / "invoices"
    root.mkdir(parents=True, exist_ok=True)
    raw = root / "pdf" / f"{invoice.key}.pdf"
    csv_path = root / "csv" / f"{invoice.key}.csv"
    index_path = root / "index.latest.json"
    invoice_id = f"{supplier}:{invoice.key}"
    csv_content = invoice.csv_bytes()
    fingerprint = invoice.fingerprint()
    with (root / ".upload.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
        except (ValueError, OSError):
            raise InvoiceConflictError("Existujúci index faktúr sa nepodarilo prečítať; nič nebolo prepísané.") from None
        if isinstance(index, dict) and isinstance(index.get("invoices"), list):
            index = index["invoices"]
        if isinstance(index, list):
            index = {entry.get("invoice_id") or f"{supplier}:{entry['number']}": entry for entry in index}
        if not isinstance(index, dict):
            raise InvoiceConflictError("Existujúci index faktúr má nepodporovaný formát.")
        existing_key = invoice_id if invoice_id in index else invoice.key if invoice.key in index else invoice_id
        existing = index.get(existing_key)
        if existing is not None and (not isinstance(existing, dict) or existing.get("source_fingerprint") != fingerprint):
            raise InvoiceConflictError("Faktúra s týmto číslom už existuje s inými alebo neoverenými údajmi.")
        if csv_path.exists() and csv_path.read_bytes() != csv_content:
            raise InvoiceConflictError("Položky existujúcej faktúry sa líšia; opakovaný príjem nebol vytvorený.")
        if raw.exists() and raw.read_bytes() != content:
            try:
                same_invoice = parse_invoice_pdf(raw.read_bytes()).fingerprint() == fingerprint
            except InvoiceParseError:
                same_invoice = False
            if not same_invoice:
                raise InvoiceConflictError("Uložené PDF tejto faktúry obsahuje iné alebo neoverené údaje.")
        replayed = existing is not None or csv_path.exists()
        # Stable filenames also make recovery after an interrupted upload safe.
        if not raw.exists():
            _write_atomic(raw, content)
        if not csv_path.exists():
            _write_atomic(csv_path, csv_content)
        entry = {
            "supplier": supplier, "invoice_id": invoice_id, "number": invoice.key,
            "original_number": invoice.number, "issue_date": invoice.issue_date.isoformat(),
            "due_date": invoice.due_date.isoformat(), "currency": "EUR", "vat_included": False,
            "total_without_vat": str(invoice.net), "vat_amount": str(invoice.vat),
            "total_with_vat": str(invoice.gross), "items_count": len(invoice.lines),
            "csv_path": csv_path.relative_to(data_root).as_posix(),
            "raw_path": raw.relative_to(data_root).as_posix(), "source_fingerprint": fingerprint,
            "status": "new", "downloaded_at": datetime.now(timezone.utc).isoformat(),
        }
        # Never reset receiving status/notes on a duplicate upload.
        if existing is None:
            index[existing_key] = entry
            _write_atomic(index_path, json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8"))
        return {
            "success": True, "filename": raw.name, "path": raw.relative_to(data_root).as_posix(),
            "csv_path": csv_path.relative_to(data_root).as_posix(), "size_bytes": raw.stat().st_size,
            "invoice_id": invoice_id, "invoice_number": invoice.number,
            "items_count": len(invoice.lines), "replayed": replayed,
        }
