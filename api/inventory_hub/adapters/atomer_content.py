"""Bounded source evidence for an explicit Atomer existing-card content pilot.

The complete upload is validated and hashed. Only selected complete families
are retained. ``raw_xml`` is a lossless-value XML serialization of the source
row; the SHA-256 identifies the unmodified original upload bytes.
"""
import codecs
from collections import Counter
import hashlib
import json
import re

import html5lib
from lxml import etree


MAX_BYTES = 100 * 1024 * 1024
MAX_ROWS = 100000
MAX_FAMILY_ROWS = 100
FIELD_TAGS = {
    "long_description": "DESCRIPTION_LONG",
    "seo_title": "SEO/SEO_TITLE",
    "seo_description": "SEO/SEO_DESCIPTION",  # Atomer's documented feed spelling.
}
FIELD_LIMITS = {"long_description": 50000, "seo_title": 250, "seo_description": 1000}
_TAGS = frozenset((
    "p", "div", "span", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "b", "em", "i", "u", "s", "small", "sub", "sup", "mark",
    "ul", "ol", "li", "dl", "dt", "dd", "blockquote", "pre", "code",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "colgroup", "col",
))
_ATTRIBUTES = frozenset(("class", "title", "lang", "dir", "style"))
_TABLE_ATTRIBUTES = frozenset(("colspan", "rowspan", "span", "scope", "headers", "abbr"))
_STYLE_KEYWORDS = {
    "text-align": {"left", "right", "center", "justify", "start", "end"},
    "font-style": {"normal", "italic", "oblique"},
    "font-weight": {"normal", "bold", "bolder", "lighter", *map(str, range(100, 1000, 100))},
    "text-decoration": {"none", "underline", "overline", "line-through"},
    "vertical-align": {"baseline", "top", "middle", "bottom", "text-top", "text-bottom", "sub", "super"},
}
_SIZE = r"(?:0|(?:\d+(?:\.\d+)?|\.\d+)(?:px|pt|em|rem|%))"


class AtomerSourceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _error(suffix):
    raise AtomerSourceError("atomer_source_" + suffix)


def _preflight(fileobj):
    """Reject encodings/DTDs before libxml ever sees the untrusted input."""
    fileobj.seek(0)
    digest, size, prefix, tail = hashlib.sha256(), 0, b"", b""
    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    try:
        while chunk := fileobj.read(1024 * 1024):
            if not isinstance(chunk, bytes):
                _error("invalid_xml")
            size += len(chunk)
            if size > MAX_BYTES:
                _error("too_large")
            if b"\x00" in chunk:
                _error("unsupported_encoding")
            decoder.decode(chunk)
            digest.update(chunk)
            prefix = (prefix + chunk)[:1024]
            scan = (tail + chunk).upper()
            if b"<!DOCTYPE" in scan or b"<!ENTITY" in scan:
                _error("dtd_forbidden")
            tail = scan[-32:]
        decoder.decode(b"", final=True)
    except UnicodeError:
        _error("unsupported_encoding")
    # The optional UTF-8 BOM is handled separately, avoiding encoding aliases.
    declaration = re.match(br"<\?xml\b[^?]*\?>", prefix.removeprefix(b"\xef\xbb\xbf"))
    if declaration:
        encoding = re.search(br"\bencoding\s*=\s*['\"]([^'\"]+)['\"]", declaration.group(), re.I)
        if encoding and encoding.group(1).lower() not in (b"utf-8", b"utf8"):
            _error("unsupported_encoding")
    if not size:
        _error("invalid_xml")
    fileobj.seek(0)
    return digest.hexdigest(), size


