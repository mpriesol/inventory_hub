"""Protected Hub-first import drafts, with separate explicit shop publication."""
from typing import Annotated
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession
from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.product_import_types import DraftCreate, DraftPatch, DraftRevision, DraftAiRequest, DraftPublish
from inventory_hub.routers.product_editor import NoStoreRoute
from inventory_hub.services import product_import as service, catalog_import
from inventory_hub.services.catalog import CatalogError

router = APIRouter(prefix='/product-imports', tags=['product-imports'], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])
DB = Annotated[AsyncSession, Depends(get_session)]
DraftId = Annotated[str, Path(pattern=r'^[a-f0-9]{32}$')]


async def call(operation):
    try:
        return await operation
    except CatalogError as error:
        raise HTTPException(error.status, detail={'code': error.code, 'message': str(error)}) from None


@router.get('')
async def history(db: DB):
    return await call(service.history(db))


@router.post('')
async def create(request: DraftCreate, db: DB):
    return await call(service.create(db, request))


@router.get('/{id}')
async def detail(id: DraftId, db: DB):
    async def operation():
        return await service.public(db, await service.get_draft(db, id))
    return await call(operation())


async def mutate(db, id, request, fn):
    async def operation():
        return await fn(db, await service.get_draft(db, id, lock=True), request)
    return await call(operation())


@router.put('/{id}')
async def patch(id: DraftId, request: DraftPatch, db: DB):
    return await mutate(db, id, request, service.patch_rows)


@router.post('/{id}/save')
async def save(id: DraftId, request: DraftRevision, db: DB):
    return await mutate(db, id, request, service.save)


@router.post('/{id}/publish-preview')
async def preview(id: DraftId, request: DraftRevision, db: DB):
    return await mutate(db, id, request, service.preview)


@router.post('/{id}/publish')
async def publish(id: DraftId, request: DraftPublish, background: BackgroundTasks, db: DB):
    result, run = await mutate(db, id, request, service.publish)
    # Commit confirmed draft state before background importer rechecks its snapshot.
    await db.commit()
    if run:
        background.add_task(catalog_import.execute_import, result['shop'], request.preview_id)
    return result


@router.post('/{id}/ai')
async def ai(id: DraftId, request: DraftAiRequest, db: DB):
    return await mutate(db, id, request, service.prepare_ai)


@router.post('/{id}/ai-start')
async def ai_start(id: DraftId, request: DraftRevision, db: DB):
    return await mutate(db, id, request, service.start_ai)


@router.post('/{id}/ai-apply')
async def ai_apply(id: DraftId, request: DraftRevision, db: DB):
    return await mutate(db, id, request, service.apply_ai)
