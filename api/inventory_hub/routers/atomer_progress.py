import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.access import operator_access
from inventory_hub.adapters.atomer_content import AtomerSourceError
from inventory_hub.database import get_session
from inventory_hub.routers.product_editor import NoStoreRoute
from inventory_hub.services.atomer_progress import parse_source
from inventory_hub.services.atomer_progress_store import latest, page, ProgressError, refresh
from inventory_hub.services import atomer_review as review
from inventory_hub.services.atomer_source_state import archive
from inventory_hub.services.atomer_review_targets import parse_targets, ReviewError
from inventory_hub.services.migration_upgates_export import ExportError


router = APIRouter(prefix="/migration/atomer-progress", tags=["migration"], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])


@router.get("")
async def progress_page(kind: Literal["variant","standalone"] | None = None,
                        state: Literal["linked","needs_link","needs_card","needs_target","needs_both","review"] | None = None,
                        bike: Literal["linked","unlinked","no_card","review"] | None = None,
                        card: Literal["exists","missing","review"] | None = None,
                        work: Literal["open","done","all"] = "all",
                        search: str = Query("", max_length=255), offset: int = Query(0,ge=0),
                        limit: int = Query(50,ge=1,le=200), db: AsyncSession = Depends(get_session)):
    return await page(db,kind,state,bike,card,search.strip(),offset,limit,work)


@router.post("/refresh")
async def progress_refresh(expected_run: Annotated[int, Form(ge=0)],
                           only_if_stale: Annotated[bool, Form()] = False,
                           source_file: Annotated[UploadFile | None, File()] = None,
                           admin_file: Annotated[UploadFile | None, File()] = None,
                           admin_complete: Annotated[bool, Form()] = False,
                           review_file: Annotated[UploadFile | None, File()] = None,
                           db: AsyncSession = Depends(get_session)):
    try:
        source = await asyncio.to_thread(parse_source,source_file.file) if source_file else None
        pins = await asyncio.to_thread(parse_targets, review_file.file) if review_file else None
        buyers = await asyncio.to_thread(archive,source_file.file,"buyers") if source_file else None
        admin = await asyncio.to_thread(archive,admin_file.file,"admin_csv") if admin_file else None
        return await refresh(db,source,expected_run,only_if_stale,pins,buyers,admin,admin_complete)
    except ReviewError as error:
        raise HTTPException(error.status,detail={"code":error.code}) from None
    except AtomerSourceError as error:
        raise HTTPException(422, detail={"code":error.code}) from None
    except ProgressError as error:
        raise HTTPException(409, detail={"code":str(error)}) from None
    except ExportError:
        raise HTTPException(502, detail={"code":"remote_read_failed"}) from None


@router.get("/targets")
async def targets(search: str = Query("",max_length=255),
                  decision: Literal["","undecided","yes","no"] = "", work: Literal["open","done","all"] = "all",
                  offset: int = Query(0,ge=0),limit: int = Query(25,ge=1,le=100),
                  db: AsyncSession = Depends(get_session)):
    head = await latest(db)
    return await review.extras(db,head["id"] if head else 0,search.strip(),decision,work,offset,limit)


@router.get("/work")
async def work(status: Literal["queued","working","blocked","done"] = "queued",
               offset: int = Query(0,ge=0),limit: int = Query(25,ge=1,le=100),
               db: AsyncSession = Depends(get_session)):
    head = await latest(db)
    return await review.queue(db,head["id"] if head else 0,status,offset,limit)


@router.post("/work")
async def save_work(change: review.Edit, request: Request, db: AsyncSession = Depends(get_session)):
    try:
        return await review.edit(db,change,_actor(request))
    except ReviewError as error:
        raise HTTPException(error.status,detail={"code":error.code}) from None


def _actor(request):
    user = getattr(request.state, "hub_user", None) or {}
    return f"user:{user['id']}" if user.get("id") is not None else "operator-token"


@router.post("/targets/keep")
async def keep_targets(selection: review.KeepSelection, request: Request, db: AsyncSession = Depends(get_session)):
    try:
        return await review.keep_selection(db, selection, _actor(request))
    except ReviewError as error:
        raise HTTPException(error.status, detail={"code":error.code}) from None
