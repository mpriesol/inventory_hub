# app/main.py
from __future__ import annotations

import os
import re
import json
from uuid import uuid4
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Optional, List, Dict

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, String, Integer, DateTime, JSON, select
from sqlalchemy.orm import DeclarativeBase, mapped_column, Mapped, sessionmaker, Session as SASession

# -----------------------------------------------------------------------------
# Config (ENV)
# -----------------------------------------------------------------------------
load_dotenv()
DATA_ROOT = Path(os.getenv("IH_DATA_ROOT", r"C:\!kafe\BikeTrek\web\inventory-data")).resolve()
DATABASE_URL = os.getenv("IH_DATABASE_URL", f"sqlite:///{(Path.cwd() / 'inventory.db').as_posix()}")

# -----------------------------------------------------------------------------
# DB setup (SQLAlchemy 2.0)
# -----------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass

class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    supplier_code: Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    adapter: Mapped[str] = mapped_column(String(60), default="generic")
    base_path: Mapped[str] = mapped_column(String(500))
    config_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Shop(Base):
    __tablename__ = "shops"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_code: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    platform: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)           # napr. "upgates"
    base_url: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)          # napr. https://www.biketrek.sk
    config_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

engine = create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base.metadata.create_all(bind=engine)

# -----------------------------------------------------------------------------
# Schemas (Pydantic)
# -----------------------------------------------------------------------------
class SupplierCreate(BaseModel):
    name: str = Field(..., max_length=120)
    adapter: str = Field(default="generic", max_length=60)
    base_path: str
    supplier_code: Optional[str] = None
    config_json: Optional[dict[str, Any]] = None

class SupplierOut(BaseModel):
    id: int
    supplier_code: Optional[str]
    name: str
    adapter: str
    base_path: str
    config_json: Optional[dict[str, Any]] = None
    class Config:
        from_attributes = True

class ShopCreate(BaseModel):
    name: str = Field(..., max_length=120)
    shop_code: str = Field(..., max_length=120)
    platform: Optional[str] = Field(default=None, max_length=60)
    base_url: Optional[str] = Field(default=None, max_length=300)
    config_json: Optional[dict[str, Any]] = None

class ShopOut(BaseModel):
    id: int
    shop_code: str
    name: str
    platform: Optional[str] = None
    base_url: Optional[str] = None
    config_json: Optional[dict[str, Any]] = None
    class Config:
        from_attributes = True

class PrepareRunRequest(BaseModel):
    supplier_ref: str                     # ID alebo supplier_code
    shop_ref: str                         # ID alebo shop_code
    months_back: int = 3                  # rozsah faktúr (mtime filter)
    upgates_csv_override: Optional[str] = None   # voliteľný lokálny path alebo URL
    notes: Optional[str] = None

class PrepareRunResponse(BaseModel):
    job_id: str
    job_path: str
    supplier_code: str
    shop_code: str
    invoices_count: int
    invoice_paths: List[str]
    latest_xml: Optional[str] = None
    latest_converted: Optional[str] = None
    latest_shop_export: Optional[str] = None

# -----------------------------------------------------------------------------
# Storage helper (semantické cesty)
# -----------------------------------------------------------------------------
class SupplierStorage:
    def __init__(self, data_root: Path, supplier_code: str):
        self.root               = (data_root / "suppliers" / supplier_code).resolve()
        self.invoices_csv       = self.root / "invoices" / "csv"
        self.invoices_pdf       = self.root / "invoices" / "pdf"
        self.invoices_html      = self.root / "invoices" / "html"
        self.invoices_processed = self.root / "invoices" / "processed"
        self.feeds_xml          = self.root / "feeds" / "xml"
        self.feeds_converted    = self.root / "feeds" / "converted"
        self.shop_exports       = self.root / "shop_exports"        # + /<shop>/YYYY/MM/...
        self.prepared_imports   = self.root / "prepared_imports"    # + /<shop>/YYYY/MM/...
        self.state              = self.root / "state"
        self.logs               = self.root / "logs"

    def yyyymm(self, base: Path, d: datetime) -> Path:
        return base / f"{d.year:04d}" / f"{d.month:02d}"

