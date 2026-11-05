"""Visibility and merchandising evidence, preserving explicit false and unknown."""
import csv
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


def parse_admin(data):
    for encoding in ("utf-8-sig","cp1250"):
        try:
            decoded = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ReviewError("invalid_admin_export",422)
    try:
        stream = io.StringIO(decoded, newline="")
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
        result = {}
        for index, row in enumerate(reader):
            if not row:
                continue
            if index >= 150_000 or len(row) != len(headers):
                raise ValueError()
            code = row[code_column].strip()
            if not any(row):
                continue
            # Only an explicit catalog number plus a parent column can introduce a new sellable identity.
            parent_code = row[parent].strip() if parent is not None and catalog is not None and code else None
            if not code and internal is not None:
                code = row[internal].strip()
            if parent_code in ("0", code):
                parent_code = ""
            ean = row[barcode].strip() if barcode is not None else ""
            if not code or any(len(v) > 255 or any(ord(c) < 32 or ord(c) == 127 for c in v)
                               for v in (code, parent_code or "", ean)):
                raise ValueError()
            value = {"visibility": flag(row[active]), "visibility_raw": row[active], "ean": ean,
                     "flags": {heading: row[pos] for heading, pos in flag_columns.items()}, "parent": parent_code,
                     "name": row[name].strip() if name is not None else "",
                     "brand": row[brand].strip() if brand is not None else ""}
            if code in result and result[code] != value:
                raise ValueError()
            result[code] = value
        if not result:
            raise ValueError()
        return result
    except (ValueError,csv.Error,IndexError):
        raise ReviewError("invalid_admin_export",422) from None


async def admin_index(db,digest):
    if not digest:
        return {}
    raw = await db.scalar(text("SELECT compressed FROM atomer_review_exports WHERE sha256=:sha AND format='admin_csv'"),{"sha":digest})
    return parse_admin(zlib.decompress(raw))


def catalog_with_admin(buyers, admin, complete=False):
    """Keep buyers rows and add identities established by the administrative export."""
    result = {r["code"]: r for r in buyers}
    parents = {r["parent"] for r in buyers if r["parent"] and r["parent"] != r["code"]}
    parents.update(item["parent"] for item in admin.values() if item["parent"])
    if complete and (not admin or not (result.keys() | parents) <= admin.keys()
                     or any(item["parent"] is None for item in admin.values())):
        raise ReviewError("incomplete_admin_export", 422)
    for code, item in admin.items():
        parent = item["parent"]
        original = result.get(code)
        if original:
            xml_parent = original["parent"] if original["parent"] != code else ""
            eans_differ = original["ean"] and item["ean"] and original["ean"] != item["ean"]
            if eans_differ or (parent is not None and parent != xml_parent):
                result[code] = dict(original, conflict=True)
        elif parent is not None and code not in parents and (parent or complete):
            # A root in a selection may have children outside that selection.
            result[code] = dict(code=code, parent=parent, name=item["name"], brand=item["brand"],
                                ean=item["ean"], axes={}, duplicates=1, conflict=False,
                                source_visibility=None, visibility_conflict=False)
    return list(result.values())


def add_states(source, rows, admin):
    by_code = {r["code"]:r for r in source}
    def evidence(code):
        original = by_code.get(code) or {}
        other = admin.get(code)
        value = original.get("source_visibility")
        if original.get("visibility_conflict"):
            return None,"conflict"
        if other is not None:
            if value is not None and other["visibility"] is not None and value!=other["visibility"]:
                return None,"conflict"
            if other["visibility"] is not None:
                return other["visibility"],"admin_csv"
        return value, "buyers" if value is not None else "unknown"

    for row in rows:
        leaf, parent = row["code"], row["parent"] if row["kind"]=="variant" else row["code"]
        state, origin = evidence(parent)
        child, child_origin = evidence(leaf)
        row["source_state"] = {"product":state,"product_origin":origin,
                               "variant":child if row["kind"]=="variant" else None,
                               "variant_origin":child_origin if row["kind"]=="variant" else "na",
                               "product_flags":(admin.get(parent) or {}).get("flags",{}),
                               "variant_flags":(admin.get(leaf) or {}).get("flags",{}) if row["kind"]=="variant" else {}}
