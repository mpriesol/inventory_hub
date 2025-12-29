# Architecture

## Components
- Backend: FastAPI (Python), handles supplier feeds, invoices, Upgates export transforms, config I/O.
- Frontend: React + Vite + Tailwind, provides Supplier Console / Import Console UI.

## Repository layout (intended)
- api/          FastAPI app and Python package
- frontend/     React/Vite app
- data/         Local runtime data (ignored by git), including inventory-data

## Runtime data (inventory-data)
inventory-data is NOT part of the repo source code. It contains:
- suppliers/{supplier_code}/...
- shops/{shop}/config.json
- logs/, state/, history/, feeds/, invoices/, imports/

The backend reads/writes under INVENTORY_DATA_ROOT (absolute path from api/.env).

## Key flows
1) Supplier feed refresh
- Fetch supplier XML/CSV (remote or local)
- Convert to “ready_to_filter” CSV and store under suppliers/<supplier>/feeds/converted

2) Invoices processing
- Index invoices (CSV/PDF)
- Prepare Upgates updates/new/unmatched outputs (idempotent by invoice id)
- Maintain invoice processing history and processed state

3) Shop export integration
- Shop export (latest.csv) stored under shops/<shop>/latest.csv (or override)
- Used for matching existing products and calculating deltas

## Design principles
- Deterministic paths: do not depend on current working directory.
- Fail fast on config errors: no silent defaults for missing/invalid JSON.
- Idempotent invoice application: prevent double counting.
