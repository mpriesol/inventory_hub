# -*- coding: utf-8 -*-
"""
Suppliers Router - v2.0
Full CRUD, version history, URL validation, invoice upload
"""
from __future__ import annotations

import os
import re
import json
import shutil
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, Body, HTTPException, UploadFile, File, Query
from pydantic import BaseModel

from inventory_hub.settings import settings
from inventory_hub.config_io import (
    load_supplier as io_load_supplier,
    save_supplier as io_save_supplier,
    supplier_path,
    DATA_ROOT,
)

router = APIRouter(tags=["suppliers"])

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
MAX_HISTORY_VERSIONS = 5
ALLOWED_INVOICE_EXTENSIONS = {".csv", ".pdf", ".xlsx", ".xls"}


# -----------------------------------------------------------------------------
# Pydantic Models
# -----------------------------------------------------------------------------
class SupplierSummary(BaseModel):
    """Summary info for supplier list"""
    code: str
    name: str
    is_active: bool
    product_prefix: str
    invoice_count: int
    feed_mode: str  # "remote" | "local" | "none"
    download_strategy: str  # "web" | "manual" | "api" | "disabled"
    last_invoice_date: Optional[str] = None
    last_feed_sync: Optional[str] = None


class SupplierHistoryEntry(BaseModel):
    """Single history version entry"""
    version: str  # timestamp-based filename
    timestamp: str  # ISO format
    size_bytes: int
    changes_summary: Optional[str] = None


class ValidationResult(BaseModel):
    """Result of config validation"""
    valid: bool
    errors: List[str]
    warnings: List[str]
    feed_url_reachable: Optional[bool] = None
    login_url_reachable: Optional[bool] = None


class SupplierCreateRequest(BaseModel):
    """Request to create new supplier"""
    code: str
    name: str
    product_prefix: str = ""
    download_strategy: str = "manual"


# -----------------------------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------------------------
def _suppliers_root() -> Path:
    """Root directory for all suppliers"""
    return DATA_ROOT / "suppliers"


def _supplier_dir(supplier: str) -> Path:
    """Directory for specific supplier"""
    return _suppliers_root() / supplier


def _history_dir(supplier: str) -> Path:
    """History directory for supplier configs"""
    return _supplier_dir(supplier) / "config_history"


def _invoices_csv_dir(supplier: str) -> Path:
    """Invoices CSV directory"""
    return _supplier_dir(supplier) / "invoices" / "csv"


def _invoices_pdf_dir(supplier: str) -> Path:
    """Invoices PDF directory"""
    return _supplier_dir(supplier) / "invoices" / "pdf"


def _index_path(supplier: str) -> Path:
    """Invoice index path"""
    return _supplier_dir(supplier) / "invoices" / "index.latest.json"


def _sanitize_code(code: str) -> str:
    """Sanitize supplier code to safe directory name"""
    code = code.lower().strip()
    code = re.sub(r'\s+', '-', code)
    code = re.sub(r'[^a-z0-9\-_]', '', code)
    return code or "unnamed"


def _list_supplier_codes() -> List[str]:
    """List all supplier codes from filesystem"""
    root = _suppliers_root()
    if not root.exists():
        return []
    
    codes = []
    for item in root.iterdir():
        if item.is_dir() and not item.name.startswith('.'):
            cfg_path = item / "config.json"
            if cfg_path.exists():
                codes.append(item.name)
    
    return sorted(codes)


def _count_invoices(supplier: str) -> int:
    """Count CSV invoices for supplier"""
    csv_dir = _invoices_csv_dir(supplier)
    if not csv_dir.exists():
        return 0
    return len([f for f in csv_dir.iterdir() if f.suffix.lower() == '.csv'])


def _get_last_invoice_date(supplier: str) -> Optional[str]:
    """Get date of most recent invoice"""
    index_path = _index_path(supplier)
    if not index_path.exists():
        return None
    
    try:
        data = json.loads(index_path.read_text(encoding='utf-8'))
        invoices = data.get('invoices', [])
        if invoices:
            sorted_inv = sorted(invoices, key=lambda x: x.get('date', ''), reverse=True)
            if sorted_inv:
                return sorted_inv[0].get('date')
    except Exception:
        pass
    
    return None


