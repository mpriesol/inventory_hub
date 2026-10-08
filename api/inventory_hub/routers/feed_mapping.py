"""Authorized feed inspection/configuration. Inspection and preview never publish products."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.access import operator_access
from inventory_hub.database import get_session
from inventory_hub.feed_mapping_types import MappingDefinition, MappingPreview, MappingRemap, MappingSave
from inventory_hub.routers.catalog import CatalogRoute
from inventory_hub.services import catalog, feed_mapping


def no_store(response: Response):
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/suppliers/{supplier}/feed-mapping", tags=["Product feed mapping"],
                   route_class=CatalogRoute, dependencies=[Depends(operator_access), Depends(no_store)])
DB = Annotated[AsyncSession, Depends(get_session)]
FeedKey = Annotated[str, Query(pattern=r"^[a-zA-Z0-9_-]{1,50}$")]
ShopKey = Annotated[str, Query(pattern=r"^[a-zA-Z0-9_-]{0,50}$")]


@router.get("")
async def get_mapping(supplier: str, db: DB, feed_key: FeedKey = "products", shop: ShopKey = ""):
    return await feed_mapping.get_mapping(db, supplier, feed_key, shop)


@router.put("")
async def save_mapping(supplier: str, body: MappingSave, db: DB):
    return await feed_mapping.save_mapping(db, supplier, body)


@router.get("/inspection")
async def inspect(supplier: str, db: DB, feed_key: FeedKey = "products",
                  sample_id: str | None = Query(None, pattern=r"^[0-9a-f]{32}$"),
                  format: Literal["auto", "xml", "csv", "json"] = "auto", record_path: str = Query("", max_length=500),
                  csv_delimiter: str = Query("", max_length=1),
                  csv_encoding: Literal["utf-8-sig", "cp1250", "iso-8859-2"] = "utf-8-sig"):
    definition = MappingDefinition(format=format, record_path=record_path, csv_delimiter=csv_delimiter, csv_encoding=csv_encoding)
    return await feed_mapping.inspect_source(db, supplier, feed_key, definition, sample_id)


@router.post("/upload")
async def upload(supplier: str, db: DB, file: UploadFile = File(...),
                 feed_key: str = Form("products", pattern=r"^[a-zA-Z0-9_-]{1,50}$"),
                 format: Literal["auto", "xml", "csv", "json"] = Form("auto"),
                 record_path: str = Form("", max_length=500), csv_delimiter: str = Form("", max_length=1),
                 csv_encoding: Literal["utf-8-sig", "cp1250", "iso-8859-2"] = Form("utf-8-sig")):
    definition = MappingDefinition(format=format, record_path=record_path, csv_delimiter=csv_delimiter, csv_encoding=csv_encoding)
    return await feed_mapping.upload_sample(db, supplier, feed_key, file, definition)


@router.post("/preview")
async def preview(supplier: str, body: MappingPreview, db: DB):
    return await feed_mapping.preview_mapping(db, supplier, body)


@router.post("/remap")
async def remap(supplier: str, body: MappingRemap, db: DB):
    path = await feed_mapping.source_path(db, supplier, body.feed_key, body.sample_id)
    return await catalog.refresh_catalog(db, supplier, body.feed_key, source_path=path,
                                         expected_mapping_revision=body.expected_revision)
