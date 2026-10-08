# Unified supplier product import

Implemented in code: one durable, editable preparation for AI and ordinary feed imports. Open **Dodávatelia → Produkty dodávateľa**, select explicit products and the target shop, then open **Náhľad importu**. The supplier feed mapping is applied first. This preparation does not create shop products.

## Operator flow

1. Review the spreadsheet of selected items. Identity, names, manufacturer, images, descriptions, SEO, prices, VAT, availability text, leaf category, product parameters, variant attributes and custom fields are editable. Parent categories are calculated from the chosen leaf; system menu roots are excluded. Variant relationships originate only from explicit supplier groups.
2. Save the draft cells. Batch edits use the same validation as individual cells. A family shares its parent descriptions, SEO, manufacturer, category, common parameters and custom fields. Conflicting edits to one shared field in the same batch are rejected.
3. Optionally choose AI for the selected families. Prepare the estimates, explicitly confirm paid processing, then apply completed valid results to the same spreadsheet. Manually edited cells and cells changed while AI was running retain their values. A mapped or manually chosen category is retained; AI category classification is a fallback for an empty category. Changed product identifiers require a fresh AI preparation.
4. **Save products in Hub** creates local products and identifiers, supplier links and local merchandising values. It creates no shop mapping, physical quantity, stock balance or stock movement. Existing canonical products are reported and retain their current data. Subsequent changes to an existing product belong to the ordinary product editor.
5. Prepare a separate shop publication preview, review its exact payload and explicitly send it. This reuses the existing create-only importer, duplicate checks, hidden product/review flag, durable write journal and readback. Existing shop products are skipped; this is not an update API. An unknown write is reconciled by reading, never by an automatic repeated POST.

The SKU remains derived from the configured supplier prefix plus supplier code. Product identity is fixed after saving it in Hub; later identity changes use the existing product editor and its conflict checks. All prices are explicit decimal strings. Purchase is without VAT; retail and selling prices are with VAT. The selected shop's VAT mode determines the wire conversion. Missing prices do not become zero. Supplier availability describes lead time and does not imply our physical stock.

The manufacturer code remains a Hub/AI source fact: the documented Upgates create/update schema has no native writable manufacturer-code field. Map it to an existing custom field when it must be sent to the shop. The editable SEO URL uses `descriptions[].seo_url`; the full `url` returned by GET is not sent as an input field. See [official product API](https://docs.upgates.com/api-reference/produkty).

SEO title and description use explicit feed mappings when available, with plain feed name/description fallbacks. No model is called to prepare ordinary feed imports. Custom fields must exist in the target shop; missing definitions block the publication preview. Existing H1 content fields reuse the AI importer's definition/creation mechanism.

## Durable data and concurrency

Migration `022_product_import_drafts.sql` adds the replay-safe `product_import_drafts` table. Its JSON document retains the full frozen source, mapping revision/provenance, typed cell values, manual field markers, category tree, linked AI jobs, saved product IDs and explicit publication preview. New source refreshes do not replace an existing draft snapshot. A new mapping after ingest can require an explicit catalog remap before preparing another draft, as documented in the feed mapping workflow.

The creation request UUID is idempotent for the same input. Every mutation uses `expected_revision`; stale requests receive `import_draft_changed` and HTTP 409. Draft edits during a queued/running/unknown shop write are blocked. Publication rechecks the saved document hash before sending. A changed category ancestry requires a fresh preview. The existing product identity advisory lock protects local product creation; saving twice does not create two products. Local editor revisions prevent a later draft save from overwriting an intervening product editor change.

Full saved merchandising cells are retained in the draft and `product_editor_overrides.data.import_fields`. Existing common/variant editor fields also receive their typed effective values. `Product.supplier_feed_data` retains the source snapshot. A `ShopProduct` row is created only by the existing importer after verified remote creation, never by draft preparation or local save.

AI jobs use the existing model, published rules, provider, budget reservation and restart recovery. They carry `context.staging_id`; worker preparation stops with content ready for the draft and never queues shop creation. The legacy AI import, retry, fork and selected-field update routes reject staged jobs. The generic catalog confirmation route rejects a preview linked to a staged draft. Closing the browser cannot lose a committed draft or authorize a new paid retry.

## API

All endpoints require operator access; cookie writes use existing same-origin protection. Responses are `no-store`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/product-imports` | Latest 100 saved preparations |
| POST | `/product-imports` | Create/replay a selection snapshot |
| GET | `/product-imports/{id}` | Spreadsheet, AI summaries and publication result |
| PUT | `/product-imports/{id}` | Atomic typed row patches with CAS |
| POST | `/product-imports/{id}/save` | Save canonical local products, no remote writes |
| POST | `/product-imports/{id}/ai` | Freeze AI estimates from current mapped cells |
| POST | `/product-imports/{id}/ai-start` | Explicitly confirm estimates and reserve budget |
| POST | `/product-imports/{id}/ai-apply` | Accept valid AI output into untouched cells |
| POST | `/product-imports/{id}/publish-preview` | Verify exact create-only shop payload |
| POST | `/product-imports/{id}/publish` | Explicitly confirm/reconcile that preview |

Selection is limited to 500 actual supplier items per draft. UI batch editing does not change that selection. Each row retains its catalog ID and immutable family relationship. HTTP mutation bodies and typed cell definitions live in `product_import_types.py`; business logic is in `services/product_import.py`. The existing `catalog_import.py` accepts frozen sources only via an internal staging argument, not an arbitrary public JSON payload.

## Validation and rollback

`test_product_import.py` checks Decimal price conversion, category ancestors, manual/late-edit precedence, identity changes, missing data, batch CAS and the legacy route boundaries. `test_product_import_db.py` checks replay-safe migration, durable creation, local-only save, explicit publication of edited data after a feed refresh, existing-product preservation and the staged AI path with fake providers. Database cases require the isolated localhost `*_catalog_test` harness; they never use production credentials or send live products.

Deployment packages and runs migration 022 after feed mapping migration 021. Both are additive and do not activate shop or stock writes. Rollback preserves both tables and all audit data. Before reverting to a worker without staging awareness, cancel queued staged AI jobs and finish/reconcile any explicitly confirmed shop publication; an old worker must not interpret a staged job as ordinary auto-import.