def _get_feed_mode(cfg: Dict[str, Any]) -> str:
    """Extract feed mode from config"""
    feeds = cfg.get('feeds', {})
    current_key = feeds.get('current_key', 'products')
    sources = feeds.get('sources', {})
    source = sources.get(current_key, {})
    return source.get('mode', 'none')


def _get_download_strategy(cfg: Dict[str, Any]) -> str:
    """Extract download strategy from config"""
    invoices = cfg.get('invoices', {})
    download = invoices.get('download', {})
    return download.get('strategy', 'manual')


def _get_product_prefix(cfg: Dict[str, Any]) -> str:
    """Extract product code prefix from config"""
    adapter = cfg.get('adapter_settings', {})
    mapping = adapter.get('mapping', {})
    postprocess = mapping.get('postprocess', {})
    return postprocess.get('product_code_prefix', '')


def _get_supplier_name(cfg: Dict[str, Any], code: str) -> str:
    """Extract supplier name from config or derive from code"""
    name = cfg.get('name', '')
    if name:
        return name
    return '-'.join(word.capitalize() for word in code.split('-'))


def _save_to_history(supplier: str, current_cfg: Dict[str, Any]) -> None:
    """Save current config to history before overwriting"""
    history_dir = _history_dir(supplier)
    history_dir.mkdir(parents=True, exist_ok=True)
    
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%S')
    history_file = history_dir / f"{ts}.json"
    
    history_file.write_text(
        json.dumps(current_cfg, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )
    
    _cleanup_old_history(supplier)


def _cleanup_old_history(supplier: str) -> None:
    """Remove old history versions, keeping only MAX_HISTORY_VERSIONS"""
    history_dir = _history_dir(supplier)
    if not history_dir.exists():
        return
    
    files = sorted(
        [f for f in history_dir.iterdir() if f.suffix == '.json'],
        key=lambda x: x.name,
        reverse=True
    )
    
    for old_file in files[MAX_HISTORY_VERSIONS:]:
        try:
            old_file.unlink()
        except Exception:
            pass


def _list_history(supplier: str) -> List[SupplierHistoryEntry]:
    """List all history versions for supplier"""
    history_dir = _history_dir(supplier)
    if not history_dir.exists():
        return []
    
    entries = []
    for f in sorted(history_dir.iterdir(), reverse=True):
        if f.suffix != '.json':
            continue
        
        try:
            ts_str = f.stem
            ts = datetime.strptime(ts_str, '%Y-%m-%dT%H-%M-%S')
            iso_ts = ts.isoformat() + 'Z'
        except Exception:
            iso_ts = f.stem
        
        entries.append(SupplierHistoryEntry(
            version=f.stem,
            timestamp=iso_ts,
            size_bytes=f.stat().st_size,
            changes_summary=None
        ))
    
    return entries


def _load_history_version(supplier: str, version: str) -> Dict[str, Any]:
    """Load specific history version"""
    history_file = _history_dir(supplier) / f"{version}.json"
    if not history_file.exists():
        raise HTTPException(status_code=404, detail=f"History version '{version}' not found")
    
    try:
        return json.loads(history_file.read_text(encoding='utf-8'))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load history: {e}")


async def _validate_url(url: str, timeout: float = 5.0) -> bool:
    """Validate URL is reachable"""
    if not url or not url.startswith(('http://', 'https://')):
        return False
    
    try:
        import httpx
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            resp = await client.head(url, follow_redirects=True)
            return resp.status_code < 500
    except Exception:
        return False


def _validate_config_syntax(cfg: Dict[str, Any]) -> ValidationResult:
    """Validate config structure and required fields"""
    errors = []
    warnings = []
    
    feeds = cfg.get('feeds', {})
    if not isinstance(feeds, dict):
        errors.append("'feeds' must be an object")
    else:
        sources = feeds.get('sources', {})
        if not isinstance(sources, dict):
            errors.append("'feeds.sources' must be an object")
        
        current_key = feeds.get('current_key', '')
        if current_key and current_key not in sources:
            warnings.append(f"'feeds.current_key' ({current_key}) not found in sources")
    
    invoices = cfg.get('invoices', {})
    if not isinstance(invoices, dict):
        errors.append("'invoices' must be an object")
    else:
        download = invoices.get('download', {})
        strategy = download.get('strategy', '')
        if strategy not in ('', 'web', 'manual', 'api', 'disabled', 'paul-lange-web', 'northfinder-web'):
            warnings.append(f"Unknown download strategy: '{strategy}'")
        
        if strategy in ('web', 'paul-lange-web', 'northfinder-web'):
            web = download.get('web', {})
            login = web.get('login', {})
            if not login.get('login_url'):
                warnings.append("Web strategy requires 'login_url' to be set")
            if not login.get('username'):
                warnings.append("Web strategy requires 'username' to be set")
    
    adapter = cfg.get('adapter_settings', {})
    if not isinstance(adapter, dict):
        errors.append("'adapter_settings' must be an object")
    
    return ValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings
    )


# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------

@router.get("/suppliers", response_model=List[SupplierSummary])
def list_suppliers() -> List[SupplierSummary]:
    """List all configured suppliers with summary info"""
    codes = _list_supplier_codes()
    summaries = []
    
    for code in codes:
        try:
            cfg = io_load_supplier(code, write_back_on_load=False)
            
            summaries.append(SupplierSummary(
                code=code,
                name=_get_supplier_name(cfg, code),
                is_active=cfg.get('is_active', True),
                product_prefix=_get_product_prefix(cfg),
                invoice_count=_count_invoices(code),
                feed_mode=_get_feed_mode(cfg),
                download_strategy=_get_download_strategy(cfg),
                last_invoice_date=_get_last_invoice_date(code),
                last_feed_sync=cfg.get('last_feed_sync'),
            ))
        except Exception:
            summaries.append(SupplierSummary(
                code=code,
                name=code,
                is_active=False,
                product_prefix="",
                invoice_count=0,
                feed_mode="none",
                download_strategy="disabled",
            ))
    
    return summaries


@router.post("/suppliers", response_model=Dict[str, Any])
def create_supplier(req: SupplierCreateRequest) -> Dict[str, Any]:
    """Create new supplier with basic config"""
    code = _sanitize_code(req.code)
    
    if not code:
        raise HTTPException(status_code=400, detail="Invalid supplier code")
    
    supplier_dir = _supplier_dir(code)
    config_path = supplier_path(code)
    
    if config_path.exists():
        raise HTTPException(status_code=409, detail=f"Supplier '{code}' already exists")
    
    supplier_dir.mkdir(parents=True, exist_ok=True)
    (supplier_dir / "invoices" / "csv").mkdir(parents=True, exist_ok=True)
    (supplier_dir / "invoices" / "pdf").mkdir(parents=True, exist_ok=True)
    (supplier_dir / "feeds" / "xml").mkdir(parents=True, exist_ok=True)
    (supplier_dir / "feeds" / "converted").mkdir(parents=True, exist_ok=True)
    (supplier_dir / "imports" / "upgates").mkdir(parents=True, exist_ok=True)
    (supplier_dir / "config_history").mkdir(parents=True, exist_ok=True)
    
    initial_config = {
        "name": req.name,
        "is_active": True,
        "feeds": {
            "current_key": "products",
            "sources": {
                "products": {
                    "mode": "remote",
                    "local_path": None,
                    "remote": {
                        "url": "",
                        "method": "GET",
                        "headers": {},
                        "params": {},
                        "auth": {
                            "mode": "none",
                            "login_url": "",
                            "user_field": "login",
                            "pass_field": "password",
                            "username": "",
                            "password": "",
                            "cookie": "",
                            "basic_user": "",
                            "basic_pass": "",
                            "token": "",
                            "header_name": "",
                            "insecure_all": False
                        }
                    }
                }
            }
        },
        "invoices": {
            "layout": "flat",
            "months_back_default": 3,
            "download": {
                "strategy": req.download_strategy,
                "web": {
                    "login": {
                        "mode": "form",
                        "login_url": "",
                        "user_field": "login",
                        "pass_field": "password",
                        "username": "",
                        "password": "",
                        "cookie": "",
                        "basic_user": "",
                        "basic_pass": "",
                        "token": "",
                        "header_name": "",
                        "insecure_all": False
                    },
                    "base_url": "",
                    "notes": ""
                }
            }
        },
        "adapter_settings": {
            "currency": "EUR",
            "vat_rate": 20,
            "mapping": {
                "invoice_to_canon": {
                    "SCM": "",
                    "EAN": "",
                    "TITLE": "",
                    "QTY": "",
                    "UNIT_PRICE_EX": "",
                    "UNIT_PRICE_INC": None
                },
                "postprocess": {
                    "unit_price_source": "ex",
                    "product_code_prefix": req.product_prefix
                },
                "canon_to_upgates": {}
            }
        }
    }
    
    return io_save_supplier(code, initial_config)


