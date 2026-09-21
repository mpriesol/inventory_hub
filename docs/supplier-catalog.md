# Supplier products and shop import

The supplier catalog is a shared, read-only view of downloaded listing feeds. It is separate from local stock and from the existing **Upgates → Hub** product download. The first parser supports Paul Lange. Other suppliers require their own parser registered in `services/catalog.py`; the API and UI remain the same.

Open **Dodávatelia → Produkty dodávateľa**. Download the listing feed, browse or search, select individual items or groups, choose the target shop, and prepare an import preview. Empty search lists all items with pagination. The page remembers the target shop. Listing badges use mappings known to Hub; the preview verifies codes and EANs against the target shop.

## API

Routes below include the production `/api` prefix. Interactive contracts are available at `/api/docs`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/suppliers/{supplier}/catalog` | Feed status, supported sources, target shops |
| POST | `/api/suppliers/{supplier}/catalog/refresh` | Download and index a listing feed; body `{"feed_key":"products"}` |
| GET | `/api/suppliers/{supplier}/catalog/products` | Paginated normalized product search |
| GET | `/api/suppliers/{supplier}/catalog/selection` | All matching item IDs and the current `run_id` |
| GET | `/api/suppliers/{supplier}/catalog/products/{id}` | Full normalized data, explicit variants, original fields and XML |
| GET | `/api/suppliers/{supplier}/catalog/products/{id}/source` | Download original product XML |
| GET | `/api/shops/{shop}/import/options` | Read target languages/currencies, categories, pricelists and VAT mode |
| POST | `/api/shops/{shop}/import/preview` | Freeze an explicit selection into a preview; no shop writes |
| POST | `/api/shops/{shop}/import` | Confirm a preview and start a background import |
| GET | `/api/shops/{shop}/import/{preview_id}` | Item outcomes and progress |

Search parameters: `feed_key=products`, `q`, exact `code`, exact `ean`, `manufacturer`, `shop`, `listing=all|listed|unlisted|warnings`, `sort=name|code|manufacturer`, `page=1`, `page_size=50` (maximum 100), `grouped=true`. `q` searches partial names without case or accents, supplier/shop/manufacturer codes and exact EANs. `%` and `_` are literal characters. EANs remain strings, including leading zeros. Multiple filters combine with AND.

The response provides normalized JSON for application use. Full descriptions, all dynamic parameters and original fields are available in the detail response; `source_xml` preserves the original item. `description_html` is sanitized for display, while the original description remains unchanged. Summary rows omit long descriptions and full parameter lists to keep browsing small.

### Explicit variant groups

Groups are recognized only from an explicit source identifier. Similar names, colors or `STA_PARAMS/PRODUCT_NAME` alone do not prove a relationship. The current Paul Lange feed has no explicit group IDs, so its items remain standalone.

For a feed that provides groups, the parent row expands into independently selectable variants with their own images and attributes. Search returns matching variants within a group; the product detail also returns all active siblings. Selecting a group selects the variants currently matching the filters. Selecting all results selects all matching item IDs across pages.

Each ID identifies an actual supplier item. Importing selected variants creates the necessary hidden parent plus **only the selected variants**. The Hub parent code is `{product_prefix}G-{supplier_group_code}`; it is a derived shop identifier, not a supplier SKU. Conflicting codes, inconsistent groups, missing/duplicate variant parameter combinations or more than 100 variants block that group. Adding variants to a parent that already exists in the target shop is intentionally not part of this create-only operation; the group is skipped.

## Preview and confirmation

Example preview request:

```json
{
  "supplier": "paul-lange",
  "feed_key": "products",
  "run_id": 42,
  "product_ids": [101, 102],
  "options": {
    "language": "sk",
    "currency": "EUR",
    "pricelist": "Predvolené",
    "category_code": null,
    "pricing": "configured",
    "include_images": true,
    "include_description": true,
    "include_parameters": true
  }
}
```

Use real item/run IDs and options returned by the API. A request accepts at most 20,000 explicit IDs. Include `run_id` to reject a selection made against an outdated catalog. A preview is valid for one hour and contains exact outgoing payloads and per-item `ready`, `exists` or `invalid` outcomes. `exists` and `invalid` items are skipped when confirming the remaining ready items. No products are imported merely because they matched a search.

Confirm with `{"preview_id":"<returned ID>"}`. Poll the result URL while `status` is `queued` or `running`. Each product ends as `created`, `exists`, `failed` or `uncertain` (invalid preview items retain `invalid`). The top-level `completed` means processing finished; inspect item outcomes for partial success.

Products are always hidden (`active_yn=false`, language visibility false) and carry `validation_required=1`. Variant visibility is nested under the hidden parent. If the checkbox does not exist, the preview explicitly states that confirmation will create it. Existing incompatible field definitions block the preview. There is no option to publish directly or send stock quantities. Only POST creation is used; existing shop products are not updated.

Price calculations reuse the Paul Lange manufacturer's configured coefficient / existing converter defaults, with Decimal rounding to two places. `pricing=retail` bypasses the coefficient. Target `/config.prices_with_vat_yn` determines whether net or gross feed prices are sent, including purchase prices. Currency conversion is not performed. VAT is taken from the supplier configuration (default 23%, also used by the existing converter), reported as `vat_source=configured`. Inconsistent gross/net/VAT pairs block import for that item. Missing prices never become zero. The first EAN is exported to Upgates; all EANs remain in Hub.

Supplier stock is informational: `6+` means `supplier_stock_min=6`, not an exact quantity. Text external availability is a boolean plus its original text, not a guessed quantity. Catalog refresh and import registration create no stock balance or stock movement. Outgoing payloads reject stock fields recursively.

Preview/result files are persisted under `shops/{shop}/catalog-imports/`. Each shop has a process-safe import lock. The item is marked uncertain before an external POST; a timeout is reconciled by reading the shop, never by blindly retrying the POST. Created products must be read back as hidden with the review flag and selected variants before local registration. A database failure after shop creation can be reconciled without another POST.

`{"preview_id":"...","retry_failed":true}` verifies uncertain items and retries items that were not sent. Uncertain items still absent from the shop remain uncertain, because a delayed request may finish later; review them before starting a new preview. A process restart is reported as an interrupted job after the lock is released. Images are downloaded asynchronously by Upgates, so a successful product creation does not prove image processing has finished.

## Configuration and deployment

No database migration or new runtime dependency is required. Existing catalog tables and the existing `004_shop_product_content.sql` table are used. The configured supplier download URL and auth stay in the existing supplier configuration. The catalog explicitly chooses `products` (or another listing source); it never implicitly chooses the supplier's current stock feed. Downloaded XML snapshots remain in the existing supplier feed directory, with successful/failed indexing recorded in PostgreSQL.

Paul Lange reads its existing `adapter_settings.mapping.postprocess.product_code_prefix`, `price_coefficients`, `vat`, and default category. Optional `adapter_settings.catalog` keys are `currency`, `parser`, `group_tag` (default `ITEMGROUP_ID`), and `variant_parameters` (names of explicit variant attributes). A source can set `catalog_parser` individually, allowing supplier/manufacturer feeds with different parsers later. Unsupported sources display a clear unavailable state.

Merge triggers the existing production build. First verify the feed download, search with/without accents, detail/XML and preview on the deployed Hub. A live create requires an explicitly selected test product. Verify its hidden visibility, review flag and images in the target shop, and verify stock remains unchanged. No live shop product has been created as part of automated tests.

Rollback: revert the feature commit and deploy through the usual approved PR flow. Existing CSV behavior is preserved. Catalog snapshots and audit files can remain on disk/in the existing tables; reverting code does not delete products already explicitly imported to a shop.

Tests use synthetic fixtures and mocked Upgates responses. PostgreSQL integration tests require `CATALOG_TEST_DATABASE_URL` pointing to a dedicated localhost database ending in `_catalog_test`; CI supplies an ephemeral PostgreSQL 16 service. They never fall back to production settings.

Reference contracts: [products](https://docs.upgates.com/api-reference/produkty), [product lists](https://docs.upgates.com/api-reference/produkty-seznamy), [shop settings](https://docs.upgates.com/api-reference/nastaveni-eshopu), [custom fields](https://docs.upgates.com/api-reference/vlastni-pole).
