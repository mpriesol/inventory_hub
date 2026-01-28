# C:\!kafe\BikeTrek\web\api\inventory_hub\main.py
# Inventory Hub v12 FINAL - PostgreSQL + Multi-EAN Support
from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict
import mimetypes
from urllib.parse import unquote
import os, shutil
from contextlib import asynccontextmanager

from fastapi import Query
from fastapi.responses import FileResponse, Response
from fastapi import UploadFile, File, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

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
    yield
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
)
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
app.mount("/data", StaticFiles(directory=str(settings.INVENTORY_DATA_ROOT)), name="data")

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
app.include_router(invoices_unified_router)

# Receiving: Use PostgreSQL-backed router if enabled, otherwise legacy JSON
if settings.USE_POSTGRES:
    app.include_router(receiving_router_db)
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
    return settings.INVENTORY_DATA_ROOT / "shop-exports" / shop_code / "latest.csv"

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
        result["database"] = db_health
        if db_health.get("status") != "healthy":
            result["status"] = "degraded"
    return result

@app.get("/suppliers")
def get_suppliers():
    return list(SUPPLIERS.values())

@app.post("/suppliers")
def create_supplier(s: SupplierIn):
    if s.supplier_code in SUPPLIERS:
        raise HTTPException(400, detail="supplier_code already exists")
    SUPPLIERS[s.supplier_code] = SupplierOut(**s.model_dump())
    return SUPPLIERS[s.supplier_code]

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

# -------- Supplier CONFIG (persist do .../suppliers/<s>/state/config.json)
@app.get("/suppliers/{supplier_code}/config")
def get_supplier_config(supplier_code: str):
    if supplier_code not in SUPPLIERS:
        raise HTTPException(404, detail="Supplier not found")
    return load_supplier_config(supplier_code).model_dump()

@app.put("/suppliers/{supplier_code}/config")
def put_supplier_config(supplier_code: str, cfg: SupplierConfig):
    if supplier_code not in SUPPLIERS:
        raise HTTPException(404, detail="Supplier not found")
    save_supplier_config(supplier_code, cfg)
    return cfg

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
    if payload.supplier_ref not in SUPPLIERS:
        raise HTTPException(404, detail=f"Unknown supplier '{payload.supplier_ref}'")

    supplier_code = payload.supplier_ref
    supplier_base = _supplier_base(supplier_code)

    # invoice CSV (relatívne voči INVENTORY_DATA_ROOT)
    invoice_csv = settings.INVENTORY_DATA_ROOT / payload.invoice_relpath
    if not invoice_csv.exists():
        raise HTTPException(404, detail=f"Invoice CSV not found: {invoice_csv}")

    # shop export CSV
    if payload.upgates_csv_override:
        shop_export_csv = Path(payload.upgates_csv_override)
    else:
        shop_export_csv = _shop_latest_export(payload.shop_ref)
    if not shop_export_csv.exists():
        raise HTTPException(404, detail=f"Shop export not found: {shop_export_csv}")

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
    p = settings.INVENTORY_DATA_ROOT / relpath
    if not p.exists() or not p.is_file():
        raise HTTPException(404, detail=f"File not found: {relpath}")
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
    root = Path(settings.INVENTORY_DATA_ROOT).expanduser().resolve()

    relpath_decoded = unquote(relpath).lstrip("/\\")
    target = (root / relpath_decoded).resolve()

    try:
        target.relative_to(root)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid relpath")

    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    media_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    out_name = filename or target.name
    headers = {"Content-Disposition": f'{disposition}; filename="{out_name}"'}

    # LOGY alebo stable režim => vytvoríme snapshot v pamäti
    if stable or target.suffix.lower() in {".log", ".txt"}:
        data = target.read_bytes()
        return Response(content=data, media_type=media_type, headers=headers)

    # Ostatné súbory: klasický FileResponse (rýchlejšie, bez kópie)
    return FileResponse(
        path=str(target),
        media_type=media_type,
        headers=headers,
    )

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

def _safe_path(relpath: str) -> Path:
    p = (settings.INVENTORY_DATA_ROOT / relpath).resolve()
    if not str(p).startswith(str(settings.INVENTORY_DATA_ROOT)):
        raise HTTPException(status_code=400, detail="Invalid path")
    return p

@app.get("/files/stat")
def files_stat(relpath: str = Query(..., description="Relatívna cesta v rámci INVENTORY_DATA_ROOT")):
    root = Path(settings.INVENTORY_DATA_ROOT).expanduser().resolve()
    relpath_decoded = unquote(relpath).lstrip("/\\")
    target = (root / relpath_decoded).resolve()
    try:
        target.relative_to(root)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid relpath")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    st = target.stat()
    return {
        "relpath": relpath_decoded,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "mtime_iso": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
    }