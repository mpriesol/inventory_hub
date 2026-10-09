# Unified supplier product import

Implemented in code: one durable, editable preparation for AI and ordinary feed imports. Open **Dodávatelia → Produkty dodávateľa**, select explicit products and the target shop, then open **Náhľad importu**. The supplier feed mapping is applied first. This preparation does not create shop products.

## Operator flow

The catalog starts with **Automaticky podľa mapovania dodávateľa**: each selected product receives its shop-specific supplier category mapping. The legacy supplier fallback (such as TEMP) is not submitted as a manual override. An intentional category choice in catalog options still overrides mapping for the whole selection; clearing it restores automatic mapping. Products without a mapping remain unassigned until a manual or AI category choice. Existing draft snapshots are not rewritten.

1. Review the spreadsheet of selected items. Identity, names, manufacturer, images, descriptions, SEO, prices, VAT, availability text, leaf category, product parameters, variant attributes and custom fields are editable. Parent categories are calculated from the chosen leaf; system menu roots are excluded. Variant relationships originate only from explicit supplier groups.
2. Save the draft cells. Batch edits use the same validation as individual cells. A family shares its parent descriptions, SEO, manufacturer, category, common parameters and custom fields. Conflicting edits to one shared field in the same batch are rejected.
3. Optionally choose AI for the selected families. The editable `ai_category_profile` defaults to `auto` and can be set for a family or in bulk. Choose **Pripraviť AI obsah** to freeze the current rules and their four automation flags. The job starts immediately when `show_cost_estimate=false`; otherwise its estimate awaits confirmation. Valid policy-approved results are applied to the same spreadsheet automatically in newly prepared jobs, unless the draft was edited in the meantime. Manually edited cells and cells changed while AI was running retain their values. A mapped or manually chosen leaf category is retained: an unambiguous category/profile mapping selects its rules without a classifier call; otherwise AI selects only the content profile. A selected parent branch is refined to one of its leaves. For an empty category, AI selects both a leaf and its profile. Deliberately clearing a category before preparation enables this automatic choice; clearing or changing it after preparation still pauses automation and rejects a stale result. Explicit expert profiles are preserved. Changed product identifiers require a fresh AI preparation.
4. **Save products in Hub** creates local products and identifiers, supplier links and local merchandising values. It creates no shop mapping, physical quantity, stock balance or stock movement. Existing canonical products are reported and retain their current data. Subsequent changes to an existing product belong to the ordinary product editor.
5. With AI enabled, first apply its results to the table (or explicitly turn AI off to use the feed). Both preview and first publication confirmation enforce this on the server; approval alone is not table acceptance. Prepare a separate shop publication preview, review its exact payload and explicitly send it. This reuses the existing create-only importer, duplicate checks, frozen visibility policy, durable write journal and readback. When every row belongs to a fresh approved AI preparation and every job has `confirm_import=false`, the worker performs local save, preview and publication automatically. Mixed AI/feed selections or any confirmation-enabled job retain manual publication. Existing shop products are skipped; this is not an update API. An unknown write is reconciled by reading, never by an automatic repeated POST.

The SKU remains derived from the configured supplier prefix plus supplier code. Product identity is fixed after saving it in Hub; later identity changes use the existing product editor and its conflict checks. All prices are explicit decimal strings. Purchase is without VAT; retail and selling prices are with VAT. The selected shop's VAT mode determines the wire conversion. Missing prices do not become zero. Supplier availability describes lead time and does not imply our physical stock.

