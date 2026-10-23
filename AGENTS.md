# Inventory Hub developer agent instructions

These instructions apply to the entire repository.

Use **BIKETREK** and **xTrek** in new documentation and user-facing copy. Keep existing technical identifiers, shop codes (`biketrek`, `xtrek`), URLs and database keys unchanged unless the task requires a migration.

## Repository identity

- Work only in `mpriesol/inventory_hub`.
- Never use, inspect, copy from, or modify `mpriesol/OLD_inventory_hub` unless the user explicitly requests a one-time comparison.
- Treat `main` as the production branch. Create a focused feature branch (prefer `codex/<short-task-name>`) for every change.
- Do not push directly to `main`.

## Project map

- `api/`: FastAPI backend on Python 3.12 with async SQLAlchemy and PostgreSQL.
- `api/inventory_hub/routers/`: HTTP endpoints. Keep business logic out of routers when it can live in `services/`.
- `api/inventory_hub/services/`: reusable business logic.
- `api/inventory_hub/adapters/`: supplier-specific parsers and B2B connectors.
- `api/inventory_hub/db_models.py`, `db_models_ext.py`, `ai_content_models.py`: relational models; a model alone does not prove a complete workflow exists.
- `frontend/`: React 18 + TypeScript + Vite + Tailwind CSS.
- `infra/db-init/`: ordered PostgreSQL schema/migration SQL.
- `.github/workflows/ci.yml`: current PR validation commands, four isolated backend test shards and the required `backend-compile` coverage gate.
- `.github/workflows/build.yml`: parallel image builds and serialized production deployment on `main`; Markdown/docs-only pushes skip this workflow.
- `docs/ci-deployment.md`: CI coverage, timing reports, caches, migration execution and deployment measurement.
- `infra/docker-compose.prod.yml`: deployment reference, not proof of the server's current configuration.
- `OVERVIEW.md`: project map, implementation status and operational context.
- `docs/supplier-catalog.md`: supplier catalog, validation, import and recovery contracts.
- `docs/central-stock.md`: accepted order/manual-line rules and phased central-stock implementation.
- `docs/order-workflows.md`: operator guide for collection, local automation and manual exceptions.
- `docs/order-automation.md`: operational settings, worker contracts, concurrency and recovery.
- `docs/stock-publication.md`: controlled maintenance stock publication, persistent warehouse holds and ambiguous-write recovery.
- `docs/product-editor.md`: unified stock/product spreadsheet, overrides, CAS, selected-field publication and recovery.
- `docs/product-links.md`: explicit existing-card links to shop products, batch review and recovery.
- `docs/supplier-availability.md`, `docs/stock-sync.md`, `docs/availability-sync.md`: supplier freshness, regular stock authority and operator controls.
- `docs/stock-adjustments.md`: physical recounts through immutable corrective movements.
- `docs/receiving-identity.md`: shared receiving/catalog identity, scanner request UUIDs, ambiguity and replay recovery.
- `docs/stock-history.md`: read-only movement history, dashboard facts, date/pagination and cost limits.
- `docs/mvp-progress.md`: approved priorities and implementation handoff; distinguish planned, tested and deployed.
- `docs/fifo.md`: FIFO layers, legacy cutover, nullable costs, physical returns and cost corrections.
- `docs/fifo-purchase-costs.md`: opt-in next-layer product purchase prices, issued-order weighted costs and explicit historical corrections.
- `docs/ai-content.md`: AI preparation, review, selected-field updates, access configuration and recovery.
- `docs/product-feed-mapping.md`: versioned supplier feed mapping, analysis and category rules.
- `docs/product-imports.md`: durable editable import drafts, protected manual values and separate Hub/shop saves.
- `docs/sku-policy.md`: automatic brand-prefixed SKU, scoped source identity and shared card creation.
- `docs/invoice-archive.md`: external supplier/shop invoice archive, manual settlements, accountant ZIP and EAN investigation.
- `docs/warehouse-model.md`: canonical IDs, one source per fact, service cards and explicit units.
- `docs/sku-migration.md`: read-only existing-SKU migration preview, native-ID checks, dependencies and pilot recovery.

There are two data stores. PostgreSQL holds relational business data; the filesystem holds supplier/shop configuration and imported/generated files. Supplier-related changes may need both representations. Do not silently update only one side.

## Task scope and existing authorization

