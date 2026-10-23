# C:\!kafe\BikeTrek\web\api\inventory_hub\main.py
# Inventory Hub v12 FINAL - PostgreSQL + Multi-EAN Support
from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict
import os, shutil
import asyncio
from contextlib import suppress
from contextlib import asynccontextmanager

from fastapi import Query
from fastapi import UploadFile, File, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from inventory_hub.file_access import PrivateDataFiles, document_path, document_response, stored_document_path

from .settings import settings
from .models import (
    SupplierIn, SupplierOut,
    RunPrepareIn, RunPrepareOut,
    RefreshFeedIn, SupplierConfig,
)
from .utils import (
    _rel, list_files, area_path,
    load_supplier_config, save_supplier_config,
    upgates_output_names,
)
from .adapters import paul_lange_v1

from .utils import (
    _rel, list_files, area_path,
    load_supplier_config, save_supplier_config, load_invoices_state, save_invoices_state,
    upgates_output_names,
    read_csv_smart, clean_upgates_headers_inplace,
)


from .configs import router as configs_router
from inventory_hub.routers.invoices import router as invoices_router
from inventory_hub.routers.shops import router as shops_router
from inventory_hub.routers.suppliers import router as suppliers_router
from inventory_hub.routers.logs import router as logs_router
from inventory_hub.routers.logs_global import router as logs_global_router
from inventory_hub.routers.imports import router as imports_router
from inventory_hub.routers.receiving import router as receiving_router_legacy

# v12 FINAL: PostgreSQL-backed receiving
from inventory_hub.routers.receiving_db import router as receiving_router_db
from inventory_hub.database import init_db, close_db, check_db_health
from inventory_hub.routers.invoices_unified import router as invoices_unified_router
from inventory_hub.routers.invoice_archive import router as invoice_archive_router
from inventory_hub.routers.invoice_export import router as invoice_export_router
from inventory_hub.routers.invoice_calendar import router as invoice_calendar_router
from inventory_hub.routers.product_trace import router as product_trace_router
from inventory_hub.routers.shop_invoice_sync import router as shop_invoice_sync_router



# ---------------------------------------------------------
# Logging setup
# ---------------------------------------------------------
from inventory_hub.logging_setup import setup_logging
setup_logging(settings)

# ---------------------------------------------------------
# Lifespan: Database init/cleanup
# ---------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    # Startup
    if settings.USE_POSTGRES:
        try:
            await init_db()
            print("✓ PostgreSQL connected")
        except Exception as e:
            print(f"⚠ PostgreSQL connection failed: {e}")
            print("  Falling back to JSON-based storage")
    worker = None
    collection_worker = None
    processing_worker = None
    publication_worker = None
    supplier_availability_worker = None
    stock_sync_worker = None
    fifo_cost_worker = None
    if settings.USE_POSTGRES:
        from inventory_hub.services.ai_content_worker import run
        worker = asyncio.create_task(run(), name="ai-content-queue")
        from inventory_hub.services.order_collection_worker import run as collect_orders
        collection_worker = asyncio.create_task(collect_orders(), name="order-collection")
        from inventory_hub.services.order_processing_worker import run as process_orders
        processing_worker = asyncio.create_task(process_orders(), name="order-processing")
        from inventory_hub.services.stock_publication_worker import run as publish_stock
        publication_worker = asyncio.create_task(publish_stock(), name="stock-publication")
        from inventory_hub.services.supplier_availability_worker import run as refresh_supplier_availability
        supplier_availability_worker = asyncio.create_task(refresh_supplier_availability(), name="supplier-availability")
        from inventory_hub.services.stock_sync_worker import run as sync_stock
        stock_sync_worker = asyncio.create_task(sync_stock(), name="stock-sync")
        from inventory_hub.services.fifo_cost_worker import run as sync_fifo_costs
        fifo_cost_worker = asyncio.create_task(sync_fifo_costs(), name="fifo-cost-sync")
    yield
    if worker:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
    if collection_worker:
        collection_worker.cancel()
        with suppress(asyncio.CancelledError):
            await collection_worker
    if processing_worker:
        processing_worker.cancel()
        with suppress(asyncio.CancelledError):
            await processing_worker
    if publication_worker:
        publication_worker.cancel()
        with suppress(asyncio.CancelledError):
            await publication_worker
    for background_worker in (supplier_availability_worker, stock_sync_worker, fifo_cost_worker):
        if background_worker:
            background_worker.cancel()
            with suppress(asyncio.CancelledError):
                await background_worker
    # Shutdown
    if settings.USE_POSTGRES:
        await close_db()
        print("✓ PostgreSQL disconnected")

