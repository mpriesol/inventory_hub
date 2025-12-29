# app/main.py
from __future__ import annotations

import os
import re
from datetime import datetime
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
    # krátky, URL-safe identifikátor do ciest (napr. "paul-lange")
    supplier_code: Mapped[Optional[str]] = mapped_column(String(120), unique=True, index=True, nullable=True)
    # zobrazovaný názov
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    # typ integračného “adaptéra” (napr. "paul_lange", "generic")
    adapter: Mapped[str] = mapped_column(String(60), default="generic")
    # absolútna cesta na workspace dodávateľa (koreň priečinka s dátami)
    base_path: Mapped[str] = mapped_column(String(500))
    # voľná konfigurácia (auth, polia formulárov, URL-ky, atď.)
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

# -----------------------------------------------------------------------------
# Storage helper (semantické cesty, bez “SUBDIR_” reťazcov)
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

from sqlalchemy.exc import NoResultFound

def load_supplier_by_ref(db: SASession, ref: str) -> Supplier | None:
    # najprv skúsime supplier_code (textový kód)
    sup = db.execute(
        select(Supplier).where(Supplier.supplier_code == ref)
    ).scalar_one_or_none()
    if sup:
        return sup
    # fallback: ak ref vyzerá ako číslo, skúsime ID
    if ref.isdigit():
        return db.get(Supplier, int(ref))
    return None

# -----------------------------------------------------------------------------
# FastAPI app
# -----------------------------------------------------------------------------
app = FastAPI(
    title="Inventory Hub API",
    version="0.1.0",
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

    # unikátny názov
    exists = db.execute(select(Supplier).where(Supplier.name == payload.name)).scalar_one_or_none()
    if exists:
        raise HTTPException(409, "Supplier with this name already exists")

    code = payload.supplier_code or make_supplier_code(payload.name)
    # riešenie kolízie kódu
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

@app.get("/suppliers/{supplier_id}", response_model=SupplierOut)
def get_supplier(supplier_id: int, db: SASession = Depends(get_db)):
    sup = db.get(Supplier, supplier_id)
    if not sup:
        raise HTTPException(404, "Supplier not found")
    return sup

@app.patch("/suppliers/{supplier_id}", response_model=SupplierOut)
def update_supplier(supplier_id: int, payload: SupplierCreate, db: SASession = Depends(get_db)):
    sup = db.get(Supplier, supplier_id)
    if not sup:
        raise HTTPException(404, "Supplier not found")

    if payload.name:
        # kontrola kolízie mena
        other = db.execute(select(Supplier).where(Supplier.name == payload.name, Supplier.id != supplier_id)).scalar_one_or_none()
        if other:
            raise HTTPException(409, f"Name '{payload.name}' is already used by another supplier")
        sup.name = payload.name

    if payload.adapter:
        sup.adapter = payload.adapter

    if payload.base_path:
        sup.base_path = str(Path(payload.base_path).resolve())

    if payload.supplier_code:
        # kontrola kolízie kódu
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
    supplier_ref: str,   # <-- pôvodne supplier_id: int
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
