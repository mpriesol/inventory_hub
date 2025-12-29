
from __future__ import annotations
from fastapi import APIRouter, HTTPException, Body, Query
from typing import Any, Dict, List, Optional
from pathlib import Path
import os, json

try:
    from ..settings import INVENTORY_DATA_ROOT  # type: ignore
except Exception:
    INVENTORY_DATA_ROOT = os.environ.get("INVENTORY_DATA_ROOT") or "inventory-data"

try:
    from ..routers.suppliers import load_supplier_config  # type: ignore
except Exception:
    from ..configs import load_supplier_config  # type: ignore
    
from ..adapters.paul_lange import refresh_paul_lange_invoices  # type: ignore
from ..invoices_util import InvoiceIndex  # type: ignore

router = APIRouter()

@router.post("/suppliers/{supplier}/invoices/refresh")
def refresh_invoices(
    supplier: str,
    months_back: int = Body(3, embed=True),
    strategy: Optional[str] = Body(None, embed=True),
    shop: Optional[str] = Query(None)  # len kvôli spätným kompatibilitám
) -> Dict[str, Any]:
    raw_cfg = load_supplier_config(supplier)
    cfg = raw_cfg.model_dump() if hasattr(raw_cfg, "model_dump") else dict(raw_cfg)

    # 1) Stratégia – poradie: override z body -> top-level -> nested
    strat = (strategy
             or cfg.get("invoice_download_strategy")
             or (cfg.get("invoices") or {}).get("download_strategy")
             or "").lower().strip()

    if strat in {"paul-lange-web", "paul_lange_web", "paul_lange"}:
        # 2) Auth – najprv top-level, potom vnorené feeds.remote.auth
        auth = cfg.get("auth") or {}
        if not auth:
            feeds = cfg.get("feeds") or {}
            remote = feeds.get("remote") or {}
            auth = remote.get("auth") or {}

        conf = {
            "auth_mode":     auth.get("mode") or "form",
            "login_url":     auth.get("login_url") or "https://vo.paul-lange-oslany.sk/index.php?id=login",
            "username":      auth.get("username") or auth.get("basic_user") or "",
            "password":      auth.get("password") or auth.get("basic_pass") or "",
            "cookie":        auth.get("cookie") or "",
            "insecure_all":  bool(auth.get("insecure_all", False)),
            "invoices":      cfg.get("invoices") or {},
        }
        data_root = Path(INVENTORY_DATA_ROOT)
        result = refresh_paul_lange_invoices(
            data_root=data_root,
            supplier=supplier,
            months_back=months_back,
            conf=conf
        )
        return {"supplier": supplier, **result}

    raise HTTPException(status_code=400, detail=f"Unsupported invoice_download_strategy: {strat!r}")


@router.get("/suppliers/{supplier}/invoices/index")
def get_invoices_index(supplier: str) -> Dict[str, Any]:
    base = Path(INVENTORY_DATA_ROOT) / "suppliers" / supplier
    idx = InvoiceIndex(base)
    try:
        latest = json.loads((idx.map_json).read_text(encoding="utf-8"))
    except Exception:
        latest = {}
    return {"supplier": supplier, "count": len(latest), "items": list(latest.values())}

@router.post("/suppliers/{supplier}/invoices/mark_processed")
def mark_invoices_processed(supplier: str, invoice_ids: List[str] = Body(..., embed=True)) -> Dict[str, Any]:
    base = Path(INVENTORY_DATA_ROOT) / "suppliers" / supplier
    idx = InvoiceIndex(base)
    changed = idx.mark_processed(invoice_ids)
    return {"supplier": supplier, "updated": changed}