@router.get("/suppliers/{supplier}/config")
def get_supplier_config(supplier: str) -> Dict[str, Any]:
    """Get supplier configuration"""
    cfg = io_load_supplier(supplier, write_back_on_load=True)
    if not cfg:
        raise HTTPException(status_code=404, detail=f"Supplier '{supplier}' not found")
    return cfg


@router.put("/suppliers/{supplier}/config")
@router.post("/suppliers/{supplier}/config")
def put_supplier_config(supplier: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Update supplier configuration with automatic history backup"""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    
    try:
        current_cfg = io_load_supplier(supplier, write_back_on_load=False)
        if current_cfg:
            _save_to_history(supplier, current_cfg)
    except Exception:
        pass
    
    return io_save_supplier(supplier, payload or {})


@router.get("/suppliers/{supplier}/history", response_model=List[SupplierHistoryEntry])
def get_supplier_history(supplier: str) -> List[SupplierHistoryEntry]:
    """Get config version history for supplier"""
    if not supplier_path(supplier).exists():
        raise HTTPException(status_code=404, detail=f"Supplier '{supplier}' not found")
    
    return _list_history(supplier)


@router.get("/suppliers/{supplier}/history/{version}")
def get_supplier_history_version(supplier: str, version: str) -> Dict[str, Any]:
    """Get specific history version of supplier config"""
    return _load_history_version(supplier, version)


@router.post("/suppliers/{supplier}/restore/{version}")
def restore_supplier_version(supplier: str, version: str) -> Dict[str, Any]:
    """Restore supplier config from history version"""
    old_cfg = _load_history_version(supplier, version)
    
    try:
        current_cfg = io_load_supplier(supplier, write_back_on_load=False)
        if current_cfg:
            _save_to_history(supplier, current_cfg)
    except Exception:
        pass
    
    return io_save_supplier(supplier, old_cfg)


@router.post("/suppliers/{supplier}/validate", response_model=ValidationResult)
async def validate_supplier_config(
    supplier: str,
    check_urls: bool = Query(default=False, description="Also check URL reachability")
) -> ValidationResult:
    """Validate supplier configuration syntax and optionally check URLs"""
    cfg = io_load_supplier(supplier, write_back_on_load=False)
    if not cfg:
        raise HTTPException(status_code=404, detail=f"Supplier '{supplier}' not found")
    
    result = _validate_config_syntax(cfg)
    
    if check_urls:
        feeds = cfg.get('feeds', {})
        current_key = feeds.get('current_key', 'products')
        sources = feeds.get('sources', {})
        source = sources.get(current_key, {})
        if source.get('mode') == 'remote':
            remote = source.get('remote', {})
            feed_url = remote.get('url', '')
            if feed_url:
                result.feed_url_reachable = await _validate_url(feed_url)
                if not result.feed_url_reachable:
                    result.warnings.append(f"Feed URL not reachable: {feed_url}")
        
        invoices = cfg.get('invoices', {})
        download = invoices.get('download', {})
        if download.get('strategy') in ('web', 'paul-lange-web', 'northfinder-web'):
            web = download.get('web', {})
            login = web.get('login', {})
            login_url = login.get('login_url', '')
            if login_url:
                result.login_url_reachable = await _validate_url(login_url)
                if not result.login_url_reachable:
                    result.warnings.append(f"Login URL not reachable: {login_url}")
    
    return result


def _sanitize_filename(filename: str) -> str:
    """
    Sanitize filename - remove spaces, special chars, quotes.
    Convert spaces to underscores, keep only safe characters.
    """
    # Get stem and extension
    stem = Path(filename).stem
    ext = Path(filename).suffix.lower()
    
    # Replace spaces with underscores
    stem = stem.replace(' ', '_')
    
    # Remove quotes and other problematic chars
    stem = re.sub(r'[\'"\(\)\[\]<>:;,!@#$%^&*+=|\\/?]', '', stem)
    
    # Replace multiple underscores with single
    stem = re.sub(r'_+', '_', stem)
    
    # Trim underscores from start/end
    stem = stem.strip('_')
    
    # Limit length
    if len(stem) > 100:
        stem = stem[:100]
    
    return f"{stem}{ext}"


def _invoices_raw_dir(supplier: str) -> Path:
    """Get raw invoices directory (for XLSX/XLS files)"""
    return _supplier_dir(supplier) / "invoices" / "raw"


@router.post("/suppliers/{supplier}/upload-invoice")
async def upload_invoice(
    supplier: str,
    file: UploadFile = File(...),
    invoice_number: Optional[str] = Query(default=None, description="Override invoice number")
):
    """Upload invoice file (CSV, XLSX, XLS, or PDF) manually"""
    if not supplier_path(supplier).exists():
        raise HTTPException(status_code=404, detail=f"Supplier '{supplier}' not found")
    
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_INVOICE_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type '{ext}'. Allowed: {', '.join(ALLOWED_INVOICE_EXTENSIONS)}"
        )
    
    # Determine target directory based on file type
    if ext == '.pdf':
        target_dir = _invoices_pdf_dir(supplier)
    elif ext in ('.xlsx', '.xls'):
        target_dir = _invoices_raw_dir(supplier)
    else:
        target_dir = _invoices_csv_dir(supplier)
    
    target_dir.mkdir(parents=True, exist_ok=True)
    
    # Build sanitized filename
    if invoice_number:
        safe_number = re.sub(r'[^a-zA-Z0-9\-_]', '', invoice_number)
        filename = f"{safe_number}{ext}"
    else:
        # Sanitize original filename - removes spaces, quotes, etc.
        filename = _sanitize_filename(file.filename)
    
    target_path = target_dir / filename
    if target_path.exists():
        ts = datetime.now().strftime('%Y%m%d%H%M%S')
        stem = Path(filename).stem
        filename = f"{stem}_{ts}{ext}"
        target_path = target_dir / filename
    
    content = await file.read()
    target_path.write_bytes(content)
    
    csv_path = None
    
    # Handle XLSX/XLS - convert to CSV
    if ext in ('.xlsx', '.xls'):
        try:
            from inventory_hub.adapters.northfinder_xlsx_parser import parse_xlsx_to_csv
            
            csv_filename = Path(filename).stem + '.csv'
            csv_dir = _invoices_csv_dir(supplier)
            csv_dir.mkdir(parents=True, exist_ok=True)
            csv_full_path = csv_dir / csv_filename
            
            result = parse_xlsx_to_csv(target_path, csv_full_path)
            if result["success"]:
                csv_path = f"suppliers/{supplier}/invoices/csv/{csv_filename}"
                # Update index with CSV using map format
                _update_invoice_index_map(supplier, csv_filename, csv_path, target_path)
        except ImportError:
            pass  # No XLSX parser available
        except Exception as e:
            import logging
            logging.warning(f"Failed to convert XLSX to CSV: {e}")
    elif ext == '.csv':
        csv_path = f"suppliers/{supplier}/invoices/csv/{filename}"
        _update_invoice_index_map(supplier, filename, csv_path, None)
    
    return {
        "success": True,
        "filename": filename,
        "path": str(target_path.relative_to(DATA_ROOT)),
        "csv_path": csv_path,
        "size_bytes": len(content)
    }


def _update_invoice_index_map(supplier: str, filename: str, csv_path: str, raw_path: Optional[Path]) -> None:
    """
    Add new invoice to index using canonical map format.
    Format: { "<invoice_id>": { entry }, ... }
    """
    index_path = _index_path(supplier)
    
    # Load existing index
    if index_path.exists():
        try:
            data = json.loads(index_path.read_text(encoding='utf-8'))
        except Exception:
            data = {}
    else:
        data = {}
    
    # Normalize old list format to map
    if isinstance(data, dict) and "invoices" in data and isinstance(data["invoices"], list):
        new_data = {}
        for i, inv in enumerate(data["invoices"]):
            if isinstance(inv, dict):
                key = str(inv.get("invoice_id") or inv.get("number") or f"inv_{i}")
                new_data[key] = inv
        data = new_data
    elif isinstance(data, list):
        new_data = {}
        for i, inv in enumerate(data):
            if isinstance(inv, dict):
                key = str(inv.get("invoice_id") or inv.get("number") or f"inv_{i}")
                new_data[key] = inv
        data = new_data
    elif not isinstance(data, dict):
        data = {}
    
    # Filter out meta keys (like "updated_at")
    data = {k: v for k, v in data.items() if isinstance(v, dict)}
    
    # Build invoice entry
    stem = Path(filename).stem
    invoice_date = None
    # Try to extract date from filename (pattern: YYYYMMDD or YYYY_MM_DD or DD_MM_YYYY)
    match = re.search(r'(\d{4})[\-_]?(\d{2})[\-_]?(\d{2})', stem)
    if match:
        try:
            invoice_date = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        except Exception:
            pass
    
    # Use stem as invoice_id
    invoice_id = stem
    
    entry = {
        "supplier": supplier,
        "invoice_id": invoice_id,
        "number": stem,
        "issue_date": invoice_date or datetime.now().strftime('%Y-%m-%d'),
        "csv_path": csv_path,
        "status": "new",
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
    }
    
    if raw_path and raw_path.exists():
        entry["raw_path"] = str(raw_path.relative_to(DATA_ROOT))
    
    # Add/update entry
    data[invoice_id] = entry
    
    # Save index
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def _update_invoice_index(supplier: str, filename: str) -> None:
    """Add new invoice to index (legacy - redirects to map version)"""
    csv_path = f"suppliers/{supplier}/invoices/csv/{filename}"
    _update_invoice_index_map(supplier, filename, csv_path, None)


@router.delete("/suppliers/{supplier}")
def delete_supplier(supplier: str, confirm: bool = Query(default=False)):
    """Delete supplier (soft delete - moves to .deleted folder)"""
    supplier_dir = _supplier_dir(supplier)
    
    if not supplier_dir.exists():
        raise HTTPException(status_code=404, detail=f"Supplier '{supplier}' not found")
    
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="Add ?confirm=true to confirm deletion"
        )
    
    deleted_root = _suppliers_root() / ".deleted"
    deleted_root.mkdir(parents=True, exist_ok=True)
    
    ts = datetime.now().strftime('%Y%m%d%H%M%S')
    deleted_path = deleted_root / f"{supplier}_{ts}"
    
    shutil.move(str(supplier_dir), str(deleted_path))
    
    return {
        "success": True,
        "message": f"Supplier '{supplier}' moved to deleted folder",
        "restore_path": str(deleted_path.relative_to(DATA_ROOT))
    }
