from typing import Optional, Dict, Any, Literal
from pydantic import BaseModel, Field

class SupplierFeedAuth(BaseModel):
    type: Literal["none", "basic", "bearer", "header", "query"] = "none"
    username: Optional[str] = None
    password: Optional[str] = None
    token: Optional[str] = None
    header_name: Optional[str] = None
    query_param: Optional[str] = None

class SupplierFeedConfig(BaseModel):
    url: str
    method: Literal["GET","POST"] = "GET"
    headers: Dict[str, str] = Field(default_factory=dict)
    params: Dict[str, str] = Field(default_factory=dict)
    body: Optional[Dict[str, Any]] = None
    auth: SupplierFeedAuth = Field(default_factory=SupplierFeedAuth)
    format_hint: Optional[str] = "xml"
    save_raw: bool = True
    timeout: float = 60.0
    verify_ssl: bool = True

class SupplierConfig(BaseModel):
    product_code_prefix: str = "PL-"
    price_coefficients: Dict[str, float] = Field(default_factory=dict)
    feed: Optional[SupplierFeedConfig] = None

class SupplierIn(BaseModel):
    name: str
    adapter: str
    base_path: str
    supplier_code: str
    config_json: Optional[Dict[str, Any]] = None

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
    invoice_relpath: str
    months_back: int = 1
    upgates_csv_override: Optional[str] = None

class RunPrepareOut(BaseModel):
    run_id: str
    outputs: Dict[str, Optional[str]]
    stats: Dict[str, int]
    log: str

class RegisterExportIn(BaseModel):
    source_path: str
    filename: Optional[str] = None
    keep_last: int = 20
    keep_days: Optional[int] = None

class RefreshFeedIn(BaseModel):
    source_url: Optional[str] = None
    format_hint: Optional[str] = None
    save_raw: bool = True
