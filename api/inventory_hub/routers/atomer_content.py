"""Authenticated Atomer XML preparation; sending uses the shared publisher."""
import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.access import operator_access
from inventory_hub.adapters.atomer_content import AtomerSourceError, parse_selected
from inventory_hub.atomer_content_types import AtomerContentBatch, AtomerRollbackRequest
from inventory_hub.database import get_session
from inventory_hub.routers.product_editor import NoStoreRoute
from inventory_hub.services import atomer_content as service, product_publication


router = APIRouter(prefix='/migration/atomer-content', tags=['migration'], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])


@router.post('/preview')
async def preview(selection: Annotated[str, Form(max_length=100000)],
                  source_file: UploadFile = File(...), db: AsyncSession = Depends(get_session)):
    try:
        request = AtomerContentBatch.model_validate_json(selection)
        snapshot = await asyncio.to_thread(parse_selected, source_file.file,
                                          [item.source_code for item in request.items])
        snapshot.update(parameter_names=request.parameter_names, formatting=request.formatting)
    except ValidationError:
        raise HTTPException(422, detail={'code': 'atomer_content_invalid_selection'}) from None
    except AtomerSourceError as exc:
        raise HTTPException(422, detail={'code': exc.code}) from None
    finally:
        await source_file.close()
    items = []
    for item in request.items:
        result = {'source_code': item.source_code, 'hub_sku': item.hub_sku}
        try:
            result['publication'] = await service.preview(db, item, snapshot)
        except service.ERROR as exc:
            await db.rollback()
            result['error'] = {'code': exc.code, 'reasons': exc.reasons}
        items.append(result)
    return {'source': {key: snapshot[key] for key in ('sha256', 'size_bytes', 'total_rows')}, 'items': items}


@router.get('/publications')
async def history(db: AsyncSession = Depends(get_session)):
    return await service.history(db)


@router.get('/publications/{identifier}/audit')
async def audit(identifier: UUID, db: AsyncSession = Depends(get_session)):
    try:
        record = await product_publication._get(db, str(identifier))
    except service.ERROR as exc:
        raise HTTPException(exc.status, detail={'code': exc.code}) from None
    if record.document.get('workflow') != service.WORKFLOW:
        raise HTTPException(404, detail={'code': 'atomer_content_publication_missing'})
    return {'publication': product_publication.summary(record), 'document': record.document}


@router.post('/publications/{identifier}/rollback-preview')
async def rollback(identifier: UUID, payload: AtomerRollbackRequest, db: AsyncSession = Depends(get_session)):
    try:
        return await service.rollback_preview(db, str(identifier), str(payload.request_id))
    except service.ERROR as exc:
        raise HTTPException(exc.status, detail={'code': exc.code}) from None
