"""Authenticated local Atomer preview and download, with no Atomer writes."""

from typing import Annotated, Literal
from uuid import UUID
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    UploadFile,
    File,
    Form,
    Response,
)
from sqlalchemy.ext.asyncio import AsyncSession
from inventory_hub.access import admin_access, operator_access
from inventory_hub.database import get_session
from inventory_hub.routers.stock_publication import NoStoreRoute
from inventory_hub.atomer_availability_types import Configure, RunInput, ExportInput
from inventory_hub.services import atomer_availability as service

router = APIRouter(
    prefix="/atomer-availability",
    tags=["atomer-availability"],
    route_class=NoStoreRoute,
    dependencies=[Depends(operator_access)],
)


async def call(operation):
    try:
        return await operation
    except service.ExportError as error:
        raise HTTPException(
            error.status,
            detail={"code": error.code, "message": error.code, "known_unapplied": True},
        ) from None


@router.get("/options")
async def options(db: AsyncSession = Depends(get_session)):
    return await call(service.options(db))


@router.post("/configure", dependencies=[Depends(admin_access)])
async def configure(payload: Configure, db: AsyncSession = Depends(get_session)):
    return await call(service.configure(db, payload))


@router.post("/source/admin", dependencies=[Depends(admin_access)])
async def upload_admin(
    file: UploadFile = File(...),
    complete: bool = Form(False),
    db: AsyncSession = Depends(get_session),
):
    raw = await file.read(service.MAX_SOURCE + 1)
    try:
        return await call(service.upload_admin(db, raw, complete))
    except (ValueError, TypeError):
        raise HTTPException(
            422,
            detail={
                "code": "atomer_availability_invalid_admin",
                "message": "atomer_availability_invalid_admin",
            },
        ) from None


@router.post("/runs")
async def enqueue(payload: RunInput, db: AsyncSession = Depends(get_session)):
    return await call(service.enqueue(db, payload))


@router.get("/runs")
async def list_runs(db: AsyncSession = Depends(get_session)):
    return await call(service.list_runs(db))


@router.get("/runs/{run_id}")
async def get_run(
    run_id: UUID,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    view: Literal["all", "changes", "issues"] = "all",
    db: AsyncSession = Depends(get_session),
):
    return await call(service.get_run(db, str(run_id), offset, limit, view))


@router.post("/runs/{run_id}/export")
async def export(
    run_id: UUID, payload: ExportInput, db: AsyncSession = Depends(get_session)
):
    return await call(service.export(db, str(run_id), payload))


@router.get("/runs/{run_id}/download")
async def download(run_id: UUID, db: AsyncSession = Depends(get_session)):
    content, filename, checksum = await call(service.download(db, str(run_id)))
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-SHA256": checksum,
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
