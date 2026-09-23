# inventory_hub/services/upgates.py
"""
Upgates API v2 client.

Auth: HTTP Basic (API login + key), created in Upgates admin (Doplnky / API).
Credentials are read from the shop filesystem config
(shops/{shop}/config.json -> upgates_api_base_url, upgates_login,
upgates_api_key) which lives outside the repository.

Pagination: GET lists return one page; `page` selects the page,
`current_page_items` (max 100) sets the page size. Errors come back in a
`messages` field. After 5 bad logins Upgates blocks the API user (403),
so we never retry on 401/403.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
import math
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import requests

from inventory_hub.config_io import load_shop as load_shop_config
from inventory_hub.settings import settings


class UpgatesError(Exception):
    """Raised for configuration or API-level failures."""
    def __init__(self, message: str, status_code: int | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


def _retry_after_seconds(value) -> int | None:
    """Keep only a bounded delay; never propagate the upstream header text."""
    if not isinstance(value, str) or len(value) > 100:
        return None
    value = value.strip()
    try:
        if value.isascii() and value.isdigit():
            seconds = int(value)
        else:
            at = parsedate_to_datetime(value)
            if at.tzinfo is None or at.utcoffset() is None:
                return None
            seconds = math.ceil((at - datetime.now(timezone.utc)).total_seconds())
        return min(604800, max(1, seconds))
    except (ValueError, TypeError, OverflowError):
        return None


class UpgatesClient:
    def __init__(
        self,
        base_url: str,
        login: str,
        api_key: str,
        verify_ssl: bool = True,
        timeout: int = 60,
        log_dir: Optional[Path] = None,
    ):
        if not base_url or not login or not api_key:
            raise UpgatesError(
                "Chýbajú Upgates API prístupy (upgates_api_base_url / "
                "upgates_login / upgates_api_key) v konfigurácii shopu."
            )
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.log_dir = log_dir
        self.session = requests.Session()
        self.session.auth = (login, api_key)
        self.session.headers.update({"Accept": "application/json"})

    # ── Construction ─────────────────────────────────────────────────────

    @classmethod
    def from_shop(cls, shop_code: str) -> "UpgatesClient":
        cfg = load_shop_config(shop_code) or {}
        log_dir = Path(settings.INVENTORY_DATA_ROOT) / "shops" / shop_code / "logs"
        return cls(
            base_url=cfg.get("upgates_api_base_url") or "",
            login=cfg.get("upgates_login") or "",
            api_key=cfg.get("upgates_api_key") or "",
            verify_ssl=bool(cfg.get("verify_ssl", True)),
            log_dir=log_dir,
        )

    # ── Low level ────────────────────────────────────────────────────────

    def _log(self, name: str, payload: Any) -> None:
        if self.log_dir is None:
            return
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            (self.log_dir / name).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2)[:2_000_000],
                encoding="utf-8",
            )
        except Exception:
            pass

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            r = self.session.get(url, params=params or {}, timeout=self.timeout, verify=self.verify_ssl)
        except requests.RequestException as e:
            raise UpgatesError(f"Upgates API nedostupné: {e}") from e
        if r.status_code in (401, 403):
            # Do NOT retry — 5 failed logins block the API user on Upgates side.
            raise UpgatesError(
                f"Upgates API odmietlo prihlásenie (HTTP {r.status_code}). "
                "Over upgates_login / upgates_api_key v konfigurácii shopu."
            )
        if r.status_code >= 400:
            raise UpgatesError(f"Upgates API chyba HTTP {r.status_code}: {r.text[:300]}")
        try:
            data = r.json()
        except ValueError as e:
            raise UpgatesError(f"Upgates API vrátilo ne-JSON odpoveď: {r.text[:200]}") from e
        msgs = data.get("messages") if isinstance(data, dict) else None
        if msgs:
            self._log("upgates_last_messages.json", msgs)
        return data

    def post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            r = self.session.post(url, json=payload, timeout=self.timeout, verify=self.verify_ssl)
        except requests.RequestException as e:
            raise UpgatesError(f"Upgates API nedostupné: {e}") from e
        if r.status_code in (401, 403):
            raise UpgatesError(
                f"Upgates API odmietlo prihlásenie (HTTP {r.status_code}). "
                "Over upgates_login / upgates_api_key v konfigurácii shopu.", status_code=r.status_code
            )
        if r.status_code >= 400:
            raise UpgatesError(f"Upgates API chyba HTTP {r.status_code}: {r.text[:300]}", status_code=r.status_code)
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text[:1000]}
        return data

    def put(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            response = self.session.put(f"{self.base_url}/{path.lstrip('/')}", json=payload,
                                        timeout=self.timeout, verify=self.verify_ssl)
        except requests.RequestException:
            raise UpgatesError("Upgates update outcome unknown") from None
        if response.status_code >= 400:
            raise UpgatesError(f"Upgates update HTTP {response.status_code}", status_code=response.status_code)
        try:
            return response.json()
        except ValueError:
            return {}

    # ── Products ─────────────────────────────────────────────────────────

    def iter_products(self, page_size: int = 100) -> Iterator[Dict[str, Any]]:
        """Yield all products across pages. Logs the first page raw for diagnostics."""
        page = 1
        while True:
            data = self.get("products", {"page": page, "current_page_items": page_size})
            if page == 1:
                self._log("upgates_products_page1.json", data)
            products = data.get("products") or []
            for p in products:
                if isinstance(p, dict):
                    yield p
            total_pages = int(data.get("number_of_pages") or 1)
            if page >= total_pages or not products:
                break
            page += 1

    def check_connection(self) -> Dict[str, Any]:
        data = self.get("products", {"page": 1, "current_page_items": 1})
        return {
            "ok": True,
            "number_of_items": data.get("number_of_items"),
            "number_of_pages": data.get("number_of_pages"),
        }

    def read_order_audit(self, params: Dict[str, Any]) -> tuple[dict, dict]:
        """Two bounded GETs, with no raw order/error-body logging or retries.

        Unlike the general diagnostic GET path, customer-bearing responses
        must stay in memory and must never be copied into public data/logs.
        The caller projects a small, explicit non-customer response shape.
        """
        return self._read_order_json("orders", params), self.read_order_statuses()

    def read_order_statuses(self) -> dict:
        return self._read_order_json("order-statuses", {})

    def read_order_collection(self, params: Dict[str, Any]) -> dict:
        """One change-discovery page; no status fetch or raw response logging."""
        return self._read_order_json("orders", params)

    def _read_order_json(self, path: str, params: Dict[str, Any]) -> dict:
        """Bounded customer-bearing reads, without diagnostic logs or retries."""
        try:
            response = self.session.get(
                f"{self.base_url}/{path}", params=params,
                timeout=(5, min(self.timeout, 25)), verify=self.verify_ssl,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise UpgatesError("Order audit connection failed") from None
        try:
            if response.status_code != 200:
                retry_after = _retry_after_seconds(response.headers.get("Retry-After")) if response.status_code == 429 else None
                raise UpgatesError("Order audit request failed", status_code=response.status_code, retry_after=retry_after)
            if len(response.content) > 8_000_000:
                raise UpgatesError("Order audit response is too large")
            try:
                data = response.json(parse_float=Decimal)
            except ValueError:
                raise UpgatesError("Order audit response is not valid JSON") from None
            if not isinstance(data, dict) or data.get("messages"):
                raise UpgatesError("Order audit response needs verification")
            return data
        finally:
            response.close()


# ── Tolerant field extraction helpers (shapes vary slightly across versions) ──

def product_title(p: Dict[str, Any], preferred_lang: str = "sk") -> str:
    descs = p.get("descriptions") or []
    if isinstance(descs, list):
        for d in descs:
            if isinstance(d, dict) and d.get("language") == preferred_lang and d.get("title"):
                return str(d["title"])
        for d in descs:
            if isinstance(d, dict) and d.get("title"):
                return str(d["title"])
    return str(p.get("title") or p.get("code") or "")


def variant_params_text(v: Dict[str, Any]) -> str:
    parts: List[str] = []
    for prm in v.get("parameters") or []:
        if not isinstance(prm, dict):
            continue
        val = prm.get("value")
        if isinstance(val, list):  # per-language values
            val = next((x.get("value") for x in val if isinstance(x, dict) and x.get("value")), None)
        if val:
            parts.append(str(val))
    return ", ".join(parts)


def first_ean(obj: Dict[str, Any]) -> str:
    ean = obj.get("ean")
    if isinstance(ean, list):
        ean = next((str(x) for x in ean if x), "")
    return (str(ean or "")).strip()
