
# Paul‑Lange invoices → Upgates (Router + Adapter)

This patch adds a production‑ready adapter and endpoints to download and process Paul‑Lange invoices, preserving behavior from the previous standalone script.

## Endpoints

### 1) Refresh invoices (download from PL)
```
POST /suppliers/{supplier}/invoices/refresh?months_back=10

Response:
{
  "ok": true,
  "downloaded": 5,
  "skipped": 12,
  "failed": 0,
  "pages": 2,
  "log_files": ["logs/last_invoice_list.html"]
}
```
- Strategy required: `SupplierConfig.invoices.download.strategy == "paul-lange-web"`
- Reads login from `SupplierConfig.invoices.download.web.login`
- Saves CSV invoices to `suppliers/<code>/invoices/csv/YYYY/MM/F....csv`
- Updates index: `suppliers/<code>/invoices/index.latest.json` and appends to `index.jsonl`

### 2) Prepare updates for a specific invoice
```
POST /runs/prepare
{
  "supplier_ref": "paul-lange",
  "shop_ref": "biketrek",
  "invoice_relpath": "invoices/csv/2025/10/F2025070708.csv",
  "use_invoice_qty": true
}
→
{
  "ok": true,
  "stats": {"existing": 8, "new": 3, "invoice_items": 19},
  "outputs": {
    "updates_existing": "imports/upgates/F2025070708_updates_existing_20251105.csv",
    "new_products": "imports/upgates/F2025070708_new_products_20251105.csv",
    "unmatched": "imports/upgates/F2025070708_unmatched_20251105.csv"
  }
}
```
Requirements:
- Upgates export must exist at `shops/<shop>/latest.csv` (use your register‑export flow).
- Converted feed must exist at `suppliers/<supplier>/feeds/converted/*.csv` (use your feed refresh).

## Matching rules (from your guide)
- Existing products: `[PRODUCT_CODE] == "PL-" + SČM`, increment `[STOCK]` by invoice qty (or by 1 if disabled), set `[AVAILABILITY]="Na sklade"`.
- New products: copy full row from converted feed, set `[META original_product_code]=SČM`, `[META validation_required]=1`.
- Unmatched CSV lists `SCM, PRODUCT_CODE, QTY, REASON`.

## Files written
- Invoices: `suppliers/<supplier>/invoices/csv/YYYY/MM/F....csv`
- Outputs:  `suppliers/<supplier>/imports/upgates/<INVOICE>_updates_existing_YYYYMMDD.csv` etc.
- Index:    `suppliers/<supplier>/invoices/index.latest.json`, `index.jsonl`

## Not yet included
- META `[META "stock_updated_by_invoices"]` idempotency.
- Using real invoice issue date for subfolders.
