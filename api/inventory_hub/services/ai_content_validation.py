"""Server-side validation of canonical content and its real Upgates API overlay.

The uploaded XML validator is not sent to or executed by AI. XML/XSD export is a
separate format; these checks validate the data actually used by the API importer.
"""
from __future__ import annotations

import copy
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from inventory_hub.ai_content_types import Content
from inventory_hub.services.catalog import CatalogError
from inventory_hub.services.catalog_html import clean_description

TAGS = {"p", "br", "strong", "b", "em", "i", "ul", "ol", "li", "h2", "h3", "h4", "table", "thead", "tbody", "tr", "th", "td"}


def text_of(value: str) -> str:
    return " ".join(BeautifulSoup(value or "", "html.parser").stripped_strings)


def validate_content(content: Content, context: dict, opened: list[str] | None = None) -> dict:
    errors, warnings = [], list(content.warnings)
    if content.missing_facts:
        errors.append("ai_missing_facts")
    for field in ("title", "short_description", "seo_title", "meta_description", "h1_descriptor", "future_name", "h1_descr_suffix"):
        if re.search(r"<[^>]+>", getattr(content, field)):
            errors.append("ai_plain_text_required:" + field)
    soup = BeautifulSoup(content.long_description, "html.parser")
    if any(tag.name not in TAGS or tag.attrs for tag in soup.find_all(True)):
        errors.append("ai_unsafe_html")
    marketing = " ".join([content.title, content.short_description, text_of(content.long_description), content.meta_description])
    if re.search(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", marketing, re.I):
        errors.append("ai_manufacturer_contact")
    if not 45 <= len(content.seo_title) <= 65:
        warnings.append("ai_seo_title_length")
    if not 140 <= len(content.meta_description) <= 165:
        warnings.append("ai_meta_description_length")
    ids = {int(p["id"]) for p in context["facts"]}
    category = context["resolved"].get("category") or {}
    definitions = {p["name"]: p for p in category.get("parameters", [])}
    parameters = {}
    for value in content.parameters:
        definition = definitions.get(value.name)
        if definition is None:
            errors.append("ai_unregistered_parameter:" + value.name)
            continue
        key = (value.name, value.product_id)
        if key in parameters:
            errors.append("ai_duplicate_parameter:" + value.name)
        parameters[key] = value.values
        if definition["scope"] == "parent" and value.product_id is not None or definition["scope"] == "variant" and value.product_id not in ids:
            errors.append("ai_parameter_scope:" + value.name)
        if definition["scope"] == "variant":
            source = next((p for p in context["facts"] if p["id"] == value.product_id), {})
            existing = next((a["value"] for a in source.get("variant_attributes", []) if a["name"] == value.name), None)
            if existing is None or value.values != [existing]:
                errors.append("ai_variant_identity_change:" + value.name)
        if definition["values"] and set(value.values) - set(definition["values"]):
            errors.append("ai_parameter_value:" + value.name)
        if any(not v.strip() or "<" in v for v in value.values):
            errors.append("ai_parameter_value:" + value.name)
    for definition in definitions.values():
        expected = ids if definition["scope"] == "variant" else [None]
        if definition["required"] and any((definition["name"], id) not in parameters for id in expected):
            errors.append("ai_required_parameter:" + definition["name"])
    if definitions:
        for product in context["facts"]:
            for attribute in product.get("variant_attributes", []):
                definition = definitions.get(attribute["name"])
                if not definition or definition["scope"] != "variant":
                    errors.append("ai_unregistered_variant_axis:" + attribute["name"])
                values = parameters.get((attribute["name"], product["id"]))
                if values is not None and values != [attribute["value"]]:
                    errors.append("ai_variant_identity_change:" + attribute["name"])
    domains = context["resolved"].get("official_domains", [])
    feed_texts = {"feed:" + str(p["id"]): text_of(str(p)) for p in context["facts"]}
    for evidence in content.evidence:
        if evidence.source.startswith("feed:"):
            if evidence.source not in feed_texts or text_of(evidence.quote).casefold() not in feed_texts[evidence.source].casefold():
                errors.append("ai_unverified_feed_evidence")
        else:
            url = urlsplit(evidence.source)
            host = (url.hostname or "").lower()
            if (url.scheme != "https" or url.username or url.password or url.query or
                not any(host == d or host.endswith("." + d) for d in domains) or evidence.source not in (opened or [])):
                errors.append("ai_unverified_official_evidence")
    if context["research"] == "official" and not any(not e.source.startswith("feed:") for e in content.evidence):
        warnings.append("ai_no_additional_official_evidence")
    if category and not category.get("automatic_import_ready", True):
        warnings.append("ai_category_requires_review")
    return {"errors": sorted(set(errors)), "warnings": sorted(set(warnings)),
            "automatic_ready": not errors and category.get("automatic_import_ready", True)}


def overlay(item, enrichment: dict, language: str):
    """Called only with a server-validated, approved job; never arbitrary request JSON."""
    payload = copy.deepcopy(item.payload)
    active = bool(enrichment["active_after_import"])
    payload["active_yn"] = active
    for description in payload.get("descriptions", []):
        description["active_yn"] = active
    content = enrichment.get("content")
    if content:
        data = Content.model_validate(content)
        description = next(d for d in payload["descriptions"] if d["language"] == language)
        description.update(title=data.title, short_description=data.short_description,
            long_description=data.long_description, seo_title=data.seo_title, seo_description=data.meta_description)
        # Preserve safety text from the supplier outside AI-written marketing prose.
        safety = enrichment.get("safety", "")
        if safety:
            description["long_description"] += "<h2>Bezpečnostné informácie</h2>" + clean_description(safety)
        item.name = data.title
        # Parameters are optional in A+B. When a register exists, output is exclusive
        # to that register; existing variant identity/choice axes are never rewritten.
        if enrichment.get("registered_parameters"):
            parent = [p for p in data.parameters if p.product_id is None]
            payload["parameters"] = [{"descriptions": [{"language": language, "name": p.name}],
                "values": [{"descriptions": [{"language": language, "value": v}]} for v in p.values]} for p in parent]
        for key in ("h1_descriptor", "future_name", "h1_descr_suffix"):
            value = getattr(data, key)
            if value:
                payload.setdefault("metas", []).append({"key": key, "values": {language: {"language": language, "value": value}}})
        for obj in [payload, *payload.get("variants", [])]:
            if enrichment.get("supplier_name"):
                obj.setdefault("metas", []).append({"key": "supplier_name", "values": {language: {"language": language, "value": enrichment["supplier_name"]}}})
            for meta in obj.get("metas", []):
                if meta.get("key") == "validation_required":
                    meta["value"] = "0"
    item.payload = payload
    return item
