# inventory_hub/config_normalize.py
from __future__ import annotations
from copy import deepcopy
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional
from inventory_hub.supplier_prefix import normalize_prefix_config

SUPPLIER_AVAILABILITY_DEFAULTS = {"orderable": "do 5 dní", "unknown": "overíme"}


class SupplierPriceCoefficientError(ValueError):
    pass


def normalize_supplier_price_coefficients(value: Any = None) -> Dict[str, Any]:
    """Validate brand multipliers without rounding or coercing saved numbers/strings."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SupplierPriceCoefficientError("adapter_settings.price_coefficients must be an object")
    seen = set()
    for brand, coefficient in value.items():
        if not isinstance(brand, str) or not brand.strip() or brand.strip().upper() in seen:
            raise SupplierPriceCoefficientError("Price coefficient brands must have unique nonempty names (case-insensitive)")
        seen.add(brand.strip().upper())
        if isinstance(coefficient, bool) or not isinstance(coefficient, (str, int, float)):
            raise SupplierPriceCoefficientError("Every price coefficient must be a finite positive number")
        try:
            number = Decimal(str(coefficient))
        except (InvalidOperation, ValueError):
            raise SupplierPriceCoefficientError("Every price coefficient must be a finite positive number") from None
        if not number.is_finite() or number <= 0:
            raise SupplierPriceCoefficientError("Every price coefficient must be a finite positive number")
    return dict(value)


def normalize_supplier_availability(value: Any = None) -> Dict[str, Any]:
    """Supplier lead-time labels, independent of physical stock and AI rules."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("adapter_settings.availability must be an object")
    result = dict(value)
    for key, default in {**SUPPLIER_AVAILABILITY_DEFAULTS, **({"unavailable": "overíme"} if "unavailable" in value else {})}.items():
        label = value.get(key)
        if label is None or (isinstance(label, str) and not label.strip()):
            label = default
        if not isinstance(label, str) or len(label.strip()) > 100:
            raise ValueError(f"adapter_settings.availability.{key} must be a label of at most 100 characters")
        result[key] = label.strip()
    return result


KANON_DEFAULT: Dict[str, Any] = {
    "feeds": {
        "current_key": "products",
        "sources": {
            "products": {
                "mode": "remote",
                "local_path": None,
                "remote": {
                    "url": "",
                    "method": "GET",
                    "headers": {},
                    "params": {},
                    "auth": { "mode": "none" }
                }
            },
            "stock": {
                "mode": "remote",
                "local_path": None,
                "remote": {
                    "url": "",
                    "method": "GET",
                    "headers": {},
                    "params": {},
                    "auth": { "mode": "none" }
                }
            },
        },
    },
    "invoices": {
        "layout": "flat",
        "months_back_default": 3,
        "download": {
            "strategy": "manual",
            "web": {
                "login": { "mode": "none" },
                "base_url": "",
                "notes": ""
            }
        }
    },
    "adapter_settings": {
        "product_code_prefix": "",
        "price_coefficients": {},
        "availability": SUPPLIER_AVAILABILITY_DEFAULTS.copy(),
    }
}

def _deep_get(d: Dict[str, Any], path: list[str], default=None):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur

def _deep_set(d: Dict[str, Any], path: list[str], value):
    cur = d
    for k in path[:-1]:
        if k not in cur or not isinstance(cur[k], dict):
            cur[k] = {}
        cur = cur[k]
    cur[path[-1]] = value

def normalize_availability_publication(publication):
    if (not isinstance(publication, dict) or set(publication) != {"enabled", "shop_code", "brands"}
            or type(publication["enabled"]) is not bool or not isinstance(publication["shop_code"], str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,49}", publication["shop_code"])
            or not isinstance(publication["brands"], list) or not 1 <= len(publication["brands"]) <= 50
            or any(not isinstance(b, str) or not b.strip() or len(b) > 100 for b in publication["brands"])):
        raise ValueError("Invalid supplier availability_publication: expected enabled, shop_code and explicit brands")
    return {**publication, "brands": sorted({brand.strip().upper() for brand in publication["brands"]})}