- Follow the current user request and permissions already granted for the project or conversation. User instructions take precedence over these repository guidelines. Do not ask for the same approval again while its scope still applies.
- A request for an audit, prompt or design authorizes that deliverable; it does not request implementation or production mutations. A request to implement authorizes the routine code, UI, API and additive schema work necessary for that task.
- Before work, state a short plan and any deployment consequence. Continue with ordinary implementation decisions inside the authorized scope. Several reasonable technical implementations are not, by themselves, a reason to stop for approval.
- Use already-granted permission for PR merge/automatic deployment or scoped live testing, including product creation, when it covers the current task. If that permission has not been granted or the action exceeds it, finish the reviewable work first and ask only for the missing authorization.
- Live testing permission is limited to the agreed behavior, target shops and products. It does not authorize unrelated bulk changes, deletion of production data, destructive migrations or bypassing the application's validation/review steps. Respect a narrower instruction in the current task.
- Server access must actually be available and authorized. Request the specific missing access or log evidence when needed; do not invent credentials or claim to have inspected the server.
- On 24 September 2026 the owner explicitly reconfirmed automatic PR approval/merge and consequent workflow deployment until revoked. This standing permission covers reviewed implementation PRs and their additive migrations; it does not activate live stock authority or authorize unrelated bulk production writes.
- These rules preserve the scope of the user's authorization; editing this file does not itself grant new permissions.

## Safety boundaries

- Production deployment, SSH, service restarts, production Docker Compose and live write endpoints must remain within the user's existing authorization. Permission to trigger deployment by merging a PR does not automatically grant arbitrary manual server operations.
- Use configured credentials through the normal application or tool mechanism. Do not print, copy into documentation, commit or invent secrets. Do not inspect secret values unnecessarily. Keep `.env`, credentials, B2B passwords, API keys, customer documents, invoices and production data out of Git.
- Destructive deletion or replacement of production data, including `DROP`, `TRUNCATE`, broad `DELETE` and destructive `ALTER`, requires specific user authorization for the affected data and a verified backup/rollback plan. Ordinary scoped product updates follow the task authorization rules above.
- Preserve `stock_movements` as an immutable ledger. Corrections should be new compensating movements, not edits or deletions of historical movements.
- Database migrations must be additive and forward-safe by default. Use the next migration number; never edit an already-deployed migration unless explicitly told it has not been deployed.
- Do not modify files under `trash/` or dated/copy backup files unless the task specifically targets them. Prefer the active modules referenced by the running application.
- A push to `main` triggers the build and production deployment workflow unless all changed paths match `**/*.md` or `docs/**`. Full PR checks still run for documentation changes. Account for deployment when applying existing merge authorization; workflow dispatch can explicitly request a build without deployment.

## Working method

1. Read the request, `OVERVIEW.md`, relevant feature documentation and active implementation files. Verify documentation against code; distinguish code inspection, runtime verification and assumptions.
2. Inspect `git status` before editing a local checkout and preserve unrelated user changes. When editing through GitHub tools without a checkout, record the base commit, preserve its unrelated files and check that the target branch has not moved before updating it.
3. State the files and intended behavior, then apply the authorization rules above. Ask only about consequential unresolved business choices or actions outside the approved scope, not routine technical decisions.
4. State assumptions when the requirement is ambiguous or could affect prices, stock, invoices, identifiers, or production data.
5. Implement the smallest coherent change. Avoid opportunistic refactors and generated backup copies.
6. Do not rename or move variables, functions, files, routes, components, or database fields unless the requested change requires it or the user explicitly approves it.
7. Do not reformat unrelated code or change shared design tokens/components for a local UI task unless the requested behavior requires it. Put optional cleanup in a separate proposal, not in the feature diff.
8. Use the existing test harness for meaningful checks of the changed behavior. Prioritize prices, stock, identifiers, retries and important UI interactions. Do not add tests that merely mirror the implementation or tests for prose-only changes. Report coverage gaps.
9. Review the final diff for secrets, unrelated changes, unsafe migrations, duplicated logic, and backward compatibility.
10. Finish with a short report: what changed, files changed, checks run, remaining risks, and any manual steps.

Prefer the smallest coherent extension of the current stack. Keep business rules reusable in services; avoid parallel import/sync implementations, unnecessary dependencies and broad rewrites. Simplicity does not justify retaining a known stock or financial correctness defect.

## Keep documentation current

