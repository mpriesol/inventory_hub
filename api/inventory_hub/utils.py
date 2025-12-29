from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
import json, os

from .settings import settings
import pandas as pd

# Canonical area roots under INVENTORY_DATA_ROOT
AREAS = {
    "invoices_csv": ["suppliers", "{supplier}", "invoices", "csv"],
    "invoices_pdf": ["suppliers", "{supplier}", "invoices", "pdf"],
    "feeds_xml": ["suppliers", "{supplier}", "feeds", "xml"],
    "feeds_converted": ["suppliers", "{supplier}", "feeds", "converted"],
    "imports_upgates": ["suppliers", "{supplier}", "imports", "upgates"],
    "logs": ["suppliers", "{supplier}", "logs"],
    "state": ["suppliers", "{supplier}", "state"],
}

def _rel(p: Path) -> str:
    # Always return POSIX-like relative path from INVENTORY_DATA_ROOT
    return str(p.relative_to(settings.INVENTORY_DATA_ROOT)).replace("\\", "/")

@dataclass
class Lock:
    path: Path
    def acquire(self) -> None:
        if self.path.exists():
            age_sec = (datetime.now().timestamp() - self.path.stat().st_mtime)
            if age_sec > 6 * 3600:
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
            else:
                raise RuntimeError(f"Pipeline is locked: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"pid": os.getpid(), "ts": datetime.now().isoformat()}), encoding="utf-8")
    def release(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

def area_path(area: str, supplier: str) -> Path:
    parts = [p.format(supplier=supplier) for p in AREAS[area]]
    return settings.INVENTORY_DATA_ROOT.joinpath(*parts)

def list_files(area: str, supplier: str, months_back: int = 3) -> List[str]:
    """
    Return list of files (relative to INVENTORY_DATA_ROOT) for given area.
    For invoices_csv we respect year subfolders; months_back is used as a coarse filter by mtime.
    """
    base = area_path(area, supplier)
    out: List[str] = []
    if not base.exists():
        return out

    cutoff = datetime.now() - timedelta(days=30*max(0, months_back))

    if area == "invoices_csv":
        # Walk all CSVs under invoices/csv/YYYY and filter by mtime (coarse months_back)
        for f in sorted(base.rglob("*.csv")):
            try:
                if datetime.fromtimestamp(f.stat().st_mtime) >= cutoff:
                    out.append(_rel(f))
            except Exception:
                out.append(_rel(f))
        return out

    # Generic: return all files under area, filter by mtime
    for f in sorted(base.rglob("*")):
        if f.is_file():
            try:
                if datetime.fromtimestamp(f.stat().st_mtime) >= cutoff:
                    out.append(_rel(f))
            except Exception:
                out.append(_rel(f))
    return out

def upgates_output_names(invoice_id: str, as_of: datetime):
    ds = as_of.strftime("%Y%m%d")
    return (
        f"{invoice_id}_updates_existing_{ds}.csv",
        f"{invoice_id}_new_products_{ds}.csv",
        f"{invoice_id}_unmatched_{ds}.csv",
    )

# Supplier config canonical path (no state/config.json duplication)
def supplier_config_path(supplier_code: str) -> Path:
    return settings.INVENTORY_DATA_ROOT / "suppliers" / supplier_code / "config.json"

def load_supplier_config(supplier_code: str) -> Dict[str, Any]:
    p = supplier_config_path(supplier_code)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_supplier_config(supplier_code: str, cfg: Dict[str, Any]) -> None:
    p = supplier_config_path(supplier_code)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

# CSV helpers
def read_csv_smart(path: Path, max_rows: int | None = None) -> pd.DataFrame:
    encodings = ["utf-8-sig", "cp1250", "latin-1"]
    seps = [",", ";", "\t", "|"]
    last_err = None
    for enc in encodings:
        for sep in seps:
            try:
                df = pd.read_csv(
                    path,
                    encoding=enc,
                    sep=sep,
                    dtype=str,
                    nrows=max_rows,
                    on_bad_lines="skip",
                )
                if df.shape[1] > 1:
                    return df
            except Exception as e:
                last_err = e
                continue
    if last_err:
        raise last_err
    raise ValueError(f"Cannot parse CSV: {path}")

def clean_upgates_headers_inplace(df: pd.DataFrame) -> None:
    cols = []
    for c in df.columns:
        s = str(c).replace("\u00A0", " ").strip()
        if s.startswith("[") and s.endswith("]"):
            s = s[1:-1]
        cols.append(s)
    df.columns = cols

def invoices_state_path(supplier: str) -> Path:
    return area_path("state", supplier) / "invoices.json"

def load_invoices_state(supplier: str) -> dict:
    p = invoices_state_path(supplier)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_invoices_state(supplier: str, state: dict) -> None:
    p = invoices_state_path(supplier)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
