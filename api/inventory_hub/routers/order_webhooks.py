"""Admin controls and three exact dedicated-credential delivery endpoints."""
import asyncio
import json
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from inventory_hub.access import admin_access
from inventory_hub.database import get_session, get_session_context
from inventory_hub.order_collection_types import CollectionConfirmation
from inventory_hub.order_webhook_types import WebhookConfigure, WebhookResolveRegistration
from inventory_hub.routers.order_collection import NoStoreRoute
from inventory_hub.services import order_collection, order_webhooks as service


router = APIRouter(prefix='/order-webhooks', tags=['order-webhooks'], route_class=NoStoreRoute,
                   dependencies=[Depends(admin_access)])
ingress_router = APIRouter(prefix='/webhooks/upgates/orders', tags=['order-webhooks'], route_class=NoStoreRoute)
ShopCode = Annotated[str, Query(pattern=r'^[a-z0-9][a-z0-9_-]{0,49}$')]


async def _call(operation):
    try:
        return await operation
    except (service.WebhookError, order_collection.CollectionError) as error:
        raise HTTPException(error.status, detail={'code': error.code, 'message': error.code}) from None


@router.get('/status')
async def status(shop_code: ShopCode, db: AsyncSession = Depends(get_session)):
    return await _call(service.status(db, shop_code))


@router.post('/configure', status_code=202)
async def configure(payload: WebhookConfigure, db: AsyncSession = Depends(get_session)):
    return await _call(service.configure(db, payload))


@router.post('/register', status_code=202)
async def register(payload: CollectionConfirmation, db: AsyncSession = Depends(get_session)):
    return await _call(service.request_registration(db, payload))


@router.post('/retry', status_code=202)
async def retry(payload: CollectionConfirmation, db: AsyncSession = Depends(get_session)):
    return await _call(service.retry_notifications(db, payload))


@router.post('/resolve-registration', status_code=202)
async def resolve_registration(payload: WebhookResolveRegistration, request: Request,
                               db: AsyncSession = Depends(get_session)):
    principal = getattr(request.state, 'hub_user', None) or {}
    actor = f"user:{principal['id']}" if principal.get('id') is not None else 'operator-token'
    return await _call(service.resolve_registration(db, payload, actor))


async def receive(request, event):
    try:
        # Includes session acquisition and commit. An ACK is returned only after
        # durable storage succeeds; the handler never waits for Upgates reads.
        async with asyncio.timeout(2):
            async with get_session_context() as db:
                shop, settings = await service.authenticate(db, request.headers.get('authorization'))
                if request.headers.get('content-encoding', 'identity') != 'identity':
                    raise service.WebhookError('order_webhook_invalid_request', 422)
                if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
                    raise service.WebhookError('order_webhook_invalid_request', 415)
                raw = bytearray()
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > service.MAX_BODY:
                        raise service.WebhookError('order_webhook_request_too_large', 413)
                try:
                    payload = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise service.WebhookError('order_webhook_invalid_request', 422) from None
                rows = service.notifications(payload, event)
                await service.accept(db, shop, settings, rows)
        return Response(status_code=204, headers={'Cache-Control': 'no-store'})
    except service.WebhookError as error:
        raise HTTPException(error.status, detail={'code': error.code, 'message': error.code}) from None
    except Exception:
        # SQL errors and request bodies can contain sensitive values. A bounded
        # generic failure lets Upgates retry; detailed payloads are never logged.
        raise HTTPException(503, detail={'code': 'order_webhook_unavailable', 'message': 'order_webhook_unavailable'}) from None


@ingress_router.post('/create', status_code=204)
async def created(request: Request):
    return await receive(request, 'create')


@ingress_router.post('/update', status_code=204)
async def updated(request: Request):
    return await receive(request, 'update')


@ingress_router.post('/delete', status_code=204)
async def deleted(request: Request):
    return await receive(request, 'delete')
