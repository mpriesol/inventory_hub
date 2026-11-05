"""Visibility and merchandising evidence, preserving explicit false and unknown."""
import csv
from collections import defaultdict
import hashlib
import io
import json
import re
import unicodedata
import zlib

from sqlalchemy import text
from inventory_hub.services.atomer_review_targets import ReviewError
from inventory_hub.services.product_identity import verified_barcodes


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
        self.exact_roots = defaultdict(lambda: defaultdict(list))
        self.parent_spellings = {}
        self.literal_children = {}
        self.parent_code_aliases = {}
        self.quarantined = []
        self.quarantined_identifiers = set()
        self.quarantined_parents = set()


def _safe_id(value):
    return len(value) <= 255 and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def native_catalog_code(raw, parent, ean):
    """Recover an ASCII SKU embedded as UTF-16LE only with a matching parent and valid GTIN."""
    code = (raw or '').strip()
    if '\x00' not in code:
        return code
    prefix = (parent or '').strip().removesuffix('x').removesuffix('X')
    if len(prefix)<3 or (ean or '').strip() not in verified_barcodes([ean]):
        return code
    # Atomer can export a single UTF-16LE cell inside the otherwise single-byte CSV.
    # Accept only a lossless alternating-byte encoding of an entire SKU.
    if not re.fullmatch(r'[a-zA-Z0-9_-](?:\x00[a-zA-Z0-9._-]){3,99}\x00?', code):
        return code
    candidate = code[::2]
    encoded = candidate.encode('utf-16le')
    if code.encode('ascii') not in (encoded, encoded[:-1]) or not candidate.casefold().startswith(prefix.casefold()+'-'):
        return code
    return candidate


def separate_parent(code, identities, existing_codes):
    """Resolve a parent and its own sellable only when their exact raw identifiers distinguish both roles."""
    if len(identities)!=2:
        return None
    roots = [(raw, native) for raw, parent, native in identities if raw.strip()==code and raw!=code
             and parent.strip() in ('','0') and native.isascii() and native.isdigit()]
    if len(roots)!=1:
        return None
    spelling, alias = roots[0]
    child = [(raw,parent,native) for raw,parent,native in identities
             if raw==code and parent==spelling and native.isascii() and native.isdigit() and native!=alias]
    return roots[0] if len(child)==1 and alias not in existing_codes else None


