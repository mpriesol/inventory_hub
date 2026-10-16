# Supplier products and shop import

The supplier catalog is a shared, read-only view of downloaded listing feeds. It is separate from local stock and from the existing **Upgates → Hub** product download. Native parsers support Paul Lange, Northfinder, Sloger, SK Profi Bike, Schindler, Cyklo Zitny, Silvini and Bike Brothers. Other suppliers can use the configurable feed mapping; the catalog and import API remain the same.

Open **Dodávatelia → Produkty dodávateľa**. Download the listing feed, browse or search, select individual items or groups, choose the target shop, and prepare an import preview. Empty search lists all items with pagination. The page remembers the target shop. Listing badges use mappings known to Hub; the preview verifies codes and EANs against the target shop.

For a listed item with saved shop content, the listing badge opens its storefront URL; a separate link opens shop administration. Detail responses and rows include `shop_url`, `shop_admin_url` and `shop_active` for the selected shop. Links come from the existing local content snapshot (preferring the Slovak URL); variants use their parent product's content. Missing URLs remain absent, and hidden products are labelled. No additional Upgates request is made to display or open these links.

## API

Shop settings use a per-shop, connection-scoped filesystem cache for up to 15 minutes. `GET /api/shops/{shop}/import/options?refresh=true` bypasses it; responses include `cache.checked_at`, `expires_at`, `from_cache` and `max_age_seconds`. The catalog shows these times and provides a refresh action. Execution always revalidates settings directly before writes, including VAT mode and the validation field.

Code/EAN checks always contact Upgates. The first check loads the complete `products/simple` list; subsequent checks replace changed parent/variant identities using `last_update_time_from` with a five-minute overlap. Full checks repeat on use after 24 hours or when preview receives `refresh_shop: true`. This explicit option detects deletions outside Hub immediately; otherwise deleted entries can conservatively remain marked as existing until the next full check. Preview/results expose `shop_check` times and full/changes mode. Failed pages do not advance the saved checkpoint and block import. Catalog listing badges still describe local Hub mappings, as indicated above the table; the identity cache is used for import duplicate checks.

`POST /api/suppliers/{supplier}/catalog/download` with `{"feed_key":"products"}` downloads the configured listing source without requiring a parser or changing catalog, stock or shop data. It returns the saved relative path, byte size and download time. The catalog's **Získať pôvodný feed** action exposes this for suppliers whose adapter is still being prepared. A successful download confirms file transfer, not successful XML parsing. TLS, timeout, connection, authentication, missing-file and rate-limit errors are distinguished without returning credentials or the supplier URL.

