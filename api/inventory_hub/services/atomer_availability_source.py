"""Exact Atomer source evidence for availability exports; no remote writes."""

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
import io
import csv

from lxml import etree
from inventory_hub.adapters.atomer_content import (
    _preflight,
    _text,
    MAX_ROWS,
    AtomerSourceError,
)
from inventory_hub.services.atomer_source_state import (
    parse_admin,
    flag,
    native_catalog_code,
    _admin_refs,
)
from inventory_hub.services.atomer_new_source import _csv, named


class ExportError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def safe_text(value, maximum=255):
    return (
        isinstance(value, str)
        and len(value) <= maximum
        and not any(ord(c) < 32 or ord(c) == 127 for c in value)
    )


def image_url(value):
    try:
        parsed = urlsplit(value or "")
        return (
            value
            if parsed.scheme in ("http", "https")
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            else None
        )
    except ValueError:
        return None


def parse_buyers(raw):
    stream = io.BytesIO(raw)
    digest, _ = _preflight(stream)
    rows, total, root = {}, 0, None
    try:
        for event, node in etree.iterparse(
            stream,
            events=("start", "end"),
            resolve_entities=False,
            load_dtd=False,
            no_network=True,
            huge_tree=False,
        ):
            if root is None:
                root = node
                if root.tag != "PRODUCTS":
                    raise ExportError("atomer_availability_invalid_buyers", 422)
            if event != "end" or node is root or node.getparent() is not root:
                continue
            total += 1
            if total > MAX_ROWS or node.tag != "PRODUCT":
                raise ExportError("atomer_availability_invalid_buyers", 422)
            code, parent = (_text(node, tag) for tag in ("PRODUCT_ID", "PARENT_ID"))
            if not code:
                raise ExportError("atomer_availability_missing_code", 422)
            value = dict(
                code=code,
                parent_code=parent,
                name=_text(node, "PRODUCT_NAME"),
                ean=_text(node, "EAN"),
                image=image_url(_text(node, "MAIN_IMAGE")),
                current_visibility=flag(_text(node, "VISIBILITY")),
                current_availability=_text(node, "AVAILABILITY").strip(),
                reasons=[],
            )
            if any(
                not safe_text(v) or v != v.strip() for v in (code, parent, value["ean"])
            ):
                value["reasons"].append("source_identity_invalid")
            if not safe_text(value["current_availability"], 100):
                value["reasons"].append("source_availability_invalid")
            if code in rows:
                rows[code]["reasons"] = ["source_duplicate_code"]
            else:
                rows[code] = value
            node.clear()
            while node.getprevious() is not None:
                del root[0]
    except (etree.XMLSyntaxError, AtomerSourceError):
        raise ExportError("atomer_availability_invalid_buyers", 422) from None
    if not rows:
        raise ExportError("atomer_availability_empty_source", 422)
    for row in rows.values():
        if row["parent_code"] == row["code"] or rows.get(row["parent_code"], {}).get(
            "parent_code"
        ):
            row["reasons"].append("source_parent_invalid")
    return digest, rows


def apply_admin(buyers, raw, complete):
    """The administrative export supplies exact catalog codes and parent state.

    Its current commercial cells take precedence over the periodically generated
    buyers feed. Conflicting identity evidence never silently changes ownership.
    """
    index = parse_admin(raw)
    if complete and (
        index.quarantined or index.quarantined_identifiers or index.quarantined_parents
    ):
        raise ExportError("atomer_availability_admin_incomplete", 422)
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        decoded = raw.decode("cp1250")
    delimiter = (
        csv.Sniffer().sniff(decoded.splitlines()[0], delimiters=";\t,").delimiter
    )
    header = next(csv.reader(io.StringIO(decoded), delimiter=delimiter))
    if header.count("Dodacia doba") != 1:
        raise ExportError("atomer_availability_admin_availability_missing", 422)
    cells, native_cells = defaultdict(list), defaultdict(list)
    for row in _csv(raw):
        code = native_catalog_code(
            named(row, "Kat. číslo"),
            named(row, "Kat. číslo rodiča"),
            named(row, "EAN kód"),
        )
        if code:
            cells[code].append(row)
        native = named(row, "Interné číslo").strip()
        if native:
            native_cells[native].append(row)
    refs = _admin_refs(index)
    result = {} if complete else {k: dict(v) for k, v in buyers.items()}
    for code, item in index.items():
        native_ids = item.get("internal_ids", (item.get("internal_id"),))
        raw_rows = [
            row
            for native in native_ids
            if native
            for row in native_cells.get(native, [])
        ]
        raw_rows = raw_rows or cells.get(code, [])
        if not raw_rows:
            if complete:
                raise ExportError("atomer_availability_admin_incomplete", 422)
            continue
        availability = {named(r, "Dodacia doba").strip() for r in raw_rows}
        pictures = {image_url(named(r, "Obrázky").split("|")[0]) for r in raw_rows}
        export_codes = {named(r, "Kat. číslo") for r in raw_rows}
        export_parents = {named(r, "Kat. číslo rodiča") for r in raw_rows}
        before = buyers.get(code, {})
        problems = []
        # Internal row IDs establish ancestry, never a replacement import SKU.
        if not item.get("has_catalog_code") or export_codes != {code}:
            problems.append("source_identity_invalid")
        if (
            item.get("conflict")
            or item.get("visibility_conflict")
            or len(availability) != 1
            or len(export_parents) != 1
        ):
            problems.append("source_duplicate_code")
        if (
            before
            and item.get("ean")
            and before.get("ean")
            and item["ean"] != before["ean"]
        ):
            problems.append("source_identity_conflict")
        parent = item.get("parent")
        if parent:
            parent = refs.get(parent, parent)
        if parent is None:
            problems.append("source_parent_unknown")
        value = next(iter(availability), "")
        if not safe_text(value, 100):
            problems.append("source_availability_invalid")
        export_parent = (
            next(iter(export_parents), "") if len(export_parents) == 1 else ""
        )
        if not safe_text(export_parent) or code in index.quarantined_identifiers:
            problems.append("source_identity_invalid")
        result[code] = dict(
            code=code,
            parent_code=parent or "",
            export_parent_code=export_parent,
            name=item.get("name") or before.get("name") or code,
            ean=item.get("ean") or before.get("ean") or "",
            image=next(iter(pictures)) if len(pictures) == 1 else before.get("image"),
            current_visibility=item.get("visibility"),
            current_availability=value,
            reasons=problems,
        )
    if complete and (
        not result
        or any(
            r["parent_code"] and r["parent_code"] not in result for r in result.values()
        )
    ):
        raise ExportError("atomer_availability_admin_incomplete", 422)
    return result


