"""Read bounded feed samples as data, preserving unknown fields and repeated lists."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from lxml import etree

from inventory_hub.feed_mapping_types import MappingDefinition

MAX_BYTES = 100 * 1024 * 1024
MAX_RECORDS = 100_000
MAX_PATHS = 4096
MAX_DEPTH = 24


def path_parts(path: str) -> list[str]:
    """Split literal paths without splitting slash characters inside XML Clark QNames."""
    parts, current, namespace = [], [], False
    for char in path.strip("/"):
        if char == "{" and not current:
            namespace = True
        elif char == "}" and namespace:
            namespace = False
        if char == "/" and not namespace:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if namespace:
        raise ValueError("Incomplete XML namespace in field path")
    parts.append("".join(current))
    return parts


def values_at(data: Any, path: str) -> list[Any]:
    """Literal slash-separated keys only; each repeated node is traversed in order."""
    if not path or path == ".":
        return data if isinstance(data, list) else [data]
    keys = path_parts(path)
    if len(path) > 500 or any(part in ("..", "*") for part in keys):
        raise ValueError("Use literal field paths, not executable expressions or wildcards")
    if isinstance(data, dict) and path in data:
        value = data[path]
        return value if isinstance(value, list) else [value]
    current = [data]
    for key in keys:
        if key.endswith("[]"):
            key = key[:-2]
        following = []
        for node in current:
            for item in node if isinstance(node, list) else [node]:
                if isinstance(item, dict) and key in item:
                    value = item[key]
                    following.extend(value if isinstance(value, list) else [value])
        current = following
    return current


def primitive(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"#text"}:
        return value["#text"]
    return value


def _check_structure(value: Any, depth: int = 0, count: list[int] | None = None):
    count = count if count is not None else [0]
    count[0] += 1
    if depth > MAX_DEPTH or count[0] > 3_000_000:
        raise ValueError("Feed nesting or element count exceeds the supported limit")
    if isinstance(value, dict):
        if len(value) > MAX_PATHS:
            raise ValueError("Feed has too many fields")
        for child in value.values():
            _check_structure(child, depth + 1, count)
    elif isinstance(value, list):
        for child in value:
            _check_structure(child, depth + 1, count)


def xml_fields(item) -> Any:
    """Keep familiar field paths and make mixed XML text/markup explicitly mappable."""
    children = [child for child in item if isinstance(child.tag, str)]
    if not children and not item.attrib:
        return item.text or ""
    result = {"@attributes": dict(item.attrib)} if item.attrib else {}
    if item.text and item.text.strip():
        result["#text"] = item.text
    for child in children:
        result.setdefault(child.tag, []).append(xml_fields(child))
    markup = {"p", "b", "strong", "em", "i", "a", "span", "div", "ul", "ol", "li", "table", "br", "h1", "h2", "h3"}
    local = etree.QName(item).localname.lower()
    mixed = bool(children and (item.text and item.text.strip() or any(child.tail and child.tail.strip() for child in children)))
    if children and (mixed or local in {"description", "long_description", "short_description", "content", "body"}
                     or any(etree.QName(child).localname.lower() in markup for child in children)):
        result["#text"] = "".join(item.itertext())
        result["#inner_xml"] = (html_escape(item.text or "") + "".join(etree.tostring(child, encoding="unicode", with_tail=True) for child in children))
    return result


def html_escape(value):
    # XML text inside a reconstructed inner fragment needs entity escaping.
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def detect_format(data: bytes, requested: str) -> str:
    if requested != "auto":
        return requested
    beginning = data.lstrip(b"\xef\xbb\xbf \r\n\t")[:1]
    return "xml" if beginning == b"<" else "json" if beginning in (b"{", b"[") else "csv"


def _xml_records(data: bytes, path: str):
    root = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False))
    if root.getroottree().docinfo.doctype:
        raise ValueError("XML DTD declarations are not supported")
    actual = path.strip("/")
    if actual:
        keys = path_parts(actual)
        if keys[0] == root.tag:
            keys = keys[1:]
        nodes = [root]
        for key in keys:
            if not key or key in ("..", "*"):
                raise ValueError("Use an explicit XML record path")
            nodes = [child for node in nodes for child in node if child.tag == key]
    else:
        node, keys = root, [root.tag]
        for _ in range(MAX_DEPTH):
            children = [c for c in node if isinstance(c.tag, str)]
            containers = [c for c in children if len(c)]
            if containers and len(containers) == len(children):
                counts = Counter(c.tag for c in containers)
                tag, count = counts.most_common(1)[0]
                if count > 1 or len(containers) == 1 and tag.lower() in ("shopitem", "product", "item", "record"):
                    keys.append(tag)
                    nodes = [c for c in children if c.tag == tag]
                    break
                if len(containers) == 1:
                    node = containers[0]
                    keys.append(node.tag)
                    continue
            nodes = [node]
            break
        else:
            raise ValueError("Choose the record path; XML nesting is too deep")
        actual = "/".join(keys)
    if len(nodes) > MAX_RECORDS:
        raise ValueError("Feed contains more than 100000 records")
    result = []
    for node in nodes:
        if not len(node):
            raise ValueError("The selected XML path does not contain product records")
        raw = xml_fields(node)
        result.append({"fields": raw, "xml": etree.tostring(node, encoding="unicode", with_tail=False)})
    return result, actual


def read_records(path: Path, definition: MappingDefinition) -> tuple[list[dict], str, str]:
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("Feed is missing or exceeds the 100 MB limit")
    data = path.read_bytes()
    if not data:
        raise ValueError("The feed is empty")
    kind = detect_format(data, definition.format)
    actual = definition.record_path
    try:
        if kind == "xml":
            records, actual = _xml_records(data, actual)
        elif kind == "json":
            value = json.loads(data.decode("utf-8-sig"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON number")))
            _check_structure(value)
            if actual:
                nodes = values_at(value, actual)
            elif isinstance(value, list):
                nodes = value
            elif isinstance(value, dict):
                candidates = [(key, item) for key, item in value.items() if isinstance(item, list) and item and isinstance(item[0], dict)]
                if len(candidates) == 1:
                    actual, nodes = candidates[0]
                elif len(candidates) > 1:
                    raise ValueError("Choose a JSON record path; the feed contains several arrays")
                else:
                    nodes = [value]
            else:
                raise ValueError("JSON must contain product objects")
            if not all(isinstance(node, dict) for node in nodes):
                raise ValueError("The JSON record path must select objects")
            records = [{"fields": node, "json": node} for node in nodes]
        else:
            text = data.decode(definition.csv_encoding)
            delimiter = definition.csv_delimiter
            if not delimiter:
                try:
                    delimiter = csv.Sniffer().sniff(text[:32768], delimiters=",;\t|").delimiter
                except csv.Error:
                    delimiter = ";" if ";" in text.partition("\n")[0] else ","
            reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
            headers = reader.fieldnames or []
            if not headers or len(headers) > MAX_PATHS or len(set(headers)) != len(headers) or any(not h.strip() for h in headers):
                raise ValueError("CSV needs unique nonempty column headings")
            records = []
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("CSV row length does not match the column headings")
                records.append({"fields": row, "csv": row})
                if len(records) > MAX_RECORDS:
                    raise ValueError("Feed contains more than 100000 records")
            actual = ""
        if not records or len(records) > MAX_RECORDS:
            raise ValueError("No records found or the feed exceeds 100000 records")
        for record in records:
            _check_structure(record["fields"])
            record["source_hash"] = hashlib.sha256(json.dumps(record["fields"], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        return records, kind, actual
    except (etree.XMLSyntaxError, UnicodeError, json.JSONDecodeError, csv.Error, RecursionError) as error:
        raise ValueError("Feed cannot be parsed with the selected format/encoding; check the source and record path") from error


def inspect_records(records: list[dict], kind: str, record_path: str) -> dict:
    fields: dict[str, dict] = {}
    # Discover every field while keeping examples and response sizes bounded.
    for record in records:
        touched = set()
        def walk(value, path="", depth=0):
            if depth > MAX_DEPTH:
                raise ValueError("Feed nesting is too deep")
            if isinstance(value, dict):
                for key, child in value.items():
                    walk(child, f"{path}/{key}".lstrip("/"), depth + 1)
            elif isinstance(value, list):
                if path and any(isinstance(child, dict) for child in value):
                    entry = fields.setdefault(path, {"path": path, "type": "list", "examples": [], "populated": 0, "total": len(records)})
                    touched.add(path)
                for child in value:
                    walk(child, path, depth + 1)
            else:
                entry = fields.setdefault(path, {"path": path, "type": "number" if isinstance(value, (int, float)) and not isinstance(value, bool) else "text", "examples": [], "populated": 0, "total": len(records)})
                if value not in (None, ""):
                    touched.add(path)
                    example = str(value)[:300]
                    if example not in entry["examples"] and len(entry["examples"]) < 3:
                        entry["examples"].append(example)
            if len(fields) > MAX_PATHS:
                raise ValueError("Feed contains more than 4096 distinct field paths")
        walk(record["fields"])
        for key in touched:
            fields[key]["populated"] += 1
    categories = set()
    for record in records:
        for path in ("CATEGORYTEXT", "CATEGORYID", "category", "category_code", "categories_b2c/category", "categories_b2b/category"):
            for value in values_at(record["fields"], path):
                if isinstance(value, (str, int)) and str(value).strip():
                    categories.add(str(value))
    samples = []
    for record in records[:3]:
        # Use visible field paths and short sample values, not megabyte descriptions.
        samples.append({entry["path"]: [str(primitive(v))[:300] for v in values_at(record["fields"], entry["path"])[:3]] for entry in fields.values() if entry["type"] != "list"})
    return {"format": kind, "record_path": record_path, "total_records": len(records),
            "fields": list(fields.values()), "sample_records": samples,
            "source_categories": sorted(categories)[:5000]}