# ---------------------------------------------------------
# FastAPI app + CORS + /data static
# ---------------------------------------------------------
app = FastAPI(
    title="Inventory Hub API",
    version="12.0.0",  # v12 FINAL
    description="BikeTrek/xTrek Inventory Management - Multi-EAN Support",
    lifespan=lifespan,
    root_path=os.environ.get("ROOT_PATH", ""),
)
from inventory_hub.routers.auth import install as install_auth
install_auth(app)
# CORS handles preflight before the private API boundary; preflight never
# executes a business endpoint or carries an authentication exemption.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Last-Modified"],
)
app.mount("/data", PrivateDataFiles(directory=str(settings.INVENTORY_DATA_ROOT)), name="data")

app.include_router(configs_router)
# ---------------------------------------------------------
# In-memory registry (stačí na náš účel)
# ---------------------------------------------------------
SUPPLIERS: Dict[str, SupplierOut] = {
    "paul-lange": SupplierOut(
        name="Paul-Lange",
        adapter="paul_lange_v1",
        base_path=str(settings.INVENTORY_DATA_ROOT / "suppliers" / "paul-lange"),
        supplier_code="paul-lange",
        config_json={
            "product_code_prefix": "PL-",
            "price_coefficients": {"Shimano": 0.88, "PRO": 0.91, "Lazer": 0.90, "Longus": 0.95, "Elite": 0.92, "Motorex": 0.96},
        },
    ),
}

app.include_router(invoices_router)
app.include_router(shops_router)
app.include_router(suppliers_router)
app.include_router(logs_router)
app.include_router(logs_global_router)
app.include_router(imports_router)
app.include_router(invoice_calendar_router)
app.include_router(invoices_unified_router)
app.include_router(invoice_archive_router)
app.include_router(invoice_export_router)
app.include_router(product_trace_router)
app.include_router(shop_invoice_sync_router)

