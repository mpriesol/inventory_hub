from __future__ import annotations
from fastapi import APIRouter, HTTPException, Body, Query
from typing import Any, Dict, List, Optional
from pathlib import Path
import os, json

try:
    from ..settings import INVENTORY_DATA_ROOT  # type: ignore
except Exception:
    INVENTORY_DATA_ROOT = os.environ.get("INVENTORY_DATA_ROOT") or r"C:\!kafe\BikeTrek\web\inventory-data"

from .suppliers import load_supplier_config  # normalized model
from ..adapters.paul_lange import refresh_paul_lange_invoices  # type: ignore
from ..invoices_util import InvoiceIndex  # type: ignore

router = APIRouter(tags=["invoices"])

@router.post("/suppliers/{supplier}/invoices/refresh")
def refresh_invoices(
    supplier: str,
    months_back: int = Body(3, embed=True),
    strategy: Optional[str] = Body(None, embed=True),
    shop: Optional[str] = Query(None)  # reserved / back-compat
) -> Dict[str, Any]:
    cfg_model = load_supplier_config(supplier)  # canonical & validated
    cfg = cfg_model.model_dump()

    # strategy: body override -> canonical
    strat = (strategy or cfg["invoices"]["download"]["strategy"] or "").lower().replace("_", "-")
    if strat not in {"paul-lange-web", "manual", "api", "email"}:
        raise HTTPException(status_code=400, detail=f"Unsupported invoice_download_strategy: {strat!r}")

    base = Path(INVENTORY_DATA_ROOT) / "suppliers" / supplier

    if strat == "paul-lange-web":
        # Postav konf pre adaptér z canonicalu
        login = ((cfg.get("invoices") or {}).get("download") or {}).get("web", {}).get("login", {}) or {}
        conf = {
            "auth_mode":    login.get("mode") or "form",
            "login_url":    login.get("login_url") or "https://vo.paul-lange-oslany.sk/index.php?cmd=default&id=login",
            "username":     login.get("username") or login.get("basic_user") or "",
            "password":     login.get("password") or login.get("basic_pass") or "",
            "cookie":       login.get("cookie") or "",
            "insecure_all": bool(login.get("insecure_all", False)),
            # adapter číta aj ďalšie veci z "invoices"
            "invoices":     cfg.get("invoices") or {},
        }
        try:
            result = refresh_paul_lange_invoices(
                data_root=base,
                supplier=supplier,
                months_back=months_back,
                conf=conf,
            )
            return {"supplier": supplier, **result}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # ďalšie stratégie dorobíme neskôr
    raise HTTPException(status_code=400, detail=f"Strategy {strat!r} not implemented yet")

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