The manufacturer code remains a Hub/AI source fact: the documented Upgates create/update schema has no native writable manufacturer-code field. Map it to an existing custom field when it must be sent to the shop. The editable SEO URL uses `descriptions[].seo_url`; the full `url` returned by GET is not sent as an input field. See [official product API](https://docs.upgates.com/api-reference/produkty).

SEO title and description use explicit feed mappings when available, with plain feed name/description fallbacks. No model is called to prepare ordinary feed imports. Custom fields must exist in the target shop; missing definitions block the publication preview. Existing H1 content fields reuse the AI importer's definition/creation mechanism.

## Durable data and concurrency

Migration `022_product_import_drafts.sql` adds the replay-safe `product_import_drafts` table. Its JSON document retains the full frozen source, mapping revision/provenance, typed cell values, manual field markers, category tree, linked AI jobs, saved product IDs and explicit publication preview. New source refreshes do not replace an existing draft snapshot. A new mapping after ingest can require an explicit catalog remap before preparing another draft, as documented in the feed mapping workflow.

The creation request UUID is idempotent for the same input. Every mutation uses `expected_revision`; stale requests receive `import_draft_changed` and HTTP 409. Draft edits during a queued/running/unknown shop write are blocked. Publication rechecks the saved document hash before sending. A changed category ancestry requires a fresh preview. The existing product identity advisory lock protects local product creation; saving twice does not create two products. Local editor revisions prevent a later draft save from overwriting an intervening product editor change.

Full saved merchandising cells are retained in the draft and `product_editor_overrides.data.import_fields`. Existing common/variant editor fields also receive their typed effective values. `Product.supplier_feed_data` retains the source snapshot. A `ShopProduct` row is created only by the existing importer after verified remote creation, never by draft preparation or local save.

AI jobs use the existing model, published rules, provider, budget reservation and restart recovery. They carry `context.staging_id`. Historical staged jobs keep the original manual apply/publication boundary. Fresh preparations additionally freeze `staging_automation_version=1` and a draft authorization containing their exact job IDs. Only these jobs may automatically apply approved output or queue the existing staged importer under inherited policies. The legacy AI import, retry, fork and selected-field update routes reject staged jobs. The generic catalog confirmation route rejects a preview linked to a staged draft. Closing the browser cannot lose a committed draft or authorize a new paid retry. Automatic completion locks the draft before its jobs, waits for every selected row, and pauses on any user edit after preparation. The per-family `active_after_import` value is saved in `ai_policy`, included in the content hash and checked again at publication. Reopening an already applied AI result can replace only cells still equal to the same job’s recorded AI values; manual edits remain protected. The immutable preparation baseline is retained. Older version-1 drafts without this snapshot require an exact saved content revision matching their applied digest; unproven values are never adopted as AI-owned. An existing failed/partial/uncertain publication is reconciled through its original journal; automation never builds a new preview to repeat it.

## API

All endpoints require operator access; cookie writes use existing same-origin protection. Responses are `no-store`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/product-imports` | Latest 100 saved preparations |
| POST | `/product-imports` | Create/replay a selection snapshot |
| GET | `/product-imports/{id}` | Spreadsheet, AI summaries and publication result |
| PUT | `/product-imports/{id}` | Atomic typed row patches with CAS |
| POST | `/product-imports/{id}/save` | Save canonical local products, no remote writes |
| POST | `/product-imports/{id}/ai` | Freeze AI jobs and inherited policies; start when the estimate gate is off |
| POST | `/product-imports/{id}/ai-start` | Explicitly confirm estimates and reserve budget |
| POST | `/product-imports/{id}/ai-apply` | Accept valid AI output into untouched cells |
| POST | `/product-imports/{id}/publish-preview` | Verify exact create-only shop payload |
| POST | `/product-imports/{id}/publish` | Explicitly confirm/reconcile that preview |

Selection is limited to 500 actual supplier items per draft. UI batch editing does not change that selection. Each row retains its catalog ID and immutable family relationship. HTTP mutation bodies and typed cell definitions live in `product_import_types.py`; business logic is in `services/product_import.py`. The existing `catalog_import.py` accepts frozen sources only via an internal staging argument, not an arbitrary public JSON payload.

## Validation and rollback

`test_product_import.py` checks Decimal price conversion, category ancestors, manual/late-edit precedence, identity changes, missing data, batch CAS and the legacy route boundaries. `test_product_import_db.py` checks replay-safe migration, durable creation, local-only save, explicit publication of edited data after a feed refresh, existing-product preservation and the staged AI path with fake providers. Database cases require the isolated localhost `*_catalog_test` harness; they never use production credentials or send live products.

Deployment packages and runs migration 022 after feed mapping migration 021. Both are additive and do not activate shop or stock writes. Rollback preserves both tables and all audit data. Before reverting the policy-aware staged worker, stop new AI preparation and finish or explicitly cancel pending automatic publications; keep uncertain writes for readback/reconciliation. Before reverting to a worker without staging awareness, cancel queued staged AI jobs and finish/reconcile any explicitly confirmed shop publication; an old worker must not interpret a staged job as ordinary auto-import.

## AI and delivery status (8 October 2026 correction)

AI `ready` on a draft-linked job means **AI content ready**, and `completed` means **applied to the table**. Neither asserts shop delivery. Job list/detail responses now include `staging` with the parent draft, linkage and separately derived publication status. The parent draft and history expose `publication_state`; rows expose `publication_status`. These values come from the existing durable importer result, scoped to the linked product IDs, without rewriting AI events or inferring delivery from the product's mere existence.

The AI detail links to its exact saved preparation/result; table rows link back to the corresponding AI detail. Staged jobs do not offer legacy import, fork or selected-field update actions. Already approved, unchanged content does not repeat the approval button. Completed publication replaces preview instructions and controls with the saved result. If an older import sent the table before accepting AI, the UI explicitly reports that the AI result was not applied; it does not pretend those texts reached Upgates.

A fully completed create-only draft is closed to cell changes, Hub saves and further AI application. Repeated preview/confirmation of that same final result returns it without creating a new preview or sending another request. Further product changes belong to the product editor. Partial and uncertain results retain the existing recovery path against the original confirmed snapshot; recovery does not require a newly edited AI job to be reapproved.

No migration, new AI generation or automatic shop content update is part of this correction. Existing saved preparations receive the corrected display on read. Validation covers synthetic UI handoffs, delivery/AI separation, duplicate-free final-result replay and the isolated-database applied-AI/publication path.
