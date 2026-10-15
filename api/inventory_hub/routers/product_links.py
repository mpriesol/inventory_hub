"""Authenticated preview/apply of local links to existing Upgates products."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.product_link_types import (
    ProductLinksApplyRequest, ProductLinksCandidatesRequest, ProductLinksPreviewRequest, ProductLinksRefreshRequest,
)
from inventory_hub.services import product_links as service


class NoStoreRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request):
            try:
                response = await original(request)
            except HTTPException as error:
                error.headers = {**(error.headers or {}), 'Cache-Control': 'no-store'}
                raise
            except RequestValidationError:
                response = JSONResponse(status_code=422, content={'detail': {'code': 'product_links_invalid_request'}})
            response.headers['Cache-Control'] = 'no-store'
            return response
        return handler


router = APIRouter(prefix='/product-links', tags=['product-links'], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])


def actor(request):
    user = getattr(request.state, 'hub_user', None)
    return {'user_id': user.get('id')} if isinstance(user, dict) else {'source': 'operator_token'}


async def call(operation):
    try:
        return await operation
    except service.LinkError as error:
        headers = {'Retry-After': str(error.retry_after)} if error.retry_after else None
        detail = {'code': error.code}
        if error.known_unapplied:
            detail['known_unapplied'] = True
        raise HTTPException(error.status, detail=detail, headers=headers) from None


@router.post('/candidates')
async def candidates(payload: ProductLinksCandidatesRequest, db: AsyncSession = Depends(get_session)):
    return await call(service.candidates(db, payload))


@router.post('/preview')
async def preview(payload: ProductLinksPreviewRequest, request: Request, db: AsyncSession = Depends(get_session)):
    return await call(service.preview(db, payload, actor=actor(request)))


@router.get('/{identifier}')
async def get(identifier: UUID, db: AsyncSession = Depends(get_session)):
    return await call(service.get(db, str(identifier)))


@router.post('/refresh')
async def refresh(payload: ProductLinksRefreshRequest, db: AsyncSession = Depends(get_session)):
    return await call(service.refresh(db, payload))


@router.post('/{identifier}/apply')
async def apply(identifier: UUID, payload: ProductLinksApplyRequest, request: Request, db: AsyncSession = Depends(get_session)):
    return await call(service.apply(db, str(identifier), payload, actor=actor(request)))
