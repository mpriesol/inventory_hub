"""Visibility and merchandising evidence, preserving explicit false and unknown."""
import csv
import hashlib
import io
import re
import unicodedata
import zlib

from sqlalchemy import text
from inventory_hub.services.atomer_review_targets import ReviewError


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
        dialect = csv.Sniffer().sniff(decoded.splitlines()[0],delimiters=";\t,")
        reader = csv.DictReader(io.StringIO(decoded,newline=""),dialect=dialect)
        headers = reader.fieldnames or []
        cols = {normalized(h):h for h in headers}
        ids = [cols[k] for k in ("katalogovecislo","katalogovecislotovaru","katalogovecislozbozi","katcislo","cataloguecode","productid","internecislo","internicislo") if k in cols]
        active = next((cols[k] for k in ("viditelny","viditelnost","visibility","aktivny","aktivni") if k in cols),None)
        if not ids or not active or len(headers)!=len(set(headers)) or "" in headers:
            raise ValueError()
        flagcols = [h for h in headers if normalized(h) in ("akcia","akce","novinka","vypredaj","vyprodej","sale","tip","odporucame","doporucujeme","top","bestseller")
                    or re.match(r"^(priznak|stitek|stitok|oznacenie|flag|akc(ia|e)(od|do)|novinka(od|do))", normalized(h))]
        barcode_col = next((cols[k] for k in ("ean","eankod") if k in cols),None)
        result = {}
        for index,row in enumerate(reader):
            if index>=150_000 or None in row or any(v is None for v in row.values()):
                raise ValueError()
            # The catalog number is the buyers PRODUCT_ID. An internal row ID is a fallback only.
            code = next(((row[k] or "").strip() for k in ids if (row[k] or "").strip()),"")
            if not code:
                continue
            value = {"visibility":flag(row[active]),"visibility_raw":row[active],"ean":row[barcode_col].strip() if barcode_col else "", "flags":{h:row[h] for h in flagcols}}
            if code in result and result[code]!=value:
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
