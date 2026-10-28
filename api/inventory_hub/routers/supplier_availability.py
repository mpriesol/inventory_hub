"""Operator-only supplier scheduling and manual queue controls."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.routers.stock_settings import NoStoreRoute
from inventory_hub.services import supplier_availability as service
from inventory_hub.services import supplier_links
from inventory_hub.services.supplier_availability_source import AvailabilityError
from inventory_hub.supplier_availability_types import (
    SupplierAvailabilityInput, SupplierAvailabilityRunInput, SupplierLinkReconcileInput, SupplierAvailabilityRecheckInput,
)

router = APIRouter(prefix="/supplier-availability", tags=["supplier-availability"], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])
SupplierCode = Annotated[str, Path(pattern=r"^[a-zA-Z0-9_-]{1,50}$")]


async def _call(operation):
    try:
        return await operation
    except AvailabilityError as error:
        raise HTTPException(error.status, detail={"code": error.code, "message": error.code}) from None


@router.get("")
async def overview(db: AsyncSession = Depends(get_session)):
    return await _call(service.overview(db))


@router.get("/{supplier}/observation")
async def source_observation(supplier: SupplierCode,
                             sku: Annotated[str, Query(min_length=1, max_length=100, pattern=r"^[^\x00-\x1f\x7f]+$")],
                             db: AsyncSession = Depends(get_session)):
    return await _call(service.source_observation(db, supplier, sku))


@router.put("/{supplier}")
async def configure(supplier: SupplierCode, payload: SupplierAvailabilityInput, db: AsyncSession = Depends(get_session)):
    return await _call(service.configure(db, supplier, payload))


@router.get("/{supplier}/runs/{run_id}/failures")
async def run_failures(supplier: SupplierCode, run_id: Annotated[int, Path(gt=0)],
                       offset: Annotated[int, Query(ge=0)] = 0,
                       limit: Annotated[int, Query(ge=1, le=100)] = 50,
                       db: AsyncSession = Depends(get_session)):
    return await _call(service.run_failures(db, supplier, run_id, offset, limit))


@router.post("/{supplier}/run", status_code=202)
async def run(supplier: SupplierCode, payload: SupplierAvailabilityRunInput, db: AsyncSession = Depends(get_session)):
    return await _call(service.request_run(db, supplier, payload))


@router.post("/{supplier}/publication/recheck", status_code=202)
async def recheck(supplier: SupplierCode, payload: SupplierAvailabilityRecheckInput, db: AsyncSession = Depends(get_session)):
    from inventory_hub.services.supplier_availability_publication import request_recheck
    return await _call(request_recheck(db, supplier, payload))


@router.post("/{supplier}/links/reconcile")
async def reconcile_links(supplier: SupplierCode, payload: SupplierLinkReconcileInput,
                          db: AsyncSession = Depends(get_session)):
    """Attach exact existing supplier identities using only local downloaded data."""
    return await _call(supplier_links.reconcile_supplier_links(
        db, supplier, after_product_id=payload.after_product_id, limit=payload.limit, match_catalog=True))


# Read-only history/recovery for the retired one-off administrative rename.
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from inventory_hub.access import admin_access
from inventory_hub.services import zookee_code_reconciliation as code_service
from inventory_hub.services.product_editor import EditorError


class CodeReconcileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID


@router.get("/{supplier}/legacy-codes", dependencies=[Depends(admin_access)])
async def legacy_codes(supplier: SupplierCode, db: AsyncSession = Depends(get_session)):
    try:
        return await code_service.preview(db,supplier)
    except EditorError as error:
        raise HTTPException(error.status,detail={"code":error.code}) from None


@router.get("/{supplier}/legacy-code-results/{request_id}", dependencies=[Depends(admin_access)])
async def legacy_code_result(supplier: SupplierCode, request_id: UUID, db: AsyncSession = Depends(get_session)):
    try:
        return await code_service.read_result(db, supplier, request_id)
    except EditorError as error:
        raise HTTPException(error.status, detail={"code": error.code}) from None


@router.post("/{supplier}/legacy-codes/{product_id}", dependencies=[Depends(admin_access)])
async def reconcile_legacy_code(supplier: SupplierCode, product_id: Annotated[int, Path(gt=0)], payload: CodeReconcileInput,
                                db: AsyncSession = Depends(get_session)):
    try:
        return await code_service.reconcile_one(db,supplier,product_id,payload.request_id)
    except EditorError as error:
        raise HTTPException(error.status,detail={"code":error.code}) from None


class CodeReconcileRow(CodeReconcileInput):
    product_id: StrictInt = Field(gt=0)


class CodeReconcileBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: list[CodeReconcileRow] = Field(min_length=1, max_length=20)


@router.post("/{supplier}/legacy-code-batches", dependencies=[Depends(admin_access)])
async def reconcile_legacy_batch(supplier: SupplierCode, payload: CodeReconcileBatch,
                                 db: AsyncSession = Depends(get_session)):
    if len({row.product_id for row in payload.rows}) != len(payload.rows) or len({row.request_id for row in payload.rows}) != len(payload.rows):
        raise HTTPException(422, detail={"code": "product_editor_invalid_request"})
    try:
        return await code_service.reconcile_batch(db, supplier, payload.rows)
    except EditorError as error:
        raise HTTPException(error.status, detail={"code": error.code}) from None
