
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from datetime import datetime

from .models import SupplierIn, SupplierOut, RunPrepareIn, RunPrepareOut, ShopIn, ShopOut, RegisterExportIn
from .settings import settings
from .utils import (
    list_files, area_path, Lock, parse_invoice_id, upgates_output_names,
    resolve_shop_export, load_shops, save_shops, ensure_shop_dirs,
    register_shop_export, prune_shop_exports
)
from .adapters import paul_lange_v1

app = FastAPI(title="Inventory Hub API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/data", StaticFiles(directory=str(settings.INVENTORY_DATA_ROOT)), name="data")

SUPPLIERS = {
    "paul-lange": SupplierOut(
        name="Paul-Lange",
        adapter="paul_lange_v1",
        base_path=str(settings.INVENTORY_DATA_ROOT / "suppliers/paul-lange"),
        supplier_code="paul-lange",
        config_json={
            "product_code_prefix": "PL-",
            "price_coefficients": {"Shimano":0.88,"PRO":0.91,"Lazer":0.90,"Longus":0.95,"Elite":0.92,"Motorex":0.96}
        }
    )
}
SHOPS = { s["shop_code"]: ShopOut(**s) for s in load_shops() }

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/suppliers")
def get_suppliers():
    return list(SUPPLIERS.values())

@app.post("/suppliers")
def create_supplier(s: SupplierIn):
    if s.supplier_code in SUPPLIERS:
        raise HTTPException(400, detail="supplier_code already exists")
    SUPPLIERS[s.supplier_code] = s
    return s

@app.get("/suppliers/{supplier_code}/files")
def supplier_files(supplier_code: str, area: str, months_back: int = 3):
    if supplier_code not in SUPPLIERS:
        raise HTTPException(404, detail="Supplier not found")
    allowed = {"invoices_csv","invoices_pdf","feeds_xml","feeds_converted","shop_exports","imports_upgates","logs","state"}
    if area not in allowed:
        raise HTTPException(400, detail="Invalid area")
    return {"files": list_files(area, supplier_code, months_back)}

# Shops
@app.get("/shops")
def get_shops():
    global SHOPS
    if not SHOPS:
        SHOPS = { s["shop_code"]: ShopOut(**s) for s in load_shops() }
    return list(SHOPS.values())

@app.post("/shops")
def create_shop(s: ShopIn):
    global SHOPS
    if s.shop_code in SHOPS:
        if s.name:
            SHOPS[s.shop_code] = ShopOut(shop_code=s.shop_code, name=s.name)
    else:
        SHOPS[s.shop_code] = ShopOut(**s.model_dump())
    save_shops([v.model_dump() for v in SHOPS.values()])
    ensure_shop_dirs(s.shop_code)
    return SHOPS[s.shop_code]

@app.post("/shops/{shop_code}/export/register")
def register_export(shop_code: str, payload: RegisterExportIn):
    if shop_code not in SHOPS:
        raise HTTPException(404, detail="Shop not found")
    src = Path(payload.source_path)
    out = register_shop_export(shop_code, src, payload.filename)
    deleted = prune_shop_exports(shop_code, keep_last=payload.keep_last, keep_days=payload.keep_days)
    return {"registered": out, "pruned": deleted}

# Runs
@app.post("/runs/prepare", response_model=RunPrepareOut)
def prepare_run(payload: RunPrepareIn):
    if payload.supplier_ref not in SUPPLIERS:
        raise HTTPException(404, detail="Supplier not found")

    sup = SUPPLIERS[payload.supplier_ref]
    supplier_base = Path(sup.base_path)

    lock = Lock(settings.INVENTORY_DATA_ROOT / settings.PIPELINE_LOCK_NAME)
    try:
        lock.acquire()
        now = datetime.now()
        invoice_id = parse_invoice_id(payload.invoice_relpath)

        shop_export = resolve_shop_export(payload.shop_ref, payload.upgates_csv_override)

        invoice_csv = settings.INVENTORY_DATA_ROOT / payload.invoice_relpath
        if not invoice_csv.exists():
            raise HTTPException(404, detail="Invoice CSV not found")

        existing_df, new_df, unmatch_df = paul_lange_v1.process_invoice(
            supplier_base=supplier_base,
            shop_export_csv=shop_export,
            invoice_csv=invoice_csv,
            as_of=now,
        )

        out_dir = area_path("imports_upgates", payload.supplier_ref)
        out_dir.mkdir(parents=True, exist_ok=True)
        fn_existing, fn_new, fn_unmatched = upgates_output_names(invoice_id, now)

        stats = {"existing": len(existing_df), "new": len(new_df), "unmatched": len(unmatch_df)}
        outputs = {"existing": None, "new": None, "unmatched": None}

        def rel_to_data(p: Path) -> str:
            return "data/" + str(p.relative_to(settings.INVENTORY_DATA_ROOT)).replace("\\","/")

        if len(existing_df):
            p = out_dir / fn_existing
            existing_df.to_csv(p, index=False)
            outputs["existing"] = rel_to_data(p)
        if len(new_df):
            p = out_dir / fn_new
            new_df.to_csv(p, index=False)
            outputs["new"] = rel_to_data(p)
        if len(unmatch_df):
            p = out_dir / fn_unmatched
            unmatch_df.to_csv(p, index=False)
            outputs["unmatched"] = rel_to_data(p)

        log_dir = area_path("logs", payload.supplier_ref)
        log_dir.mkdir(parents=True, exist_ok=True)
        run_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{invoice_id}"
        log_file = log_dir / f"{run_id}.log"
        log_file.write_text(
            f"Run {run_id}\nInvoice: {invoice_csv}\nShop export: {shop_export}\nStats: {stats}\n",
            encoding="utf-8",
        )

        return RunPrepareOut(run_id=run_id, outputs=outputs, stats=stats, log=rel_to_data(log_file))
    finally:
        lock.release()

@app.get("/runs/{run_id}/files")
def get_run_file(run_id: str, supplier: str, kind: str):
    if supplier not in SUPPLIERS:
        raise HTTPException(404, detail="Supplier not found")
    out_dir = area_path("imports_upgates", supplier)
    matches = list(out_dir.glob(f"*{run_id}*.csv"))
    if not matches:
        raise HTTPException(404, detail="Run outputs not found")
    select = [m for m in matches if kind in m.name]
    if not select:
        raise HTTPException(404, detail=f"No file of kind={kind}")
    return FileResponse(select[0])
