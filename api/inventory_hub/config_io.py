
from __future__ import annotations
from pathlib import Path
from typing import Dict, Any
import json, os

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
    cfg = dict(cfg or {})
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
    cfg.setdefault("adapter_settings", {})
    mp = cfg.setdefault("adapter_settings", {}).setdefault("mapping", {}).setdefault("postprocess", {})
    if "product_code_prefix" in cfg.get("adapter_settings", {}):
        mp.setdefault("product_code_prefix", cfg["adapter_settings"]["product_code_prefix"] or "")
        cfg["adapter_settings"].pop("product_code_prefix", None)
    return cfg

def console_path() -> Path:
    return DATA_ROOT / "console" / "config.json"

def shop_path(shop: str) -> Path:
    return DATA_ROOT / "shops" / shop / "config.json"

def supplier_path(supplier: str) -> Path:
    return DATA_ROOT / "suppliers" / supplier / "config.json"

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
    raw = _read_json(supplier_path(supplier))
    norm = _norm_supplier(raw)
    if write_back_on_load and raw != norm:
        _atomic_write(supplier_path(supplier), norm)
    return norm

def save_supplier(supplier: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    cur = load_supplier(supplier)
    if not isinstance(payload, dict):
        payload = {}
    merged = dict(cur)
    for k, v in payload.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v
    merged = _norm_supplier(merged)
    _atomic_write(supplier_path(supplier), merged)
    return merged
