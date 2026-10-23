"""Invoice amounts and append-only settlements; no FX, stock or bank writes."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator
from sqlalchemy import text


CENT = Decimal("0.01")
MAX_AMOUNT = Decimal("999999999999.99")


class FinanceError(Exception):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


def money(value):
    return format(value or Decimal(0), ".2f") if value is not None else None


def amount_value(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or abs(amount) > MAX_AMOUNT or amount != amount.quantize(CENT):
            raise ValueError("Use a finite amount with at most two decimal places")
        return amount
    except (InvalidOperation, TypeError):
        raise ValueError("Use a finite amount with at most two decimal places") from None


class PaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_revision: StrictInt = Field(ge=0)
    kind: Literal["payment", "refund"]
    amount: StrictStr = Field(max_length=32)
    paid_at: datetime
    note: StrictStr = Field(default="", max_length=2000)
    opening_paid_amount: StrictStr | None = Field(default=None, max_length=32)

    @field_validator("amount", "opening_paid_amount")
    @classmethod
    def valid_amount(cls, value):
        if value is not None:
            amount_value(value)
        return value

    @field_validator("amount")
    @classmethod
    def positive_amount(cls, value):
        if Decimal(value) <= 0:
            raise ValueError("The settlement amount must be positive")
        return value

    @field_validator("paid_at")
    @classmethod
    def explicit_timezone(cls, value):
        if value.tzinfo is None:
            raise ValueError("Supply the payment time with its timezone")
        return value


class ReversalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_revision: StrictInt = Field(ge=0)
    note: StrictStr = Field(default="", max_length=2000)


def derived_totals(total_amount, vat_rate, vat_included):
    """Only derive from an explicit amount, VAT basis and rate."""
    if total_amount is None:
        return {"total_without_vat": None, "vat_amount": None, "total_with_vat": None}
    total = amount_value(total_amount)
    if vat_included is None:
        return {"total_without_vat": None, "vat_amount": None, "total_with_vat": None}
    if vat_rate is None:
        return {"total_without_vat": None if vat_included else total,
                "vat_amount": None, "total_with_vat": total if vat_included else None}
    rate = Decimal(str(vat_rate))
    if not rate.is_finite() or rate < 0 or rate > 100:
        raise FinanceError("invoice_vat_rate_invalid", 422)
    factor = 1 + rate / 100
    net = (total / factor).quantize(CENT, rounding=ROUND_HALF_UP) if vat_included else total
    gross = total if vat_included else (net * factor).quantize(CENT, rounding=ROUND_HALF_UP)
    if abs(gross) > MAX_AMOUNT:
        raise FinanceError("invoice_amount_out_of_range", 422)
    return {"total_without_vat": net, "vat_amount": gross - net, "total_with_vat": gross}


def gross_amount(row):
    if row.get("total_with_vat") is not None:
        amount = Decimal(row["total_with_vat"])
    else:
        amount = derived_totals(row.get("total_amount"), row.get("vat_rate"), row.get("vat_included"))["total_with_vat"]
    # Issuers represent credit-note totals both ways. Financial effect follows
    # the explicit document type; the archived source amounts remain untouched.
    return -abs(amount) if amount is not None and row.get("document_type") == "credit_note" else amount


def source_paid_amount(row):
    if row.get("source_paid_amount") is not None:
        amount = Decimal(row["source_paid_amount"])
    elif row.get("paid_amount") is not None:
        amount = Decimal(row["paid_amount"])
    # An explicit source observation of unpaid is evidence of zero. The old
    # column's default 'unpaid' alone is not evidence about a historical file.
    elif row.get("source_payment_status") == "unpaid":
        amount = Decimal(0)
    else:
        return None
    return -abs(amount) if row.get("document_type") == "credit_note" else amount


def balance_valid(gross, settled):
    return gross is not None and settled is not None and min(gross, 0) <= settled <= max(gross, 0)


def snapshot(row, payments=(), *, today=None):
    today = today or date.today()
    gross = gross_amount(row)
    currency = row.get("currency")
    issues = []
    if payments:
        settled = sum((Decimal(item["amount"]) for item in payments), Decimal(0))
        if any(item["currency"] != currency for item in payments):
            issues.append("invoice_payment_currency_conflict")
    elif row.get("ledger_count", 0):
        settled = row["ledger_paid_amount"]
        if row.get("ledger_currency") != currency:
            issues.append("invoice_payment_currency_conflict")
    else:
        settled = source_paid_amount(row)
    if not currency:
        issues.append("invoice_currency_unknown")
    if gross is not None and settled is not None and not balance_valid(gross, settled):
        issues.append("invoice_payment_balance_conflict")
    outstanding = gross - settled if gross is not None and settled is not None else None
    if gross is None or settled is None or issues:
        status = "unknown"
    else:
        status = "paid" if settled == gross else "unpaid" if settled == 0 else "partial"
    due = row.get("due_date")
    overdue = bool(due and due < today and outstanding is not None and outstanding != 0 and status != "unknown")
    reversed_by = {item["reverses_payment_id"]: item["id"] for item in payments if item.get("reverses_payment_id")}
    return {
        "payment_revision": row.get("payment_revision", 0), "currency": currency,
        "gross_amount": money(gross), "paid_amount": money(settled),
        "outstanding_amount": money(outstanding), "status": status, "is_overdue": overdue,
        "opening_required": not payments and not row.get("ledger_count", 0) and settled is None,
        "issues": issues,
        "payments": [{"id": item["id"], "kind": item["kind"], "amount": money(item["amount"]),
            "currency": item["currency"], "paid_at": item["paid_at"].isoformat() if item["paid_at"] else None,
            "note": item["note"], "actor": item["actor"], "reverses_payment_id": item["reverses_payment_id"],
            "reversed_by_id": reversed_by.get(item["id"]), "created_at": item["created_at"].isoformat()}
            for item in payments],
    }


async def payment_rows(db, invoice_id):
    result = await db.execute(text("SELECT * FROM invoice_payments WHERE invoice_id=:id ORDER BY id"), {"id": invoice_id})
    return [dict(row) for row in result.mappings().all()]


async def detail(db, row):
    return snapshot(dict(row), await payment_rows(db, row["id"]))


async def update_metadata(db, invoice_id, changes, expected_revision):
    allowed = {"invoice_number", "invoice_date", "due_date", "currency", "total_amount", "vat_rate", "vat_included", "items_count", "notes"}
    if not changes or changes.keys() - allowed:
        raise FinanceError("invoice_metadata_invalid", 422)
    found = (await db.execute(text("SELECT * FROM uploaded_invoices WHERE id=:id FOR UPDATE"), {"id": invoice_id})).mappings().first()
    if found is None:
        raise FinanceError("invoice_not_found", 404)
    row = dict(found)
    if row["payment_revision"] != expected_revision:
        raise FinanceError("invoice_payment_revision_changed")
    changed_fields = {key for key, value in changes.items() if row.get(key) != value}
    if row.get("source_kind") in ("supplier", "shop") and changed_fields - {"notes", "due_date"}:
        raise FinanceError("invoice_source_facts_read_only")
    monetary = {"currency", "total_amount", "vat_rate", "vat_included"}
    payments = await payment_rows(db, invoice_id)
    if payments and changed_fields & monetary:
        raise FinanceError("invoice_payment_financial_basis_locked")
    updated = {**row, **changes}
    if changes.keys() & {"total_amount", "vat_rate", "vat_included"}:
        derived = derived_totals(updated.get("total_amount"), updated.get("vat_rate"), updated.get("vat_included"))
        changes = {**changes, **derived}
        updated.update(derived)
    settled = source_paid_amount(row)
    if settled is not None and settled != 0 and not payments:
        if "currency" in changed_fields:
            raise FinanceError("invoice_payment_financial_basis_locked")
        if changed_fields & monetary and not balance_valid(gross_amount(updated), settled):
            raise FinanceError("invoice_payment_balance_conflict")
    assignments = [f"{key}=:{key}" for key in changes]
    await db.execute(text(f"""UPDATE uploaded_invoices SET {','.join(assignments)},
        payment_revision=payment_revision+1,updated_at=NOW() WHERE id=:id"""), {"id": invoice_id, **changes})
    return {"id": invoice_id, "payment_revision": expected_revision + 1, "message": "Invoice updated"}


async def operation_result(db, invoice_id, request_id):
    result = await db.execute(text("SELECT invoice_id, result FROM invoice_payment_operations WHERE request_id=:request_id"),
                              {"request_id": request_id})
    operation = result.mappings().first()
    if operation is None or operation["invoice_id"] != invoice_id:
        raise FinanceError("invoice_payment_operation_not_found", 404)
    return operation["result"]


async def apply_payment(db, invoice_id, request, *, actor, reverses_payment_id=None):
    """Serialize with metadata/source edits; replay is checked before revision."""
    payload_hash = sha256(json.dumps({"invoice_id": invoice_id, "reverses_payment_id": reverses_payment_id,
        "request": request.model_dump(mode="json")}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    # The UUID lock also serializes accidental reuse against another invoice.
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                     {"key": int.from_bytes(request.request_id.bytes[:8], "big", signed=True)})
    replay = (await db.execute(text("SELECT invoice_id,payload_hash,result FROM invoice_payment_operations WHERE request_id=:request_id"),
                              {"request_id": request.request_id})).mappings().first()
    if replay:
        if replay["invoice_id"] != invoice_id or replay["payload_hash"] != payload_hash:
            raise FinanceError("invoice_payment_request_conflict")
        return replay["result"]
    found = (await db.execute(text("SELECT * FROM uploaded_invoices WHERE id=:id FOR UPDATE"), {"id": invoice_id})).mappings().first()
    if found is None:
        raise FinanceError("invoice_not_found", 404)
    row = dict(found)
    if row["payment_revision"] != request.expected_revision:
        raise FinanceError("invoice_payment_revision_changed")
    currency = row.get("currency")
    if not currency or len(currency) != 3:
        raise FinanceError("invoice_currency_unknown")
    gross = gross_amount(row)
    if gross is None:
        raise FinanceError("invoice_total_unknown")
    payments = await payment_rows(db, invoice_id)
    if any(item["currency"] != currency for item in payments):
        raise FinanceError("invoice_payment_currency_conflict")
    settled = sum((item["amount"] for item in payments), Decimal(0)) if payments else source_paid_amount(row)
    opening = None
    if not payments:
        supplied = getattr(request, "opening_paid_amount", None)
        if settled is None:
            if supplied is None:
                raise FinanceError("invoice_payment_opening_required")
            settled = amount_value(supplied)
        elif supplied is not None and amount_value(supplied) != settled:
            raise FinanceError("invoice_payment_opening_conflict")
        opening = settled
    elif getattr(request, "opening_paid_amount", None) is not None:
        raise FinanceError("invoice_payment_opening_already_recorded")
    if not balance_valid(gross, settled):
        raise FinanceError("invoice_payment_balance_conflict")
    if reverses_payment_id is not None:
        original = next((item for item in payments if item["id"] == reverses_payment_id), None)
        if original is None:
            raise FinanceError("invoice_payment_not_found", 404)
        if original["kind"] in ("opening", "reversal") or any(item["reverses_payment_id"] == original["id"] for item in payments):
            raise FinanceError("invoice_payment_not_reversible")
        delta, kind, paid_at = -original["amount"], "reversal", datetime.now(timezone.utc)
    else:
        sign = Decimal(-1) if gross < 0 else Decimal(1)
        delta = amount_value(request.amount) * sign * (-1 if request.kind == "refund" else 1)
        kind, paid_at = request.kind, request.paid_at
    if not balance_valid(gross, settled + delta):
        raise FinanceError("invoice_payment_amount_exceeds_balance")
    insert_sql = text("""INSERT INTO invoice_payments(invoice_id,kind,amount,currency,paid_at,note,actor,reverses_payment_id)
        VALUES(:id,:kind,:amount,:currency,:paid_at,:note,:actor,:reverses_payment_id)""")
    if opening is not None:
        await db.execute(insert_sql, {"id": invoice_id, "kind": "opening", "amount": opening, "currency": currency,
            "paid_at": row.get("source_paid_at") or row.get("paid_at"), "note": "Opening paid balance confirmed before first manual settlement",
            "actor": actor, "reverses_payment_id": None})
    await db.execute(insert_sql, {"id": invoice_id, "kind": kind, "amount": delta, "currency": currency,
        "paid_at": paid_at, "note": request.note, "actor": actor, "reverses_payment_id": reverses_payment_id})
    row["payment_revision"] += 1
    current = snapshot(row, await payment_rows(db, invoice_id))
    await db.execute(text("""UPDATE uploaded_invoices SET payment_revision=:revision,payment_status=:status,
        paid_amount=:amount,paid_at=:paid_at,updated_at=NOW() WHERE id=:id"""),
        {"id": invoice_id, "revision": row["payment_revision"], "status": current["status"],
         "amount": settled + delta, "paid_at": paid_at if settled + delta != 0 else None})
    response = {"invoice_id": invoice_id, "request_id": str(request.request_id),
                "payment_revision": row["payment_revision"], "finance": current}
    await db.execute(text("""INSERT INTO invoice_payment_operations(request_id,invoice_id,payload_hash,result)
        VALUES(:request_id,:id,:payload_hash,CAST(:result AS jsonb))"""),
        {"request_id": request.request_id, "id": invoice_id, "payload_hash": payload_hash, "result": json.dumps(response)})
    return response


def sorted_invoice_ids(rows, finances, *, sort_by, descending=False):
    """Order displayed finance values exactly, with stable ties and unknowns last."""
    field = {"total_amount": "gross_amount", "outstanding_amount": "outstanding_amount",
             "payment_status": "status"}[sort_by]
    def value(row):
        found = finances[row["id"]][field]
        if field == "status":
            return {"unpaid": 0, "partial": 1, "paid": 2}.get(found)
        return Decimal(found) if found is not None else None
    ordered = sorted(rows, key=lambda row: row["id"])
    known = [row for row in ordered if value(row) is not None]
    unknown = [row for row in ordered if value(row) is None]
    known.sort(key=value, reverse=descending)
    return [row["id"] for row in known + unknown]


async def financial_rows(db, *, conditions="1=1", params=None):
    result = await db.execute(text(f"""SELECT ui.id,ui.supplier_id,ui.shop_id,ui.direction,ui.document_type,
        ui.invoice_date,ui.due_date,ui.currency,ui.total_amount,ui.total_with_vat,ui.vat_rate,ui.vat_included,
        ui.source_paid_amount,ui.source_payment_status,ui.paid_amount,ui.payment_revision,
        s.code AS supplier_code,s.name AS supplier_name,
        sh.code AS shop_code,sh.name AS shop_name,ledger.ledger_count,ledger.ledger_paid_amount,ledger.ledger_currency
        FROM uploaded_invoices ui LEFT JOIN suppliers s ON ui.supplier_id=s.id
        LEFT JOIN shops sh ON ui.shop_id=sh.id
        LEFT JOIN receiving_sessions rs ON ui.receiving_session_id=rs.id AND rs.supplier_id=ui.supplier_id
        LEFT JOIN (SELECT invoice_id,COUNT(*) AS ledger_count,SUM(amount) AS ledger_paid_amount,
            MIN(currency) AS ledger_currency FROM invoice_payments GROUP BY invoice_id) ledger ON ledger.invoice_id=ui.id
        WHERE {conditions}"""), params or {})
    return [dict(row) for row in result.mappings().all()]


def summarize(rows, *, today=None):
    today = today or date.today()
    by_currency, by_month, by_age = {}, {}, {}
    for row in rows:
        finance = snapshot(row, today=today)
        currency = finance["currency"]
        group = by_currency.setdefault(currency, {"currency": currency, "invoice_count": 0,
            "total_amount": Decimal(0), "outstanding_amount": Decimal(0), "overdue_amount": Decimal(0),
            "unpaid_count": 0, "overdue_count": 0, "unknown_amount_count": 0,
            "unknown_outstanding_count": 0, "unknown_overdue_count": 0})
        group["invoice_count"] += 1
        gross, outstanding = finance["gross_amount"], finance["outstanding_amount"]
        if gross is None:
            group["unknown_amount_count"] += 1
        else:
            group["total_amount"] += Decimal(gross)
        if outstanding is None or finance["status"] == "unknown":
            group["unknown_outstanding_count"] += 1
            if row.get("due_date") and row["due_date"] < today:
                group["unknown_overdue_count"] += 1
        else:
            group["outstanding_amount"] += Decimal(outstanding)
            group["unpaid_count"] += Decimal(outstanding) != 0
            if finance["is_overdue"]:
                group["overdue_count"] += 1
                group["overdue_amount"] += Decimal(outstanding)
            if Decimal(outstanding) != 0:
                days = (today - row["due_date"]).days if row.get("due_date") else None
                bucket = "unknown_due" if days is None else "not_due" if days <= 0 else "1_30" if days <= 30 else "31_60" if days <= 60 else "61_90" if days <= 90 else "over_90"
                aging = by_age.setdefault((bucket, currency), {"bucket": bucket, "currency": currency,
                    "outstanding_amount": Decimal(0), "invoice_count": 0})
                aging["outstanding_amount"] += Decimal(outstanding)
                aging["invoice_count"] += 1
        if row.get("invoice_date"):
            key = (row["invoice_date"].strftime("%Y-%m"), currency)
            month = by_month.setdefault(key, {"month": key[0], "currency": currency, "invoice_count": 0,
                "total_amount": Decimal(0), "unknown_amount_count": 0})
            month["invoice_count"] += 1
            if gross is None:
                month["unknown_amount_count"] += 1
            else:
                month["total_amount"] += Decimal(gross)
    groups = []
    for currency in sorted(by_currency, key=lambda value: value or ""):
        group = by_currency[currency]
        group["total_amount"] = None if group["unknown_amount_count"] else money(group["total_amount"])
        group["outstanding_amount"] = None if group["unknown_outstanding_count"] else money(group["outstanding_amount"])
        group["unpaid_amount"] = group["outstanding_amount"]
        group["overdue_amount"] = None if group["unknown_overdue_count"] else money(group["overdue_amount"])
        groups.append(group)
    for month in by_month.values():
        month["total_amount"] = None if month["unknown_amount_count"] else money(month["total_amount"])
    for aging in by_age.values():
        aging["outstanding_amount"] = money(aging["outstanding_amount"])
    single = groups[0] if len(groups) == 1 and groups[0]["currency"] else None
    return {"total_invoices": len(rows), "filtered_count": len(rows),
        "unpaid_count": sum(item["unpaid_count"] for item in groups),
        "overdue_count": sum(item["overdue_count"] for item in groups),
        "currency": single["currency"] if single else None,
        "total_amount": single["total_amount"] if single else None,
        "filtered_total": single["total_amount"] if single else None,
        "unpaid_amount": single["unpaid_amount"] if single else None,
        "unpaid_total": single["unpaid_amount"] if single else None,
        "overdue_amount": single["overdue_amount"] if single else None,
        "totals_by_currency": groups,
        "monthly": sorted(by_month.values(), key=lambda item: (item["month"], item["currency"] or "")),
        "aging": sorted(by_age.values(), key=lambda item: (item["bucket"], item["currency"] or ""))}
