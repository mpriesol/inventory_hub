# api/inventory_hub/routers/suppliers.py
"""
Suppliers router (operational config + effective + feed refresh).

Design:
- Supports multi-source feeds (e.g., products, stock, prices) with per-source auth.
- Invoices have their own download section (web login etc.), independent of feeds.
- Backward-compatible migration from older "flat" payloads (feed_url, auth, invoice_download_strategy, default_months_window).
- Deep-merge save to avoid wiping secrets on partial updates.
"""

from __future__ import annotations
from typing import Any, Dict, Optional, Literal, Tuple
from pathlib import Path
from copy import deepcopy
import json

from fastapi import APIRouter, HTTPException, Query, Body
from pydantic import BaseModel, Field

# Data root
try:
    from inventory_hub.settings import INVENTORY_DATA_ROOT  # type: ignore
except Exception:
    import os
    INVENTORY_DATA_ROOT = os.environ.get("INVENTORY_DATA_ROOT", r"C:\!kafe\BikeTrek\web\inventory-data")
INVENTORY_DATA_ROOT = Path(INVENTORY_DATA_ROOT)

router = APIRouter(tags=["suppliers"])


KANON_DEFAULT = {
    "feeds": {
        "current_key": "products",
        "sources": {
            "products": {"mode": "remote", "local_path": None, "remote": {"url": "", "method": "GET", "headers": {}, "params": {}, "auth": {"mode": "none"}}},
            "stock":    {"mode": "remote", "local_path": None, "remote": {"url": "", "method": "GET", "headers": {}, "params": {}, "auth": {"mode": "none"}}},
        }
    },
    "invoices": {
        "layout": "flat",
        "months_back_default": 3,
        "download": { "strategy": "manual", "web": { "login": { "mode": "none" }, "base_url": "", "notes": "" } }
    },
    "adapter_settings": { "product_code_prefix": "", "price_coefficients": {} }
}


# ----------------- MODELS -----------------
class AuthConfig(BaseModel):
    mode: Literal["none","form","cookie","basic","token","header"] = "none"
    # form login
    login_url: str = ""
    user_field: str = "login"
    pass_field: str = "password"
    username: str = ""
    password: str = ""
    # cookie/basic
    cookie: str = ""
    basic_user: str = ""
    basic_pass: str = ""
    # token/header
    token: str = ""
    header_name: str = ""
    # security
    insecure_all: bool = False

class RemoteEndpoint(BaseModel):
    url: str = ""
    method: Literal["GET","POST"] = "GET"
    headers: Dict[str, str] = Field(default_factory=dict)
    params: Dict[str, str] = Field(default_factory=dict)
    auth: AuthConfig = Field(default_factory=AuthConfig)

class FeedSource(BaseModel):
    mode: Literal["remote","local"] = "remote"
    local_path: Optional[str] = None
    remote: RemoteEndpoint = Field(default_factory=RemoteEndpoint)

class FeedsConfig(BaseModel):
    current_key: Literal["products","stock","prices"] = "products"
    sources: Dict[str, FeedSource] = Field(default_factory=lambda: {
        "products": FeedSource(),
        "stock": FeedSource(),
    })

class InvoicesWebLogin(BaseModel):
    login: AuthConfig = Field(default_factory=lambda: AuthConfig(mode="form"))
    base_url: str = ""
    notes: str = ""

class InvoicesDownload(BaseModel):
    strategy: Literal["manual","paul-lange-web","api","email"] = "manual"
    web: Optional[InvoicesWebLogin] = None

class InvoicesConfig(BaseModel):
    layout: Literal["flat","by_number_date"] = "flat"
    months_back_default: int = 3
    download: InvoicesDownload = Field(default_factory=InvoicesDownload)

class SupplierConfig(BaseModel):
    feeds: FeedsConfig = Field(default_factory=FeedsConfig)
    invoices: InvoicesConfig = Field(default_factory=InvoicesConfig)

# ----------------- HELPERS -----------------
def _supplier_cfg_path(supplier: str) -> Path:
    return INVENTORY_DATA_ROOT / "suppliers" / supplier / "config.json"

SENSITIVE_KEYS = {"password", "basic_pass", "cookie", "token"}