def normalize_supplier_config(raw_in: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Z ľubovoľného (aj legacy) vstupu spraví kanonickú štruktúru.
    Po prvom uložen í budú configs už len v kanonike.
    """
    raw = normalize_prefix_config(raw_in or {})
    for key in ("availability_publication", "catalog_image_fallback"):
        policy = (raw.get("adapter_settings") or {}).get(key)
        if policy is not None:
            raw["adapter_settings"][key] = normalize_availability_publication(policy)
    out = deepcopy(KANON_DEFAULT)

    # ---- FEEDS ----
    # jednoduchý legacy: {"feed_url": "..."}
    feed_url = raw.get("feed_url") or _deep_get(raw, ["feeds", "remote", "url"])
    if feed_url:
        _deep_set(out, ["feeds", "sources", "products", "remote", "url"], feed_url)

    # nový tvar 'feeds.sources'
    srcs = _deep_get(raw, ["feeds", "sources"])
    if isinstance(srcs, dict):
        for key, val in srcs.items():
            merged = {**out["feeds"]["sources"].get(key, {
                "mode": "remote",
                "local_path": None,
                "remote": {"url": "", "method": "GET", "headers": {}, "params": {}, "auth": {"mode": "none"}}
            }), **val}
            _deep_set(out, ["feeds", "sources", key], merged)

    ck = _deep_get(raw, ["feeds", "current_key"]) or raw.get("feed_current_key")
    if ck:
        _deep_set(out, ["feeds", "current_key"], ck)

    # starší jednoduchý mód/cesta
    mode = _deep_get(raw, ["feeds", "mode"])
    if mode in ("local", "remote"):
        _deep_set(out, ["feeds", "sources", "products", "mode"], mode)
    curp = _deep_get(raw, ["feeds", "current_path"])
    if curp is not None:
        _deep_set(out, ["feeds", "sources", "products", "local_path"], curp)

    old_feed_auth = _deep_get(raw, ["feeds", "remote", "auth"])
    if isinstance(old_feed_auth, dict):
        _deep_set(out, ["feeds", "sources", "products", "remote", "auth"], old_feed_auth)

    # ---- INVOICES ----
    layout = _deep_get(raw, ["invoices", "layout"]) or raw.get("layout")
    if layout:
        _deep_set(out, ["invoices", "layout"], layout)

    mb = _deep_get(raw, ["invoices", "months_back_default"]) or raw.get("default_months_window")
    if isinstance(mb, int):
        _deep_set(out, ["invoices", "months_back_default"], mb)

    strategy = _deep_get(raw, ["invoices", "download", "strategy"]) or raw.get("invoice_download_strategy")
    if strategy:
        _deep_set(out, ["invoices", "download", "strategy"], strategy)

    # login – JEDINÉ miesto
    login_new = _deep_get(raw, ["invoices", "download", "web", "login"])
    if isinstance(login_new, dict):
        _deep_set(out, ["invoices", "download", "web", "login"], {**_deep_get(out, ["invoices", "download", "web", "login"], {}), **login_new})

    # legacy downloader.auth -> presuň
    dl_auth = _deep_get(raw, ["downloader", "auth"])
    if isinstance(dl_auth, dict):
        _deep_set(out, ["invoices", "download", "web", "login"], {**_deep_get(out, ["invoices", "download", "web", "login"], {}), **dl_auth})

    # voľné polia
    for k in ("mode","login_url","user_field","pass_field","username","password","cookie","insecure_all","basic_user","basic_pass","token","header_name"):
        v = raw.get(k)
        if v not in (None, ""):
            _deep_set(out, ["invoices", "download", "web", "login", k], v)

    # default login_url pre PL ak chýba
    login = _deep_get(out, ["invoices", "download", "web", "login"]) or {}
    if not login.get("login_url"):
        _deep_set(out, ["invoices", "download", "web", "login", "login_url"], "https://vo.paul-lange-oslany.sk/index.php?cmd=default&id=login")

    # ---- adapter_settings ----
    adapt = raw.get("adapter_settings") or {}
    _deep_set(out, ["adapter_settings"], {**out["adapter_settings"], **adapt})
    if "product_code_prefix" in raw:
        _deep_set(out, ["adapter_settings", "product_code_prefix"], raw["product_code_prefix"])
    if "price_coefficients" in raw and isinstance(raw["price_coefficients"], dict):
        _deep_set(out, ["adapter_settings", "price_coefficients"], raw["price_coefficients"])
    out["adapter_settings"]["price_coefficients"] = normalize_supplier_price_coefficients(out["adapter_settings"].get("price_coefficients"))
    out["adapter_settings"]["availability"] = normalize_supplier_availability(adapt.get("availability"))

    return normalize_prefix_config(out)
