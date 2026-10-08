"""Read bounded feed samples as data, preserving unknown fields and repeated lists."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import defaultdict
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


def _field_name(value: str) -> str:
    return re.sub(r"[\s_-]", "", value.rsplit("}", 1)[-1]).casefold()


_RECORD_NAMES = {"shopitem", "product", "item", "record", "entry", "article", "offer"}
_WRAPPER_NAMES = {"shop", "shopitems", "products", "items", "records", "entries", "offers", "catalog", "feed", "root", "data", "response", "result", "results"}
_TAXONOMY_NAMES = {"categories", "taxonomy", "categorytree"}
_IDENTITY_NAMES = {"id", "itemid", "productid", "idproduct", "sku", "code", "reference"}
_TITLE_NAMES = {"name", "title", "product", "productname", "nameb2c"}


def _product_fields(keys) -> bool:
    names = {_field_name(key) for key in keys}
    return bool(names & _IDENTITY_NAMES and names & _TITLE_NAMES)


def _xml_record_nodes(root):
    """Search sibling collections, without descending into a chosen product's parameters."""
    if _field_name(root.tag) in _RECORD_NAMES or _product_fields(child.tag for child in root if isinstance(child.tag, str) and not len(child)):
        return [root], root.tag
    candidates = defaultdict(list)
    priorities = {}

    def visit(node, keys, depth):
        if depth > MAX_DEPTH:
            raise ValueError("Choose the record path; XML nesting is too deep")
        groups = defaultdict(list)
        for child in node:
            if isinstance(child.tag, str) and (len(child) or child.attrib):
                groups[child.tag].append(child)
        for tag, nodes in groups.items():
            if _field_name(tag) in _TAXONOMY_NAMES:
                continue
            path = "/".join([*keys, tag])
            known = _field_name(tag) in _RECORD_NAMES
            product = any(_product_fields(child.tag for child in item if isinstance(child.tag, str) and not len(child)) for item in nodes)
            scalar_names = {_field_name(child.tag) for item in nodes for child in item if isinstance(child.tag, str) and not len(child)}
            scalar_fields = bool(scalar_names)
            metadata = scalar_names <= {"count", "total", "totalcount", "version", "timestamp", "date", "generatedat", "updatedat", "lastupdate"}
            wrapper = _field_name(tag) in _WRAPPER_NAMES or metadata
            if known or product or scalar_fields and not wrapper:
                candidates[path].extend(nodes)
                priorities[path] = 2 if known else 1 if product else 0
            else:
                for child in nodes:
                    visit(child, [*keys, tag], depth + 1)

    visit(root, [root.tag], 0)
    if candidates:
        priority = max(priorities.values())
        paths = [path for path in candidates if priorities[path] == priority]
        if len(paths) != 1:
            raise ValueError("Choose an XML record path; the feed contains several possible record collections")
        return candidates[paths[0]], paths[0]
    if _field_name(root.tag) in _WRAPPER_NAMES:
        raise ValueError("No product records found; choose an explicit XML record path")
    return [root], root.tag


def _json_record_nodes(value):
    if isinstance(value, list):
        return value, ""
    if not isinstance(value, dict):
        raise ValueError("JSON must contain product objects")
    def product_fields(node):
        return isinstance(node, dict) and _product_fields(key for key, item in node.items() if not isinstance(item, (dict, list)))

    if product_fields(value):
        return [value], ""
    candidates = []

    def visit(node, keys):
        for key, item in node.items():
            if _field_name(key) in _TAXONOMY_NAMES:
                continue
            path = "/".join([*keys, key])
            if isinstance(item, list):
                name = _field_name(key)
                known = name in _RECORD_NAMES or name.rstrip("s") in _RECORD_NAMES
                if known or any(isinstance(child, dict) for child in item):
                    product = any(product_fields(child) for child in item)
                    candidates.append((2 if known else 1 if product else 0, path, item))
            elif isinstance(item, dict):
                visit(item, [*keys, key])

    visit(value, [])
    if not candidates:
        return [value], ""
    priority = max(item[0] for item in candidates)
    matches = [item for item in candidates if item[0] == priority]
    if len(matches) != 1:
        raise ValueError("Choose a JSON record path; the feed contains several arrays")
    _, path, nodes = matches[0]
    return nodes, path


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
        nodes, actual = _xml_record_nodes(root)
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
            else:
                nodes, actual = _json_record_nodes(value)
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


def _scalar_values(value):
    items = value if isinstance(value, list) else [value]
    return [str(item).strip() for child in items if (item := primitive(child)) is not None
            and isinstance(item, (str, int, float)) and not isinstance(item, bool) and str(item).strip()]