def _combine_admin_rows(rows):
    """Keep every identity as presence evidence, but expose only agreed values."""
    combined = rows[0].copy()
    if all(row == combined for row in rows):
        return combined
    # A repeated physical leaf can have two administrative row numbers and galleries.
    # Preserve both native references only when every commercial cell agrees.
    if (combined.get("parent") and combined.get("ean") in verified_barcodes([combined.get("ean")])
            and len({row.get("commercial_signature") for row in rows}) == 1
            and combined.get("commercial_signature")
            and all({k:v for k,v in r.items() if k!="internal_id"}==
                    {k:v for k,v in combined.items() if k!="internal_id"} for r in rows)):
        combined["internal_ids"] = sorted({row["internal_id"] for row in rows if row["internal_id"]})
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
        commercial = [i for i,h in enumerate(headers) if i!=internal and normalized(h)!='obrazky']
        result, duplicates = AdminCatalog(), {}
        blank_roots = defaultdict(list)
        blank_parent_rows = []
        overlapping_roles = defaultdict(list)
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
            ean = row[barcode].strip() if barcode is not None else ""
            code = native_catalog_code(code,parent_code,ean) or internal_id
            if parent_code in ("0", code):
                parent_code = ""
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
            signature = hashlib.sha256(json.dumps([row[i] for i in commercial],ensure_ascii=False,
                                                   separators=(',',':')).encode()).digest()
            value = {"commercial_signature":signature.hex(), "internal_id": internal_id, "has_catalog_code": has_catalog_code,
                     "visibility": flag(row[active]), "visibility_raw": row[active], "ean": ean,
                     "flags": {heading: row[pos] for heading, pos in flag_columns.items()}, "parent": parent_code,
                     "name": row[name].strip() if name is not None else "",
                     "brand": row[brand].strip() if brand is not None else ""}
            if catalog is not None and parent is not None:
                raw_code, raw_parent = row[catalog], row[parent]
                if raw_code and not raw_code.strip() and raw_parent.strip() == "0" and internal_id:
                    blank_roots[raw_code].append(internal_id)
                if raw_parent and not raw_parent.strip() and has_catalog_code:
                    blank_parent_rows.append((code, raw_parent, value))
                if raw_code.strip() and (raw_code!=code or raw_parent.strip()==code):
                    overlapping_roles[code].append(((raw_code,raw_parent,internal_id),value))
            # Keep literal parent references. Two different native roots can share a trimmed SKU.
            # The raw spellings establish ancestry only; inventory codes stay canonical.
            if catalog is not None and parent is not None and has_catalog_code:
                raw_code, raw_parent = row[catalog], row[parent]
                if raw_parent.strip() in ("", "0"):
                    result.exact_roots[code][raw_code].append(value)
                previous = result.parent_spellings.get(code, raw_parent)
                result.parent_spellings[code] = raw_parent if previous == raw_parent else None
            if code in result:
                duplicates.setdefault(code, [result[code]]).append(value)
            else:
                result[code] = value
        if not result:
            raise ValueError()
        parent_aliases = {}
        for code, values in overlapping_roles.items():
            if len(duplicates.get(code,()))!=len(values):
                continue
            separate = separate_parent(code,[metadata for metadata,_ in values],result)
            if not separate:
                continue
            spelling, alias = separate
            for (raw,parent,native), value in values:
                if native==alias:
                    result[alias] = value
                else:
                    value["parent"] = alias
                    result[code] = value
            parent_aliases[spelling] = alias
            result.parent_code_aliases[code] = alias
            result.parent_spellings[code] = spelling
            del duplicates[code]
        if parent_aliases:
            for code, spelling in result.parent_spellings.items():
                if spelling in parent_aliases:
                    for value in duplicates.get(code, [result[code]]):
                        value["parent"] = parent_aliases[spelling]
        # A literal whitespace SKU and the same nonempty parent cell can be
        # significant in historical catalogs. Require one explicit native
        # parent. The buyers sibling independently corroborates this family
        # when the two sources are joined below.
        for code, spelling, value in blank_parent_rows:
            candidates = blank_roots[spelling]
            if len(candidates) == 1:
                value["parent"] = candidates[0]
                result.literal_children[code] = candidates[0]
            elif candidates:
                value["conflict"] = True
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


def _parent_record(admin, child, key):
    record = admin.get(key) or {}
    if record.get("conflict") and isinstance(admin, AdminCatalog):
        raw = admin.parent_spellings.get(child)
        matches = admin.exact_roots.get(key, {}).get(raw, [])
        if len(matches) == 1 and raw and raw.strip() == key:
            return matches[0]
    return record


def catalog_with_admin(buyers, admin, complete=False):
    """Keep buyers rows and add identities established by the administrative export."""
    aliases = getattr(admin,"parent_code_aliases",{})
    if aliases:
        buyers = [dict(r,parent=aliases[r["parent"]]) if (r["parent"] in aliases and
                  admin.get(r["code"],{}).get("parent")==aliases[r["parent"]]) else r for r in buyers]
    result = {r["code"]: r for r in buyers}
    refs = _admin_refs(admin)
    xml_groups = defaultdict(set)
    literal = getattr(admin, "literal_children", {})
    witnessed_literal_roots = {literal[r["code"]] for r in buyers if r["code"] in literal
                               and refs.get(r["parent"],r["parent"]) == literal[r["code"]]
                               and not r.get("conflict")}
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
        ambiguous = bool(item.get("conflict") or (code in literal and parent_key not in witnessed_literal_roots)
                         or (parent and parent_key is None) or len(groups) > 1
                         or _parent_record(admin, code, parent_key).get("conflict"))
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
        parent_record = _parent_record(admin, leaf, parent_code) if row["kind"]=="variant" else admin.get(parent_code) or {}
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
