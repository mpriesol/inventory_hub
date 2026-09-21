"""Supplier feed reads and explicit Hub → Upgates imports (separate from stock sync)."""
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.catalog_types import CatalogPage, CatalogRefreshRequest, CatalogSelection, ShopImportPreview, ShopImportPreviewRequest, ShopImportRequest, ShopImportResult
from inventory_hub.database import get_session
from inventory_hub.services import catalog, catalog_import
from inventory_hub.services.catalog_html import clean_description
from inventory_hub.services.catalog_images import paul_lange_image


class CatalogRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request):
            try:
                return await original(request)
            except catalog.CatalogError as error:
                raise HTTPException(error.status, detail={"code": error.code, "message": str(error)}) from None
        return handler


router = APIRouter(tags=["Supplier catalog"], route_class=CatalogRoute)
DB = Annotated[AsyncSession, Depends(get_session)]
FeedKey = Annotated[str, Query(pattern=r"^[a-zA-Z0-9_-]{1,50}$")]


def filters(q: str = Query("", max_length=200), code: str = Query("", max_length=100),
            ean: str = Query("", max_length=100), manufacturer: str = Query("", max_length=100),
            sort: Literal["name", "code", "manufacturer"] = "name",
            shop: str | None = Query(None, pattern=r"^[a-zA-Z0-9_-]{1,50}$"),
            listing: Literal["all", "listed", "unlisted", "warnings"] = "all") -> dict:
    return dict(q=q, code=code, ean=ean, manufacturer=manufacturer, sort=sort, shop=shop, listing=listing)


@router.get("/suppliers/{supplier}/catalog", summary="Feed status, supported sources and target shops")
async def status(supplier: str, db: DB, feed_key: FeedKey = "products"):
    return await catalog.catalog_status(db, supplier, feed_key)


@router.post("/suppliers/{supplier}/catalog/refresh", summary="Download and atomically index a listing feed")
async def refresh(supplier: str, request: CatalogRefreshRequest, db: DB):
    return await catalog.refresh_catalog(db, supplier, request.feed_key)


@router.post("/suppliers/{supplier}/catalog/download", summary="Save the configured listing feed without parsing or importing products")
def download(supplier: str, request: CatalogRefreshRequest):
    return catalog.download_catalog_source(supplier, request.feed_key)


@router.get("/suppliers/{supplier}/catalog/products", response_model=CatalogPage,
            summary="Search normalized products; empty q lists all. Names ignore case and accents.")
async def products(supplier: str, db: DB, feed_key: FeedKey = "products",
                   page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                   grouped: bool = True, criteria: dict = Depends(filters)):
    return await catalog.catalog_page(db, supplier, feed_key, page=page, page_size=page_size, grouped=grouped, **criteria)


@router.get("/suppliers/{supplier}/catalog/selection", response_model=CatalogSelection,
            summary="IDs of all matching items for explicit selection across pages")
async def selection(supplier: str, db: DB, feed_key: FeedKey = "products", criteria: dict = Depends(filters)):
    return await catalog.catalog_selection(db, supplier, feed_key, **criteria)


@router.get("/suppliers/{supplier}/catalog/products/{product_id}", summary="Full normalized detail, explicit variants and original source data")
async def detail(supplier: str, product_id: int, db: DB, include_variants: bool = True, shop: str | None = None):
    result = await catalog.catalog_detail(db, supplier, product_id, include_variants, shop)
    p = result["product"]
    result["description_html"] = clean_description("\n".join(filter(None, [p.description, p.manufacturer_description, p.safety_information])))
    return result


@router.get("/suppliers/{supplier}/catalog/products/{product_id}/source", summary="Download the original product XML")
async def source(supplier: str, product_id: int, db: DB):
    data = await catalog.catalog_detail(db, supplier, product_id, include_variants=False)
    return Response(data["source_xml"], media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="supplier-product-{product_id}.xml"'})


@router.get("/shops/{shop}/import/options", summary="Read verified target languages, currencies, categories and price VAT mode")
def import_options(shop: str, refresh: bool = False):
    return catalog_import.cached_import_options(shop, refresh=refresh)


@router.get("/suppliers/paul-lange/catalog/images/{filename}", response_class=Response,
            summary="Display an HTTP-only Paul Lange JPEG through the Hub HTTPS origin")
async def catalog_image(filename: str):
    return Response(await paul_lange_image(filename), media_type="image/jpeg", headers={
        "Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    })


@router.post("/shops/{shop}/import/preview", response_model=ShopImportPreview,
             summary="Freeze selected feed IDs into a one-hour, create-only import preview")
async def preview(shop: str, request: ShopImportPreviewRequest, db: DB):
    return await catalog_import.create_preview(db, shop, request)


@router.post("/shops/{shop}/import", response_model=ShopImportResult, status_code=202,
             summary="Confirm a preview; hidden products with validation_required, no stock writes")
def start_import(shop: str, request: ShopImportRequest, background: BackgroundTasks):
    result, queued = catalog_import.queue_import(shop, request.preview_id, request.retry_failed)
    if queued:
        background.add_task(catalog_import.execute_import, shop, request.preview_id)
    return result


@router.get("/shops/{shop}/import/{preview_id}", response_model=ShopImportResult, summary="Per-product import outcome and recovery status")
def import_result(shop: str, preview_id: str):
    return catalog_import.import_result(shop, preview_id)