Routes below include the production `/api` prefix. Interactive contracts are available at `/api/docs`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/suppliers/{supplier}/catalog` | Feed status, supported sources, target shops |
| POST | `/api/suppliers/{supplier}/catalog/refresh` | Download and index a listing feed; body `{"feed_key":"products"}` |
| GET | `/api/suppliers/{supplier}/catalog/products` | Paginated normalized product search |
| GET | `/api/suppliers/{supplier}/catalog/selection` | All matching item IDs and the current `run_id` |
| GET | `/api/suppliers/{supplier}/catalog/products/{id}` | Full normalized data, explicit variants, original fields and XML |
| GET | `/api/suppliers/{supplier}/catalog/products/{id}/source` | Download original product XML |
| GET | `/api/suppliers/paul-lange/catalog/images/{filename}` | Display Paul Lange's HTTP-only JPEG through Hub HTTPS |
| GET | `/api/shops/{shop}/import/options` | Read target languages/currencies, categories, pricelists and VAT mode |
| POST | `/api/shops/{shop}/import/preview` | Freeze an explicit selection into a preview; no shop writes |
| POST | `/api/shops/{shop}/import` | Confirm a preview and start a background import |
| GET | `/api/shops/{shop}/import/{preview_id}` | Item outcomes and progress |

Search parameters: `feed_key=products`, `q`, exact `code`, exact `ean`, `manufacturer`, `shop`, `listing=all|listed|unlisted|warnings`, `sort=name|code|manufacturer`, `page=1`, `page_size=50` (maximum 100), `grouped=true`. `q` searches partial names without case or accents, supplier/shop/manufacturer codes and exact EANs. `%` and `_` are literal characters. EANs remain strings, including leading zeros. Multiple filters combine with AND.

The response provides normalized JSON for application use. Full descriptions, all dynamic parameters and original fields are available in the detail response; `source_xml` preserves the original item. `description_html` is sanitized for display, while the original description remains unchanged. Manufacturer contact information from `manufacturer_description` remains available in the source data but is not appended to the displayed or imported description. Safety information is preserved. Summary rows omit long descriptions and full parameter lists to keep browsing small.

Paul Lange currently serves its photographs over HTTP on port 8081, without TLS. Catalog thumbnails, galleries and import previews display them through the Hub HTTPS image route to avoid browser mixed-content failures. This route accepts only `ito5-<alphanumeric>.jpg` filenames at the fixed Paul Lange image origin, refuses redirects and non-JPEG responses, and limits download size (5 MiB), duration and concurrency. Successful images have a one-day private browser cache. Original feed URLs, XML and outgoing Upgates payloads remain unchanged. No shared image component or global browser security setting is changed.

### Explicit variant groups

Groups are recognized only from an explicit source identifier. Similar names, colors or `STA_PARAMS/PRODUCT_NAME` alone do not prove a relationship. The current Paul Lange feed has no explicit group IDs, so its items remain standalone.

For a feed that provides groups, the parent row expands into independently selectable variants with their own images and attributes. Search returns matching variants within a group; the product detail also returns all active siblings. Selecting a group selects the variants currently matching the filters. Selecting all results selects all matching item IDs across pages.

Each ID identifies an actual supplier item. Importing selected variants creates the necessary hidden parent plus **only the selected variants**. The Hub parent code is `{product_prefix}G-{supplier_group_code}`; it is a derived shop identifier, not a supplier SKU. Conflicting codes, inconsistent groups, missing/duplicate variant parameter combinations or more than 100 variants block that group. Adding variants to a parent that already exists in the target shop is intentionally not part of this create-only operation; the group is skipped.

## Preview and confirmation

Northfinder uses the parent `reference` as the explicit group and each variant's `reference` as its SKU. `name_b2c`, color/size attributes, variant/parent images and common features are preserved. `price` is purchase price **without VAT**; `recomended_retail_price` is recommended retail **with VAT**. The default sales coefficient is 1.0. Empty variant prices inherit a positive corresponding parent price and receive individual `inherited_purchase_price`, `inherited_retail_price` or `inherited_discount_price` warnings. Explicit zero or malformed prices are not replaced. An MOC below purchase including VAT receives `retail_below_purchase`; no price is raised automatically.

Repeated Northfinder SKUs are represented by one visibly blocked catalog row with `import_blockers: ["duplicate_supplier_code"]`. All candidate EANs remain searchable in `eans`, but none is chosen as the database's canonical EAN or exported. `source_xml` contains a `conflicting_items` wrapper with each competing record. Ordinary variant source XML contains the original parent context and only that variant, avoiding duplication of all siblings. Importing a selection that includes a blocked variant blocks that selected group; users can select valid variants separately. Manual pricing cannot remove this blocker.

Example preview request:

```json
{
  "supplier": "paul-lange",
  "feed_key": "products",
  "run_id": 42,
  "product_ids": [101, 102],
  "sale_price_overrides": {"101": "119.99"},
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

Optional `sale_price_overrides` maps selected catalog IDs to positive selling prices **including VAT**, with at most two decimal places. An override applies only to this preview and target shop, bypasses the supplier coefficient, and does not change the source MOC or purchase price. It can supply a missing selling price without inventing an MOC. For a shop configured with net prices, the server converts the override using the item's VAT. IDs outside the selection are rejected. `price_lines` returns each item's MOC, net purchase, gross selling price, override state and source warnings. Selling below purchase produces `sale_below_purchase` without changing the requested price.

The preview's **Upraviť predajné ceny** action edits individual products or variants. Typing makes no API calls and disables confirmation until **Prepočítať a overiť náhľad** creates a new verified preview. Clearing an override restores supplier pricing. A failed recheck keeps the draft and cannot confirm the old price. Repricing reuses cached shop settings and incremental code/EAN verification; it does not repeat a manually requested full check. Confirmation uses the frozen verified payload, while stale-source and remote-identity checks remain in effect.

Confirm with `{"preview_id":"<returned ID>"}`. Poll the result URL while `status` is `queued` or `running`. Each product ends as `created`, `exists`, `failed` or `uncertain` (invalid preview items retain `invalid`). The top-level `completed` means processing finished; inspect item outcomes for partial success. Polling returns compact display data in `items[].payload`; the preview contains the exact complete API payload. Both responses include the frozen import options and `prices_with_vat`.

Products are always hidden (`active_yn=false`, language visibility false) and carry `validation_required=1`. Variant visibility is nested under the hidden parent. If the checkbox does not exist, the preview explicitly states that confirmation will create it. Existing incompatible field definitions block the preview. There is no option to publish directly or send stock quantities. Only POST creation is used; existing shop products are not updated.

Price calculations reuse the Paul Lange manufacturer's configured coefficient / existing converter defaults, with Decimal rounding to two places. `pricing=retail` bypasses the coefficient. Target `/config.prices_with_vat_yn` determines whether net or gross feed prices are sent, including purchase prices. Currency conversion is not performed. VAT is taken from the supplier configuration (default 23%, also used by the existing converter), reported as `vat_source=configured`. Inconsistent gross/net/VAT pairs block import for that item. Missing prices never become zero. The first EAN is exported to Upgates; all EANs remain in Hub.

Supplier availability labels come exclusively from `adapter_settings.availability` in the supplier configuration: `orderable` defaults to `do 5 dní`, `unknown` to `overíme`. Missing/blank values use defaults; explicit values such as `do 7 dní` are retained (maximum 100 characters). The active supplier General tab edits both values. Legacy AI availability/hide-zero overrides no longer apply. Zero/unknown supplier stock does not disable cart ordering or hide variants; newly created parents still remain hidden with `validation_required=1`. Existing selected-field availability updates never change cart/active/stock. Previews freeze the effective labels; changed config or an older preview without that snapshot requires a fresh preview before a new write. Uncertain-result reconciliation remains read-only against the original payload.

Supplier stock is informational: `6+` means `supplier_stock_min=6`, not an exact quantity. Text external availability is a boolean plus its original text, not a guessed quantity. Catalog refresh and import registration create no stock balance or stock movement. Outgoing payloads reject stock fields recursively.

Preview/result files are persisted under `shops/{shop}/catalog-imports/`. Each shop has a process-safe import lock. Item state changes are written to a durable journal, so large imports do not rewrite all descriptions on every update. The item is marked uncertain before an external POST; a timeout is reconciled by reading the shop, never by blindly retrying the POST. Created products must be read back as hidden with the review flag and selected variants before local registration. A database failure after shop creation can be reconciled without another POST.

`{"preview_id":"...","retry_failed":true}` verifies uncertain items and retries items that were not sent. Uncertain items still absent from the shop remain uncertain, because a delayed request may finish later; review them before starting a new preview. A process restart is reported as an interrupted job after the lock is released. Images are downloaded asynchronously by Upgates, so a successful product creation does not prove image processing has finished.

## Configuration and deployment

### Sloger listing feeds

Implemented in code: Sloger's `products/product` XML and nested `options/option` variants. Configure one `products` source with `catalog_parser: "sloger"`, `mode: "remote"` and `remote.urls` containing the brand listing URLs. The Sloger Product feed tab accepts one URL per line. All brand snapshots form one atomic catalog; an unavailable, empty or malformed source fails the refresh and retains the previous successful catalog. Use the catalog's manufacturer filter to browse a brand. A single remote URL remains supported.

Sloger aggregate GET downloads wait one second between brands. HTTP 429 permits at most three attempts per source, honoring `Retry-After` seconds or HTTP dates; missing/invalid headers use 5- then 10-second delays. A requested delay above 60 seconds aborts without an early retry. Waiting counts against the existing whole-refresh deadline, and retries retain the combined byte limit and atomic catalog contract. POST and other suppliers do not receive these retries. Final download errors identify the source position without disclosing its URL.

The combined catalog is intentional: supplier codes can collide across brands. Parent `<id>` defines an explicit family, while actual option `<code>` remains the supplier SKU. Repeated SKUs become blocked rows with all competing source evidence; neither an arbitrary EAN nor a replacement SKU is invented. Similar names do not create groups. Explicit option labels such as `Veľkosť M` define that axis when the family agrees; other labels remain the `Variant` value. Missing or ambiguous variant labels block the affected import. Empty/all-zero EANs remain unknown.

`price_msrp` is EUR DMOC including VAT; `price_msrp_czk` is not used for the EUR catalog. The configured VAT rate derives net retail. A missing variant price may inherit a positive parent DMOC with a warning; an explicit zero or malformed value remains invalid. The listing feeds contain no purchase prices or availability. Purchase fields remain null locally and `prices[].price_purchase` is omitted from Upgates create payloads. Neither a zero acquisition cost nor supplier/physical stock is generated.

**Dodávatelia → Sloger → Ceny** edits `adapter_settings.price_coefficients` by manufacturer. Initialize the actual manufacturer names to `1` for unchanged DMOC. A later coefficient affects newly prepared configured-price imports, not existing products or already frozen previews. Missing brand coefficients default to `1`; Paul Lange's converter defaults apply only to Paul Lange. Manual sale overrides and the explicit retail-only option retain their existing behavior.

For the requested Sloger operation, set `adapter_settings.availability.orderable` to `do 2-4 dní` and leave unknown at `overíme`. Listing alone cannot establish that a product is orderable from supplier stock. Any future authenticated B2B observations must include their verification time and remain distinct from actual receipt/FIFO acquisition costs. No B2B login or crawler is enabled by this change.

No migration or new dependency is required. These are implementation/configuration contracts, not proof of live activation or a successful remote product creation. Local verification on 12 October 2026 parsed all 38 supplied brand snapshots into 14,702 unique supplier-code rows. The snapshots contained 26 conflicting codes; full-family payload checks also detected ambiguous variants and a zero-price family. Live configuration, import preview, remote creation and readback must be checked separately through the existing workflow. Rollback retains catalog records and imported shop products; remove the multi-source configuration before reverting to code that does not understand it.

### SK Profi Bike listing feeds

Configure supplier `sk-profi-bike`, prefix `SKP-`, and a products source with `catalog_parser: "sk-profi-bike"`. The native XML parser reads parent descriptions, manufacturer, category, parameters and pictures together with each `options/option` SKU, EAN, explicit axes, prices and stock. A single option without axes is standalone; empty parents do not create products. Pictures restricted to an option's axes never leak to sibling variants. Duplicate supplier codes block import. Existing products matched by the shared identity resolver retain their canonical SKU.

`voc` and a supplied `voc_akcia` are purchase prices **without VAT**; the latter is the current promotional purchase offer. An invalid promotional value remains unknown rather than falling back to the regular price. `moc` is regular retail **with VAT**; `akcia` is retained as a separate promotional retail price. The default configured selling price is MOC × 1; existing manufacturer coefficients and manual edits remain available. VAT must be explicitly configured (the CSV `dph` field is not a percentage). Supplier offers never rewrite historical FIFO acquisition costs.

Use the existing editable import draft: feed data first, optional AI enrichment, Hub save, then the selected shop preview/send. This supplier has no Sloger B2B dependency. The full listing XML can also serve as the availability source. The public simplified stock XML is supported separately, but its assortment can differ: missing SKUs stay unknown or expire, never become zero. Negative or malformed quantities are unknown. See [supplier availability](supplier-availability.md) and [SK Profi Bike invoices](receiving-identity.md#sk-profi-bike-pdf-invoices).

Synthetic tests cover identities, prices, variants, images and stock. A private 2026-10-13 replay parsed 3,947 options in 1,688 sellable families from nine brands and built every family payload; 141 listing SKUs were absent from that day's simplified stock feed. These are snapshot findings, not fixed catalog-size assumptions. URLs containing access tokens and supplier documents must stay outside Git. No API ordering or automatic stock publication is enabled by this adapter.

### Schindler listing and availability feed

Configure supplier `schindler`, product prefix `SCH-`, default shop `biketrek`, and the supplied Slovak/EUR export as the remote `products` source. The parser is selected by supplier code, or explicitly with `catalog_parser: "schindler"`. Keep the private export URL in the supplier configuration, never in Git. No B2B password is required for downloading that URL. The export is a gzip attachment without HTTP `Content-Encoding`; the shared downloader detects its magic bytes, decompresses it once, and enforces the byte limit on both the compressed and expanded feed. Corrupt/truncated archives and oversized expansion fail before a completed snapshot is installed.

The supplier's [official XML specification](https://docs.google.com/document/d/1z6LS21WxrFgPLSHOaALIfByLBI6oecjvRfoNWc5XZ6A) defines these fields:

| Source | Hub meaning |
| --- | --- |
| `CODE` | Supplier SKU, preserving leading zeros; the new canonical code uses the configured `SCH-` prefix. `ITEM_ID` is not a SKU. |
| `PRODUCTNAME`, `MANUFACTURER`, `EAN` | Product name, brand and EAN. A missing EAN stays unknown and warns. |
| `DESCRIPTION`, `DESCRIPTION_SHORT` | Long HTML and short description; outgoing HTML uses the existing sanitizer. |
| `IMGURL`, repeated `IMGURL_ALTERNATIVE`, `PARAM` | Images and named technical parameters. |
| `CATEGORYTEXT` | Full supplier category path. Map it to the actual target shop's category code in **Mapovanie feedu**; no target code is inferred from a similar name. |
| `PURCHASE_PRICE`, `VAT` | Purchase **including VAT**, with net purchase derived using Decimal and cent rounding. Feed VAT is required. |
| `CUSTOMER_PRICE` | Retail **including VAT**, used by the existing configured-price import; the default coefficient is 1. |
| `CATALOGUE_PRICE` | Separate manufacturer's reference price, retained in the raw source. It is not a purchase discount or an automatically created shop meta field. The existing importer uses `CUSTOMER_PRICE` as its retail/common price. |
| `ITEMGROUP_ID`, `VARIANT` | Explicit family and option label. A family containing just one SKU becomes standalone with a fixed parameter. Similar names alone never create families. |
| `YEAR`, `AVAILABILITY_DATE`, `DELIVERY_DATE` | Preserved source facts; an expected delivery date never proves stock. |

Schindler's `WAREHOUSES` distinguishes `central`, `internal` and `external`. Central/internal quantities are aggregated as **the supplier's** immediately or shortly dispatchable stock; external stock remains separate. `IS_GREATER_THAN=true`, `VALUE=10` proves at least 11 whole pieces, not exactly 10. Aggregating any lower bound retains minimum semantics. If no warehouse detail exists, `STOCK_ITEM` supplies the declared quantity, including an explicit zero; missing values remain unknown. Unknown warehouse names or malformed counts reject that availability row rather than fabricate a quantity.

The complete products feed can also serve as the regularly refreshed availability source. Its stock reader is independent of catalog name/price validation: valid observations survive other rejected/unknown/duplicate rows through the shared partial-feed report and expiry rules. A structurally broken XML/download still fails as a whole. This does not create receiving movements, initialize our physical stock, change FIFO costs or activate shop stock publication. Configure and verify a manual availability refresh before enabling its regular schedule.

Use the normal catalog → editable import draft → Hub save → BIKETREK preview/send workflow. New listings remain hidden and marked for review. Existing identity matching and variant validation are unchanged; repeated/missing option labels block the affected preview until reviewed. No new database migration or dependency is required.

Private snapshot verification on 15 October 2026 parsed 14,934 supplier SKUs, 236 category paths and 2,383 multi-SKU families; all 14,934 stock rows were readable. The snapshot had 4,193 missing EANs, 906 missing image sets, 130 retail-below-purchase warnings, 34 families with repeated option labels and one missing option label. Building all 6,825 family/standalone payloads produced 6,790 ready previews and 35 blocked previews for ambiguous/missing variant attributes. These are observations of one export, not catalog-size assumptions. The supplier's signed-in B2B page confirmed one gross/net purchase pair against the feed. Synthetic tests cover prices, leading-zero identities, grouping, hidden no-stock payloads, partial stock acceptance, external/minimum stock, gzip limits and damaged archives. Live category mappings, scheduled refresh and a remote trial import must be verified separately; local parsing alone does not prove them.

### Configuration and rollout

No database migration is required for these catalog adapters. Existing catalog tables and the existing `004_shop_product_content.sql` table are used. The configured supplier download URL and auth stay in the existing supplier configuration. The catalog explicitly chooses `products` (or another listing source); it never implicitly chooses the supplier's current stock feed. Downloaded XML snapshots remain in the existing supplier feed directory, with successful/failed indexing recorded in PostgreSQL. The separate SK Profi Bike invoice adapter adds `pypdf` as a runtime dependency.

Paul Lange reads its existing `adapter_settings.mapping.postprocess.product_code_prefix`, `price_coefficients`, `vat`, and default category. Optional `adapter_settings.catalog` keys are `currency`, `parser`, `group_tag` (default `ITEMGROUP_ID`), and `variant_parameters` (names of explicit variant attributes). Northfinder reads its existing `adapter_settings.northfinder_feed` field mapping, the configured product prefix and VAT; its listing parser accepts the actual `products` XML root or a `root` wrapper. A source can set `catalog_parser` individually, allowing supplier/manufacturer feeds with different parsers later. Unsupported sources display a clear unavailable state.

Merge triggers the existing production build. First verify the feed download, search with/without accents, detail/XML and preview on the deployed Hub. A live create requires an explicitly selected test product. Verify its hidden visibility, review flag and images in the target shop, and verify stock remains unchanged. No live shop product has been created as part of automated tests.

Rollback: revert the feature commit and deploy through the usual approved PR flow. Existing CSV behavior is preserved. Catalog snapshots and audit files can remain on disk/in the existing tables; reverting code does not delete products already explicitly imported to a shop.

Tests use synthetic fixtures and mocked Upgates responses. PostgreSQL integration tests require `CATALOG_TEST_DATABASE_URL` pointing to a dedicated localhost database ending in `_catalog_test`; CI supplies an ephemeral PostgreSQL 16 service. They never fall back to production settings. `node frontend/tests/catalog-ui.cjs` checks the actual React interactions in jsdom (a development-only dependency); it does not verify browser layout.

Reference contracts: [products](https://docs.upgates.com/api-reference/produkty), [product lists](https://docs.upgates.com/api-reference/produkty-seznamy), [shop settings](https://docs.upgates.com/api-reference/nastaveni-eshopu), [custom fields](https://docs.upgates.com/api-reference/vlastni-pole).

### Fixed attributes and singleton families (15 October 2026)

A complete feed family with only one actual SKU is normalized to a standalone item; fixed colour/size stays in its product parameters. The full-feed count, not the current selection count, determines this. Preparation also handles old cached singleton groups before canonical identity resolution, including receiving sources. Real families retain their explicit relationships and all exported variants have `main_yn=false`. SK Profi Bike's one-option records no longer become groups merely because `atribute_1`/`atribute_2` are named. Existing persisted drafts and shop products are not restructured automatically.


### Cyklo Zitny customer-discount export

Use supplier code `cyklo-zitny`, prefix `CZ-`, home shop `biketrek`, destination currency EUR and `adapter_settings.vat_rate: 23`. Select `catalog_parser: "cyklo-zitny"` (also selected by supplier code). Configure the privately supplied **complete customer-discount export, version 3**, as `products`. The same complete URL is recommended for `stock`; keep API keys in supplier configuration, never in Git. Downloading the export requires no B2B password.

The XML has sibling `items type="categories"` and `items type="products"` containers. Inspection selects only products, including an explicit `export/items/item` record path. The native category picker resolves the dictionary and offers exactly the deepest complete source category selected for each product, with its actual code and readable hierarchy. Ties retain supplier order. Missing ancestors/cycles are not invented. Parallel category ID/text arrays are not zipped because legacy IDs can lack a text entry. Original arrays and descriptions remain in the source snapshot.

| Source | Normalized value |
| --- | --- |
| `code` | Exact raw supplier SKU, including leading zeros and punctuation; `id` is not a SKU |
| `ean` | Source EAN, retaining leading zeros |
| `price_buy_novat_eur` | EUR acquisition price without VAT, after the customer discount in export 3 |
| `price_moc_eur` | EUR recommended gross retail price under the default `preserve_gross` policy |
| `store_count` | Supplier quantity, including explicit zero; never local physical stock |
| `images/url` | Supplier images; only valid HTTP(S) URLs |
| `description`, `description_full` | Short text and full description/HTML |

The source also contains CZK columns and Czech VAT 21%. They remain raw evidence; they cannot be relabelled as EUR or as Slovak VAT. The configured destination VAT is explicit and required for derived prices. `preserve_gross` retains the supplied EUR gross MOC and recalculates its destination net base; acquisition net remains unchanged. Optional `adapter_settings.catalog.retail_vat_policy: "preserve_net"` instead multiplies `price_moc_novat_eur` by destination VAT. Example: purchase net 4.03 EUR and MOC gross 8.38 EUR at destination VAT 23% become purchase gross 4.96 and retail net 6.81. No live existing shop prices are changed by catalog refresh. Missing/invalid EUR columns never fall back to CZK or zero.

There is no explicit family identifier or variant structure in this export. Items remain standalone regardless of similar names, internal IDs or repeated categories. Subsequent imports use ordinary duplicate checks, existing-card links and the hidden-product preview/send workflow.

The abbreviated availability export contains internal `id` and `delivery_date`, without supplier SKU or quantity. It is deliberately refused as a quantity source. Use the complete customer-discount export for availability, optionally with hourly scheduling. This reuses independent stock parsing: malformed prices do not block valid stock rows; malformed quantities, duplicate identities and missing codes receive normal row diagnostics. Delivery code `0` never becomes a fabricated count of one, and code `3` is not interpreted as a three-day delivery promise. Missing products preserve their previous observation only until its original expiry.

Local source analysis on 15 October 2026 found 2,671 products, 388 dictionary entries and 296 selected source categories; 1,781 rows had positive stock and 890 explicit zero. All 2,671 rows passed pure import-payload validation with a synthetic target category and all stock rows were accepted; 29 rows lacked EAN and 21 lacked images. These checks do not establish a saved production configuration, real shop category assignment or a completed remote import. No migration is added and deployment does not itself create/configure this supplier or enable stock authority.

### Silvini listing; update feed and price basis pending verification

Use supplier code `silvini`, prefix `SIL-`, EUR and explicit destination VAT 23%. Select `catalog_parser: "silvini"` for the supplied listing source. URLs and B2B credentials stay in private supplier configuration. The adapter reads `SHOP/SHOPITEM/VARIANTS/VARIANT`: exact `CODE` is the supplier SKU, `EAN` remains text, and `PAIRCODE` explicitly groups the parent's variants. `STYLE` or similar names never join separate families. Real colour/size parameters come from each variant's named `PARAMETERS`. A one-SKU family becomes standalone with fixed parameters. Parent descriptions, composition, gender, features, materials, sports, manufacturer and supplier image URLs remain available.

The source repeats whole families for individual sizes. Equal variant records under equal parent context are deduplicated by exact SKU, merging supplier images. Parent `WEIGHT` varies among repeated records but has no association to a particular variant; it is retained as raw `parent_weights`, never assigned to a leaf or exported. Conflicting EAN, prices, parameters, family or other parent context block the affected SKU with `duplicate_supplier_code`; other identities remain usable. Each normalized raw record contains only its own leaf, so later mapping cannot correlate parallel sibling code/EAN/price arrays. Category inspection uses the same `DEFAULT_CATEGORY` strings as normalization and counts unique SKUs, including those with import blockers.

The supplied listing contains no VAT field establishing whether `PURCHASE_PRICE` or `STANDARD_PRICE` is net or gross. Until independently confirmed, prices remain unset with a visible `price_basis_unconfirmed` warning; raw values are retained. Set `adapter_settings.catalog.purchase_price_basis` and `retail_price_basis` to `net` or `gross` only after verification. Configured VAT then derives the other basis with Decimal cent rounding. An enabled `DISCOUNT_PURCHASE_YESNO` selects `DISCOUNT_PURCHASE_PRICE`; a bad active discount never falls back to regular purchase. Regular `STANDARD_PRICE` remains the MOC source, while all promotional retail fields remain raw evidence. These are current supplier offers, not historical FIFO costs or updates of existing shop prices.

The listing is not an authorized availability source: normalized quantities/availability remain unknown with `supplier_stock_unverified`. Availability scheduling/parsing for `silvini` is explicitly unavailable until an update schema is obtained and verified. An `INFO` response is not an empty successful stock snapshot; neither the listing nor an unexpected update document can silently substitute for a supported quantity source.

Synthetic tests cover duplicate identities, explicit families, singleton attributes, scoped raw records, category mapping, configured price bases and refusal of unverified availability. Operational feed findings and account details remain outside this repository. No migration or additional dependency is required; deployment does not itself configure suppliers, create shop products or activate synchronization.

### Bike Brothers

Use supplier code `bike-brothers`, prefix `BRO-`, home shop `biketrek` and explicit `adapter_settings.vat_rate: 23`. Select `catalog_parser: "bike-brothers"`. Configure the privately supplied authenticated API export as `products` and, separately, `stock`. Keep the export token and B2B credentials outside Git. The ordinary download link can require a signed-in B2B session; the API source does not require copying browser cookies into Hub.

The native parser reads `SHOP/SHOPITEM`. `kod` is the exact supplier SKU, including leading zeros; `ean` remains text. Internal `id` and a child's `mastergroup` establish explicit parent relationships. A row referenced as a parent is context only: it is not another sellable product and its aggregate stock is never accepted as a leaf observation. Parent descriptions, brand, category and images can fill missing child content; EAN, EUR prices and quantity always belong to the child. Parent context is stored separately from leaf XML. Nested/cyclic families and duplicate internal IDs are refused rather than flattened. Stable shop parent codes use the ordinary `BRO-G-<mastergroup>` convention even when the source omits a parent row.

The export has no separate size/colour axes. Explicit groups retain each exact supplier title as a neutral `Variant` label with `variant_label_from_title`; the parser never guesses colour or size from punctuation. One-child families become standalone. Repeated EANs on different leaf codes block those identities visibly until reviewed. Source category strings remain exact; brand categories must not silently stand in for a specific product type. The graphic mapper and category CSV workflow remain available, and their source counts exclude parent rows.

`cena_bez_dph_eurvoc` is EUR purchase **without VAT**, and `cena_bez_dph_eurmoc` is EUR retail **without VAT**. Destination VAT explicitly derives gross amounts with Decimal cent rounding. CZK columns are retained as source evidence but are never relabelled as EUR or substituted for missing EUR values. A catalog refresh does not change existing shop prices, physical stock or FIFO history.

Check the language of every supplied export. Prefer a verified Slovak export when available. The currently supported source schema has one set of text fields; `popis_1`, `popis_2` and `popis_3` are content blocks, not language selectors. They are combined without duplicate blocks. Set `adapter_settings.catalog.source_language` to the independently verified `cs` (default) or `sk`. Czech sources retain original facts and display `source_language_cs`: use the existing editable import/Slovak AI preparation to translate names, descriptions and text parameters before sending. Changing the language setting or the target shop's language does not itself translate content. Never present source Czech text as a completed Slovak translation.

Availability reads only leaf `kod`/`stav_zasob`, independently of prices and descriptions. Exact zero is meaningful; missing/invalid quantities remain unknown or rejected with per-row diagnostics. `dostupnost_dny=0` is not an alternative quantity. Missing SKUs do not imply zero. Synthetic tests cover financial conversion, parent aggregates, identity conflicts, language warnings, partial stock acceptance and native mapping. No migration, dependency or automatic production stock authority is added; live setup, refresh and a hidden trial import require separate operational verification.