def _text(row, tag, *, identity=False):
    children = row.findall(tag)
    if len(children) > 1 or any(len(child) for child in children):
        _error("ambiguous_field")
    value = children[0].text or "" if children else ""
    if identity and (len(value) > 255 or value != value.strip()
                     or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        _error("invalid_identity")
    return value


def parse_selected(fileobj, source_codes):
    """Read a seekable binary buyers XML; no network requests or mutations."""
    if (not isinstance(source_codes, list) or not 1 <= len(source_codes) <= 10
            or any(not isinstance(code, str) or not 1 <= len(code) <= 255 or code != code.strip()
                   or any(ord(c) < 32 or ord(c) == 127 for c in code) for code in source_codes)
            or len(set(source_codes)) != len(source_codes)):
        _error("invalid_selection")
    digest, size = _preflight(fileobj)
    groups = {code: {"source_code": code, "rows": []} for code in source_codes}
    codes, kinds, total, root = Counter(), {code: set() for code in source_codes}, 0, None
    try:
        context = etree.iterparse(fileobj, events=("start", "end"), load_dtd=False,
            no_network=True, resolve_entities=False, recover=False, huge_tree=False)
        for event, row in context:
            if root is None:
                root = row
                if row.tag != "PRODUCTS":
                    _error("invalid_root")
            if event != "end" or row is root:
                continue
            if row.getparent() is not root:
                if row.tag == "PRODUCT":
                    _error("invalid_structure")
                continue
            if row.tag != "PRODUCT":
                _error("invalid_structure")
            total += 1
            if total > MAX_ROWS:
                _error("too_many_rows")
            code = _text(row, "PRODUCT_ID")
            parent = _text(row, "PARENT_ID")
            codes[code] += 1
            selected = parent or code
            if selected in groups:
                _text(row, "PRODUCT_ID", identity=True)
                _text(row, "PARENT_ID", identity=True)
                if not code:
                    _error("invalid_identity")
                kinds[selected].add(bool(parent))
                if len(kinds[selected]) != 1:
                    _error("mixed_family")
                rows = groups[selected]["rows"]
                if len(rows) >= MAX_FAMILY_ROWS:
                    _error("family_too_large")
                rows.append({"code": code, "parent_code": parent,
                    "ean": _text(row, "EAN", identity=True),
                    "manufacturer": _text(row, "MANUFACTURER"),
                    **{field: _text(row, tag) for field, tag in FIELD_TAGS.items()},
                    "raw_xml": etree.tostring(row, encoding="unicode", with_tail=False),
                    "source_position": total})
            row.clear()
            while row.getprevious() is not None:
                del root[0]
        if root is None or not total:
            _error("invalid_xml")
    except (etree.XMLSyntaxError, ValueError, TypeError) as exc:
        if isinstance(exc, AtomerSourceError):
            raise
        _error("invalid_xml")
    for group in groups.values():
        if not group["rows"]:
            _error("selection_missing")
        if any(codes[row["code"]] != 1 for row in group["rows"]):
            _error("duplicate_code")
    return {"sha256": digest, "size_bytes": size, "total_rows": total, "groups": groups}


def _style(value):
    """Small presentation-only CSS subset; no escape/function-based bypasses."""
    result = {}
    for declaration in value.split(";"):
        if not declaration.strip():
            continue
        if ":" not in declaration:
            _error("unsafe_html")
        key, item = (part.strip().lower() for part in declaration.split(":", 1))
        if key in result or not item or any(c in item for c in "\\@!/*<>"):
            _error("unsafe_html")
        if key in _STYLE_KEYWORDS:
            accepted = item in _STYLE_KEYWORDS[key]
        elif key in ("color", "background-color"):
            accepted = bool(re.fullmatch(r"(?:#[0-9a-f]{3,8}|[a-z]+|(?:rgb|rgba|hsl|hsla)\([\d.,%\s]+\))", item))
        elif key in ("font-size", "width", "height", "max-width", "min-width", "line-height"):
            accepted = bool(re.fullmatch(_SIZE, item)) or (key == "line-height" and bool(re.fullmatch(r"\d+(?:\.\d+)?", item)))
        elif key in ("margin", "padding", "margin-left", "margin-right", "margin-top", "margin-bottom",
                     "padding-left", "padding-right", "padding-top", "padding-bottom"):
            accepted = 1 <= len(item.split()) <= 4 and all(re.fullmatch(_SIZE, token) for token in item.split())
        else:
            accepted = False
        if not accepted:
            _error("unsafe_html")
        result[key] = item
    return sorted(result.items())


def _html_tree(value):
    if not isinstance(value, str) or len(value) > FIELD_LIMITS["long_description"]:
        _error("invalid_content")
    try:
        parser = html5lib.HTMLParser(strict=True, namespaceHTMLElements=False)
        root = parser.parseFragment(value)
    except (ValueError, html5lib.html5parser.ParseError):
        _error("invalid_html")

    def canonical(node, *, fragment=False):
        if not fragment and (not isinstance(node.tag, str) or node.tag not in _TAGS):
            _error("unsafe_html")
        attrs = []
        for key, item in node.attrib.items():
            if key not in _ATTRIBUTES and not (node.tag in ("table", "th", "td", "col", "colgroup") and key in _TABLE_ATTRIBUTES):
                _error("unsafe_html")
            if key == "style":
                item = _style(item)
            elif key in ("colspan", "rowspan", "span") and (not item.isdigit() or not 1 <= int(item) <= 100):
                _error("unsafe_html")
            elif key == "dir" and item not in ("ltr", "rtl", "auto"):
                _error("unsafe_html")
            attrs.append((key, item))
        return ["fragment" if fragment else node.tag, sorted(attrs), node.text or "",
                [[canonical(child), child.tail or ""] for child in node]]

    return root, canonical(root, fragment=True)


def html_fingerprint(value):
    """Canonical parsed structure, not merely text; formatting loss is a mismatch."""
    _, canonical = _html_tree(value)
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def source_values(group, fields):
    if (not isinstance(fields, list) or not fields or any(not isinstance(field, str) or field not in FIELD_TAGS for field in fields)
            or len(fields) != len(set(fields))
            or not isinstance(group, dict) or not isinstance(group.get("rows"), list) or not group["rows"]):
        _error("invalid_selection")
    values = {}
    for field in fields:
        entries = [row.get(field) for row in group["rows"]]
        if any(not isinstance(value, str) or not value.strip() or len(value) > FIELD_LIMITS[field]
               or any(ord(char) < 32 and char not in "\n\r\t" for char in value) for value in entries):
            _error("invalid_content")
        if len(set(entries)) != 1:
            _error("family_content_conflict")
        value = entries[0]
        if field == "long_description":
            tree, _ = _html_tree(value)
            if not "".join(tree.itertext()).strip():
                _error("invalid_content")
        elif "<" in value or ">" in value:
            _error("invalid_content")
        values[field] = value
    return values