def _deep_merge_keep(base: dict, override: dict) -> dict:
    """Deep merge dicts; for sensitive keys do not overwrite non-empty values with empty ones."""
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge_keep(base[k], v)
        else:
            if (v is None or v == "") and k in SENSITIVE_KEYS:
                continue
            base[k] = v
    return base

def _norm_auth(a: Dict[str, Any]) -> Dict[str, Any]:
    base = AuthConfig().model_dump()
    base.update(a or {})
    return base

def _migrate_incoming_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Backward-compat migration:
    - feed_url -> feeds.sources.products.remote.url
    - feeds.{mode,current_path,remote} -> feeds.sources.products + current_key="products"
    - auth (top-level) -> if strategy looks like paul-lange-web => invoices.download.web.login, else -> feeds.sources.products.remote.auth
    - default_months_window -> invoices.months_back_default
    - invoice_download_strategy -> invoices.download.strategy

    Dôležité: nevytváraj predvolené invoices.download/strategy, pokiaľ
    ich nepýta vstup (inak by sme resetli existujúcu stratégiu).
    """
    orig = dict(payload or {})
    p = dict(payload or {})

    # FEEDS
    feeds = p.setdefault("feeds", {})

    # 1) Jednozdrojový feed → sources.products
    if "remote" in feeds or "mode" in feeds or "current_path" in feeds:
        sources = feeds.setdefault("sources", {})
        if "products" not in sources:
            sources["products"] = {
                "mode": feeds.get("mode", "remote"),
                "local_path": feeds.get("current_path"),
                "remote": feeds.get("remote", RemoteEndpoint().model_dump()),
            }
        feeds.setdefault("current_key", "products")
        feeds.pop("remote", None)
        feeds.pop("mode", None)
        feeds.pop("current_path", None)

    # 2) feed_url → products.remote.url (vždy pretlač)
    if "feed_url" in p:
        sources = feeds.setdefault("sources", {})
        prod = sources.setdefault("products", FeedSource().model_dump())
        prod.setdefault("remote", RemoteEndpoint().model_dump())
        prod["remote"]["url"] = p.pop("feed_url") or ""
        feeds.setdefault("current_key", "products")

    # INVOICES (vytváraj len ak to dáva zmysel)
    had_invoices_in_input = "invoices" in orig or any(
        k in orig for k in ("default_months_window", "invoice_download_strategy")
    )
    invoices = p.get("invoices", {})
    if had_invoices_in_input and "invoices" not in p:
        p["invoices"] = invoices  # založ prázdne len ak vstup rieši invoices

    # 3) auth → podľa stratégie
    #    Ak vstup explicitne rieši invoices stratégiu alebo má auth + naznačenú PL stratégiu,
    #    doplň download/web/login. Inak auth patrí feedu.
    old_auth = None
    if "auth" in p:
        old_auth = p.pop("auth") or {}
    else:
        old_auth = (
            feeds.get("sources", {})
                 .get("products", {})
                 .get("remote", {})
                 .get("auth", None)
        )

    # Kandidát na stratégiu z inputu (bez defaultu!)
    input_strategy = (
        orig.get("invoice_download_strategy")
        or (orig.get("invoices") or {}).get("download", {}).get("strategy")
    )
    strat_pl = (
        input_strategy and str(input_strategy).lower().replace("_", "-") in
        {"paul-lange-web", "paul-lange", "paul-lange-web"}
    )

    if old_auth and strat_pl:
        inv = p.setdefault("invoices", {})
        dl = inv.setdefault("download", {})
        dl["strategy"] = "paul-lange-web"
        web = dl.setdefault("web", {})
        web["login"] = _norm_auth(old_auth)
    elif old_auth and not strat_pl and "auth" in (feeds.get("sources", {}).get("products", {}).get("remote", {})):
        # ak už v p.feeds ... auth existuje, nerieš; inak priraď k feedu
        sources = feeds.setdefault("sources", {})
        prod = sources.setdefault("products", FeedSource().model_dump())
        prod["remote"]["auth"] = _norm_auth(old_auth)

    # 4) default_months_window
    if "default_months_window" in p:
        inv = p.setdefault("invoices", {})
        inv["months_back_default"] = int(p.pop("default_months_window") or 3)

    # 5) invoice_download_strategy
    if "invoice_download_strategy" in p:
        inv = p.setdefault("invoices", {})
        dl = inv.setdefault("download", {})
        dl["strategy"] = p.pop("invoice_download_strategy") or "manual"

    return p


def load_supplier_config(supplier: str) -> SupplierConfig:
    path = _supplier_cfg_path(supplier)
    if not path.exists():
        return SupplierConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return SupplierConfig(**data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read supplier config: {e!s}")

def save_supplier_config(supplier: str, payload: Dict[str, Any]) -> SupplierConfig:
    # 1) load existing
    existing_cfg = load_supplier_config(supplier).model_dump()
    # 2) migrate incoming
    incoming = _migrate_incoming_payload(payload)
    # 3) deep merge
    merged = _deep_merge_keep(deepcopy(existing_cfg), incoming)
    # 4) validate + save
    cfg = SupplierConfig(**merged)
    path = _supplier_cfg_path(supplier)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cfg.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8")
    return cfg

# Helpers for feed selection
def _select_feed(cfg: SupplierConfig, key: Optional[str]) -> Tuple[str, FeedSource]:
    k = key or cfg.feeds.current_key or "products"
    fs = cfg.feeds.sources.get(k)
    if fs is None:
        # fallback to products
        k = "products"
        fs = cfg.feeds.sources.get("products", FeedSource())
    return k, fs





def _deep_get(d, path, default=None):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur

def _deep_set(d, path, value):
    cur = d
    for k in path[:-1]:
        if k not in cur or not isinstance(cur[k], dict):
            cur[k] = {}
        cur = cur[k]
    cur[path[-1]] = value

def normalize_supplier_config(raw: dict) -> dict:
    """
    Vezme 'raw' (hoci aj z legacy) a vráti kanonický tvar.
    Ak zistí legacy polia, presunie ich na kanonickú cestu.
    """
    out = deepcopy(KANON_DEFAULT)

    # --- FEEDS ---
    # staré polia typu: { "feed_url": "...", "feeds": { "mode": "remote", "remote": { ... } } }
    feed_url = raw.get("feed_url") or _deep_get(raw, ["feeds", "remote", "url"])
    if feed_url:
        _deep_set(out, ["feeds", "sources", "products", "remote", "url"], feed_url)
    # ak máš už nový tvar 'feeds.sources', mergni
    srcs = _deep_get(raw, ["feeds", "sources"])
    if isinstance(srcs, dict):
        for key, val in srcs.items():
            _deep_set(out, ["feeds", "sources", key], {**out["feeds"]["sources"].get(key, {}), **val})
    # current_key
    ck = _deep_get(raw, ["feeds", "current_key"]) or raw.get("feed_current_key")
    if ck:
        _deep_set(out, ["feeds", "current_key"], ck)
    # jednoduchý starý 'feeds.mode'/'current_path'
    mode = _deep_get(raw, ["feeds", "mode"])
    if mode in ("local", "remote"):
        _deep_set(out, ["feeds", "sources", "products", "mode"], mode)
    curp = _deep_get(raw, ["feeds", "current_path"])
    if curp is not None:
        _deep_set(out, ["feeds", "sources", "products", "local_path"], curp)
    # staré auth pre feeds
    old_feed_auth = _deep_get(raw, ["feeds", "remote", "auth"])
    if isinstance(old_feed_auth, dict):
        _deep_set(out, ["feeds", "sources", "products", "remote", "auth"], old_feed_auth)

    # --- INVOICES ---
    # layout, months_back_default
    for k in ("layout",):
        v = _deep_get(raw, ["invoices", k]) or raw.get(k)
        if v:
            _deep_set(out, ["invoices", k], v)
    mb = _deep_get(raw, ["invoices", "months_back_default"]) or raw.get("default_months_window")
    if isinstance(mb, int):
        _deep_set(out, ["invoices", "months_back_default"], mb)
    # strategy
    strategy = _deep_get(raw, ["invoices", "download", "strategy"]) or raw.get("invoice_download_strategy")
    if strategy:
        _deep_set(out, ["invoices", "download", "strategy"], strategy)

    # --- LOGIN (JEDINÉ miesto) ---
    # nový tvar:
    login_new = _deep_get(raw, ["invoices", "download", "web", "login"])
    if isinstance(login_new, dict):
        _deep_set(out, ["invoices", "download", "web", "login"], {**_deep_get(out, ["invoices", "download", "web", "login"], {}), **login_new})
    # legacy downloader.auth -> presuň
    dl_auth = _deep_get(raw, ["downloader", "auth"])
    if isinstance(dl_auth, dict):
        _deep_set(out, ["invoices", "download", "web", "login"], {**_deep_get(out, ["invoices", "download", "web", "login"], {}), **dl_auth})
    # úplne voľné polia (username/password/login_url/...)
    for k in ("mode","login_url","user_field","pass_field","username","password","cookie","insecure_all","basic_user","basic_pass","token","header_name"):
        v = raw.get(k)
        if v not in (None, ""):
            _deep_set(out, ["invoices", "download", "web", "login", k], v)
    # ak chýba login_url a je to PL, default:
    login = _deep_get(out, ["invoices", "download", "web", "login"]) or {}
    if not login.get("login_url"):
        _deep_set(out, ["invoices", "download", "web", "login", "login_url"], "https://vo.paul-lange-oslany.sk/index.php?cmd=default&id=login")

    # --- adapter_settings ---
    adapt = raw.get("adapter_settings") or {}
    _deep_set(out, ["adapter_settings"], {**out["adapter_settings"], **adapt})
    # staré: product_code_prefix, price_coefficients
    if "product_code_prefix" in raw:
        _deep_set(out, ["adapter_settings", "product_code_prefix"], raw["product_code_prefix"])
    if "price_coefficients" in raw and isinstance(raw["price_coefficients"], dict):
        _deep_set(out, ["adapter_settings", "price_coefficients"], raw["price_coefficients"])

    return out








# ----------------- ENDPOINTS -----------------
@router.get("/suppliers/{supplier}/config")
def get_supplier_config(supplier: str) -> Dict[str, Any]:
    return load_supplier_config(supplier).model_dump()

@router.post("/suppliers/{supplier}/config")
def post_supplier_config(supplier: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    cfg = save_supplier_config(supplier, payload)
    return cfg.model_dump()

@router.get("/suppliers/{supplier}/effective")
def supplier_effective(supplier: str, full: int = Query(0)) -> Dict[str, Any]:
    cfg = load_supplier_config(supplier)
    key, fs = _select_feed(cfg, None)
    if fs.mode == "local" and fs.local_path:
        using = {"key": key, "mode": "local", "path": fs.local_path}
    else:
        using = {"key": key, "mode": "remote", "url": fs.remote.url}
    resp = {
        "supplier": supplier,
        "using_feed": using,
        "invoices": {
            "months_back_default": cfg.invoices.months_back_default,
            "strategy": cfg.invoices.download.strategy,
            "layout": cfg.invoices.layout,
        },
    }
    if full:
        resp["config"] = cfg.model_dump()
    return resp

@router.post("/suppliers/{supplier}/feeds/refresh")
def refresh_feeds(
    supplier: str,
    source: Optional[str] = Query(None, description="Feed key to refresh (products, stock, prices). Defaults to current_key."),
    body: Optional[Dict[str, Any]] = Body(None),
) -> Dict[str, Any]:
    """Refresh selected feed source. Supports override via body.source_url."""
    cfg = load_supplier_config(supplier)
    key, fs = _select_feed(cfg, source)

    source_url = None
    if isinstance(body, dict):
        source_url = body.get("source_url")

    if source_url:
        chosen = {"kind": "override", "value": source_url, "key": key}
    elif fs.mode == "local" and fs.local_path:
        chosen = {"kind": "local", "value": fs.local_path, "key": key}
    else:
        chosen = {"kind": "remote", "value": fs.remote.url, "key": key}

    # TODO: Call real adapter refresh here, e.g. refresh_supplier_feed(supplier, key, chosen["value"])
    result = {"status": "ok", "using": chosen}

    return {"ok": True, "supplier": supplier, **result}