- Update the relevant feature document in the same PR as a behavior or API change. Update `OVERVIEW.md` when routes, architecture, implementation status, configuration, migrations or deployment change.
- Label functionality as implemented in code, partial/model-only, or planned. Never describe the presence of a table, route or button as proof of a verified end-to-end workflow.
- Record what was actually verified and when. Keep operator configuration and live data separate from facts established by the repository.
- Reuse the detailed catalog and AI documents rather than copying their full contracts into the overview. If instructions and implementation diverge, document and resolve the concrete discrepancy within the task's scope.
- FIFO and the unified spreadsheet editor are implemented. Selected-field editor-to-shop publication now has a separate preview/send/recovery workflow; unsupported leaf fields and missing target products remain explicit. Complete revenue/margin accounting and general RBAC remain planned. Keep local saves distinct from verified remote delivery. Preserve explicit legacy FIFO cutover and immutable issue snapshots.

## Required validation

Run checks relevant to the changed area. Do not claim a check passed unless it was actually run.

Use `.github/workflows/ci.yml` as the current CI reference. For documentation-only changes, review the diff, links, commands and factual claims; no new application tests or production data writes are needed. Existing PR checks still run.

### Backend

From the repository root:

```bash
python -m compileall -q api/inventory_hub
PYTHONPATH=api python -m unittest discover -s api/tests -v
```

Install dependencies from `api/requirements.txt` in the local environment when needed. The database-backed catalog and AI tests require `CATALOG_TEST_DATABASE_URL` pointing to a dedicated localhost PostgreSQL database whose name ends in `_catalog_test`; follow the test guards and CI setup. Without this configuration, database cases can be skipped: report that explicitly. Never point tests at the production database or bypass the guards.

CI runs the same discovery inventory through `scripts/ci_backend_tests.py` in four module-preserving shards, each with its own PostgreSQL service. The `backend-compile` gate requires all shard jobs to succeed and verifies that every discovered test was executed exactly once, with no skipped tests. Timing weights only balance work; they must never filter tests. JSON reports and setup/body/teardown timings are retained for 14 days; see `docs/ci-deployment.md`. The serial command above remains available for local diagnosis.

### Frontend

From the repository root:

```bash
npm --prefix frontend ci
npm --prefix frontend run build
node frontend/tests/catalog-ui.cjs
node frontend/tests/invoices-ui.cjs
node frontend/tests/product-trace-ui.cjs
node frontend/tests/feed-mapping-ui.cjs
node frontend/tests/product-import-ui.cjs
node frontend/tests/ai-content-ui.cjs
node frontend/tests/accounts-ui.cjs
node frontend/tests/upgates-import-ui.cjs
node frontend/tests/supplier-config-ui.cjs
node frontend/tests/zookee-codes-ui.cjs
node frontend/tests/order-audit-ui.cjs
node frontend/tests/opening-stock-ui.cjs
node frontend/tests/order-stock-ui.cjs
node frontend/tests/order-collection-ui.cjs
node frontend/tests/stock-settings-ui.cjs
node frontend/tests/stock-publication-ui.cjs
node frontend/tests/product-editor-ui.cjs
node frontend/tests/product-links-ui.cjs
node frontend/tests/supplier-links-ui.cjs
node frontend/tests/product-import-identity-ui.cjs
node frontend/tests/fifo-ui.cjs
node frontend/tests/receiving-scan-ui.cjs
node frontend/tests/receiving-lookup-ui.cjs
node frontend/tests/stock-history-ui.cjs
node frontend/tests/availability-sync-ui.cjs
node frontend/tests/stock-adjustments-ui.cjs
node frontend/tests/fifo-cost-sync-ui.cjs
```

Run the relevant UI interaction checks above for changed workflows; CI runs the interaction suites listed above. They use jsdom and do not verify browser layout. Use browser inspection for material layout changes when available, and state any limitation.

For user-facing text, update both `frontend/src/i18n/sk.json` and `frontend/src/i18n/en.json`. Keep the corresponding types in `frontend/src/api/`, `frontend/src/types/` and `frontend/src/types.ts` aligned with API responses.

### Containers and infrastructure

For Docker-related changes, build locally when available. Do not use the production compose definition as a test environment. Validate SQL and migration order in an isolated database; applying a production migration must be covered by the task's authorization and rollback plan.

The deployment runs `python -m inventory_hub.migrate` in one API container before restarting services. Its explicit `MIGRATIONS` tuple calls the twenty-nine migration modules in order `005`–`033`, preserving each module's own transaction and advisory lock. It stops on the first failure; completed migrations remain committed and the API restart is not reached. All twenty-nine upgrade SQL files are packaged in the image. `024_upgates_api_budget.sql`, `025_stock_native.sql` and `026_stock_delta.sql` add shared API admission, native-order evidence and delta checkpoints respectively; they do not activate stock writes. `027_product_links.sql` adds durable previews and results for explicit links to existing shop products. `028_supplier_card_links.sql` adds the corresponding reviewed supplier-card link records. Neither migration links historical data automatically. Adding another numbered SQL file does not automatically make it run on an existing production database: update the explicit runner, image packaging and verification together. Docker's initialization directory is not a general upgrade runner. Production workflows are serialized with cancellation disabled; never interrupt a migration to speed up another release. See `docs/ci-deployment.md` for timing and recovery.

