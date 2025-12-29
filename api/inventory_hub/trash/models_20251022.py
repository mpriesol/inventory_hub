
from pydantic import BaseModel, Field
from typing import Optional, Dict

class SupplierIn(BaseModel):
    name: str
    adapter: str
    base_path: str
    supplier_code: str
    config_json: Optional[Dict] = None

class SupplierOut(SupplierIn):
    pass

class ShopIn(BaseModel):
    shop_code: str
    name: Optional[str] = None

class ShopOut(ShopIn):
    pass

class RunPrepareIn(BaseModel):
    supplier_ref: str
    shop_ref: str
    invoice_relpath: str  # e.g. "suppliers/paul-lange/invoices/csv/2025/10/F2025060682.csv"
    months_back: int = 1
    upgates_csv_override: Optional[str] = None  # local path or URL (URL support TBD)

class RunPrepareOut(BaseModel):
    run_id: str
    outputs: Dict[str, Optional[str]]
    stats: Dict[str, int]
    log: str

class RegisterExportIn(BaseModel):
    source_path: str  # absolute local path to CSV
    filename: Optional[str] = None  # if None -> export-products-YYYYMMDD.csv
    keep_last: int = 20
    keep_days: Optional[int] = None
