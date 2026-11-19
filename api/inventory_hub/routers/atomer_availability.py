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
    Request,
)
from sqlalchemy.ext.asyncio import AsyncSession
from inventory_hub.access import admin_access, operator_access
from inventory_hub.database import get_session
from inventory_hub.routers.stock_publication import NoStoreRoute
from inventory_hub.atomer_availability_types import Configure, RunInput, ExportInput, EditInput, RowFilter
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
    search: Annotated[str, Query(max_length=255)] = "",
    kind: Literal["all", "parent", "variant", "standalone"] = "all",
    status: Literal["all", "changed", "unchanged", "context", "blocked", "unmatched"] = "all",
    visibility: Literal["all", "visible", "hidden", "unknown"] = "all",
    current_visibility: Literal["all", "visible", "hidden", "unknown"] = "all",
    proposed_visibility: Literal["all", "visible", "hidden", "unknown"] = "all",
    availability: Annotated[str | None, Query(max_length=100)] = None,
    current_availability: Annotated[str | None, Query(max_length=100)] = None,
    proposed_availability: Annotated[str | None, Query(max_length=100)] = None,
    include_in_export: bool | None = None,
    manual_override: bool | None = None,
    sort: Literal["code", "name", "sku", "ean", "current_availability", "proposed_availability",
                  "current_visibility", "proposed_visibility", "qty_available", "status",
                  "include_in_export", "manual_override"] = "code",
    direction: Literal["asc", "desc"] = "asc",
    db: AsyncSession = Depends(get_session),
):
    filters = RowFilter(view=view, search=search, kind=kind, status=status, visibility=visibility,
                        current_visibility=current_visibility, proposed_visibility=proposed_visibility,
                        availability=availability, current_availability=current_availability,
                        proposed_availability=proposed_availability,
                        include_in_export=include_in_export, manual_override=manual_override)
    return await call(service.get_run(db, str(run_id), offset, limit, filters=filters, sort=sort, direction=direction))


def actor(request):
    user = getattr(request.state, "hub_user", None)
    return f"user:{user['id']}" if user and user.get("id") is not None else "operator-token"


@router.post("/runs/{run_id}/edit")
async def edit(run_id: UUID, payload: EditInput, request: Request, db: AsyncSession = Depends(get_session)):
    return await call(service.edit(db, str(run_id), payload, actor=actor(request)))


@router.get("/runs/{run_id}/edits/{request_id}")
async def edit_result(run_id: UUID, request_id: UUID, db: AsyncSession = Depends(get_session)):
    return await call(service.get_edit(db, str(run_id), str(request_id)))


@router.post("/runs/{run_id}/export")
async def export(
    run_id: UUID, payload: ExportInput, request: Request, db: AsyncSession = Depends(get_session)
):
    return await call(service.export(db, str(run_id), payload, actor=actor(request)))


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
