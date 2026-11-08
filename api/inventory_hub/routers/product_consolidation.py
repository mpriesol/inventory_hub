"""Operator-only, durable review of one unchanged complete retail unit."""
from uuid import UUID
from typing import Annotated
from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field
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


@router.get('/selection')
async def selection(product_ids: list[Annotated[int, Field(gt=0, le=9007199254740991)]] = Query(min_length=2, max_length=10),
                    db: AsyncSession = Depends(get_session)):
    from inventory_hub.services import product_consolidation_selection
    return await call(product_consolidation_selection.inspect(db, product_ids))


@router.get('/{identifier}')
async def get(identifier: UUID, db: AsyncSession = Depends(get_session)):
    return await call(service.get(db, str(identifier)))


@router.get('/{identifier}/dependency-diagnostics')
async def dependency_diagnostics(identifier: UUID, db: AsyncSession = Depends(get_session)):
    from inventory_hub.services import product_consolidation_diagnostics
    return await call(product_consolidation_diagnostics.inspect(db, str(identifier)))


@router.get('/{identifier}/dependency-evidence')
async def dependency_evidence(identifier: UUID,
                              job_ids: list[Annotated[str, Field(pattern=r'^[0-9a-f]{32}$')]] = Query(min_length=1, max_length=8),
                              db: AsyncSession = Depends(get_session)):
    from inventory_hub.services import product_dependency_evidence
    return await call(product_dependency_evidence.inspect(db, str(identifier), job_ids))


@router.post('/{identifier}/apply')
async def apply(identifier: UUID, payload: ProductConsolidationApplyRequest, request: Request,
                db: AsyncSession = Depends(get_session)):
    return await call(service.apply(db, str(identifier), payload, actor=actor(request)))


@router.post('/{identifier}/discard')
async def discard(identifier: UUID, payload: ProductConsolidationApplyRequest,
                  db: AsyncSession = Depends(get_session)):
    return await call(service.discard(db, str(identifier), payload))
