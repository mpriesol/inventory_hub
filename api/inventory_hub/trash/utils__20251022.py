
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple, Optional, Dict
from datetime import datetime, timedelta
import os, json, shutil

from .settings import settings

AREAS = {
    "invoices_csv": ["suppliers", "{supplier}", "invoices", "csv"],
    "invoices_pdf": ["suppliers", "{supplier}", "invoices", "pdf"],
    "feeds_xml": ["suppliers", "{supplier}", "feeds", "xml"],
    "feeds_converted": ["suppliers", "{supplier}", "feeds", "converted"],
    "shop_exports": ["suppliers", "{supplier}", "shop-exports"],  # legacy only
    "imports_upgates": ["suppliers", "{supplier}", "imports", "upgates"],
    "logs": ["suppliers", "{supplier}", "logs"],
    "state": ["suppliers", "{supplier}", "state"],
}

def _rel(p: Path) -> str:
    return str(p.relative_to(settings.INVENTORY_DATA_ROOT)).replace("\\","/")

@dataclass
class Lock:
    path: Path
    def acquire(self) -> None:
        if self.path.exists():
            age_sec = (datetime.now().timestamp() - self.path.stat().st_mtime)
            if age_sec > 6 * 3600:
                try: self.path.unlink()
                except FileNotFoundError: pass
            else:
                raise RuntimeError(f"Pipeline is locked: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"pid": os.getpid(), "ts": datetime.now().isoformat()}), encoding="utf-8")

    def release(self) -> None:
        try: self.path.unlink()
        except FileNotFoundError: pass

def area_path(area: str, supplier: str) -> Path:
    parts = [p.format(supplier=supplier) for p in AREAS[area]]
    return settings.INVENTORY_DATA_ROOT.joinpath(*parts)

def _year_month_pairs(months_back: int) -> List[Tuple[int, int]]:
    now = datetime.now()
    y, m = now.year, now.month
    out = []
    for i in range(months_back):
        mm = m - i
        yy = y
        while mm <= 0:
            mm += 12
            yy -= 1
        out.append((yy, mm))
    return out

def list_files(area: str, supplier: str, months_back: int = 3) -> List[str]:
    base = area_path(area, supplier)
    out: List[str] = []
    if area == "invoices_csv":
        for (year, month) in _year_month_pairs(months_back):
            p = base / f"{year:04d}" / f"{month:02d}"
            if p.exists():
                for f in sorted(p.rglob("*.csv")):
                    out.append(_rel(f))
    else:
        if base.exists():
            for f in sorted(base.rglob("*")):
                if f.is_file():
                    out.append(_rel(f))
    return out

def parse_invoice_id(invoice_relpath: str) -> str:
    return Path(invoice_relpath).stem

def upgates_output_names(invoice_id: str, as_of: datetime) -> Tuple[str, str, str]:
    ds = as_of.strftime("%Y%m%d")
    return (
        f"{invoice_id}_updates_existing_{ds}.csv",
        f"{invoice_id}_new_products_{ds}.csv",
        f"{invoice_id}_unmatched_{ds}.csv",
    )

# ---------------------------
# Shops helpers
# ---------------------------

def shops_state_path() -> Path:
    state_dir = settings.INVENTORY_DATA_ROOT / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "shops.json"

def load_shops() -> List[Dict]:
    p = shops_state_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return []
    se_root = settings.INVENTORY_DATA_ROOT / "shop-exports"
    if se_root.exists():
        return [{"shop_code": d.name, "name": d.name} for d in se_root.iterdir() if d.is_dir()]
    return []

def save_shops(items: List[Dict]) -> None:
    p = shops_state_path()
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

def ensure_shop_dirs(shop_ref: str) -> Path:
    root = settings.INVENTORY_DATA_ROOT / "shop-exports" / shop_ref
    (root / "archive").mkdir(parents=True, exist_ok=True)
    return root

def resolve_shop_export(shop_ref: str, override: Optional[str]) -> Path:
    if override:
        p = Path(override)
        if not p.exists():
            raise FileNotFoundError(f"Override not found: {p}")
        return p
    root = ensure_shop_dirs(shop_ref)
    latest = root / "latest.csv"
    if latest.exists():
        return latest
    archive = root / "archive"
    candidates = sorted(archive.glob("export-products-*.csv"))
    if candidates:
        return candidates[-1]
    raise FileNotFoundError(f"No shop export found for {shop_ref} (looked in {root})")

def register_shop_export(shop_ref: str, source_path: Path, filename: Optional[str] = None) -> Dict[str, str]:
    root = ensure_shop_dirs(shop_ref)
    if not Path(source_path).exists():
        raise FileNotFoundError(source_path)
    if filename is None:
        ds = datetime.now().strftime("%Y%m%d")
        filename = f"export-products-{ds}.csv"
    archive_path = root / "archive" / filename
    shutil.copy2(source_path, archive_path)
    latest = root / "latest.csv"
    shutil.copy2(source_path, latest)
    return {"latest": _rel(latest), "archive": _rel(archive_path)}

def prune_shop_exports(shop_ref: str, keep_last: int = 20, keep_days: Optional[int] = None) -> List[str]:
    root = ensure_shop_dirs(shop_ref)
    archive = root / "archive"
    deleted: List[str] = []
    files = sorted([f for f in archive.glob("*.csv") if f.is_file()], key=lambda p: p.stat().st_mtime)
    if keep_last is not None and len(files) > keep_last:
        to_del = files[:len(files)-keep_last]
        for f in to_del:
            try:
                f.unlink()
                deleted.append(_rel(f))
            except FileNotFoundError:
                pass
    if keep_days is not None:
        threshold = datetime.now() - timedelta(days=keep_days)
        for f in list(archive.glob("*.csv")):
            if datetime.fromtimestamp(f.stat().st_mtime) < threshold:
                try:
                    f.unlink()
                    deleted.append(_rel(f))
                except FileNotFoundError:
                    pass
    return deleted
