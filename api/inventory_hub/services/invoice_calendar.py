"""Bounded due-date projection using the same authoritative settlement snapshot."""
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import text

from inventory_hub.invoice_calendar_types import InvoiceCalendarQuery
from inventory_hub.services import invoice_finance as finance


MAX_CALENDAR_INVOICES = 5000


def payment_summary(items):
    """Cash still to pay, never netted against credits or unknown settlements."""
    groups = {}
    for item in items:
        currency = item['currency']
        group = groups.setdefault(currency, {'currency': currency, 'outstanding_amount': Decimal('0.00'),
                                             'unknown_count': 0, 'invoice_count': 0})
        group['invoice_count'] += 1
        if item['outstanding_amount'] is None:
            group['unknown_count'] += 1
        else:
            group['outstanding_amount'] += max(Decimal(item['outstanding_amount']), Decimal('0.00'))
    return [{**groups[key], 'outstanding_amount': None
             if groups[key]['unknown_count'] == groups[key]['invoice_count']
             else format(groups[key]['outstanding_amount'], '.2f')}
            for key in sorted(groups, key=lambda value: value or '')]


def planning_periods(items, first, last, *, weekly):
    result = []
    start = first
    while start <= last:
        if weekly:
            end = min(start + timedelta(days=6-start.weekday()), last)
        else:
            next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
            end = min(next_month - timedelta(days=1), last)
        selected = [item for item in items if start.isoformat() <= item['due_date'] <= end.isoformat()]
        result.append({'from': start.isoformat(), 'to': end.isoformat(),
                       'totals': payment_summary(selected), 'invoice_ids': [item['id'] for item in selected]})
        start = end + timedelta(days=1)
    return result


class InvoiceCalendarError(Exception):
    def __init__(self, code, status=422):
        super().__init__(code)
        self.code, self.status = code, status


def calendar_conditions(query):
    conditions = ["ui.direction=:direction", "ui.due_date BETWEEN :range_from AND :range_to"]
    params = {"direction": query.direction, "range_from": query.range_from, "range_to": query.range_to}
    if query.supplier_code:
        conditions.append("s.code=:supplier_code")
        params["supplier_code"] = query.supplier_code
    if query.f_supplier:
        owner = "s" if query.direction == "purchase" else "sh"
        conditions.append(f"({owner}.name ILIKE :supplier_search OR {owner}.code ILIKE :supplier_search)")
        params["supplier_search"] = f"%{query.f_supplier}%"
    if query.f_invoice_number:
        conditions.append("(ui.invoice_number ILIKE :invoice_search OR ui.original_filename ILIKE :invoice_search)")
        params["invoice_search"] = f"%{query.f_invoice_number}%"
    for field, column in (("date_from", "invoice_date"), ("date_to", "invoice_date"),
                          ("due_from", "due_date"), ("due_to", "due_date")):
        value = getattr(query, field)
        if value:
            operator = ">=" if field.endswith("from") else "<="
            conditions.append(f"ui.{column} {operator} :{field}")
            params[field] = value
    for field, operator in (("f_amount_min", ">="), ("f_amount_max", "<=")):
        value = getattr(query, field)
        if value is not None:
            conditions.append(f"ui.total_amount {operator} :{field}")
            params[field] = value
    if query.f_currency:
        conditions.append("ui.currency=:currency")
        params["currency"] = query.f_currency.upper()
    if query.receiving_status:
        if query.receiving_status == "not_started":
            conditions.append("ui.receiving_session_id IS NULL")
        else:
            conditions.append("rs.status=:receiving_status")
            params["receiving_status"] = query.receiving_status
    return " AND ".join(conditions), params


async def calendar(db, query, *, today=None):
    if not isinstance(query, InvoiceCalendarQuery):
        query = InvoiceCalendarQuery.model_validate(query)
    where, params = calendar_conditions(query)
    # Fetch one bounded ordered set, independent of the invoice table's page.
    # The extra row detects overflow before any partial calendar is returned.
    result = await db.execute(text(f"""SELECT ui.id,ui.invoice_number,ui.original_filename
        FROM uploaded_invoices ui
        LEFT JOIN suppliers s ON s.id=ui.supplier_id
        LEFT JOIN shops sh ON sh.id=ui.shop_id
        LEFT JOIN receiving_sessions rs ON rs.id=ui.receiving_session_id AND rs.supplier_id=ui.supplier_id
        WHERE {where} ORDER BY ui.due_date,ui.id LIMIT :calendar_limit"""),
        {**params, "calendar_limit": MAX_CALENDAR_INVOICES + 1})
    headers = [dict(row) for row in result.mappings().all()]
    if len(headers) > MAX_CALENDAR_INVOICES:
        raise InvoiceCalendarError("invoice_calendar_range_too_large", 409)
    financial = await finance.financial_rows(db,
        conditions=f"({where}) AND ui.id=ANY(CAST(:calendar_ids AS bigint[]))",
        params={**params, "calendar_ids": [row["id"] for row in headers]}) if headers else []
    by_id = {row["id"]: row for row in financial}
    items = []
    for header in headers:
        row = by_id.get(header["id"])
        if row is None:
            continue
        current = finance.snapshot(row, today=today)
        if query.payment_status and current["status"] != query.payment_status:
            continue
        if query.is_overdue is not None and current["is_overdue"] != query.is_overdue:
            continue
        items.append({"id": row["id"], "invoice_number": header["invoice_number"],
            "original_filename": header["original_filename"], "direction": row["direction"],
            "supplier_code": row["supplier_code"], "supplier_name": row["supplier_name"],
            "shop_code": row["shop_code"], "shop_name": row["shop_name"],
            "due_date": row["due_date"].isoformat(), "currency": current["currency"],
            "gross_amount": current["gross_amount"], "outstanding_amount": current["outstanding_amount"],
            "payment_status": current["status"], "is_overdue": current["is_overdue"]})
    return {"range_from": query.range_from.isoformat(), "range_to": query.range_to.isoformat(),
            "items": items, "total": len(items), "limit": MAX_CALENDAR_INVOICES,
            "totals": payment_summary(items),
            "weeks": planning_periods(items, query.range_from, query.range_to, weekly=True),
            "months": planning_periods(items, query.range_from, query.range_to, weekly=False)}
