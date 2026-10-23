"""Private download endpoint; selecting documents does not send them to anyone."""
from typing import Annotated
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse
from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.routers.fifo import NoStoreRoute
from inventory_hub.services.invoice_export import export

router = APIRouter(prefix='/invoices', tags=['invoice-export'], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])

class ExportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    invoice_ids: list[Annotated[int, Field(gt=0, strict=True)]] = Field(min_length=1, max_length=200)

@router.post('/export')
async def download(payload: ExportRequest, db: AsyncSession = Depends(get_session)):
    bundle = await export(db, payload.invoice_ids)
    def chunks():
        try:
            while block := bundle.read(64 * 1024):
                yield block
        finally:
            bundle.close()
    return StreamingResponse(chunks(), media_type='application/zip', headers={
        'Content-Disposition': 'attachment; filename="invoices.zip"', 'Cache-Control': 'private, no-store',
        'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer'})
