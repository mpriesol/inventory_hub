"""Authenticated download for catalog reconciliation; no import/write actions."""
import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.routers.product_editor import NoStoreRoute
from inventory_hub.routers.upgates_sync import _fetch_upgates_products, PREVIEW_CACHE_TTL_S
from inventory_hub.services.migration_export import build_export, download_chunks
from inventory_hub.services.migration_upgates_export import Dataset, ExportError, build_remote_export


router = APIRouter(prefix="/migration", tags=["migration"], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])


@router.get("/upgates-export")
async def upgates_export(dataset: Dataset, shop_code: Literal["biketrek", "xtrek"] = "xtrek"):
    try:
        output = await asyncio.to_thread(build_remote_export, shop_code, dataset)
    except ExportError as error:
        raise HTTPException(502, detail={"code": str(error)}) from None
    return StreamingResponse(download_chunks(output), media_type="application/zip", headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'attachment; filename="upgates-{shop_code}-{dataset}.zip"',
    }, background=BackgroundTask(output.close))


@router.get("/catalog-export")
async def catalog_export(shop_code: Literal["biketrek", "xtrek"] = "biketrek", refresh: bool = False,
                         local_only: bool = False,
                         db: AsyncSession = Depends(get_session)):
    if local_only:
        products, meta = [], {"source": "local_only", "catalog_included": False}
    else:
        products, meta = await asyncio.to_thread(_fetch_upgates_products, shop_code,
            ttl_s=PREVIEW_CACHE_TTL_S, force_refresh=refresh)
    output = await build_export(db, products, meta, shop_code)
    return StreamingResponse(download_chunks(output), media_type="application/zip", headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'attachment; filename="inventory-catalog-{shop_code}.zip"',
    }, background=BackgroundTask(output.close))
