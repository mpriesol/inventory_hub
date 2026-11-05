"""Visibility and merchandising evidence, preserving explicit false and unknown."""
import csv
from collections import defaultdict
import hashlib
import io
import re
import unicodedata
import zlib

from sqlalchemy import text
from inventory_hub.services.atomer_review_targets import ReviewError


CATALOG_CODES = ("katalogovecislo", "katalogovecislotovaru", "katalogovecislozbozi", "katcislo", "cataloguecode", "productid")
PARENT_CODES = ("katcislorodica", "katalogovecislorodica", "katcislorodice", "parentid")
ACTIVE_FIELDS = ("viditelny", "viditelnost", "visibility", "aktivny", "aktivni")
BARCODES = ("ean", "eankod")
NAMES = ("nazovtovaru", "nazevzbozi", "productname")
BRANDS = ("vyrobca", "vyrobce", "manufacturer")
INTERNAL_IDS = ("internecislo", "internicislo")
FLAGS = {"akcia", "akce", "novinka", "vypredaj", "vyprodej", "sale", "tip", "odporucany", "doporuceny",
         "odporucame", "doporucujeme", "top", "bestseller"}
FLAG_PREFIX = re.compile(r"^(priznak|stitek|stitok|oznacenie|flag|akc(ia|e)(od|do)|novinka(od|do))")


def flag(value):
    raw = (value or "").strip().casefold()
    if raw in ("1","true","áno","ano","yes"):
        return True
    if raw in ("0","false","nie","ne","no"):
        return False
    return None


def normalized(value):
    value = unicodedata.normalize("NFKD",value.casefold())
    return "".join(c for c in value if c.isascii() and c.isalnum())


def archive(fileobj,fmt):
    fileobj.seek(0)
    data = fileobj.read(105_000_001)
    if not data or len(data)>105_000_000:
        raise ReviewError("export_size",422)
    fileobj.seek(0)
    return hashlib.sha256(data).hexdigest(),fmt,zlib.compress(data,3)


async def save_archive(db,entry):
    if entry:
        digest,fmt,data = entry
        await db.execute(text("""
            INSERT INTO atomer_review_exports(sha256,format,compressed) VALUES(:sha,:fmt,:data)
            ON CONFLICT(sha256) DO NOTHING
        """),dict(sha=digest,fmt=fmt,data=data))


class AdminCatalog(dict):
    def __init__(self):
        super().__init__()
        self.quarantined = []
        self.quarantined_identifiers = set()
        self.quarantined_parents = set()


def _safe_id(value):
    return len(value) <= 255 and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _combine_admin_rows(rows):
    """Keep every identity as presence evidence, but expose only agreed values."""
    combined = rows[0].copy()
    if all(row == combined for row in rows):
        return combined
    combined["conflict"] = True
    combined["internal_ids"] = sorted({row["internal_id"] for row in rows if row["internal_id"]})
    combined["all_eans"] = sorted({row["ean"] for row in rows if row["ean"]})
    combined["all_parents"] = sorted({row["parent"] for row in rows if row["parent"]})
    for key in ("ean", "name", "brand", "parent", "visibility", "visibility_raw"):
        if any(row[key] != rows[0][key] for row in rows):
            combined[key] = None if key in ("visibility", "parent") else ""
    combined["visibility_conflict"] = any(row["visibility"] != rows[0]["visibility"] for row in rows)
    combined["flags"] = {k: v for k, v in rows[0]["flags"].items() if all(row["flags"].get(k) == v for row in rows)}
    combined["has_catalog_code"] = all(row["has_catalog_code"] for row in rows)
    return combined


def parse_admin(data):
    try:
        data.decode("utf-8-sig")
        encoding = "utf-8-sig"
    except UnicodeDecodeError:
        encoding = "cp1250"
    try:
        stream = io.TextIOWrapper(io.BytesIO(data), encoding=encoding, newline="")
        dialect = csv.Sniffer().sniff(stream.readline(), delimiters=";\t,")
        stream.seek(0)
        # A header without escaped quotes cannot reveal the quoting of multiline product descriptions.
        reader = csv.reader(stream, delimiter=dialect.delimiter, strict=True)
        headers = next(reader, [])
        fields = set(CATALOG_CODES + PARENT_CODES + ACTIVE_FIELDS + BARCODES + NAMES + BRANDS + INTERNAL_IDS)
        columns, flag_columns = {}, {}
        for position, heading in enumerate(headers):
            key = normalized(heading)
            is_flag = key in FLAGS or FLAG_PREFIX.match(key)
            if key not in fields and not is_flag:
                # Native exports repeat characteristic names. Their original positions stay in the archive.
                continue
            if key in columns:
                raise ValueError()
            columns[key] = position
            if is_flag:
                flag_columns[heading] = position

        def field(aliases):
            return next((columns[name] for name in aliases if name in columns), None)

        catalog, internal = field(CATALOG_CODES), field(INTERNAL_IDS)
        code_column = catalog if catalog is not None else internal
        active, parent = field(ACTIVE_FIELDS), field(PARENT_CODES)
        barcode, name, brand = field(BARCODES), field(NAMES), field(BRANDS)
        if code_column is None or active is None:
            raise ValueError()
        result, duplicates = AdminCatalog(), {}
        for index, row in enumerate(reader):
            if not row:
                continue
            if index >= 150_000 or len(row) != len(headers):
                raise ValueError()
            code = row[code_column].strip()
            if not any(row):
                continue
            # Only an explicit catalog number plus a parent column can introduce a new sellable identity.
            has_catalog_code = bool(code and catalog is not None)
            internal_id = row[internal].strip() if internal is not None else ""
            parent_code = row[parent].strip() if parent is not None and catalog is not None else None
            code = code or internal_id
            if parent_code in ("0", code):
                parent_code = ""
            ean = row[barcode].strip() if barcode is not None else ""
            if not _safe_id(internal_id):
                raise ValueError()
            if not code or not all(_safe_id(v) for v in (code, parent_code or "", ean)):
                if not internal_id:
                    raise ValueError()
                # Keep the exact bytes in the archive. Never synthesize a SKU from corrupt text.
                result.quarantined.append(internal_id)
                result.quarantined_identifiers.update(v for v in (code, ean) if v and _safe_id(v))
                if parent_code and _safe_id(parent_code):
                    result.quarantined_parents.add(parent_code)
                continue
            value = {"internal_id": internal_id, "has_catalog_code": has_catalog_code,
                     "visibility": flag(row[active]), "visibility_raw": row[active], "ean": ean,
                     "flags": {heading: row[pos] for heading, pos in flag_columns.items()}, "parent": parent_code,
                     "name": row[name].strip() if name is not None else "",
                     "brand": row[brand].strip() if brand is not None else ""}
            if code in result:
                duplicates.setdefault(code, [result[code]]).append(value)
            else:
                result[code] = value
        if not result:
            raise ValueError()
        result.update((code, _combine_admin_rows(rows)) for code, rows in duplicates.items())
        return result
    except (ValueError,csv.Error,IndexError):
        raise ReviewError("invalid_admin_export",422) from None


