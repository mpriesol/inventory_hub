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
- `.github/workflows/ci.yml`: current PR validation commands and isolated test database setup.
- `.github/workflows/build.yml`: build and production deployment on `main`; even a documentation-only merge currently triggers it.
- `infra/docker-compose.prod.yml`: deployment reference, not proof of the server's current configuration.
- `OVERVIEW.md`: project map, implementation status and operational context.
- `docs/supplier-catalog.md`: supplier catalog, validation, import and recovery contracts.
- `docs/central-stock.md`: accepted order/manual-line rules and phased central-stock implementation.
- `docs/ai-content.md`: AI preparation, review, selected-field updates, access configuration and recovery.

There are two data stores. PostgreSQL holds relational business data; the filesystem holds supplier/shop configuration and imported/generated files. Supplier-related changes may need both representations. Do not silently update only one side.

## Task scope and existing authorization

- Follow the current user request and permissions already granted for the project or conversation. User instructions take precedence over these repository guidelines. Do not ask for the same approval again while its scope still applies.
- A request for an audit, prompt or design authorizes that deliverable; it does not request implementation or production mutations. A request to implement authorizes the routine code, UI, API and additive schema work necessary for that task.
- Before work, state a short plan and any deployment consequence. Continue with ordinary implementation decisions inside the authorized scope. Several reasonable technical implementations are not, by themselves, a reason to stop for approval.
- Use already-granted permission for PR merge/automatic deployment or scoped live testing, including product creation, when it covers the current task. If that permission has not been granted or the action exceeds it, finish the reviewable work first and ask only for the missing authorization.
- Live testing permission is limited to the agreed behavior, target shops and products. It does not authorize unrelated bulk changes, deletion of production data, destructive migrations or bypassing the application's validation/review steps. Respect a narrower instruction in the current task.
- Server access must actually be available and authorized. Request the specific missing access or log evidence when needed; do not invent credentials or claim to have inspected the server.
- These rules preserve the scope of the user's authorization; editing this file does not itself grant new permissions.

## Safety boundaries

- Production deployment, SSH, service restarts, production Docker Compose and live write endpoints must remain within the user's existing authorization. Permission to trigger deployment by merging a PR does not automatically grant arbitrary manual server operations.
- Use configured credentials through the normal application or tool mechanism. Do not print, copy into documentation, commit or invent secrets. Do not inspect secret values unnecessarily. Keep `.env`, credentials, B2B passwords, API keys, customer documents, invoices and production data out of Git.
- Destructive deletion or replacement of production data, including `DROP`, `TRUNCATE`, broad `DELETE` and destructive `ALTER`, requires specific user authorization for the affected data and a verified backup/rollback plan. Ordinary scoped product updates follow the task authorization rules above.
- Preserve `stock_movements` as an immutable ledger. Corrections should be new compensating movements, not edits or deletions of historical movements.
- Database migrations must be additive and forward-safe by default. Use the next migration number; never edit an already-deployed migration unless explicitly told it has not been deployed.
- Do not modify files under `trash/` or dated/copy backup files unless the task specifically targets them. Prefer the active modules referenced by the running application.
- A push to `main` triggers the build and production deployment workflow. Account for this when applying existing merge authorization; a documentation-only change does not skip deployment.

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
- FIFO, a general spreadsheet-like product editor and the complete central-stock sales/reservation workflow are design requirements at this handoff, not completed features. Change their status only after implementation and verification.

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

### Frontend

From the repository root:

```bash
npm --prefix frontend ci
npm --prefix frontend run build
node frontend/tests/catalog-ui.cjs
node frontend/tests/ai-content-ui.cjs
node frontend/tests/upgates-import-ui.cjs
node frontend/tests/supplier-config-ui.cjs
node frontend/tests/order-audit-ui.cjs
node frontend/tests/opening-stock-ui.cjs
node frontend/tests/order-stock-ui.cjs
```

Run the relevant UI interaction checks above for changed workflows; CI runs all seven suites. They use jsdom and do not verify browser layout. Use browser inspection for material layout changes when available, and state any limitation.

For user-facing text, update both `frontend/src/i18n/sk.json` and `frontend/src/i18n/en.json`. Keep the corresponding types in `frontend/src/api/`, `frontend/src/types/` and `frontend/src/types.ts` aligned with API responses.

### Containers and infrastructure

For Docker-related changes, build locally when available. Do not use the production compose definition as a test environment. Validate SQL and migration order in an isolated database; applying a production migration must be covered by the task's authorization and rollback plan.

The current deployment explicitly runs `inventory_hub.ai_content_migrate` for `005_ai_content.sql`, then `inventory_hub.opening_stock_migrate` for `006_opening_stock.sql` and `inventory_hub.order_stock_migrate` for `007_order_stock.sql`, before restarting the API. All three SQL files are packaged in its image. Adding another numbered SQL file does not automatically make it run on an existing production database. Plan the application and verification of each new migration explicitly; Docker's initialization directory is not a general upgrade runner. Test schemas using current `StockMovement` or order ORM models need migration `007` too.

## Domain rules

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
