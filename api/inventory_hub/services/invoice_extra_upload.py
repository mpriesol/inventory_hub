"""Archive service invoices and attach validated original Vavrys ISDOC files."""
from datetime import date, datetime
from decimal import Decimal
import hashlib
from io import BytesIO
from pathlib import Path
import re

from lxml import etree
from pypdf import PdfReader
from sqlalchemy import text

from inventory_hub import config_io
from inventory_hub.adapters import vavrys_invoice
from inventory_hub.invoice_archive_types import ArchiveMetadata
from inventory_hub.services import invoice_archive as archive
from inventory_hub.services.invoice_issuers import issuer_key
from inventory_hub.services.receiving_source import lock_supplier
from inventory_hub.settings import settings


def _formatted_result(result, filename, size, number, supplier=None, *, receiving=False):
    return {**result, "success": True, "supplier_code": supplier, "original_filename": filename, "filename": filename,
            "invoice_number": number, "size_bytes": size, "items_count": result.get("lines_count", 0),
            "archive": {"status": "completed", "id": result["id"]},
            "processing": {"status": "complete", "parsed_lines": result.get("lines_count", 0),
                           "stored_lines": result.get("lines_count", 0), "receiving_ready": receiving,
                           "issues": [], "missing_fields": []}}


def _pdf_service_metadata(content):
    """Read only explicitly labelled values. The form supplies missing values."""
    try:
        reader = PdfReader(BytesIO(content))
        if len(reader.pages) > 50:
            return {}
        value = reader.pages[0].extract_text(extraction_mode="layout") or ""
    except Exception:
        return {}
    result = {}
    number = re.search(r"(?:číslo (?:faktúry|dokladu)|invoice number)\s*:?\s+([\w/-]+)", value, re.I)
    if number:
        result["invoice_number"] = number[1]
    for key, label in (("invoice_date", "vystavenia"), ("due_date", "splatnosti")):
        match = re.search(rf"\b(?:dátum )?{label}[ :]+(\d{{1,2}}\.\d{{1,2}}\.\d{{4}})", value, re.I)
        if match:
            try:
                result[key] = datetime.strptime(match[1], "%d.%m.%Y").date()
            except ValueError:
                continue
    total = re.search(r"\b(?:celkom|spolu|suma) na úhradu[: ]+([\d .]+,\d{2})\s*(EUR|CZK|USD|€)", value, re.I)
    if total:
        result.update(total_with_vat=Decimal(total[1].replace(" ", "").replace(".", "").replace(",", ".")),
                      currency="EUR" if total[2] == "€" else total[2].upper())
    return result


async def save_service_invoice(db, *, filename, content, issuer, invoice_number, invoice_date, due_date, total, currency, actor):
    issuer = issuer.strip()
    if not issuer or len(issuer) > 255 or not issuer_key(issuer):
        raise archive.InvoiceArchiveError("invoice_service_fields_required", 422)
    if Path(filename).suffix.lower() == ".pdf":
        import asyncio
        values = await asyncio.to_thread(_pdf_service_metadata, content)
    else:
        values = {}
    try:
        if invoice_number:
            values["invoice_number"] = invoice_number.strip()
        if invoice_date:
            values["invoice_date"] = date.fromisoformat(invoice_date)
        if due_date:
            values["due_date"] = date.fromisoformat(due_date)
        if currency:
            values["currency"] = currency.strip().upper()
        if total:
            values["total_with_vat"] = total.strip().replace(",", ".")
        values["total_amount"] = values.get("total_with_vat")
        if any(values.get(key) is None for key in ("invoice_number", "invoice_date", "due_date", "currency", "total_with_vat")):
            raise ValueError
        metadata = ArchiveMetadata.model_validate(values)
    except ValueError:
        raise archive.InvoiceArchiveError("invoice_service_fields_required", 422) from None
    stored = await archive.ingest_invoice(db, direction="purchase", source_kind="upload",
        source_key="sha256:" + hashlib.sha256(content).hexdigest(), metadata=metadata,
        files=[{"filename": filename, "content": content}], issuer_name=issuer, service_invoice=True,
        source_data={"kind": "service-upload"}, actor=actor)
    if not stored["replayed"]:
        await db.execute(text("""UPDATE uploaded_invoices SET vat_included=TRUE, payment_assumed_unpaid=TRUE
            WHERE id=:id"""), {"id": stored["id"]})
    await db.commit()
    return _formatted_result(stored, filename, len(content), metadata.invoice_number)


def ensure_vavrys_config():
    if not config_io.supplier_path("vavrys").is_file():
        config_io.save_supplier("vavrys", {"name": "Vavrys (Primus)", "is_active": True})


async def attach_vavrys_isdoc(db, *, filename, content):
    import asyncio
    try:
        evidence = await asyncio.to_thread(vavrys_invoice.parse_isdoc, content)
    except (etree.LxmlError, AttributeError, ArithmeticError, ValueError, TypeError):
        raise archive.InvoiceArchiveError("invoice_vavrys_isdoc_invalid", 422) from None
    try:
        supplier = await archive.resolve_supplier(db, "vavrys")
    except archive.InvoiceArchiveError as error:
        if error.code != "invoice_owner_missing":
            raise
        raise archive.InvoiceArchiveError("invoice_vavrys_pdf_required", 422) from None
    await lock_supplier(db, supplier.id)
    number = evidence["invoice_number"]
    key = f"{evidence['issuer_country'].lower()}-{number}"
    found = (await db.execute(text("""SELECT * FROM uploaded_invoices
        WHERE supplier_id=:supplier AND source_key=:key AND archived_at IS NULL FOR UPDATE"""),
        {"supplier": supplier.id, "key": key})).mappings().first()
    if found is None:
        raise archive.InvoiceArchiveError("invoice_vavrys_pdf_required", 422)
    compared = ("currency", "total_without_vat", "total_with_vat", "vat_amount")
    rows = (await db.execute(text("""SELECT supplier_sku,quantity,total_price
        FROM uploaded_invoice_lines WHERE invoice_id=:id ORDER BY line_number"""), {"id": found["id"]})).all()
    if (any(found[field] != evidence[field] for field in compared)
            or sorted((row.supplier_sku or "", row.quantity, row.total_price) for row in rows) != sorted(evidence["lines"])):
        raise archive.InvoiceArchiveError("invoice_vavrys_isdoc_mismatch", 422)
    data = archive._file(Path(settings.INVENTORY_DATA_ROOT), "suppliers", "vavrys", {"filename": filename, "content": content})
    insert = await db.execute(text("""INSERT INTO invoice_files(invoice_id,role,filename,file_path,file_type,sha256,size_bytes)
        VALUES(:id,:role,:filename,:file_path,:file_type,:sha256,:size_bytes) ON CONFLICT(invoice_id,sha256) DO NOTHING"""),
        {**data, "id": found["id"]})
    await db.commit()
    return _formatted_result({"id": found["id"], "replayed": not insert.rowcount, "lines_count": len(rows)},
                             filename, len(content), number, "vavrys")
