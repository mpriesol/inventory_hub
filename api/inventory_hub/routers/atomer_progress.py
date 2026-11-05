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
from inventory_hub.services import atomer_variant_repair as repairs, atomer_recode, atomer_family_batch, atomer_generations, atomer_generation_cards, atomer_generation_recovery, atomer_generation_availability
from inventory_hub.services.catalog import CatalogError
from inventory_hub.services.stock_publication_source import SourceError
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


@router.post("/variant-parameters/{mode}")
async def repair_parameters(mode: Literal["preview","apply"], selection: repairs.Family, request: Request,
                            db: AsyncSession = Depends(get_session)):
    try:
        return await repairs.process(db,selection,mode,_actor(request))
    except (ReviewError, CatalogError, SourceError) as error:
        raise HTTPException(error.status,detail={"code":error.code, "retry_after":getattr(error,"retry_after",None)}) from None


@router.post("/review-blocks")
async def block_review(selection: repairs.Blocks, request: Request, db: AsyncSession = Depends(get_session)):
    try:
        return await repairs.record_blocks(db,selection,_actor(request))
    except ReviewError as error:
        raise HTTPException(error.status,detail={"code":error.code}) from None


@router.post("/verified-barcodes/{mode}")
async def repair_barcodes(mode: Literal["preview","apply"], selection: atomer_recode.BarcodeFamily,
                          request: Request, db: AsyncSession = Depends(get_session)):
    try:
        return await repairs.process(db,selection,mode,_actor(request))
    except (ReviewError, CatalogError, SourceError) as error:
        raise HTTPException(error.status,detail={"code":error.code, "retry_after":getattr(error,"retry_after",None)}) from None

@router.post("/review-batch/{mode}")
async def repair_batch(mode: Literal["preview","apply"], selection: atomer_family_batch.Pack,
                       request: Request, db: AsyncSession = Depends(get_session)):
    try:
        return await atomer_family_batch.process(db,selection,mode,_actor(request))
    except (ReviewError, CatalogError, SourceError) as error:
        raise HTTPException(error.status,detail={"code":error.code,"retry_after":getattr(error,"retry_after",None)}) from None


@router.post("/variant-generations/{mode}")
async def variant_generations(mode: Literal["preview","apply"], selection: atomer_generations.Selection,
                              request: Request, db: AsyncSession = Depends(get_session)):
    try:
        return await atomer_generations.process(db,selection,mode,_actor(request))
    except (ReviewError, CatalogError, SourceError, AtomerSourceError) as error:
        raise HTTPException(getattr(error,"status",422),detail={"code":error.code,
            "retry_after":getattr(error,"retry_after",None)}) from None

@router.post("/generation-cards/{mode}")
async def finish_generation_cards(mode: Literal["preview","apply"], selection: atomer_generations.Selection,
                                  request: Request, db: AsyncSession = Depends(get_session)):
    try:
        return await atomer_generation_cards.finish_cards(db,selection,mode,_actor(request))
    except (ReviewError, CatalogError, SourceError) as error:
        raise HTTPException(error.status,detail={"code":error.code,"retry_after":getattr(error,"retry_after",None)}) from None

@router.post("/generation-recovery/{action}/{mode}")
async def recover_generation_fields(action: Literal["fields","adopt"],mode: Literal["preview","apply"],
                                    selection: atomer_generations.Selection,request: Request,
                                    db: AsyncSession = Depends(get_session)):
    try:
        return await atomer_generation_recovery.process(db,selection,mode,_actor(request),action=="adopt")
    except (ReviewError, CatalogError, SourceError) as error:
        raise HTTPException(error.status,detail={"code":error.code,"retry_after":getattr(error,"retry_after",None)}) from None


@router.get("/availability-options")
async def availability_options(kind: Literal["default","OnRequest","NotAvailable","InStock","Custom"]="default",
                               native_id:int|None=Query(None,gt=0)):
    return await asyncio.to_thread(atomer_generation_availability.registry,kind,native_id)


class AvailabilityBinding(atomer_generations.Strict):
    selection: atomer_generations.Selection
    options: dict[repairs.Token,Annotated[int,Query(gt=0)]]


@router.post("/generation-availability/{mode}")
async def generation_availability(mode:Literal["preview","apply"],binding:AvailabilityBinding,request:Request,
                                  db: AsyncSession=Depends(get_session)):
    try:
        return await atomer_generation_availability.bind(db,binding.selection,mode,_actor(request),binding.options)
    except (ReviewError,CatalogError,SourceError) as error:
        raise HTTPException(error.status,detail={"code":error.code,"retry_after":getattr(error,"retry_after",None)}) from None


@router.post("/generation-card-review/{mode}")
async def record_generation_review(mode: Literal["preview","apply"], selection: atomer_generations.Selection,
                                   request: Request, db: AsyncSession = Depends(get_session)):
    try:
        return await atomer_generation_cards.finish_cards(db,selection,mode,_actor(request),record_pending=True)
    except (ReviewError,CatalogError,SourceError) as error:
        raise HTTPException(error.status,detail={"code":error.code,"retry_after":getattr(error,"retry_after",None)}) from None