# Receiving: Use PostgreSQL-backed router if enabled, otherwise legacy JSON
if settings.USE_POSTGRES:
    app.include_router(receiving_router_db)
    from inventory_hub.routers.stock import router as stock_router
    app.include_router(stock_router)
    from inventory_hub.routers.stock_history import router as stock_history_router
    app.include_router(stock_history_router)
    from inventory_hub.routers.fifo import router as fifo_router
    from inventory_hub.routers.fifo_returns import router as fifo_returns_router
    from inventory_hub.routers.product_editor import router as product_editor_router
    from inventory_hub.routers.product_links import router as product_links_router
    from inventory_hub.routers.supplier_card_links import router as supplier_card_links_router
    app.include_router(fifo_router)
    app.include_router(fifo_returns_router)
    app.include_router(product_editor_router)
    app.include_router(product_links_router)
    app.include_router(supplier_card_links_router)
    from inventory_hub.routers.sku_policy import router as sku_policy_router
    app.include_router(sku_policy_router)
    from inventory_hub.routers.opening_stock import router as opening_stock_router
    app.include_router(opening_stock_router)
    from inventory_hub.routers.upgates_sync import router as upgates_sync_router
    app.include_router(upgates_sync_router)
    from inventory_hub.routers.order_audit import router as order_audit_router
    app.include_router(order_audit_router)
    from inventory_hub.routers.order_stock import router as order_stock_router
    app.include_router(order_stock_router)
    from inventory_hub.routers.order_collection import router as order_collection_router
    app.include_router(order_collection_router)
    from inventory_hub.routers.stock_settings import router as stock_settings_router
    app.include_router(stock_settings_router)
    from inventory_hub.routers.stock_publication import router as stock_publication_router
    app.include_router(stock_publication_router)
    from inventory_hub.routers.supplier_availability import router as supplier_availability_router
    from inventory_hub.routers.stock_sync import router as stock_sync_router
    from inventory_hub.routers.stock_adjustments import router as stock_adjustments_router
    app.include_router(supplier_availability_router)
    app.include_router(stock_sync_router)
    app.include_router(stock_adjustments_router)
    from inventory_hub.routers.fifo_cost_sync import router as fifo_cost_sync_router
    app.include_router(fifo_cost_sync_router)
    from inventory_hub.routers.order_processing import router as order_processing_router
    app.include_router(order_processing_router)
    from inventory_hub.routers.catalog import router as catalog_router
    app.include_router(catalog_router)
    from inventory_hub.routers.feed_mapping import router as feed_mapping_router
    from inventory_hub.routers.product_import import router as product_import_router
    app.include_router(feed_mapping_router)
    app.include_router(product_import_router)
    from inventory_hub.routers.ai_content import router as ai_content_router
    app.include_router(ai_content_router)
    from inventory_hub.routers.sloger_probe import router as sloger_probe_router
    app.include_router(sloger_probe_router)
    from inventory_hub.routers.supplier_prefixes import router as supplier_prefixes_router
    app.include_router(supplier_prefixes_router)
else:
    app.include_router(receiving_router_legacy)

# ---------------------------------------------------------
# Helpers (lokálne pre main)
# ---------------------------------------------------------
def _supplier_base(supplier_code: str) -> Path:
    sup = SUPPLIERS[supplier_code]
    return Path(sup.base_path)

def _shop_latest_export(shop_code: str) -> Path:
    """
    Štandardizované miesto pre shop export:
    inventory-data/shop-exports/{shop_code}/latest.csv
    """
    return settings.INVENTORY_DATA_ROOT / "shops" / shop_code / "latest.csv"

