"""Seed a verified adapter checkpoint in ledger tests, not source-validation tests.

These tests construct receiving rows directly instead of uploading documents.
Source-validation tests must exercise the real stored original/parser boundary.
"""
import hashlib
from sqlalchemy import select
from inventory_hub.db_models_ext import ReceivingLine
from inventory_hub.services.receiving_source import ReceivingSource


async def verified_receipt(db, receipt):
    await db.flush()
    lines = list(await db.scalars(select(ReceivingLine).where(ReceivingLine.session_id == receipt.id)
                                  .order_by(ReceivingLine.line_number)))
    rows = [{"supplier_sku": line.supplier_sku, "ean": line.ean, "description": line.description,
             "ordered_qty": line.ordered_qty, "unit_price": line.unit_price} for line in lines]
    identity = hashlib.sha256(f"synthetic-adapter:{receipt.id}:{receipt.invoice_number}".encode()).hexdigest()
    source = ReceivingSource(rows, identity, identity, receipt.invoice_number, "synthetic_adapter")
    receipt.source_hash, receipt.import_source = source.source_hash, source.adapter
    receipt.invoice_currency, receipt.vat_included = "EUR", False
    receipt.session_data = {**(receipt.session_data or {}), "receiving_source": source.snapshot()}