async def admin_index(db,digest):
    if not digest:
        return {}
    raw = await db.scalar(text("SELECT compressed FROM atomer_review_exports WHERE sha256=:sha AND format='admin_csv'"),{"sha":digest})
    return parse_admin(zlib.decompress(raw))


def _admin_refs(admin):
    """Both native IDs and catalog codes can occur in the parent column."""
    refs = {code: code for code in admin}
    for code, item in admin.items():
        for native_id in item.get("internal_ids", (item.get("internal_id"),)):
            if native_id:
                refs[native_id] = code if refs.get(native_id, code) == code else None
    return refs


def catalog_with_admin(buyers, admin, complete=False):
    """Keep buyers rows and add identities established by the administrative export."""
    result = {r["code"]: r for r in buyers}
    refs = _admin_refs(admin)
    xml_groups = defaultdict(set)
    for row in buyers:
        parent = row["parent"]
        if parent and parent != row["code"]:
            xml_groups[refs.get(parent, parent)].add(parent)
    parents = set(xml_groups)
    parents.update(refs.get(parent, parent) for parent in getattr(admin, "quarantined_parents", ()))
    for item in admin.values():
        for parent in item.get("all_parents", (item["parent"],)):
            if parent:
                parents.add(refs.get(parent, parent))
    if complete and (not admin or not (result.keys() | parents) <= admin.keys()
                     or any(item["parent"] is None or (not item.get("has_catalog_code", True) and code not in parents)
                            for code, item in admin.items())):
        raise ReviewError("incomplete_admin_export", 422)
    for code, item in admin.items():
        parent = item["parent"]
        parent_key = refs.get(parent, parent)
        groups = xml_groups.get(parent_key, set())
        ambiguous = bool(item.get("conflict") or (parent and parent_key is None) or len(groups) > 1
                         or (admin.get(parent_key) or {}).get("conflict"))
        original = result.get(code)
        if original:
            xml_parent = original["parent"] if original["parent"] != code else ""
            eans_differ = original["ean"] and item["ean"] and original["ean"] != item["ean"]
            if ambiguous or eans_differ or (parent is not None and parent_key != refs.get(xml_parent, xml_parent)):
                result[code] = dict(original, conflict=True)
        elif item.get("has_catalog_code", True) and parent is not None and code not in parents and (parent or complete):
            # A root in a selection may have children outside that selection.
            # Preserve an existing XML family key used by decisions and operator instructions.
            group = next(iter(groups)) if len(groups) == 1 else parent_key or parent
            result[code] = dict(code=code, parent=group, name=item["name"], brand=item["brand"],
                                ean=item["ean"], axes={}, duplicates=1, conflict=ambiguous,
                                source_visibility=None, visibility_conflict=False)
    return list(result.values())


def add_states(source, rows, admin):
    by_code = {r["code"]:r for r in source}
    refs = _admin_refs(admin)

    def evidence(code, other):
        original = by_code.get(code) or {}
        value = original.get("source_visibility")
        if original.get("visibility_conflict") or other.get("visibility_conflict"):
            return None,"conflict"
        if other:
            if value is not None and other["visibility"] is not None and value!=other["visibility"]:
                return None,"conflict"
            if other["visibility"] is not None:
                return other["visibility"],"admin_csv"
        return value, "buyers" if value is not None else "unknown"

    for row in rows:
        leaf, parent = row["code"], row["parent"] if row["kind"]=="variant" else row["code"]
        parent_code = refs.get(parent, parent) if row["kind"] == "variant" else leaf
        parent_record = admin.get(parent_code) or {}
        variant_record = admin.get(leaf) or {}
        state, origin = evidence(parent, parent_record)
        if parent_code is None:
            state, origin = None, "conflict"
        child, child_origin = evidence(leaf, variant_record)
        row["source_state"] = {"product":state,"product_origin":origin,
                               "variant":child if row["kind"]=="variant" else None,
                               "variant_origin":child_origin if row["kind"]=="variant" else "na",
                               "product_flags":parent_record.get("flags",{}),
                               "variant_flags":variant_record.get("flags",{}) if row["kind"]=="variant" else {}}
