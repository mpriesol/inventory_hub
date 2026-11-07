"""Operator-only, durable review of one unchanged complete retail unit."""
from uuid import UUID
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.product_consolidation_types import ProductConsolidationApplyRequest, ProductConsolidationPreviewRequest
from inventory_hub.routers.product_links import NoStoreRoute, actor, call
from inventory_hub.services import product_consolidation as service

class ConsolidationRoute(NoStoreRoute):
    validation_error_code = 'consolidation_invalid_request'


router = APIRouter(prefix='/product-consolidations', tags=['product-consolidations'], route_class=ConsolidationRoute,
                   dependencies=[Depends(operator_access)])


@router.post('/preview')
async def preview(payload: ProductConsolidationPreviewRequest, request: Request, db: AsyncSession = Depends(get_session)):
    return await call(service.preview(db, payload, actor=actor(request)))


@router.get('/{identifier}')
async def get(identifier: UUID, db: AsyncSession = Depends(get_session)):
    return await call(service.get(db, str(identifier)))


@router.post('/{identifier}/apply')
async def apply(identifier: UUID, payload: ProductConsolidationApplyRequest, request: Request,
                db: AsyncSession = Depends(get_session)):
    return await call(service.apply(db, str(identifier), payload, actor=actor(request)))