# -----------------------------------------------------------------------------
# Utils
# -----------------------------------------------------------------------------
def make_supplier_code(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^a-z0-9\-]+", "", s)
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-") or "supplier"

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def load_supplier_by_ref(db: SASession, ref: str) -> Supplier | None:
    sup = db.execute(select(Supplier).where(Supplier.supplier_code == ref)).scalar_one_or_none()
    if sup:
        return sup
    if ref.isdigit():
        return db.get(Supplier, int(ref))
    return None

def load_shop_by_ref(db: SASession, ref: str) -> Shop | None:
    shp = db.execute(select(Shop).where(Shop.shop_code == ref)).scalar_one_or_none()
    if shp:
        return shp
    if ref.isdigit():
        return db.get(Shop, int(ref))
    return None

def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p

# -----------------------------------------------------------------------------
# FastAPI app
# -----------------------------------------------------------------------------
app = FastAPI(
    title="Inventory Hub API",
    version="0.2.0",
    description="Integrates supplier feeds and invoices with shop stock updates.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # v produkcii sprísniť
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------------------------------------------------------
# Health
# -----------------------------------------------------------------------------
@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}

# -----------------------------------------------------------------------------
# Suppliers CRUD
# -----------------------------------------------------------------------------
@app.post("/suppliers", response_model=SupplierOut)
def create_supplier(payload: SupplierCreate, db: SASession = Depends(get_db)):
    base = Path(payload.base_path).resolve()
    if not base.exists():
        raise HTTPException(400, f"Base path does not exist: {base}")

    exists = db.execute(select(Supplier).where(Supplier.name == payload.name)).scalar_one_or_none()
    if exists:
        raise HTTPException(409, "Supplier with this name already exists")

    code = payload.supplier_code or make_supplier_code(payload.name)
    base_code, i = code, 2
    while db.execute(select(Supplier).where(Supplier.supplier_code == code)).scalar_one_or_none():
        code = f"{base_code}-{i}"
        i += 1

    sup = Supplier(
        name=payload.name,
        supplier_code=code,
        adapter=payload.adapter,
        base_path=str(base),
        config_json=payload.config_json or {}
    )
    db.add(sup)
    db.commit()
    db.refresh(sup)
    return sup

@app.get("/suppliers", response_model=List[SupplierOut])
def list_suppliers(db: SASession = Depends(get_db)):
    rows = db.execute(select(Supplier).order_by(Supplier.id.asc())).scalars().all()
    return rows

@app.get("/suppliers/{supplier_ref}", response_model=SupplierOut)
def get_supplier(supplier_ref: str, db: SASession = Depends(get_db)):
    sup = load_supplier_by_ref(db, supplier_ref)
    if not sup:
        raise HTTPException(404, "Supplier not found")
    return sup

@app.patch("/suppliers/{supplier_id}", response_model=SupplierOut)
def update_supplier(supplier_id: int, payload: SupplierCreate, db: SASession = Depends(get_db)):
    sup = db.get(Supplier, supplier_id)
    if not sup:
        raise HTTPException(404, "Supplier not found")

    if payload.name:
        other = db.execute(select(Supplier).where(Supplier.name == payload.name, Supplier.id != supplier_id)).scalar_one_or_none()
        if other:
            raise HTTPException(409, f"Name '{payload.name}' is already used by another supplier")
        sup.name = payload.name

    if payload.adapter:
        sup.adapter = payload.adapter

    if payload.base_path:
        sup.base_path = str(Path(payload.base_path).resolve())

    if payload.supplier_code:
        other = db.execute(select(Supplier).where(Supplier.supplier_code == payload.supplier_code, Supplier.id != supplier_id)).scalar_one_or_none()
        if other:
            raise HTTPException(409, f"Supplier code '{payload.supplier_code}' is already used by another supplier")
        sup.supplier_code = payload.supplier_code

    if payload.config_json is not None:
        sup.config_json = payload.config_json

    db.commit()
    db.refresh(sup)
    return sup

