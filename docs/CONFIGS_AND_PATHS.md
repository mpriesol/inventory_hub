# Configs and Paths

## Absolute rule: INVENTORY_DATA_ROOT
The backend MUST use `INVENTORY_DATA_ROOT` from `api/.env` as the single source of truth.
Never fallback to `Path.cwd()/inventory-data`.

### Why
Working directory changes (especially with reload) can cause accidental creation of new inventory-data trees,
leading to "blank" configs and confusing behavior.

## Where .env lives
- api/.env (local only, ignored by git)
- api/.env.example (committed template)

### api/.env.example (template)
INVENTORY_DATA_ROOT=C:\!kafe\BikeTrek\inventory_hub\data\inventory-data
INVENTORY_DB_URL=sqlite:///C:/!kafe/BikeTrek/inventory_hub/data/inventory.db

## Config file locations under INVENTORY_DATA_ROOT
- shops/<shop>/config.json
- suppliers/<supplier>/config.json

## Shop config keys (current)
- upgates_api_base_url
- upgates_login
- upgates_api_key
- upgates_full_export_url_csv
- verify_ssl
- ca_bundle_path
- export_retention
- console.import_console.columns.{updates,new,unmatched}

Notes:
- UI must show errors when loading fails.
- Avoid wiping config editor to empty on fetch error.

## Supplier config (high level)
- feeds.current_key
- feeds.sources.*
- invoices.*

## Error handling policy
- Missing config file -> 404 with absolute path in detail
- Invalid JSON -> 422 with line/col
- Unexpected I/O -> 500
Never return silent default config as a successful 200 response.
