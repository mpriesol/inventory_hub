"""Authenticated, request-only credentials for a read-only supplier AI probe."""
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from inventory_hub.access import operator_access
from inventory_hub.services import sloger_probe
from inventory_hub.services.catalog import CatalogError
from inventory_hub.sloger_probe_types import ProbeRequest


class ProbeRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except RequestValidationError:
                raise HTTPException(422, detail={"code": "sloger_probe_request_invalid", "message": "Check the lookup fields"},
                                    headers={"Cache-Control": "no-store"}) from None
            except CatalogError as error:
                raise HTTPException(error.status, detail={"code": error.code, "message": str(error)},
                                    headers={"Cache-Control": "no-store"}) from None
            except HTTPException as error:
                error.headers = {**(error.headers or {}), "Cache-Control": "no-store"}
                raise
            except Exception:
                raise HTTPException(503, detail={"code": "sloger_probe_unavailable", "message": "The lookup could not be completed; check its request ID before retrying"},
                                    headers={"Cache-Control": "no-store"}) from None
        return handler


def no_store(response: Response):
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/suppliers/sloger/lookup", tags=["Sloger lookup"], route_class=ProbeRoute,
                   dependencies=[Depends(operator_access), Depends(no_store)])


@router.post("/preview")
async def preview(body: ProbeRequest):
    return await sloger_probe.probe(body)


@router.get("/{id}")
async def result(id: str):
    if len(id) != 32 or any(c not in "0123456789abcdef" for c in id):
        raise HTTPException(404, detail={"code": "sloger_probe_not_found", "message": "Sloger lookup was not found"})
    return await sloger_probe.read(id)