@app.delete("/suppliers/{supplier_id}")
def delete_supplier(supplier_id: int, db: SASession = Depends(get_db)):
    sup = db.get(Supplier, supplier_id)
    if not sup:
        raise HTTPException(404, "Supplier not found")
    db.delete(sup)
    db.commit()
    return {"deleted": supplier_id}

# -----------------------------------------------------------------------------
# Shops CRUD
# -----------------------------------------------------------------------------
@app.post("/shops", response_model=ShopOut)
def create_shop(payload: ShopCreate, db: SASession = Depends(get_db)):
    if db.execute(select(Shop).where(Shop.name == payload.name)).scalar_one_or_none():
        raise HTTPException(409, "Shop with this name already exists")
    if db.execute(select(Shop).where(Shop.shop_code == payload.shop_code)).scalar_one_or_none():
        raise HTTPException(409, "Shop with this code already exists")

    shp = Shop(
        shop_code=payload.shop_code,
        name=payload.name,
        platform=payload.platform,
        base_url=payload.base_url,
        config_json=payload.config_json or {}
    )
    db.add(shp)
    db.commit()
    db.refresh(shp)
    return shp

@app.get("/shops", response_model=List[ShopOut])
def list_shops(db: SASession = Depends(get_db)):
    rows = db.execute(select(Shop).order_by(Shop.id.asc())).scalars().all()
    return rows

@app.get("/shops/{shop_ref}", response_model=ShopOut)
def get_shop(shop_ref: str, db: SASession = Depends(get_db)):
    shp = load_shop_by_ref(db, shop_ref)
    if not shp:
        raise HTTPException(404, "Shop not found")
    return shp

# -----------------------------------------------------------------------------
# Area enum + univerzálny listing súborov v supplier workspace
# -----------------------------------------------------------------------------
class Area(str, Enum):
    invoices_csv = "invoices_csv"
    invoices_pdf = "invoices_pdf"
    invoices_html = "invoices_html"
    feeds_xml = "feeds_xml"
    feeds_converted = "feeds_converted"
    shop_exports = "shop_exports"
    prepared_imports = "prepared_imports"

@app.get("/suppliers/{supplier_ref}/files")
def list_files_for_supplier(
    supplier_ref: str,
    area: Area,
    shop: str | None = Query(None, description="Required for shop_exports and prepared_imports"),
    db: SASession = Depends(get_db),
):
    sup = load_supplier_by_ref(db, supplier_ref)
    if not sup:
        raise HTTPException(404, "Supplier not found")

    code = sup.supplier_code or make_supplier_code(sup.name)
    st = SupplierStorage(DATA_ROOT, code)

    if area == Area.invoices_csv:
        base = st.invoices_csv
    elif area == Area.invoices_pdf:
        base = st.invoices_pdf
    elif area == Area.invoices_html:
        base = st.invoices_html
    elif area == Area.feeds_xml:
        base = st.feeds_xml
    elif area == Area.feeds_converted:
        base = st.feeds_converted
    elif area in (Area.shop_exports, Area.prepared_imports):
        if not shop:
            raise HTTPException(400, "Parameter 'shop' is required for this area")
        parent = st.shop_exports if area == Area.shop_exports else st.prepared_imports
        base = parent / shop
    else:
        raise HTTPException(400, "Unsupported area")

    if not base.exists():
        return []

    out: List[Dict[str, Any]] = []
    for p in base.rglob("*"):
        if p.is_file():
            rel = p.relative_to(st.root)
            try:
                stt = p.stat()
                out.append({
                    "path": rel.as_posix(),
                    "size": stt.st_size,
                    "modified": datetime.fromtimestamp(stt.st_mtime).isoformat()
                })
            except OSError:
                continue

    return sorted(out, key=lambda x: x["path"])

