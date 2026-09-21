# Inventory Hub developer agent instructions

These instructions apply to the entire repository.

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
- `frontend/`: React 18 + TypeScript + Vite + Tailwind CSS.
- `infra/db-init/`: ordered PostgreSQL schema/migration SQL.
- `infra/docker-compose.prod.yml`: production reference; do not run it without explicit approval.
- `OVERVIEW.md`: primary architecture and operational context.

There are two data stores. PostgreSQL holds relational business data; the filesystem holds supplier/shop configuration and imported/generated files. Supplier-related changes may need both representations. Do not silently update only one side.

## Safety boundaries

- Never deploy, SSH to the server, restart production services, run production Docker Compose, or call production write endpoints unless the user explicitly asks for that exact action.
- Never read, print, commit, or invent secrets. Keep `.env`, credentials, B2B passwords, API keys, customer documents, invoices, and production data out of Git.
- Do not delete or rewrite production data. Do not run destructive SQL (`DROP`, `TRUNCATE`, broad `DELETE`, destructive `ALTER`) without explicit approval and a verified backup/rollback plan.
- Preserve `stock_movements` as an immutable ledger. Corrections should be new compensating movements, not edits or deletions of historical movements.
- Database migrations must be additive and forward-safe by default. Use the next migration number; never edit an already-deployed migration unless explicitly told it has not been deployed.
- Do not modify files under `trash/` or dated/copy backup files unless the task specifically targets them. Prefer the active modules referenced by the running application.
- A push to `main` triggers the build and production deployment workflow. Treat merge approval as a production action.

## Working method

1. Read the request, `OVERVIEW.md`, and only the relevant implementation files.
2. Inspect `git status` before editing and preserve unrelated user changes.
3. Before editing, propose a short change plan naming the files and intended behavior. Wait for explicit approval when the change could affect shared UI/design, database schema, API contracts, production behavior, or when several reasonable implementations exist.
4. State assumptions when the requirement is ambiguous or could affect prices, stock, invoices, identifiers, or production data.
5. Implement the smallest coherent change. Avoid opportunistic refactors and generated backup copies.
6. Do not rename or move variables, functions, files, routes, components, or database fields unless the requested change requires it or the user explicitly approves it.
7. Do not reformat unrelated code, change shared design tokens/components for a local UI task, or optimize working code without explicit approval. Put optional cleanup in a separate proposal, not in the feature diff.
8. Add or update tests when a test harness exists. If no harness covers the area, run the strongest safe checks available and clearly report the gap.
9. Review the final diff for secrets, unrelated changes, unsafe migrations, duplicated logic, and backward compatibility.
10. Finish with a short report: what changed, files changed, checks run, remaining risks, and any manual steps.

## Required validation

Run checks relevant to the changed area. Do not claim a check passed unless it was actually run.

### Backend

From the repository root:

```bash
python -m compileall -q api/inventory_hub
```

When dependencies and safe local configuration are available, also run an application import or focused API tests. Never point tests at the production database.

### Frontend

From the repository root:

```bash
npm --prefix frontend ci
npm --prefix frontend run build
```

For user-facing text, update both `frontend/src/i18n/sk.json` and `frontend/src/i18n/en.json`. Keep API response types and `frontend/src/types.ts` aligned.

### Containers and infrastructure

For Docker-related changes, build locally when available. Do not start the production compose definition. Validate SQL syntax and migration order without applying migrations to production.

## Domain rules

- Do not invent EAN, SKU, supplier identifiers, prices, VAT, stock quantities, invoice values, or product relationships.
- Keep monetary calculations explicit about VAT and rounding. Use decimal-safe logic on the backend; do not rely on binary floating point for persisted financial values.
- EAN is not the only product identity. Respect the multi-identifier model in `product_identifiers` and its uniqueness rules.
- Missing or malformed supplier data must produce a visible warning or validation error; never silently coerce a missing price to zero.
- Preserve Upgates parent/variant relationships and existing BikeTrek import rules when changing exports.
- Receiving finalization and stock mutation must be idempotent or explicitly protected against repeated submission.

## Git and pull requests

- Keep commits focused and use descriptive messages.
- Do not commit generated builds, local data, credentials, logs, uploaded documents, or dependency directories.
- Before proposing a merge, summarize any database migration, configuration change, deployment consequence, and rollback step.
- Never merge a pull request or trigger deployment without explicit user approval.
