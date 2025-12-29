# Supplier: Paul-Lange (paul-lange)

## Product code normalization
- Canonical product code: "PL-" + SCM (SČM)
- For NEW items created from feed/export:
  - Set META.validation_required = 1
  - Set META.original_product_code = SCM
  - Zero out NEW/SPECIAL/SELLOUT and label flags
  - Images are ';'-separated URLs; preserve quoting

## Invoice processing rules (idempotency)
- For existing items:
  - Match both raw SCM and PL-prefixed code
  - Increment STOCK by invoice quantity ("Množstvo")
  - Set AVAILABILITY = "Na sklade"
  - Append invoice id to META.stock_updated_by_invoices to prevent double counting
- For new items:
  - Set PRODUCT_CODE = "PL-" + SCM
  - STOCK = invoice qty
  - AVAILABILITY = "Na sklade"
  - META.original_product_code / validation_required / stock_updated_by_invoices set
  - Reset label/special flags

## Price calculation (Price with VAT: "Predvolené")
PRICE_WITH_VAT("Predvolené") = MOC * coefficient by manufacturer
Coefficients:
- Shimano 0.88
- PRO 0.91
- Lazer 0.90
- Longus 0.95
- Elite 0.92
- Motorex 0.96

## Output naming
- Output CSV names must include the invoice ID (for traceability).

## Data layout (relative to INVENTORY_DATA_ROOT)
- suppliers/paul-lange/feeds/xml
- suppliers/paul-lange/feeds/converted
- suppliers/paul-lange/invoices/csv
- suppliers/paul-lange/invoices/pdf
- suppliers/paul-lange/imports/upgates
- suppliers/paul-lange/invoices/history