def _ensure_parent(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------
# Endpoints
# ---------------------------------------------------------
@app.get("/health")
async def health():
    """Health check endpoint with database status."""
    result = {
        "status": "ok",
        "version": "12.0.0",
        "use_postgres": settings.USE_POSTGRES,
    }
    if settings.USE_POSTGRES:
        db_health = await check_db_health()
        # Public liveness must not expose database exception/connection details.
        result["database"] = {"status": db_health.get("status", "unhealthy")}
        if db_health.get("status") != "healthy":
            result["status"] = "degraded"
    from fastapi.responses import JSONResponse
    return JSONResponse(result, status_code=200 if result["status"] == "ok" else 503)


@app.get("/suppliers/{supplier_code}/files")
def supplier_files(supplier_code: str, area: str, months_back: int = 3):
    if supplier_code not in SUPPLIERS:
        raise HTTPException(404, detail="Supplier not found")

    raw = (area or "").strip()
    ALIASES = {
        "feeds": "feeds_xml",
        "feeds_raw": "feeds_xml",
        "feeds/xml": "feeds_xml",
        "feeds_converted": "feeds_converted",
        "feeds/converted": "feeds_converted",
        "invoices": "invoices_csv",
        "invoices_raw": "invoices_csv",
        "invoices/csv": "invoices_csv",
    }
    area_norm = ALIASES.get(raw, raw).replace("/", "_")

    allowed = {"invoices_csv","invoices_pdf","feeds_xml","feeds_converted","imports_upgates","logs","state"}
    if area_norm not in allowed:
        raise HTTPException(400, detail=f"Invalid area '{area}'")

    return {"files": list_files(area_norm, supplier_code, months_back)}

# -------- Runs/prepare (invoice + shop export -> Upgates CSVs)
@app.post("/runs/prepare_legacy", response_model=RunPrepareOut)
def prepare_run(payload: RunPrepareIn):
    """
    Očakáva:
      - supplier_ref: "paul-lange"
      - shop_ref: napr. "biketrek"
      - invoice_relpath: napr. "suppliers/paul-lange/invoices/csv/2025/10/F2025060682.csv"
      - upgates_csv_override: voliteľná absolútna PATH na shop export CSV (inak použije shops/{shop}/latest.csv)
    """
    from inventory_hub.services.legacy_import_guard import require_legacy_import_mode
    require_legacy_import_mode()
    if payload.supplier_ref not in SUPPLIERS:
        raise HTTPException(404, detail=f"Unknown supplier '{payload.supplier_ref}'")

    supplier_code = payload.supplier_ref
    supplier_base = _supplier_base(supplier_code)

    # invoice CSV (relatívne voči INVENTORY_DATA_ROOT)
    invoice_csv = document_path(settings.INVENTORY_DATA_ROOT, payload.invoice_relpath)

    # shop export CSV
    if payload.upgates_csv_override:
        shop_export_csv = stored_document_path(settings.INVENTORY_DATA_ROOT, payload.upgates_csv_override)
    else:
        shop_export_csv = document_path(settings.INVENTORY_DATA_ROOT, f"shops/{payload.shop_ref}/latest.csv")

    now = datetime.now()
    try:
        existing_df, new_df, unmatch_df = paul_lange_v1.process_invoice(
            supplier_base=supplier_base,
            shop_export_csv=shop_export_csv,
            invoice_csv=invoice_csv,
            as_of=now,
        )
    except Exception as e:
        raise HTTPException(500, detail=f"Adapter error: {e}")

    # výstupy -> imports/upgates
    out_dir = area_path("imports_upgates", supplier_code)
    _ensure_parent(out_dir / "dummy")  # len na vytvorenie priečinka

    invoice_id = invoice_csv.stem
    fname_existing, fname_new, fname_unmatched = upgates_output_names(invoice_id, now)

    out_existing = out_dir / fname_existing
    out_new      = out_dir / fname_new
    out_unmatch  = out_dir / fname_unmatched

    # zapisuj len ne-prázdne
    outputs: Dict[str, Optional[str]] = {"existing": None, "new": None, "unmatched": None}
    stats = {"existing_rows": 0, "new_rows": 0, "unmatched_rows": 0}

    if existing_df is not None and not existing_df.empty:
        existing_df.to_csv(out_existing, index=False, encoding="utf-8-sig")
        outputs["existing"] = "data/" + _rel(out_existing)
        stats["existing_rows"] = int(existing_df.shape[0])

    if new_df is not None and not new_df.empty:
        new_df.to_csv(out_new, index=False, encoding="utf-8-sig")
        outputs["new"] = "data/" + _rel(out_new)
        stats["new_rows"] = int(new_df.shape[0])

    if unmatch_df is not None and not unmatch_df.empty:
        unmatch_df.to_csv(out_unmatch, index=False, encoding="utf-8-sig")
        outputs["unmatched"] = "data/" + _rel(out_unmatch)
        stats["unmatched_rows"] = int(unmatch_df.shape[0])

    run_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{invoice_id}"


    # ...
    inv_name = Path(invoice_csv).name  # napr. F2025060682.csv
    try:
        state = load_invoices_state(supplier_code)
        entry = state.setdefault("invoices", {}).setdefault(inv_name, {})
        entry["processed_at"] = datetime.now().isoformat(timespec="seconds")
        entry["stats"] = {
            "existing": stats.get("existing_rows", 0),
            "new": stats.get("new_rows", 0),
            "unmatched": stats.get("unmatched_rows", 0),
        }
        save_invoices_state(supplier_code, state)
    except Exception as e:
        print("WARN: cannot update invoices.json:", e)

    return RunPrepareOut(run_id=run_id, outputs=outputs, stats=stats, log="ok")

@app.get("/files/preview")
def preview_file(relpath: str, max_rows: int = 50, strip_upgates_brackets: bool = True):
    """
    Náhľad CSV v rámci INVENTORY_DATA_ROOT.
    Query:
      - relpath: relatívna cesta od INVENTORY_DATA_ROOT (napr. 'suppliers/paul-lange/feeds/converted/export_v2_20251013.csv'
                 alebo 'shops/biketrek/latest.csv' či 'suppliers/.../imports/upgates/xxx.csv')
      - max_rows: koľko riadkov načítať (default 50)
      - strip_upgates_brackets: odstrániť [] z hlavičiek pre čitateľnejší náhľad (default True)

    Vracia:
      { columns: [...], rows: [[...],...], total_columns: int, preview_rows: int }
    """
    p = document_path(settings.INVENTORY_DATA_ROOT, relpath)
    if p.suffix.lower() != ".csv":
        raise HTTPException(400, detail="Only CSV preview is supported")

    try:
        df = read_csv_smart(p, max_rows=max_rows)
        if strip_upgates_brackets:
            clean_upgates_headers_inplace(df)
        df = df.fillna("")
        return {
            "columns": list(df.columns),
            "rows": df.values.tolist(),
            "total_columns": int(df.shape[1]),
            "preview_rows": int(df.shape[0]),
        }
    except Exception as e:
        raise HTTPException(500, detail=f"Preview failed: {e}")

@app.api_route("/files/download", methods=["GET", "HEAD"])
def files_download(
    relpath: str = Query(..., description="Relatívna cesta v rámci INVENTORY_DATA_ROOT"),
    disposition: str = Query("attachment", regex="^(attachment|inline)$"),
    filename: str | None = Query(None, description="Prepísanie názvu súboru v hlavičke"),
    stable: bool = Query(False, description="Ak True, pošle snapshot (bez Content-Length konfliktov)"),
):
    """
    Bezpečné stiahnutie súboru z INVENTORY_DATA_ROOT podľa relpath.
    - chráni pred path traversal
    - správny MIME podľa prípony
    - podpora Content-Disposition: attachment/inline
    - pre .log / stable=1 pošle SNAPSHOT (fixuje Content-Length)
    """
    target = document_path(settings.INVENTORY_DATA_ROOT, relpath)
    return document_response(target, disposition=disposition, filename=filename, stable=stable)

@app.post("/suppliers/{supplier}/feeds/upload")
async def upload_feed_xml(supplier: str, file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".xml"):
        raise HTTPException(status_code=400, detail="Očakávam XML súbor")

    target_dir = settings.INVENTORY_DATA_ROOT / "suppliers" / supplier / "feeds" / "xml"
    target_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    target_path = target_dir / f"uploaded_{ts}.xml"

    with target_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    return {"message": "Feed uploaded", "saved_path": str(target_path)}

@app.get("/suppliers/{supplier}/invoices/state")
def get_invoices_state(supplier: str):
    if supplier not in SUPPLIERS:
        raise HTTPException(404, detail="Supplier not found")
    return load_invoices_state(supplier),

@app.get("/files/stat")
def files_stat(relpath: str = Query(..., description="Relatívna cesta v rámci INVENTORY_DATA_ROOT")):
    target = document_path(settings.INVENTORY_DATA_ROOT, relpath)
    relpath_decoded = target.relative_to(Path(settings.INVENTORY_DATA_ROOT).resolve()).as_posix()
    st = target.stat()
    return {
        "relpath": relpath_decoded,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "mtime_iso": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
    }
