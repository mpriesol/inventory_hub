"""Read-only invoice discovery for a physical scan, across suppliers and states."""
import asyncio
from functools import lru_cache

from sqlalchemy import select

from inventory_hub.db_models import Supplier, SupplierProduct
from inventory_hub.db_models_ext import ReceivingSession, ReceivingLine
from inventory_hub.services.product_identity import RemoteIdentity, load_identity_index, verified_barcodes
from inventory_hub.supplier_prefix import get_supplier_prefix
from inventory_hub import config_io
from inventory_hub.settings import settings


@lru_cache(maxsize=512)
def _rows(path, modified, size):
    # Reuse the canonical receiving CSV grammar; metadata invalidates the cache.
    from inventory_hub.routers.receiving_db import _parse_invoice_csv
    return _parse_invoice_csv(path)


def _files(suppliers):
    from inventory_hub.routers.receiving_db import _invoice_csv_path
    result, unreadable = [], 0
    for supplier in suppliers:
        root = settings.INVENTORY_DATA_ROOT / "suppliers" / supplier / "invoices" / "csv"
        for path in sorted(root.rglob("*.csv")) if root.exists() else []:
            try:
                # Match the actual create-session resolver, including nested
                # invoice directories; never open a path supplied by a client.
                resolved = _invoice_csv_path(supplier, path.stem)
                if resolved.resolve() != path.resolve():
                    continue
                stat = path.stat()
                if stat.st_size > 20 * 1024 * 1024:
                    raise ValueError("Oversized invoice")
                result.append((supplier, path.stem, _rows(path, stat.st_mtime_ns, stat.st_size)))
            except Exception:
                unreadable += 1
    return result, unreadable


def matches(code, row, prefix, source_skus, product_id=None):
    sku = (row.get("supplier_sku") or "").strip()
    return (code in verified_barcodes((row.get("ean") or "",))
            or bool(sku and (code in (sku, prefix + sku) or sku in source_skus))
            or product_id is not None and row.get("product_id") == product_id)


async def lookup(db, code):
    suppliers = {supplier.id: supplier for supplier in await db.scalars(select(Supplier))}
    source_skus = {}
    # A source barcode narrows invoice discovery within that supplier. It does
    # not create a canonical product or persist an identifier/link.
    sources = await db.scalars(select(SupplierProduct).where(
        SupplierProduct.is_active.is_(True), SupplierProduct.ean.contains(code)))
    for source in sources:
        if code in verified_barcodes((source.ean or "",)):
            source_skus.setdefault(source.supplier_id, set()).add(source.supplier_sku)
    identity = RemoteIdentity(0, code, barcodes=(code,))
    index = await load_identity_index(db, 0, [identity], local=True)
    resolution = index.resolve(identity)
    # Conflicts remain discoverable as separate choices; the scan endpoint
    # revalidates product identity before any quantity change.
    product_id = resolution.product_id if resolution.status != "conflict" else None
    prefixes = {}
    for supplier in suppliers.values():
        try:
            prefixes[supplier.code] = get_supplier_prefix(config_io.load_supplier(supplier.code, write_back_on_load=False))
        except (ValueError, OSError):
            prefixes[supplier.code] = ""
    sessions = list(await db.scalars(select(ReceivingSession)))
    by_key = {(suppliers[s.supplier_id].code, s.invoice_number): s for s in sessions if s.supplier_id in suppliers}
    db_lines = list(await db.scalars(select(ReceivingLine)))
    rows_by_session = {}
    for line in db_lines:
        rows_by_session.setdefault(line.session_id, []).append(line)
    candidates = []

    def add(supplier, invoice, rows, session=None):
        matching = [row for row in rows if matches(code, row, prefixes.get(supplier.code, ""),
                    source_skus.get(supplier.id, set()), product_id)]
        if matching:
            candidates.append({"supplier": supplier.code, "supplier_name": supplier.name,
                "invoice_no": invoice, "session_id": str(session.id) if session else None,
                "status": session.status.value if session else "new",
                "invoice_date": session.invoice_date.isoformat() if session and session.invoice_date else None,
                "lines": [{"id": row.get("id"), "title": row.get("description") or "",
                    "sku": row.get("supplier_sku") or "", "line_number": row.get("line_number"),
                    "ordered_qty": str(row["ordered_qty"]), "received_qty": str(row.get("received_qty", 0))} for row in matching]})

    for key, session in by_key.items():
        rows = [{name: getattr(line, name) for name in ("id", "line_number", "supplier_sku", "ean", "description",
                    "product_id", "ordered_qty", "received_qty")} for line in rows_by_session.get(session.id, [])]
        add(suppliers[session.supplier_id], session.invoice_number, rows, session)
    files, unreadable = await asyncio.to_thread(_files, [supplier.code for supplier in suppliers.values()])
    by_code = {supplier.code: supplier for supplier in suppliers.values()}
    for supplier, invoice, rows in files:
        if (supplier, invoice) not in by_key:
            add(by_code[supplier], invoice, rows)
    candidates.sort(key=lambda c: (c["status"] in ("completed", "cancelled"), c["supplier"], c["invoice_no"]))
    return {"code": code, "candidates": candidates, "unreadable_invoices": unreadable}