def desired_values(row, quantity, offer):
    """Known own pieces win; unknown own stock must not become a zero."""
    if quantity is None:
        return (
            row["current_visibility"],
            row["current_availability"],
            "stock_unconfirmed",
        )
    try:
        qty = Decimal(quantity)
    except (InvalidOperation, TypeError):
        return row["current_visibility"], row["current_availability"], "stock_invalid"
    if not qty.is_finite() or qty < 0:
        return row["current_visibility"], row["current_availability"], "stock_invalid"
    if qty > 0:
        return True, "SKLADOM", None
    label = (
        offer.get("label", "do 5 dní")
        if offer.get("fresh") and offer.get("available") is True
        else "overíme"
    )
    return row["current_visibility"], label, None


def assemble(source, projections, selected_ids=None):
    """Build parents and sellable rows without inventing absent source records."""
    parents = {
        r["parent_code"]
        for r in source.values()
        if r["parent_code"] and r["parent_code"] != r["code"]
    }
    result = []
    selected = set(selected_ids) if selected_ids is not None else None
    for code, original in source.items():
        if code in parents:
            continue
        row = dict(original, reasons=list(original["reasons"]))
        state = projections.get(code, {})
        if selected is not None and state.get("product_id") not in selected:
            continue
        row.update(
            kind="variant" if row["parent_code"] else "standalone",
            product_id=state.get("product_id"),
            sku=state.get("sku"),
            qty_available=state.get("quantity"),
            proposed_visibility=row["current_visibility"],
            proposed_availability=row["current_availability"],
        )
        if row["reasons"]:
            row["status"] = "blocked"
        elif not state.get("product_id"):
            row["status"] = "blocked" if state.get("reason") else "unmatched"
            row["reasons"] = [state.get("reason") or "hub_product_missing"]
        elif state.get("reason"):
            row["status"] = "blocked"
            row["reasons"] = [state["reason"]]
        else:
            visibility, label, error = desired_values(
                row, state.get("quantity"), state.get("offer", {})
            )
            row.update(proposed_visibility=visibility, proposed_availability=label)
            if error:
                row["status"] = "blocked"
                row["reasons"] = [error]
            elif row["current_visibility"] is None or not row["current_availability"]:
                row["status"] = "blocked"
                row["reasons"] = ["source_state_unknown"]
            else:
                row["status"] = (
                    "changed"
                    if (visibility, label)
                    != (row["current_visibility"], row["current_availability"])
                    else "unchanged"
                )
        result.append(row)
    by_parent = defaultdict(list)
    for row in result:
        if row["parent_code"] and row["parent_code"] != row["code"]:
            by_parent[row["parent_code"]].append(row)
    for code, children in by_parent.items():
        original = source.get(code)
        parent = dict(
            original
            or dict(
                code=code,
                parent_code="",
                name=code,
                ean="",
                image=None,
                current_visibility=None,
                current_availability="",
                reasons=["source_parent_missing"],
            )
        )
        parent.update(
            kind="parent",
            product_id=None,
            sku=None,
            qty_available=None,
            proposed_visibility=parent["current_visibility"],
            proposed_availability=parent["current_availability"],
            status="context",
        )
        enable = any(
            c["status"] not in ("blocked", "unmatched")
            and c.get("qty_available") is not None
            and Decimal(c["qty_available"]) > 0
            for c in children
        )
        if (
            enable
            and original
            and not parent["reasons"]
            and parent["current_visibility"] is False
        ):
            parent.update(proposed_visibility=True, status="changed")
        for child in children:
            child.update(
                parent_name=parent["name"],
                parent_image=parent["image"],
                parent_visibility=parent["current_visibility"],
            )
            if (
                child["current_visibility"] is False
                and child["proposed_visibility"] is True
                and (
                    not original
                    or parent["reasons"]
                    or parent["current_visibility"] is None
                )
            ):
                child.update(
                    status="blocked",
                    reasons=[*child["reasons"], "parent_state_unknown"],
                )
        result.append(parent)
    return sorted(
        result,
        key=lambda r: (r["parent_code"] or r["code"], r["kind"] != "parent", r["code"]),
    )


def summarize(rows):
    counts = Counter(r["status"] for r in rows)
    return dict(
        total=len(rows),
        changed=counts["changed"],
        blocked=counts["blocked"],
        unmatched=counts["unmatched"],
        unchanged=counts["unchanged"],
    )