# -----------------------------------------------------------------------------
# Prepare run: vyber nespracované faktúry + najnovšie feedy/exporty a vytvor job JSON
# -----------------------------------------------------------------------------
@app.post("/runs/prepare", response_model=PrepareRunResponse)
def prepare_run(payload: PrepareRunRequest, db: SASession = Depends(get_db)):
    sup = load_supplier_by_ref(db, payload.supplier_ref)
    if not sup:
        raise HTTPException(404, "Supplier not found")

    shp = load_shop_by_ref(db, payload.shop_ref)
    if not shp:
        raise HTTPException(404, "Shop not found")

    supplier_code = sup.supplier_code or make_supplier_code(sup.name)
    st = SupplierStorage(DATA_ROOT, supplier_code)

    # find invoice CSVs by mtime window and not processed (no .processed marker next to file)
    now = datetime.now()
    cutoff = now - timedelta(days=payload.months_back * 30)

    invoice_paths: List[Path] = []
    if st.invoices_csv.exists():
        for p in st.invoices_csv.rglob("*.csv"):
            try:
                mtime = datetime.fromtimestamp(p.stat().st_mtime)
            except OSError:
                continue
            if mtime >= cutoff:
                marker = p.parent / f"{p.name}.processed"
                if not marker.exists():
                    invoice_paths.append(p)

    # sort by path for determinism
    invoice_paths = sorted(invoice_paths)
    rel_invoice_paths = [p.relative_to(st.root).as_posix() for p in invoice_paths]

    # latest XML + converted
    latest_xml = None
    if st.feeds_xml.exists():
        xmls = sorted(st.feeds_xml.glob("*.xml"))
        latest_xml = xmls[-1].relative_to(st.root).as_posix() if xmls else None

    latest_converted = None
    if st.feeds_converted.exists():
        conv = sorted(st.feeds_converted.glob("*.csv"))
        latest_converted = conv[-1].relative_to(st.root).as_posix() if conv else None

    # latest shop export (if exists) under shop_exports/<shop_code>/
    latest_shop_export = None
    shop_exports_root = st.shop_exports / shp.shop_code
    if shop_exports_root.exists():
        exports = sorted(shop_exports_root.rglob("*.csv"))
        latest_shop_export = exports[-1].relative_to(st.root).as_posix() if exports else None

    # ensure jobs directory
    jobs_root = ensure_dir(DATA_ROOT / "jobs" / f"{now.year:04d}" / f"{now.month:02d}")

    job_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"
    job_path = jobs_root / f"{job_id}.json"

    job = {
        "job_id": job_id,
        "created_at": now.isoformat(),
        "supplier": {
            "id": sup.id,
            "supplier_code": supplier_code,
            "adapter": sup.adapter,
            "base_path": sup.base_path,
        },
        "shop": {
            "id": shp.id,
            "shop_code": shp.shop_code,
            "platform": shp.platform,
            "base_url": shp.base_url,
        },
        "params": {
            "months_back": payload.months_back,
            "upgates_csv_override": payload.upgates_csv_override,
            "notes": payload.notes,
        },
        "inputs": {
            "invoices_csv": rel_invoice_paths,
            "latest_xml": latest_xml,
            "latest_converted": latest_converted,
            "latest_shop_export": latest_shop_export,
        },
        "outputs": {
            # worker neskôr vyplní:
            "prepared_updates_csv": None,
            "prepared_new_csv": None,
            "prepared_unmatched_csv": None,
            "log_path": None,
        },
        "status": "READY"
    }
    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")

    return PrepareRunResponse(
        job_id=job_id,
        job_path=job_path.as_posix(),
        supplier_code=supplier_code,
        shop_code=shp.shop_code,
        invoices_count=len(rel_invoice_paths),
        invoice_paths=rel_invoice_paths,
        latest_xml=latest_xml,
        latest_converted=latest_converted,
        latest_shop_export=latest_shop_export,
    )
