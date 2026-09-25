
from __future__ import annotations
from pathlib import Path
from typing import Dict, Any
from copy import deepcopy
from contextlib import contextmanager
import fcntl
import json, os
from inventory_hub.config_normalize import normalize_supplier_availability
from inventory_hub.supplier_prefix import (
    SupplierPrefixError, get_supplier_prefix, normalize_prefix_config, prefix_values,
)

def _data_root() -> Path:
    env = os.environ.get("INVENTORY_DATA_ROOT")
    if env:
        p = Path(env).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p.resolve()
    p = Path.cwd() / "inventory-data"
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()

DATA_ROOT = _data_root()

def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def _read_json(p: Path) -> Dict[str, Any]:
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_supplier_json(path: Path) -> Dict[str, Any]:
    """Missing is empty; unreadable supplier identity data must never be reset."""
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError):
        raise SupplierPrefixError("supplier_config_invalid", "Supplier configuration could not be read; its original file was preserved", 409) from None
    try:
        result = json.loads(contents)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise SupplierPrefixError("supplier_config_invalid", "Supplier configuration must contain a valid JSON object; its original file was preserved", 409) from None

def _atomic_write(path: Path, data: Dict[str, Any]) -> None:
    _ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)

def _norm_console(cfg: Dict[str, Any]) -> Dict[str, Any]:
    cfg = dict(cfg or {})
    cfg.setdefault("language", "en")
    cfg.setdefault("default_currency", "EUR")
    cfg.setdefault("currency_rates", {"CZK": {"EUR": 0.0}})
    cfg.setdefault("default_months_window", 3)
    return cfg

def _norm_shop(cfg: Dict[str, Any]) -> Dict[str, Any]:
    cfg = dict(cfg or {})
    console = cfg.setdefault("console", {})
    imp = console.setdefault("import_console", {})
    imp.setdefault("columns", {"updates": [], "new": [], "unmatched": []})
    return cfg

def _norm_supplier(cfg: Dict[str, Any]) -> Dict[str, Any]:
    cfg = normalize_prefix_config(cfg or {})
    feeds = cfg.setdefault("feeds", {})
    feeds.setdefault("current_key", "products")
    feeds.setdefault("sources", {
        "products": {"mode": "remote", "local_path": None, "remote": {"url": "", "method": "GET", "headers": {}, "params": {}, "auth": {"mode":"none"}}},
        "stock":    {"mode": "remote", "local_path": None, "remote": {"url": "", "method": "GET", "headers": {}, "params": {}, "auth": {"mode":"none"}}},
    })
    inv = cfg.setdefault("invoices", {})
    inv.setdefault("layout", "flat")
    inv.setdefault("months_back_default", 3)
    dl = inv.setdefault("download", {})
    web = dl.setdefault("web", {})
    web.setdefault("login", {"mode":"form","login_url":"","user_field":"login","pass_field":"password",
                             "username":"","password":"","cookie":"","basic_user":"","basic_pass":"",
                             "token":"","header_name":"","insecure_all":False})
    adapter = cfg.setdefault("adapter_settings", {})
    adapter["availability"] = normalize_supplier_availability(adapter.get("availability"))
    mp = cfg.setdefault("adapter_settings", {}).setdefault("mapping", {}).setdefault("postprocess", {})
    return cfg

def console_path() -> Path:
    return DATA_ROOT / "console" / "config.json"

def shop_path(shop: str) -> Path:
    return DATA_ROOT / "shops" / shop / "config.json"

