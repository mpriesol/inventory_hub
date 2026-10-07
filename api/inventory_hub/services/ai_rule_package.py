"""Offline, lossless preparation of private product-rule packages.

This module has no database or transport writes. A compiled book is a review
draft, never a published revision. Original package metadata remains intact.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

from pydantic import ValidationError

from inventory_hub.ai_content_types import CategoryProfile, Policy, Rule, RuleBook, Scope
from inventory_hub.services import ai_content_provider as provider, ai_content_rules as rules


class PackageError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def checksum(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _fail(code, detail):
    raise PackageError(code, detail)


def safe_validation_error(error: ValidationError) -> str:
    """Only schema locations and error codes; never private inputs or context."""
    return canonical([{"loc": list(item["loc"]), "type": item["type"]}
                      for item in error.errors(include_input=False, include_context=False)
                      ][:20])


def _validate(model, value, code="package_schema"):
    try:
        return model.model_validate(value)
    except ValidationError as error:
        _fail(code, safe_validation_error(error))


def _list(value, label):
    if not isinstance(value, list):
        _fail("package_schema", label + " must be an array")
    return value


def _object(value, label):
    if not isinstance(value, dict):
        _fail("package_schema", label + " must be an object")
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        _fail("package_schema", label + " must be a nonempty string")
    return value


def _unique(rows, label):
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            _fail("package_schema", label + " entries must be objects")
        key = _text(row.get("id"), label + ".id")
        if key in result:
            _fail("package_duplicate_id", label + ": " + key)
        result[key] = row
    return result


def _no_credentials(value):
    # Report only an identifier, never matched secrets or URLs.
    serialized = canonical(value)
    if (re.search(r"https?://[^\s/\"<>]+:[^\s/\"<>]+@", serialized, re.I)
            or re.search(r"[?&](?:api[_-]?key|token|password|secret|access_token)=[^&\s\"<>]+", serialized, re.I)
            or re.search(r"\bsk-(?:proj-|svcacct-)[A-Za-z0-9_-]{16,}", serialized)):
        _fail("package_credentials", "Remove credential-bearing URLs or secrets before export")


def load_packages(directory: str | Path) -> list[dict]:
    """Require an explicit private directory; no repository/default source path."""
    files = sorted(Path(directory).glob("*.json"))
    if not files:
        _fail("package_missing", "No JSON packages in the explicit directory")
    packages = []
    for file in files:
        try:
            value = json.loads(file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            _fail("package_unreadable", file.name)
        if isinstance(value, dict) and value.get("format") == "biketrek-rule-package-v1":
            bundled = _list(value.get("packages"), "packages")
            if value.get("sha256") != checksum(bundled):
                _fail("package_checksum_mismatch", file.name)
            packages.extend(bundled)
        else:
            packages.append(value)
    validate_packages(packages)
    return packages


def _index(packages):
    documents, modules, profiles, annotations = [], [], [], {}
    for package in packages:
        if not isinstance(package, dict):
            _fail("package_schema", "Each package must be an object")
        documents.extend(_list(package.get("documents"), "documents"))
        modules.extend(_list(package.get("modules"), "modules"))
        profiles.extend(_list(package.get("profiles", []), "profiles"))
        for key, value in _object(package.get("module_annotations", {}), "module_annotations").items():
            if key in annotations:
                _fail("package_duplicate_annotation", key)
            if not isinstance(value, dict):
                _fail("package_schema", "Module annotation must be an object")
            annotations[key] = value
    return _unique(documents, "document"), _unique(modules, "module"), _unique(profiles, "profile"), annotations


def _metadata(module, annotations):
    result = dict(module)
    for key, value in annotations.get(module["id"], {}).items():
        if key in result and result[key] != value:
            _fail("package_metadata_conflict", module["id"] + ": " + key)
        result[key] = value
    return result


def validate_packages(packages: list[dict]) -> dict:
    """Check operational fields; retain every other metadata field in the export.

    Source hashes are provenance, not falsely claimed verification of unavailable
    originals. Export and module hashes cover the actual prepared package.
    """
    _no_credentials(packages)
    documents, modules, profiles, annotations = _index(packages)
    if not documents or not modules:
        _fail("package_empty", "Documents and modules are required")
    for key in annotations:
        if key not in modules:
            _fail("package_reference_missing", key)
    coverage = {}
    document_annotations = {}
    for package in packages:
        for key, value in _object(package.get("document_annotations", {}), "document_annotations").items():
            if key not in documents:
                _fail("package_reference_missing", key)
            if key in document_annotations:
                _fail("package_duplicate_annotation", key)
            document_annotations[key] = _object(value, key + ".document_annotation")
        aliases = _object(package.get("aliases", {}), "aliases")
        for dimension in ("supplier", "brand"):
            for key, values in _object(aliases.get(dimension, {}), "aliases." + dimension).items():
                _text(key, "alias")
                for value in _list(values, "alias values"):
                    _text(value, "alias value")
    for key, doc in documents.items():
        _text(doc.get("name"), key + ".name")
        details = {**doc, **document_annotations.get(key, {})}
        for field, value in document_annotations.get(key, {}).items():
            if field in doc and doc[field] != value:
                _fail("package_metadata_conflict", key + ": " + field)
        count = details.get("source_line_count", details.get("line_count"))
        if count is not None and (type(count) is not int or count < 1):
            _fail("package_schema", key + ".line_count")
        for field in ("sha256", "source_sha256"):
            if field in doc and not re.fullmatch(r"[a-f0-9]{64}", str(doc[field])):
                _fail("package_schema", key + "." + field)
        coverage[key] = {"source_line_count": count, "covered": set(),
                         "source_hash_verified": False}
    for key, original in modules.items():
        module = _metadata(original, annotations)
        for field in ("name", "document", "revision", "section", "text"):
            _text(module.get(field), key + "." + field)
        if module["document"] not in documents:
            _fail("package_reference_missing", key + ": document")
        if module.get("kind") not in ("rule", "knowledge", "reference", "history"):
            _fail("package_schema", key + ".kind")
        if module.get("status") not in ("active", "draft", "mixed"):
            _fail("package_schema", key + ".status")
        try:
            Scope.model_validate(module.get("scope", {}))
        except ValueError:
            _fail("package_schema", key + ".scope")
        for field in ("knowledge_ids", "related_knowledge_ids", "required_modules"):
            for ref in _list(module.get(field, []), key + "." + field):
                _text(ref, key + "." + field)
                if ref not in modules:
                    _fail("package_reference_missing", key + ": " + str(ref))
        for field in ("selection_categories", "selection_terms"):
            for value in _list(module.get(field, []), key + "." + field):
                _text(value, key + "." + field)
        if module.get("selection_mode", "common") not in ("common", "profile"):
            _fail("package_schema", key + ".selection_mode")
        if "selection_requires_confirmed_facts" in module and type(module["selection_requires_confirmed_facts"]) is not bool:
            _fail("package_schema", key + ".selection_requires_confirmed_facts")
        if "sha256" in module and module["sha256"] != hashlib.sha256(module["text"].encode()).hexdigest():
            _fail("package_checksum_mismatch", key)
        lines = module.get("source_lines")
        if lines is not None:
            if (not isinstance(lines, list) or len(lines) != 2 or any(type(n) is not int for n in lines)
                    or lines[0] < 1 or lines[1] < lines[0]):
                _fail("package_schema", key + ".source_lines")
            entry = coverage[module["document"]]
            if entry["source_line_count"] and lines[1] > entry["source_line_count"]:
                _fail("package_source_range", key)
            entry["covered"].update(range(lines[0], lines[1] + 1))
    for key, profile in profiles.items():
        _text(profile.get("name"), key + ".name")
        if profile.get("status", "active") not in ("active", "draft", "mixed"):
            _fail("package_schema", key + ".status")
        for field in ("module_ids", "knowledge_ids"):
            for ref in _list(profile.get(field, []), key + "." + field):
                _text(ref, key + "." + field)
                if ref not in modules:
                    _fail("package_reference_missing", key + ": " + str(ref))
    for entry in coverage.values():
        covered = entry.pop("covered")
        count = entry["source_line_count"]
        entry.update(covered_line_count=len(covered),
                     missing_lines=sorted(set(range(1, count + 1)) - covered) if count else None,
                     complete=(len(covered) == count) if count else None)
    return {"sha256": checksum(packages), "documents": len(documents), "modules": len(modules),
            "profiles": len(profiles), "coverage": coverage,
            "module_sha256": {key: hashlib.sha256(m["text"].encode()).hexdigest() for key, m in modules.items()},
            "metadata_preserved": True, "published": False,
            "scope": "Package structure, module checksums and declared line coverage; not semantic rule approval or source truth"}


def export_package(packages: list[dict]) -> dict:
    report = validate_packages(packages)
    return {"format": "biketrek-rule-package-v1", "sha256": report["sha256"],
            "packages": copy.deepcopy(packages), "report": report}


def _alias(packages, dimension, value):
    matches = set()
    for package in packages:
        aliases = package.get("aliases", {}).get(dimension, {})
        for canonical_name, alternatives in aliases.items():
            if value.casefold() in {str(v).casefold() for v in [canonical_name, *alternatives]}:
                matches.add(canonical_name)
    if len(matches) > 1:
        _fail("package_alias_conflict", dimension)
    return next(iter(matches), value)


def split_text(text: str, limit: int) -> list[str]:
    """Prefer paragraph boundaries; concatenating chunks exactly restores text."""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit + 1)
        cut = cut + 2 if cut >= 0 and cut + 2 <= limit else limit
        chunks.append(text[:cut])
        text = text[cut:]
    if text:
        chunks.append(text)
    return chunks


def compile_draft(packages: list[dict], *, shop: str, supplier: str, brand: str,
                  profile: str, product: str = "", knowledge_ids=(), current_book: dict | None = None) -> dict:
    """Compile one explicit scope. Material knowledge is opt-in, never inferred.

    Existing applicable rules must be reconciled by an operator; this tool cannot
    silently replace or append conflicting old instructions in a published book.
    """
    report = validate_packages(packages)
    documents, originals, profiles, annotations = _index(packages)
    modules = {key: _metadata(m, annotations) for key, m in originals.items()}
    if profile not in profiles:
        _fail("package_profile_missing", profile)
    selected_profile = profiles[profile]
    if selected_profile.get("status", "active") != "active":
        _fail("package_profile_unapproved", profile)
    # Aliases select source blocks only; live matching uses the actual unchanged
    # supplier/brand values and parent product code used by AI job creation.
    runtime_scope = _validate(Scope, dict(shop=shop, supplier=supplier, brand=brand, category=profile, product=product))
    selection_context = _validate(Scope, dict(shop=shop, supplier=_alias(packages, "supplier", supplier),
        brand=_alias(packages, "brand", brand), category=profile, product=product))
    requested_knowledge = set(knowledge_ids)
    if requested_knowledge and not product.strip():
        _fail("package_product_scope_required", "Confirmed knowledge requires the actual parent product code")
    explicit = set(selected_profile.get("module_ids", [])) | set(selected_profile.get("knowledge_ids", []))
    for key in knowledge_ids:
        if key not in modules or modules[key]["kind"] != "knowledge":
            _fail("package_knowledge_missing", key)
        explicit.add(key)

    def matches(module):
        return (all(getattr(selection_context, k).casefold() == v.casefold() for k, v in module.get("scope", {}).items() if v)
                and (not module.get("selection_categories") or profile in module["selection_categories"]))

    def require_product_scope(module):
        if module.get("scope", {}).get("product") and not product.strip():
            _fail("package_product_scope_required", "Product-specific rules require the actual parent product code")

    def conditional(module):
        return module.get("selection_requires_confirmed_facts") or module.get("selection_terms")

    selected = set()
    for key, module in modules.items():
        if module["kind"] in ("reference", "history"):
            continue
        if key in explicit:
            if conditional(module) and key not in requested_knowledge:
                continue
            require_product_scope(module)
            if not matches(module):
                _fail("package_scope_conflict", key)
            selected.add(key)
        elif matches(module):
            if module.get("selection_mode") == "profile":
                continue
            if conditional(module):
                continue
            selected.add(key)
    pending = list(selected)
    confirmed = set(requested_knowledge)
    while pending:
        module = modules[pending.pop()]
        for ref in [*module.get("knowledge_ids", []), *module.get("required_modules", [])]:
            if conditional(modules[ref]) and ref not in confirmed:
                if module["id"] in confirmed:
                    confirmed.add(ref)
                else:
                    continue
            require_product_scope(modules[ref])
            if not matches(modules[ref]):
                _fail("package_scope_conflict", ref)
            if modules[ref]["kind"] in ("reference", "history"):
                _fail("package_reference_not_instruction", ref)
            if ref not in selected:
                selected.add(ref)
                pending.append(ref)
    for key in selected:
        module = modules[key]
        if module["status"] != "active":
            _fail("package_module_unapproved", key)
        if module.get("binding_required"):
            _fail("package_binding_required", key)
        if module.get("selection_requires_confirmed_facts") and key not in confirmed:
            _fail("package_knowledge_confirmation_required", key)
    _no_credentials(current_book)
    book = _validate(RuleBook, current_book or {}, "package_current_book_invalid")
    for rule in book.rules:
        if rule.enabled and all(getattr(runtime_scope, k).casefold() == v.casefold()
                                for k, v in rule.scope.model_dump().items() if v):
            _fail("package_existing_scope_conflict", rule.id)
    if any(c.id == profile for c in book.categories):
        _fail("package_existing_profile_conflict", profile)
    # Parameters are preserved as original text; never invent a structured
    # registry from prose. Keep automation disabled pending that separate work.
    book.categories.append(_validate(CategoryProfile, dict(id=profile, name=selected_profile["name"],
        shop_categories=selected_profile.get("shop_categories", {}), automatic_import_ready=False,
        policy=Policy(review_required=True, active_after_import=False, show_cost_estimate=True, confirm_import=True))))
    ordered = sorted(selected, key=lambda key: (modules[key]["document"], modules[key].get("source_lines", [0])[0], key))
    for index, key in enumerate(ordered):
        module = modules[key]
        doc = documents[module["document"]]
        header = f"Podklad: {doc['name']} | {module['revision']} | {module['section']} | {key}\n"
        if len(header) >= 2000:
            _fail("package_provenance_too_long", key)
        chunks = split_text(module["text"], 24000 - len(header) - 40)
        for part, chunk in enumerate(chunks, 1):
            # Confirmed material knowledge always binds this snapshot to the
            # exact runtime parent code, not merely its brand/category family.
            rule_id = f"pack_{index:04d}_{hashlib.sha256(key.encode()).hexdigest()[:12]}_{part:04d}"
            if any(r.id == rule_id for r in book.rules):
                _fail("package_existing_rule_conflict", rule_id)
            book.rules.append(_validate(Rule, dict(id=rule_id, name=module["name"][:180] + f" ({part}/{len(chunks)})",
                scope=runtime_scope, instructions=header + f"Časť {part}/{len(chunks)}\n" + chunk)))
    try:
        book = RuleBook.model_validate(book.model_dump())
        resolved = rules.resolve(book, runtime_scope)
        body = provider.request_body({"model": "gpt-5.6-sol", "shop": shop, "options": {"language": "sk"},
                                      "facts": [], "research": "official", "resolved": resolved})
    except ValidationError as error:
        _fail("package_rulebook_limit", safe_validation_error(error))
    except Exception as error:
        if getattr(error, "code", None) == "ai_context_too_large":
            _fail("package_prompt_too_large", f"Full selection exceeds {provider.MAX_PROMPT_BYTES} bytes; no text was truncated")
        raise
    request_bytes = len(json.dumps(body, ensure_ascii=False).encode())
    report.update(selected_modules=ordered, excluded_modules=sorted(set(modules) - selected),
        selected_context=runtime_scope.model_dump(), source_selection_context=selection_context.model_dump(),
        confirmed_knowledge=sorted(requested_knowledge),
        prompt_bytes_without_product_facts=request_bytes,
        prompt_limit_bytes=provider.MAX_PROMPT_BYTES,
        remaining_prompt_bytes=provider.MAX_PROMPT_BYTES - request_bytes,
        current_book_supplied=current_book is not None,
        status="review_draft", generation_verified=False,
        limitations=[
            "Offline draft only: not saved or published to Hub; existing jobs retain frozen rules.",
            "Without a current book, reconcile other scopes before saving as a new full RuleBook version.",
            "Prompt size excludes product facts; the runtime checks the complete request again.",
            "Prose registries are retained losslessly but have not become machine parameter definitions.",
            "Material knowledge selection is an operator assertion of confirmed facts, not inferred product evidence.",
            "Draft/mixed blocks remain in the package; they are never activated by this compiler.",
            "XML generation/validation, B2B access, gallery research and control A/B/C need separate workflow support.",
            "The current evidence validator does not prove knowledge-base explanations or model facts true.",
        ])
    return {"book": book.model_dump(), "report": report}
