"""Record one explicit settlement per selected invoice using the common ledger."""
from datetime import date, datetime, time, timezone
import hashlib
import json
from typing import Annotated
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import text

from inventory_hub.services import invoice_finance as finance


class BatchPaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    invoice_ids: list[Annotated[StrictInt, Field(gt=0)]] = Field(min_length=1, max_length=100)
    paid_date: date
    method: finance.PaymentMethod | None = None


async def operation(db, request_id):
    result = (await db.execute(text("""SELECT payload_hash, result
        FROM invoice_batch_payment_operations WHERE request_id=:id"""), {"id": request_id})).mappings().first()
    if result is None:
        raise finance.FinanceError("invoice_batch_payment_not_found", 404)
    return result


async def apply(db, request, *, actor):
    ids = sorted(set(request.invoice_ids))
    payload = {**request.model_dump(mode="json"), "invoice_ids": ids}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                     {"key": int.from_bytes(request.request_id.bytes[:8], "big", signed=True)})
    previous = (await db.execute(text("""SELECT payload_hash, result
        FROM invoice_batch_payment_operations WHERE request_id=:id"""), {"id": request.request_id})).mappings().first()
    if previous:
        if previous["payload_hash"] != digest:
            raise finance.FinanceError("invoice_payment_request_conflict")
        return previous["result"]

    rows = (await db.execute(text("""SELECT * FROM uploaded_invoices
        WHERE id=ANY(CAST(:ids AS bigint[])) ORDER BY id FOR UPDATE"""), {"ids": ids})).mappings().all()
    by_id = {row["id"]: row for row in rows}
    result = {"request_id": str(request.request_id), "paid": [], "skipped": []}
    for identifier in ids:
        row = by_id.get(identifier)
        if row is None or row["archived_at"] is not None or row["direction"] != "purchase":
            result["skipped"].append({"invoice_id": identifier, "reason": "unavailable"})
            continue
        current = await finance.detail(db, row)
        outstanding = current["outstanding_amount"]
        if current["status"] == "paid":
            reason = "paid"
        elif current["status"] == "unknown" or outstanding is None:
            reason = "needs_review"
        elif outstanding.startswith("-"):
            reason = "credit"
        else:
            settlement = finance.PaymentRequest(request_id=uuid5(request.request_id, str(identifier)),
                expected_revision=current["payment_revision"], kind="payment", amount=outstanding,
                paid_at=datetime.combine(request.paid_date, time.min, tzinfo=timezone.utc), method=request.method)
            await finance.apply_payment(db, identifier, settlement, actor=actor)
            result["paid"].append({"invoice_id": identifier, "amount": outstanding, "currency": current["currency"]})
            continue
        result["skipped"].append({"invoice_id": identifier, "reason": reason})
    await db.execute(text("""INSERT INTO invoice_batch_payment_operations(request_id,payload_hash,result,actor)
        VALUES(:id,:hash,CAST(:result AS jsonb),:actor)"""),
        {"id": request.request_id, "hash": digest, "result": json.dumps(result), "actor": actor})
    return result


async def result(db, request_id):
    return (await operation(db, request_id))["result"]