def supplier_path(supplier: str) -> Path:
    import re
    if not isinstance(supplier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", supplier):
        raise SupplierPrefixError("supplier_code_invalid", "Invalid supplier identifier")
    return DATA_ROOT / "suppliers" / supplier / "config.json"


@contextmanager
def _prefix_registry():
    """Serialize prefix reservations with config writes across API/worker processes.

    The registry is on the same persistent volume as supplier configuration, but
    outside editable configs/history. Existing nonempty prefixes are locked on
    discovery: old database use cannot safely be disproved from filesystem data.
    """
    root = DATA_ROOT / "suppliers"
    root.mkdir(parents=True, exist_ok=True)
    registry_path = root / ".product-prefixes.json"
    with (root / ".product-prefixes.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if registry_path.exists():
                try:
                    registry = json.loads(registry_path.read_text(encoding="utf-8"))
                    if registry.get("version") != 1 or not isinstance(registry.get("suppliers"), dict):
                        raise ValueError()
                    for entry in registry["suppliers"].values():
                        if (not isinstance(entry, dict) or not isinstance(entry.get("prefix"), str)
                                or not isinstance(entry.get("locked"), bool)):
                            raise ValueError()
                        get_supplier_prefix({"product_code_prefix": entry["prefix"]})
                except (ValueError, AttributeError, OSError):
                    raise SupplierPrefixError("supplier_prefix_registry_invalid", "Supplier prefix registry could not be read; identity changes are blocked", 409) from None
            else:
                registry = {"version": 1, "suppliers": {}}
            before = deepcopy(registry)
            entries = registry["suppliers"]
            for path in root.glob("*/config.json"):
                code = path.parent.name
                if code.startswith(".") or code in entries:
                    continue
                try:
                    prefix = get_supplier_prefix(_read_supplier_json(path))
                except SupplierPrefixError:
                    # An invalid supplier is rejected when used, not allowed to
                    # stop unrelated supplier reads. Collision checks below also
                    # examine its individual legacy prefix values.
                    continue
                entries[code] = {"prefix": prefix, "locked": bool(prefix),
                                 "lock_reason": "legacy_existing_configuration" if prefix else None}
            # Preserve discovered legacy locks even when a following edit fails.
            if registry != before:
                _atomic_write(registry_path, registry)
                before = deepcopy(registry)
            yield entries
            if registry != before:
                _atomic_write(registry_path, registry)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _validate_prefix(supplier: str, prefix: str, entries: dict, *, require_complete: bool = False) -> None:
    entry = entries.get(supplier)
    if entry and entry["locked"] and prefix != entry["prefix"]:
        raise SupplierPrefixError("supplier_prefix_locked", "Supplier prefix is locked because it already identifies products", 409)
    if not prefix:
        return
    def overlaps(other: str) -> bool:
        # Prefix namespaces must be disjoint: PL- + A-001 and PL-A- + 001
        # otherwise identify unrelated supplier products with the same SKU.
        left, right = prefix.casefold(), other.casefold()
        return bool(right) and (left.startswith(right) or right.startswith(left))
    for code, reserved in entries.items():
        if code != supplier and overlaps(reserved["prefix"]):
            raise SupplierPrefixError("supplier_prefix_duplicate", f"Supplier prefix overlaps the namespace reserved by '{code}'", 409)
    for path in (DATA_ROOT / "suppliers").glob("*/config.json"):
        code = path.parent.name
        if code == supplier or code.startswith("."):
            continue
        try:
            other_cfg = _read_supplier_json(path)
        except SupplierPrefixError:
            # Reading an unrelated config does not assign any SKU. New prefix
            # reservations/claims require either valid source data or a known
            # immutable reservation for every existing supplier.
            reserved = entries.get(code, {})
            if require_complete and not (reserved.get("locked") and reserved.get("prefix")):
                raise SupplierPrefixError("supplier_prefix_validation_incomplete", f"Cannot verify prefix uniqueness while supplier '{code}' has unreadable configuration", 409) from None
            continue
        for other in prefix_values(other_cfg, strict=False):
            if overlaps(other):
                raise SupplierPrefixError("supplier_prefix_duplicate", f"Supplier prefix overlaps the namespace configured for '{code}'", 409)


def supplier_prefix_status(supplier: str) -> Dict[str, Any]:
    cfg = load_supplier(supplier, write_back_on_load=False)
    with _prefix_registry() as entries:
        prefix = get_supplier_prefix(cfg)
        _validate_prefix(supplier, prefix, entries)
        entry = entries.get(supplier, {})
        return {"product_prefix": prefix, "product_prefix_locked": bool(entry.get("locked")),
                "product_prefix_lock_reason": entry.get("lock_reason")}


def claim_supplier_prefix(supplier: str, expected_prefix: str) -> str:
    """Freeze the configured prefix immediately before its first persistent use."""
    path = supplier_path(supplier)
    with _prefix_registry() as entries:
        if not path.is_file():
            raise SupplierPrefixError("supplier_not_found", "Supplier configuration was not found", 404)
        prefix = get_supplier_prefix(_read_supplier_json(path))
        _validate_prefix(supplier, prefix, entries, require_complete=True)
        if not prefix:
            raise SupplierPrefixError("supplier_prefix_required", "Configure a unique supplier prefix before creating or linking product identities")
        if prefix != expected_prefix:
            raise SupplierPrefixError("supplier_prefix_changed", "Supplier prefix changed since these products were prepared; reload the supplier data", 409)
        entry = entries.get(supplier, {})
        entries[supplier] = {"prefix": prefix, "locked": True,
                             "lock_reason": entry.get("lock_reason") or "used_for_product_identity"}
        return prefix

def load_console() -> Dict[str, Any]:
    return _norm_console(_read_json(console_path()))

def save_console(payload: Dict[str, Any]) -> Dict[str, Any]:
    cur = load_console()
    merged = dict(cur)
    if isinstance(payload, dict):
        for k, v in payload.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
    merged = _norm_console(merged)
    _atomic_write(console_path(), merged)
    return merged

def load_shop(shop: str, write_back_on_load: bool = False) -> Dict[str, Any]:
    raw = _read_json(shop_path(shop))
    norm = _norm_shop(raw)
    if write_back_on_load and raw != norm:
        _atomic_write(shop_path(shop), norm)
    return norm

def save_shop(shop: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    cur = load_shop(shop)
    if not isinstance(payload, dict):
        payload = {}
    merged = dict(cur)
    for k, v in payload.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v
    merged = _norm_shop(merged)
    _atomic_write(shop_path(shop), merged)
    return merged

def load_supplier(supplier: str, write_back_on_load: bool = True) -> Dict[str, Any]:
    path = supplier_path(supplier)
    with _prefix_registry() as entries:
        raw = _read_supplier_json(path)
        norm = _norm_supplier(raw)
        _validate_prefix(supplier, get_supplier_prefix(norm), entries)
        if write_back_on_load and raw != norm and path.exists():
            _atomic_write(path, norm)
        return norm

def save_supplier(supplier: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    path = supplier_path(supplier)
    # Normalize the patch before merging so a legacy alias is an intentional
    # change, not silently shadowed by the current canonical prefix.
    raw_patch = payload if isinstance(payload, dict) else {}
    patch = normalize_prefix_config(raw_patch)
    supplied_prefix = bool(prefix_values(patch))
    requested_prefix = get_supplier_prefix(patch)
    if supplied_prefix and "mapping" not in raw_patch.get("adapter_settings", {}):
        # A legacy scalar prefix alias is not an explicit replacement of the
        # invoice mapping. Its generated canonical path is applied separately.
        patch["adapter_settings"].pop("mapping", None)
    with _prefix_registry() as entries:
        cur = _norm_supplier(_read_supplier_json(path))
        current_prefix = get_supplier_prefix(cur)
        _validate_prefix(supplier, current_prefix, entries)
        # Preserve the established one-level patch contract: supplied nested
        # maps replace their old contents, so clearing headers, removing feed
        # sources or deleting mapping fields through the JSON editor works.
        merged = deepcopy(cur)
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(deepcopy(value))
            else:
                merged[key] = deepcopy(value)
        # Only the protected identity prefix survives omission. An explicit
        # empty or changed prefix remains an attempted edit and is validated.
        if supplied_prefix or current_prefix:
            merged.setdefault("adapter_settings", {}).setdefault("mapping", {}).setdefault("postprocess", {})["product_code_prefix"] = requested_prefix if supplied_prefix else current_prefix
        merged = _norm_supplier(merged)
        prefix = get_supplier_prefix(merged)
        _validate_prefix(supplier, prefix, entries, require_complete=prefix != current_prefix or supplier not in entries)
        previous = entries.get(supplier, {})
        _atomic_write(path, merged)
        entries[supplier] = {"prefix": prefix, "locked": bool(previous.get("locked")),
                             "lock_reason": previous.get("lock_reason")}
        return merged
