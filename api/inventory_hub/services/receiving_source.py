"""Verified EUR/net invoice sources for physical receiving, never exchange guesses.

CSV is a parser intermediate. The source contract is established from a stored
supplier original/download, not from a filename or a client-supplied checkbox.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
import csv
import hashlib
import io
import json
import re

from fastapi import HTTPException
from sqlalchemy import select, text

from inventory_hub.db_models_ext import ReceivingSession
from inventory_hub.file_access import stored_document_path


class ReceivingSourceError(Exception):
    def __init__(self, code, *, line_number=None, field=None, existing=None):
        self.code = code
        self.detail = {"code": code}
        if line_number is not None:
            self.detail["line_number"] = line_number
        if field:
            self.detail["field"] = field
        if existing is not None:
            self.detail.update(session_id=existing.id, invoice_number=existing.invoice_number,
                               status=existing.status.value)
        super().__init__(code)


def decimal_value(value, scale, field, line_number, *, optional=False):
    raw = str(value).strip() if value is not None else ""
    if optional and not raw:
        return None
    if not re.fullmatch(r"[0-9]+(?:[.,][0-9]+)?", raw, flags=re.ASCII):
        raise ReceivingSourceError("receiving_csv_invalid", line_number=line_number, field=field)
    try:
        amount = Decimal(raw.replace(",", "."))
        maximum = Decimal("999999999.999") if field == "quantity" else Decimal("99999999.9999")
        if (not amount.is_finite() or amount > maximum
                or amount != amount.quantize(Decimal(1).scaleb(-scale))):
            raise InvalidOperation()
        return amount
    except InvalidOperation:
        raise ReceivingSourceError("receiving_csv_invalid", line_number=line_number, field=field) from None


def parse_csv(content, *, headers, normalize, get_value, delimiter, decode):
    value = decode(content)
    first = next((line for line in value.splitlines() if line.strip()), "")
    reader = csv.reader(io.StringIO(value), delimiter=delimiter(first), strict=True)
    try:
        columns = next((row for row in reader if any(cell.strip() for cell in row)), [])
    except csv.Error:
        raise ReceivingSourceError("receiving_csv_invalid", field="headers") from None
    normalized = [normalize(column) for column in columns]
    if not columns or len(set(normalized)) != len(normalized):
        raise ReceivingSourceError("receiving_csv_invalid", field="headers")
    index = dict(zip(normalized, range(len(columns))))
    if not any(normalize(key) in index for key in headers["quantity"]):
        raise ReceivingSourceError("receiving_csv_invalid", field="quantity")
    result = []
    try:
        for line_number, row in enumerate(reader, 2):
            if not any(cell.strip() for cell in row):
                continue
            if len(row) != len(columns):
                raise ReceivingSourceError("receiving_csv_invalid", line_number=line_number, field="columns")
            get = lambda names: get_value(row, index, names)
            currency = get(["CURRENCY", "MENA", "CURRENCY_ID"])
            if currency and currency.upper() != "EUR":
                raise ReceivingSourceError("receiving_source_currency_unsupported", line_number=line_number)
            vat = get(["VAT_INCLUDED", "PRICE_INCLUDES_VAT"])
            if vat and vat.lower() not in ("false", "0", "no"):
                raise ReceivingSourceError("receiving_source_vat_unverified", line_number=line_number)
            result.append({"ean": get(headers["ean"]), "supplier_sku": get(headers["sku"]),
                "description": get(headers["title"]),
                "ordered_qty": decimal_value(get(headers["quantity"]), 3, "quantity", line_number),
                "unit_price": decimal_value(get(headers["price"]), 4, "price", line_number, optional=True)})
            if len(result) > 10000:
                raise ReceivingSourceError("receiving_csv_invalid", field="rows")
    except csv.Error:
        raise ReceivingSourceError("receiving_csv_invalid") from None
    if not result:
        raise ReceivingSourceError("receiving_csv_invalid", field="rows")
    return result


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def rows_hash(rows):
    def number(value):
        return None if value is None else format(Decimal(value).normalize(), "f")
    facts = [[str(row.get("supplier_sku") or ""), str(row.get("ean") or ""),
              str(row.get("description") or ""), number(row["ordered_qty"]), number(row.get("unit_price"))]
             for row in rows]
    return _digest(json.dumps(facts, ensure_ascii=False, separators=(",", ":")).encode())


@dataclass(frozen=True)
class ReceivingSource:
    rows: list
    source_hash: str
    csv_hash: str
    document_number: str
    adapter: str

    def snapshot(self):
        return {"version": 1, "source_hash": self.source_hash, "csv_hash": self.csv_hash,
                "rows_hash": rows_hash(self.rows), "document_number": self.document_number,
                "adapter": self.adapter, "currency": "EUR", "vat_included": False}


def _source_path(root, supplier, value):
    value = str(value or "")
    if not value:
        raise ReceivingSourceError("receiving_source_unverified")
    if not Path(value).is_absolute() and not value.startswith("suppliers/"):
        value = f"suppliers/{supplier}/{value}"
    try:
        path = stored_document_path(root, value)
    except HTTPException:
        raise ReceivingSourceError("receiving_source_unverified") from None
    if not path.is_relative_to(Path(root).resolve() / "suppliers" / supplier / "invoices"):
        raise ReceivingSourceError("receiving_source_unverified")
    return path


def _entries(root, supplier, path):
    index = Path(root) / "suppliers" / supplier / "invoices/index.latest.json"
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("invoices"), list):
            data = data["invoices"]
        entries = list(data.values()) if isinstance(data, dict) else data
        matches = []
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("csv_path"):
                continue
            try:
                candidate = _source_path(root, supplier, entry["csv_path"])
            except ReceivingSourceError:
                continue  # An unrelated missing historical file is not this source.
            if candidate == path.resolve():
                matches.append(entry)
        if len(matches) != 1:
            raise ReceivingSourceError("receiving_source_unverified")
        return matches[0]
    except (OSError, ValueError, TypeError):
        raise ReceivingSourceError("receiving_source_unverified") from None


def load_source(root, supplier, invoice_number, path, parse):
    try:
        return _load_source(root, supplier, invoice_number, path, parse)
    except (OSError, UnicodeError):
        raise ReceivingSourceError("receiving_source_unverified") from None


def _load_source(root, supplier, invoice_number, path, parse):
    """Verify existing known adapter outputs without changing source/index files."""
    path = Path(path)
    raw_csv = path.read_bytes()
    entry = _entries(root, supplier, path)
    number = str(entry.get("original_number") or entry.get("number") or "")
    if not number:
        raise ReceivingSourceError("receiving_source_unverified")
    cfg = {}
    if supplier == "paul-lange":
        try:
            cfg = json.loads((Path(root) / "suppliers" / supplier / "config.json").read_text())
        except (OSError, ValueError):
            raise ReceivingSourceError("receiving_source_unverified") from None
    adapter_cfg, invoice_cfg = cfg.get("adapter_settings") or {}, cfg.get("invoices") or {}
    mapping = adapter_cfg.get("mapping") or invoice_cfg.get("mapping") or {}
    rows = parse(path, mapping.get("invoice_to_canon") if supplier == "paul-lange" else None, content=raw_csv)
    if supplier in ("sk-profi-bike", "zookee"):
        from inventory_hub.adapters import sk_profi_bike_invoice, zookee_invoice
        parser = zookee_invoice.parse_invoice_pdf if supplier == "zookee" else sk_profi_bike_invoice.parse_invoice_pdf
        original = _source_path(root, supplier, entry.get("raw_path"))
        try:
            invoice = parser(original.read_bytes())
        except (ValueError, OSError):
            raise ReceivingSourceError("receiving_source_unverified") from None
        if (entry.get("source_fingerprint") != invoice.fingerprint() or number != invoice.number
                or raw_csv != invoice.csv_bytes()):
            raise ReceivingSourceError("receiving_source_changed")
        source_hash = _digest((supplier + ":pdf:" + invoice.fingerprint()).encode())
        adapter = supplier + "_pdf"
    elif supplier == "northfinder":
        from inventory_hub.adapters.northfinder_xlsx_parser import validated_receiving_rows
        original = _source_path(root, supplier, entry.get("raw_path"))
        if entry.get("currency") not in (None, "EUR"):
            raise ReceivingSourceError("receiving_source_currency_unsupported")
        expected = validated_receiving_rows(original)
        if rows_hash(expected) != rows_hash(rows):
            raise ReceivingSourceError("receiving_source_changed")
        source_hash, adapter = _digest(original.read_bytes()), "northfinder_xlsx"
    elif supplier == "paul-lange":
        # Read the actual saved contract, never config_normalize's EUR defaults.
        if (adapter_cfg.get("currency") or invoice_cfg.get("currency")) != "EUR":
            raise ReceivingSourceError("receiving_source_currency_unsupported")
        if (mapping.get("postprocess") or {}).get("unit_price_source") != "ex":
            raise ReceivingSourceError("receiving_source_vat_unverified")
        if (entry.get("sha1") != hashlib.sha1(raw_csv).hexdigest()
                or entry.get("receiving_source") != "paul_lange_download_v1"
                or entry.get("receiving_document_number") != number):
            raise ReceivingSourceError("receiving_source_unverified")
        # The native CSV omits the invoice number. Bind its bytes to the number
        # observed on the supplier download link, so distinct genuine invoices
        # with identical item rows remain distinct receipts.
        identity = json.dumps([supplier, number, _digest(raw_csv)], separators=(",", ":"))
        source_hash, adapter = _digest(identity.encode()), "paul_lange_download"
    else:
        raise ReceivingSourceError("receiving_source_unverified")
    return ReceivingSource(rows, source_hash, _digest(raw_csv), number, adapter)


async def lock_supplier(db, supplier_id):
    key = int(_digest(f"receiving-source:{supplier_id}".encode())[:15], 16)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def ensure_unique(db, supplier_id, source, root, *, excluding=None):
    sessions = list(await db.scalars(select(ReceivingSession).where(ReceivingSession.supplier_id == supplier_id)))
    for existing in sessions:
        if existing.id == excluding:
            continue
        proof = (existing.session_data or {}).get("receiving_source") or {}
        if proof.get("document_number") == source.document_number and existing.source_hash != source.source_hash:
            raise ReceivingSourceError("receiving_source_changed", existing=existing)
        document_adapters = {"sk-profi-bike_pdf", "zookee_pdf", "paul_lange_download"}
        distinct_documents = (proof.get("version") == 1
            and proof.get("source_hash") == existing.source_hash
            and source.adapter in document_adapters and proof.get("adapter") == source.adapter
            and proof.get("document_number") and source.document_number
            and proof["document_number"] != source.document_number)
        if (existing.source_hash == source.source_hash
                or (proof.get("csv_hash") == source.csv_hash and not distinct_documents)):
            raise ReceivingSourceError("receiving_source_duplicate", existing=existing)
        if existing.source_hash is None and existing.invoice_file_path:
            # Old sessions did not retain a hash. Compare only a confined source
            # that still exists; an unrelated missing historical file is not proof.
            try:
                old_path = stored_document_path(root, existing.invoice_file_path)
                same = _digest(old_path.read_bytes()) == source.csv_hash
            except (OSError, ValueError, HTTPException):
                same = False
            if same:
                raise ReceivingSourceError("receiving_source_duplicate", existing=existing)


async def validate_session(db, session, supplier, root, parse):
    rows = [{"supplier_sku": line.supplier_sku, "ean": line.ean, "description": line.description,
             "ordered_qty": line.ordered_qty, "unit_price": line.unit_price}
            for line in sorted(session.lines, key=lambda row: row.line_number)]
    proof = (session.session_data or {}).get("receiving_source")
    if proof:
        if (proof.get("version") != 1 or proof.get("currency") != "EUR" or proof.get("vat_included") is not False
                or session.invoice_currency != "EUR" or session.source_hash != proof.get("source_hash")
                or proof.get("rows_hash") != rows_hash(rows)):
            raise ReceivingSourceError("receiving_source_changed")
        return
    if not session.invoice_file_path:
        raise ReceivingSourceError("receiving_source_unverified")
    path = _source_path(root, supplier, session.invoice_file_path)
    source = load_source(root, supplier, session.invoice_number, path, parse)
    if rows_hash(source.rows) != rows_hash(rows):
        raise ReceivingSourceError("receiving_source_changed")
    # Legacy verification is part of the caller's transaction; no ledger rewrite.
    await ensure_unique(db, session.supplier_id, source, root, excluding=session.id)
    session.source_hash = source.source_hash
    session.invoice_currency, session.fx_rate_to_eur, session.vat_included = "EUR", Decimal("1"), False
    session.session_data = {**(session.session_data or {}), "receiving_source": source.snapshot()}
