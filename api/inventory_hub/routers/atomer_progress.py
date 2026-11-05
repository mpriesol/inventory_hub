import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.access import operator_access
from inventory_hub.adapters.atomer_content import AtomerSourceError
from inventory_hub.database import get_session
from inventory_hub.routers.product_editor import NoStoreRoute
from inventory_hub.services.atomer_progress import parse_source
from inventory_hub.services.atomer_progress_store import page, ProgressError, refresh
from inventory_hub.services.migration_upgates_export import ExportError


router = APIRouter(prefix="/migration/atomer-progress", tags=["migration"], route_class=NoStoreRoute,
                   dependencies=[Depends(operator_access)])


@router.get("")
async def progress_page(kind: Literal["variant","standalone"] | None = None,
                        state: Literal["linked","needs_link","needs_card","needs_target","needs_both","review"] | None = None,
                        bike: Literal["linked","unlinked","no_card","review"] | None = None,
                        card: Literal["exists","missing","review"] | None = None,
                        search: str = Query("", max_length=255), offset: int = Query(0,ge=0),
                        limit: int = Query(50,ge=1,le=200), db: AsyncSession = Depends(get_session)):
    return await page(db,kind,state,bike,card,search.strip(),offset,limit)


@router.post("/refresh")
async def progress_refresh(expected_run: Annotated[int, Form(ge=0)],
                           only_if_stale: Annotated[bool, Form()] = False,
                           source_file: Annotated[UploadFile | None, File()] = None,
                           db: AsyncSession = Depends(get_session)):
    try:
        source = await asyncio.to_thread(parse_source,source_file.file) if source_file else None
        return await refresh(db,source,expected_run,only_if_stale)
    except AtomerSourceError as error:
        raise HTTPException(422, detail={"code":error.code}) from None
    except ProgressError as error:
        raise HTTPException(409, detail={"code":str(error)}) from None
    except ExportError:
        raise HTTPException(502, detail={"code":"remote_read_failed"}) from None