Test schemas using current `StockMovement` or order ORM models need migration `007` too; collector/settings/processing tests also need `008` and `009`. Physical writer tests now also need `010`, since all balance writers check its persistent publication hold. The read-only order collector is opt-in per shop and must never call stock apply or populate the sync outbox. Its metadata is not a complete stock snapshot. The separate opt-in order processor may mutate the local ledger under an explicit per-shop mode and activation cutoff. It must not fabricate manual physical confirmations or publish shop stock. Warehouse settings pause automatic order processing only; they do not freeze manual receiving or other confirmed stock operations. Controlled publication uses a separate persistent hold that gates all local stock writers. `STOCK_PUBLICATION_WRITE_ENABLED` defaults to false and is never enabled by migration. Never retry an ambiguous stock PUT or release its hold merely because a read matches; follow the explicit resolution protocol in `docs/stock-publication.md`.

## Domain rules

New card allocation uses `services/sku_policy.py`: a registered brand namespace plus the exact concrete sale-unit code. On 22 October 2026, after reviewing cross-brand code collisions, the owner explicitly retained automatic brand prefixes so operators do not manage SKU uniqueness. Existing cards retain their SKU; `products.id` and explicit foreign keys carry internal identity. Missing MPN alone does not block a card when a concrete scoped supplier leaf is known. Do not invent suffixes, infer verified MPN from feed field names, match independent supplier codes globally, or allow a different product to occupy an existing code. New products use the same allocated Hub SKU in BIKETREK and xTrek, while existing verified shop mappings may have different codes. Preserve raw codes, historical proof records and receiving/import replay protection. See `docs/sku-policy.md` and `docs/warehouse-model.md`.

Migrations `030`–`032` normalize shop native IDs, add explicit stock/service card types and base units, and consolidate supplier ownership in `product_supply_sources`. They do not enable stock or shop writes. One Hub card has at most one sales representation in each shop. A BIKETREK standalone/POS variant duplicate must be resolved explicitly by keeping the standalone product; do not silently remap or delete a remote listing. Service cards must never create physical stock, reservations or FIFO layers. Purchase-package conversion is explicit per receiving line; existing `pack_size` never implies a conversion. Once physical history exists, the card type and base unit cannot be changed. Immutable ledger, FIFO costs, operation results and uncertain-write evidence remain necessary business records; the request to remove historical shortcuts does not authorize deleting them.

Migrations `021_product_feed_mapping.sql` and `022_product_import_drafts.sql` are additive and run after 020 before the API restart. Supplier mapping definitions and revision history, draft source snapshots, manual values and saved results belong in PostgreSQL. Replaying a request must not create another product. Saving a draft into the Hub must not create a shop listing or physical stock. Historical draft-linked AI jobs prepare content only. Fresh jobs explicitly marked `staging_automation_version=1` may continue through the draft's existing save/preview/send workflow under their frozen four policies. Automatic publication requires every row to belong to the new approved preparation, no intervening draft edit, and confirmation disabled in every participating job. Preserve draft→job lock order and existing catalog recovery; never restart partial or uncertain publication through a fresh preview. Preserve ordinary catalog and existing-product AI workflows. Rollback retains these tables and saved work; prefer a compatible forward fix once drafts have been used.

Migration `020_user_sessions.sql` adds account, opaque session and login-limit tables; it is packaged and explicitly run after 019 before the API restart. Existing operator-token access remains compatible. See `docs/user-accounts.md`; cookie writes require same-origin protection. Keep passwords, raw session tokens and operator credentials out of responses, logs and Git. General operational RBAC remains planned; only account administration distinguishes admin and operator roles.

Migration `019_stock_tracking.sql` adds an initially empty physical-stock confirmation registry, explicitly run after 018 and packaged in the API image. Old movements do not confirm current quantities. Use `services/stock_tracking.py` for stock readers/writers and preserve historical movements/layers outside the new start. See `docs/stock-tracking.md`; tests using stock/evidence queries require migration 019. Never backfill confirmation from imported balances or enable shop publication through this migration.

