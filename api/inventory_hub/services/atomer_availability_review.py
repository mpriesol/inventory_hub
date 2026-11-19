"""Shared server-side selection for paginated reviews and whole-result edits."""

from decimal import Decimal
import unicodedata


def editable(row):
    unconfirmed = row.get("reasons") == ["stock_unconfirmed"] and bool(row.get("product_id"))
    return (
        (row["status"] not in ("blocked", "unmatched") or unconfirmed)
        and row.get("current_visibility") is not None
        and bool(row.get("current_availability"))
        and (not row.get("reasons") or unconfirmed)
    )


def normalize(value):
    return "".join(
        c for c in unicodedata.normalize("NFKD", str(value or "").casefold())
        if not unicodedata.combining(c)
    )


def matches(row, filters):
    if filters.view == "changes" and row["status"] != "changed":
        return False
    if filters.view == "issues" and row["status"] not in ("blocked", "unmatched") and not row.get("warnings"):
        return False
    for field in ("kind", "status"):
        if getattr(filters, field) != "all" and row[field] != getattr(filters, field):
            return False
    for field, target in (("visibility", "proposed_visibility"), ("current_visibility", "current_visibility"),
                          ("proposed_visibility", "proposed_visibility")):
        chosen = getattr(filters, field)
        if chosen != "all" and row.get(target) is not {"visible": True, "hidden": False, "unknown": None}[chosen]:
            return False
    for field, target in (("availability", "proposed_availability"), ("current_availability", "current_availability"),
                          ("proposed_availability", "proposed_availability")):
        chosen = getattr(filters, field)
        if chosen is not None and row.get(target) != chosen:
            return False
    for field in ("include_in_export", "manual_override"):
        chosen = getattr(filters, field)
        if chosen is not None and bool(row.get(field, field == "include_in_export")) != chosen:
            return False
    terms = normalize(filters.search).split()
    haystack = normalize(" ".join(str(row.get(k) or "") for k in (
        "code", "parent_code", "name", "parent_name", "sku", "ean", "supplier_source"
    )))
    return all(term in haystack for term in terms)


def filter_rows(all_rows, filters, sort="code", direction="asc"):
    """Sort complete families, retain parents as context, select only real matches."""
    matching = [r for r in all_rows if matches(r, filters)]
    codes = {r["code"] for r in matching}
    parents = {r["parent_code"] for r in matching if r.get("parent_code")}
    visible = [r for r in all_rows if r["code"] in codes or r["kind"] == "parent" and r["code"] in parents]
    families = {}
    for row in visible:
        families.setdefault(row["parent_code"] or row["code"], []).append(row)

    def value(row):
        cell = row.get(sort)
        if sort == "qty_available":
            return (cell is None, Decimal(cell or 0), row["code"])
        if sort in ("current_visibility", "proposed_visibility", "include_in_export", "manual_override"):
            if sort in ("include_in_export", "manual_override"):
                cell = row.get(sort, sort == "include_in_export")
            return (cell is None, bool(cell), row["code"])
        return (cell is None, normalize(cell), row["code"])

    reverse = direction == "desc"
    ordered = sorted(families.values(), key=lambda group: value(min(
        (r for r in group if r["code"] in codes), key=value
    )), reverse=reverse)
    rows = []
    for group in ordered:
        rows.extend(sorted((r for r in group if r["kind"] == "parent"), key=value, reverse=reverse))
        rows.extend(sorted((r for r in group if r["kind"] != "parent"), key=value, reverse=reverse))
    return rows, matching


def selected_rows(rows, selection):
    wanted, excluded = set(selection.codes), set(selection.excluded_codes)
    return [r for r in rows if r["code"] not in excluded and (
        matches(r, selection.filter) if selection.all_filtered else r["code"] in wanted
    )]
