
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
import hashlib, json, os, shutil, datetime as dt, re

@dataclass
class LayoutSpec:
    layout: str  # "flat" | "by_date" | "by_number_date"
    date_strategy: List[str]
    prune_on_refresh: bool
    retention_days: int

def effective_layout_for_supplier(supplier: str, invoices_cfg: Dict[str, Any]) -> LayoutSpec:
    layout = (invoices_cfg.get("layout") or "flat").lower()
    if layout == "default":
        layout = "by_number_date" if supplier.lower() in {"paul-lange","paul_lange","paul-lange-oslany"} else "flat"
    if "layout" not in invoices_cfg and supplier.lower() in {"paul-lange","paul_lange","paul-lange-oslany"}:
        layout = "by_number_date"
    return LayoutSpec(
        layout=layout,
        date_strategy=invoices_cfg.get("date_strategy") or ["from_list","from_file","from_filename","from_header","download_time"],
        prune_on_refresh=bool(invoices_cfg.get("prune_on_refresh", False)),
        retention_days=int(invoices_cfg.get("retention_days", 180)),
    )

def extract_year_month_from_number(number: str) -> Optional[Tuple[str,str]]:
    if not number: return None
    m = re.match(r"^[A-Z]?([12][0-9]{3})([01][0-9])", number)
    if m:
        y, mo = m.group(1), m.group(2)
        if 1 <= int(mo) <= 12:
            return y, mo
    return None

def sha1_of_file(p: Path) -> str:
    h = hashlib.sha1()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def now_date_str() -> str:
    return dt.date.today().strftime("%Y-%m-%d")

def now_stamp() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat()+"Z"

def compute_paths(base: Path, supplier: str, layout: LayoutSpec, number: Optional[str], issue_date: Optional[str], ext: str, prefer_dir: str) -> Tuple[Path, Path, str]:
    day = (issue_date or now_date_str()).replace("-","")
    id_part = (number or "")[:40] if number else None
    if not id_part:
        id_part = "no-num"
    if layout.layout == "by_number_date":
        m = extract_year_month_from_number(number or "")
        if m:
            y, mo = m
            csv_rel = Path("invoices") / "csv" / y / mo / f"{number}.csv"
            raw_rel = Path("invoices") / "raw" / f"{number}{ext}"
            return (base / raw_rel, base / csv_rel, "by_number_date")
        layout_fallback = "by_date"
    else:
        layout_fallback = layout.layout

    if layout_fallback == "by_date":
        try:
            dtobj = dt.datetime.strptime((issue_date or now_date_str()), "%Y-%m-%d").date()
            y, mo = f"{dtobj.year:04d}", f"{dtobj.month:02d}"
        except Exception:
            y, mo = "YYYY", "MM"
        csv_rel = Path("invoices") / "csv" / y / mo / f"{id_part}.csv"
        raw_rel = Path("invoices") / "raw" / f"{id_part}{ext}"
        return (base / raw_rel, base / csv_rel, "by_date")

    csv_rel = Path("invoices") / "csv" / f"{day}_{id_part}.csv"
    raw_rel = Path("invoices") / "raw" / f"{day}_{id_part}{ext}"
    return (base / raw_rel, base / csv_rel, "flat")

class InvoiceIndex:
    def __init__(self, supplier_base: Path):
        self.base = supplier_base
        self.index_dir = supplier_base / "invoices"
        self.jsonl = self.index_dir / "index.jsonl"
        self.map_json = self.index_dir / "index.latest.json"
        self.index_dir.mkdir(parents=True, exist_ok=True)
        if not self.map_json.exists():
            self.map_json.write_text("{}", encoding="utf-8")

    def append(self, entry: Dict[str, Any]) -> None:
        line = json.dumps(entry, ensure_ascii=False)
        with self.jsonl.open("a", encoding="utf-8") as f:
            f.write(line + "\\n")
        current = json.loads(self.map_json.read_text(encoding="utf-8"))
        invoice_id = entry.get("invoice_id") or entry.get("number") or entry.get("sha1")
        if invoice_id:
            current[invoice_id] = entry
        self.map_json.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")

    def mark_processed(self, invoice_ids: List[str]) -> int:
        current = json.loads(self.map_json.read_text(encoding="utf-8"))
        changed = 0
        now = now_stamp()
        for iid in invoice_ids:
            entry = current.get(iid)
            if not entry:
                continue
            if entry.get("status") != "processed":
                entry["status"] = "processed"
                entry["processed_at"] = now
                current[iid] = entry
                changed += 1
                # trail
                with self.jsonl.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"event":"mark_processed","invoice_id":iid,"processed_at":now}, ensure_ascii=False) + "\\n")
        self.map_json.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        return changed

def prune_raw_if_needed(base: Path, layout: LayoutSpec) -> None:
    if not layout.prune_on_refresh:
        return
    raw_dir = base / "invoices" / "raw"
    if raw_dir.exists():
        for p in raw_dir.glob("*"):
            try:
                if p.is_file() or p.is_symlink():
                    p.unlink()
                elif p.is_dir():
                    shutil.rmtree(p)
            except Exception:
                pass

def retention_cleanup_raw(base: Path, layout: LayoutSpec) -> int:
    raw_dir = base / "invoices" / "raw"
    if not raw_dir.exists(): return 0
    days = layout.retention_days or 0
    if days <= 0: return 0
    keep_after = dt.datetime.utcnow() - dt.timedelta(days=days)
    removed = 0
    for p in raw_dir.glob("*"):
        try:
            mtime = dt.datetime.utcfromtimestamp(p.stat().st_mtime)
            if mtime < keep_after:
                if p.is_file() or p.is_symlink():
                    p.unlink(); removed += 1
                elif p.is_dir():
                    shutil.rmtree(p); removed += 1
        except Exception:
            pass
    return removed
