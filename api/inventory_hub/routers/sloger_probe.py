"""Authenticated, write-only login configuration and read-only supplier AI probe."""
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from inventory_hub.access import admin_access, operator_access
from inventory_hub.services import sloger_b2b, sloger_credentials, sloger_probe
from inventory_hub.services.catalog import CatalogError
from inventory_hub.sloger_probe_types import SlogerCredentials


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
async def preview():
    raise HTTPException(410, detail={"code": "sloger_probe_retired",
        "message": "Sloger data is read directly during product import and manual availability refresh"})


@router.get("/connection")
async def connection():
    return await sloger_b2b.connection()


@router.get("/credentials")
def credentials_status():
    return sloger_credentials.status()


@router.put("/credentials", dependencies=[Depends(admin_access)])
def save_credentials(body: SlogerCredentials):
    return sloger_credentials.save(body)


@router.get("/{id}")
async def result(id: str):
    if len(id) != 32 or any(c not in "0123456789abcdef" for c in id):
        raise HTTPException(404, detail={"code": "sloger_probe_not_found", "message": "Sloger lookup was not found"})
    return await sloger_probe.read(id)
