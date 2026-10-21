"""Authenticated brand registry and read-only shared SKU preview."""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.routers.product_editor import NoStoreRoute
from inventory_hub.sku_policy_types import BrandAliasCreate, BrandCreate, SkuPreviewRequest
from inventory_hub.services import sku_policy as service


router = APIRouter(prefix="/sku-policy", tags=["sku-policy"], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])


def _actor(request):
    user = getattr(request.state, "hub_user", None)
    return f"user:{user['id']}" if user and user.get("id") is not None else "operator-token"


async def _call(operation):
    try:
        return await operation
    except service.SkuPolicyError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.code,
            "reasons": error.reasons, "candidate_product_ids": error.candidate_product_ids}) from None


@router.get("/brands")
async def brands(db: AsyncSession = Depends(get_session)):
    return await _call(service.list_brands(db))


@router.post("/brands")
async def create_brand(payload: BrandCreate, request: Request, db: AsyncSession = Depends(get_session)):
    result = await _call(service.create_brand(db, payload, actor=_actor(request)))
    await db.commit()
    return result


@router.post("/brands/{brand_id}/aliases")
async def add_alias(brand_id: int, payload: BrandAliasCreate, request: Request,
                    db: AsyncSession = Depends(get_session)):
    result = await _call(service.add_brand_alias(db, brand_id, payload, actor=_actor(request)))
    await db.commit()
    return result


@router.post("/preview")
async def preview(payload: SkuPreviewRequest, db: AsyncSession = Depends(get_session)):
    return await _call(service.preview_sku(db, payload.sku_identity, barcodes=payload.barcodes,
        supplier_id=payload.supplier_id, supplier_sku=payload.supplier_sku,
        expected_product_id=payload.expected_product_id, sku=payload.sku))