def _inspection_metadata(records):
    """Discover category identities and paired parameters without correlating unrelated lists."""
    categories, options, parameters = set(), {}, {}
    category_paths = {"code_path": set(), "label_path": set()}
    for record in records:
        category_touched, parameter_touched = set(), set()

        def category(source, label=None, code=None, path=None):
            if not source or len(source) > 1000:
                return
            categories.add(source)
            if source not in options and len(options) >= 5000:
                return
            entry = options.setdefault(source, {"source": source, "label": label or source, "count": 0})
            if code:
                entry["code"] = code
            if path:
                entry["path"] = path
            category_touched.add(source)

        def walk(value, path="", depth=0):
            if depth > MAX_DEPTH:
                raise ValueError("Feed nesting is too deep")
            if isinstance(value, list):
                for child in value:
                    walk(child, path, depth + 1)
                return
            if not isinstance(value, dict):
                return
            names = defaultdict(list)
            for key in value:
                names[_field_name(key)].append(key)
            local = _field_name(path_parts(path)[-1]) if path else ""
            category_context = local in {"category", "categories", "categoryinfo"}
            codes, labels = [], []
            for key, child in value.items():
                name = _field_name(key)
                field_path = f"{path}/{key}".lstrip("/")
                scalars = _scalar_values(child)
                if path in {"categories_b2c", "categories_b2b"} and key == "category":
                    # These are components of the native category path, not independently mappable identities.
                    categories.update(scalars)
                    continue
                if name in {"categoryid", "categorycode"} or category_context and name in {"id", "code"}:
                    if scalars:
                        category_paths["code_path"].add(field_path)
                    codes.extend(scalars)
                if name in {"categorytext", "categoryname", "categorypath", "category"} or category_context and name in {"name", "title", "path"}:
                    if scalars:
                        category_paths["label_path"].add(field_path)
                    labels.extend(scalars)
            codes, labels = list(dict.fromkeys(codes)), list(dict.fromkeys(labels))
            if len(codes) == len(labels) == 1:
                category(codes[0], labels[0], codes[0], labels[0])
                categories.add(labels[0])  # Preserve the old raw category-value list.
            else:
                for code in codes:
                    category(code, code=code)
                for label in labels:
                    category(label, path=label)

            # A pair is matched within one object; independent name/value lists are never zipped.
            if path:
                for name_key, value_key in (("desc", "val"), ("name", "value"), ("name", "values"),
                                            ("n", "v"), ("group", "value"), ("key", "value"), ("label", "value")):
                    if len(names[name_key]) != 1 or len(names[value_key]) != 1:
                        continue
                    name_path, value_path = names[name_key][0], names[value_key][0]
                    found_names = _scalar_values(value[name_path])
                    if len(found_names) != 1 or len(found_names[0]) > 100:
                        continue
                    name = found_names[0]
                    key = (path, name_path, value_path, name)
                    if key not in parameters and len(parameters) >= 1000:
                        break
                    entry = parameters.setdefault(key, {"name": name, "source": path,
                        "param_name_path": name_path, "param_value_path": value_path,
                        "examples": [], "populated": 0, "total": len(records)})
                    values = _scalar_values(value[value_path])
                    if values:
                        parameter_touched.add(key)
                    for example in values:
                        if example[:300] not in entry["examples"] and len(entry["examples"]) < 3:
                            entry["examples"].append(example[:300])
                    break
            for key, child in value.items():
                walk(child, f"{path}/{key}".lstrip("/"), depth + 1)

        walk(record["fields"])
        # Northfinder's native parser uses the complete B2C path (or its B2B fallback).
        for source_path in ("categories_b2c/category", "categories_b2b/category"):
            values = _scalar_values(values_at(record["fields"], source_path))
            if values:
                category(" | ".join(values), path=" | ".join(values))
                break
        for key in category_touched:
            options[key]["count"] += 1
        for key in parameter_touched:
            parameters[key]["populated"] += 1
    return {"source_categories": sorted(categories)[:5000],
            "source_category_options": sorted(options.values(), key=lambda entry: (entry["label"], entry["source"])),
            "category_fields": {key: next(iter(paths)) for key, paths in category_paths.items() if len(paths) == 1},
            "source_parameters": sorted(parameters.values(), key=lambda entry: (entry["name"], entry["source"]))}


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
    samples = []
    for record in records[:3]:
        # Use visible field paths and short sample values, not megabyte descriptions.
        samples.append({entry["path"]: [str(primitive(v))[:300] for v in values_at(record["fields"], entry["path"])[:3]] for entry in fields.values() if entry["type"] != "list"})
    return {"format": kind, "record_path": record_path, "total_records": len(records),
            "fields": list(fields.values()), "sample_records": samples,
            **_inspection_metadata(records)}