- Do not invent EAN, SKU, supplier identifiers, prices, VAT, stock quantities, invoice values, or product relationships.
- Keep monetary calculations explicit about VAT and rounding. Use decimal-safe logic on the backend; do not rely on binary floating point for persisted financial values.
- EAN is not the only product identity. Respect the multi-identifier model in `product_identifiers` and its uniqueness rules.
- Missing or malformed supplier data must produce a visible warning or validation error; never silently coerce a missing price to zero.
- Preserve Upgates parent/variant relationships and existing BIKETREK import rules when changing exports.
- Receiving finalization and stock mutation must be idempotent or explicitly protected against repeated submission.
- Supplier availability/quantity is separate from our own physical stock. Catalog refresh or product listing must not manufacture receiving movements. Unknown or stale data must not silently become zero stock.
- Preserve the existing preview, `validation_required`, hidden-product and selected-field update contracts. Distinguish create-only imports, content updates, stock changes and read-only pulls before modifying a path.
- Do not silently replace weighted-average valuation with FIFO, treat selling prices as acquisition costs, or recalculate historical purchase costs from the latest feed. Changes to valuation need explicit business rules and a migration plan.

## Git and pull requests

- Keep commits focused and use descriptive messages.
- Do not commit generated builds, local data, credentials, logs, uploaded documents, or dependency directories.
- Before proposing a merge, summarize any database migration, configuration change, deployment consequence, and rollback step.
- Merge only within existing user authorization, after reviewing the diff and relevant checks. Do not ask again when that authorization already covers the PR. If authorization is missing, leave a reviewable PR and explain the exact remaining approval.
- After an authorized merge, inspect the resulting deployment status and report failures or pending work accurately. Do not claim deployment success from a successful merge alone.

FIFO stock balance readers/writers require migration `011`; stock display also reads manual overrides from `012`. Quarantine must be subtracted from availability everywhere. `NULL` acquisition cost is unknown, not zero; provisional cost is incomplete. New pristine balances start FIFO, legacy balances require a documented cutover. Never reconstruct historic receipt layers from supplier feeds or weighted averages. Do not roll back to pre-FIFO writers while FIFO state exists; use a reviewed forward fix. Migration011 rebuilds only the derived availability column and its known views/index under a transaction; unknown view dependencies must fail safely, not be dropped with CASCADE.

Scanner UUIDs identify individual operations, never an EAN or a time window. Ten fresh scans mean ten increments. Preserve successful UUID results atomically with scan events, including after reset/finalization; only replay the same payload. Migration013 widens compound invoice EAN without truncation and rebuilds its derived fingerprint and the known `v_invoice_lines_detail` view in one transaction, preserving the view definition, ownership and access grants; unknown dependencies must fail without CASCADE. Do not roll back to UUID-unaware scanners as a retry recovery method. Receiving/catalog identity uses the shared resolver and revalidates stored product IDs; supplier SKU aliases remain supplier-scoped.

Regular publication (`015`) is distinct from maintenance holds: it requires explicit Hub authority, all shared channels configured for order processing and no competing external stock writer. `STOCK_SYNC_WRITE_ENABLED` defaults to false. Never auto-retry an ambiguous stock or selected-field PUT. Supplier observations (`014`) expire and never create stock movements. Physical recounts (`017`) append `ADJUSTMENT_IN`/`ADJUSTMENT_OUT` through the existing FIFO engine; reservations, quarantine and computed values are not independent overwrite inputs.

Purchase-cost synchronization adds migration `018_fifo_cost_sync.sql`, packaged in the API image and explicitly run after `017` before restart. It is independent of quantity publication; `FIFO_COST_SYNC_WRITE_ENABLED` and per-shop flags default to false. Products use the next known available FIFO layer, while orders use their original issued allocation quantities and current documented costs. Never derive historic order costs from today's inventory. Preserve original issue snapshots, audit revisions and durable uncertain-write fences. Tests exercising shared AI/manual/FIFO product publication guards require migration `018` too. Zero stock/unknown costs must not manufacture a zero purchase price. No new migration activates historical bulk corrections.

Invoice documents are issued externally. Migration033 extends uploaded_invoices/lines and adds original-file and append-only settlement records. Never issue or number sales invoices in the Hub, infer unknown payments as zero, combine currencies without an explicit rate, or mutate stock from invoice import. Preserve UUID recovery and original document bytes; use owner-scoped source identity and verified line relationships. EAN investigation and its sales views are read-only and distinguish downloaded invoice quantities from Hub ledger quantities.
