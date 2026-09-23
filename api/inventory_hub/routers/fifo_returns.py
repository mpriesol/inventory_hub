"""Operator-only physical returns, quarantine releases and cost corrections."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.fifo_return_types import FifoCostRevisionInput, FifoReleaseInput, FifoReturnInput
from inventory_hub.services import fifo
from inventory_hub.services import fifo_returns as service
from inventory_hub.services.stock_publication_gate import StockPublicationHoldError


class NoStoreRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                response = await original(request)
            except HTTPException as error:
                error.headers = {**(error.headers or {}), "Cache-Control": "no-store"}
                raise
            except RequestValidationError:
                response = JSONResponse(status_code=422, content={"detail": {
                    "code": "fifo_invalid_request", "message": "fifo_invalid_request"}})
            response.headers["Cache-Control"] = "no-store"
            return response
        return handler


router = APIRouter(prefix="/fifo", tags=["fifo"], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])
Identifier = Annotated[int, Path(gt=0)]


async def _call(operation):
    try:
        return await operation
    except (service.FifoReturnError, fifo.FifoError, StockPublicationHoldError) as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.code}) from None


@router.get("/issues/{movement_id}/return-options")
async def return_options(movement_id: Identifier, db: AsyncSession = Depends(get_session)):
    return await _call(service.return_options(db, movement_id))


@router.post("/returns")
async def receive_return(payload: FifoReturnInput, db: AsyncSession = Depends(get_session)):
    return await _call(service.receive_return(db, payload))


@router.post("/quarantine/release")
async def release_quarantine(payload: FifoReleaseInput, db: AsyncSession = Depends(get_session)):
    return await _call(service.release_quarantine(db, payload))


@router.get("/costs/{root_layer_id}")
async def cost_options(root_layer_id: Identifier, db: AsyncSession = Depends(get_session)):
    return await _call(service.cost_options(db, root_layer_id))


@router.post("/costs/revise")
async def revise_cost(payload: FifoCostRevisionInput, db: AsyncSession = Depends(get_session)):
    return await _call(service.revise_cost(db, payload))
