# BikeTrek Inventory Hub – API Reference

This document describes the HTTP API exposed by the Inventory Hub backend (FastAPI).
It is intended as an operational reference for developers and agents (Codex).

Important:
- Always confirm exact request/response shapes in the OpenAPI UI (`/docs`) for the current commit.
- Path and config behavior must be deterministic (see docs/CONFIGS_AND_PATHS.md).

---

## Base URL

Local development (typical):
- http://127.0.0.1:8000

Content type:
- Requests/Responses are JSON unless stated otherwise.

---

## Conventions

### Path parameters
- `{supplier}` usually refers to a supplier identifier (commonly supplier_code like `paul-lange`)
- `{shop}` refers to a shop identifier (commonly `biketrek`)

### Error handling policy (required)
- Missing config file: 404 with absolute path in `detail`
- Invalid JSON: 422 with line/col in `detail`
- Unexpected errors: 500 with a meaningful message in `detail`
Never return a successful 200 response with “blank defaults” when a file is missing/unreadable.

### Runtime data root (required)
All file-backed operations must read/write under:
- `INVENTORY_DATA_ROOT` from `api/.env` (single source of truth)

---

## Endpoints

### 1) Health

#### GET `/health`
Returns service status.

Response (example):
```json
{"status":"ok"}
```

---

### 2) Shops – Raw Shop Config (source of truth)

#### GET `/shops/{shop}/config`
Loads the shop config from:
`<INVENTORY_DATA_ROOT>/shops/{shop}/config.json`

Notes:
- Must fail fast (404/422), never silently return defaults.

#### PUT `/shops/{shop}/config`
Overwrites the shop config file with provided JSON.

Notes:
- Payload must be a JSON object (dictionary).
- UI should not send partial payloads unless the server merges; otherwise it overwrites.

Example (curl):
```bash
curl -sS http://127.0.0.1:8000/shops/biketrek/config
curl -sS -X PUT http://127.0.0.1:8000/shops/biketrek/config \
  -H "Content-Type: application/json" \
  --data-binary @shop_config.json
```

---

### 3) Console Config (global UI config)

#### GET `/configs/console`
Returns the console config (used by UI preferences like Import Console columns).

#### POST `/configs/console`
Writes console config.

Example:
```bash
curl -sS http://127.0.0.1:8000/configs/console
curl -sS -X POST http://127.0.0.1:8000/configs/console \
  -H "Content-Type: application/json" \
  --data-binary @console_config.json
```

---

### 4) Suppliers – Registry and Config

#### GET `/suppliers`
Lists suppliers registered in backend (name, supplier_code, adapter, etc.).

#### POST `/suppliers`
Creates/registers a supplier entry.

#### GET `/suppliers/{supplier}/config`
Loads supplier config (file-backed).

#### PUT `/suppliers/{supplier}/config`
Writes supplier config.

Example:
```bash
curl -sS http://127.0.0.1:8000/suppliers
curl -sS http://127.0.0.1:8000/suppliers/paul-lange/config
```

---

### 5) Supplier Feeds

#### POST `/suppliers/{supplier}/feeds/refresh`
Fetches a supplier feed source (remote or local) and converts it to a normalized CSV
(implementation depends on supplier adapter).

Notes:
- Paul-Lange: XML -> CSV in “ready_to_filter” format under:
  `<INVENTORY_DATA_ROOT>/suppliers/{supplier}/feeds/converted/`

Payload:
- Often optional; some implementations support a `source_url` override for local files.

Example:
```bash
curl -sS -X POST http://127.0.0.1:8000/suppliers/paul-lange/feeds/refresh \
  -H "Content-Type: application/json" \
  -d '{"source_url":"C:/path/to/export_v2.xml"}'
```

---

### 6) Invoices – Indexing and Processing (Supplier)

> Endpoint names may include: refresh/index/reindex/mark_processed. Confirm in `/docs`.

Typical capabilities:
- Refresh invoices list from disk into an index
- Keep per-invoice “processed” state and history
- Generate Upgates outputs (updates/new/unmatched)

#### POST `/suppliers/{supplier}/invoices/refresh`
Rebuilds/updates invoices index while preserving processed state (carry-over).

#### GET `/suppliers/{supplier}/invoices/index`
Returns the current invoices index (often enriched with history_count and last_processed_at).

#### POST `/suppliers/{supplier}/invoices/reindex`
Forces a full reindex (if exposed).

#### POST `/suppliers/{supplier}/invoices/mark_processed`
Marks invoice as processed (payload typically contains invoice id).

Notes:
- Files live under `<INVENTORY_DATA_ROOT>/suppliers/{supplier}/invoices/...`
- History snapshots under `.../invoices/history/`

---

### 7) Runs – Prepare import outputs

#### POST `/runs/prepare`
Produces Upgates import outputs for a supplier based on:
- supplier feed (converted CSV)
- shop export latest.csv (or override)
- invoice CSV (if processing invoice-based run)

Outputs commonly:
- updates_existing_*.csv
- new_products_*.csv
- unmatched_*.csv

Notes:
- Some implementations allow overriding the Upgates export CSV path/URL.

Example (shape varies; confirm in `/docs`):
```bash
curl -sS -X POST http://127.0.0.1:8000/runs/prepare \
  -H "Content-Type: application/json" \
  -d '{"supplier":"paul-lange","shop":"biketrek"}'
```

---

### 8) Files – List/Preview/Download

#### GET `/suppliers/{supplier}/files?area=...`
Lists files within a supplier area.

Common `area` values (examples; confirm in `/docs`):
- `feeds_xml`
- `feeds/converted`
- `invoices/csv`
- `invoices/pdf`
- `imports/upgates`

#### GET `/files/download?...`
Downloads a specific file (streaming).

#### POST `/files/preview`
Returns a “smart preview” of a CSV (encoding detection, delimiter autodetect, limited rows).

Notes:
- Preview is intended for UI “quick look”.
- Download endpoint should be used for full file retrieval.

---

### 9) Imports – Selected outputs (UI integration)

> Confirm exact endpoint name in `/docs`.

Typical:
- Download a “selected” imports CSV (prepared by UI selection)
- Used by Import Console actions

Examples (confirm):
- GET `/suppliers/{supplier}/imports/selected`

---

### 10) Receiving Sessions (optional / evolving)

> Confirm exact endpoints and payloads in `/docs`.

Purpose:
- Support warehouse receiving workflow (scan EAN/SCM/PRODUCT_CODE)
- Maintain a receiving “session” and reconcile scanned items with prepared outputs

---

## OpenAPI

FastAPI Swagger UI:
- GET `/docs`

Use `/docs` as the authoritative reference for exact payloads and additional endpoints.
This file is a “stable operator guide” and should be kept in sync with the actual API.

---

## Quick “Known Good” Checks

1) Health:
- `curl -sS http://127.0.0.1:8000/health`

2) Shop config must not degrade:
- `curl -i http://127.0.0.1:8000/shops/biketrek/config`
Expected:
- 200 with full config keys from disk
Not acceptable:
- 200 with only defaults if file is missing/unreadable

3) No accidental data roots:
- No `<repo>/api/inventory-data/` should appear.
If it appears, you have a cwd-derived path bug.
