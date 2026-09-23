"""Operator-only local edits with safe validation and durable save recovery."""
from typing import Annotated, Literal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession
from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.product_editor_types import ProductEditorSaveRequest
from inventory_hub.services import product_editor as service


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
                response = JSONResponse(status_code=422, content={"detail": {"code": "product_editor_invalid_request",
                                                                            "message": "product_editor_invalid_request"}})
            response.headers["Cache-Control"] = "no-store"
            return response
        return handler


router = APIRouter(prefix="/product-editor", tags=["product-editor"], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])
WarehouseCode = Annotated[str | None, Query(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,49}$")]


async def _call(operation):
    try:
        return await operation
    except service.EditorError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.code}) from None


@router.get("/options")
async def options(db: AsyncSession = Depends(get_session)):
    return await _call(service.options(db))


@router.get("/products")
async def products(q: Annotated[str, Query(max_length=200)] = "", brand: Annotated[str | None, Query(max_length=100)] = None,
    shop_code: Annotated[str | None, Query(pattern=r"^[a-z0-9][a-z0-9_-]{0,49}$")] = None,
    warehouse_code: WarehouseCode = None, page: Annotated[int, Query(ge=1, le=1000000)] = 1,
    page_size: Annotated[int, Query(ge=25, le=100)] = 50, sort: Literal["group_sku", "sku", "name"] = "group_sku",
    direction: Literal["asc", "desc"] = "asc", db: AsyncSession = Depends(get_session)):
    if page_size not in (25, 50, 100):
        raise HTTPException(422, detail={"code": "product_editor_invalid_request", "message": "product_editor_invalid_request"})
    return await _call(service.list_products(db, q=q, brand=brand, shop_code=shop_code, warehouse_code=warehouse_code,
        page=page, page_size=page_size, sort=sort, direction=direction))


@router.get("/products/{product_id}")
async def product(product_id: int, warehouse_code: WarehouseCode = None, db: AsyncSession = Depends(get_session)):
    return await _call(service.detail(db, product_id, warehouse_code=warehouse_code))


@router.post("/save")
async def save(payload: ProductEditorSaveRequest, db: AsyncSession = Depends(get_session)):
    return await _call(service.save(db, payload))


@router.get("/saves/{request_id}")
async def saved(request_id: UUID, db: AsyncSession = Depends(get_session)):
    return await _call(service.get_save(db, str(request_id)))
