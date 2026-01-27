# inventory_hub/config_normalize.py
from __future__ import annotations
from copy import deepcopy
from typing import Any, Dict, Optional

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
        "price_coefficients": {}
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

def normalize_supplier_config(raw_in: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Z ľubovoľného (aj legacy) vstupu spraví kanonickú štruktúru.
    Po prvom uložen í budú configs už len v kanonike.
    """
    raw = raw_in or {}
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

    return out
