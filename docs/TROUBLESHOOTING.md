# Troubleshooting

## Symptom: API returns "blank" / default-only shop config
Example response:
{"console":{"import_console":{"columns":{"updates":[],"new":[],"unmatched":[]}}}}

Root causes:
1) Backend used wrong INVENTORY_DATA_ROOT (cwd fallback created a new inventory-data tree)
2) shop config file missing under the chosen root
3) invalid JSON or read error was swallowed and replaced by defaults

Fix:
- Ensure settings reads api/.env deterministically (absolute env_file path).
- Ensure config I/O uses settings.INVENTORY_DATA_ROOT only.
- Ensure GET /shops/{shop}/config fails fast with 404/422.

Quick checks:
- curl http://127.0.0.1:8000/health
- curl -i http://127.0.0.1:8000/shops/biketrek/config
- Verify disk file exists at: <INVENTORY_DATA_ROOT>/shops/biketrek/config.json

## Symptom: Unexpected folder appears: <repo>/api/inventory-data/...
Cause:
- Some code derived data root from Path.cwd() and auto-created directories.

Fix:
- Remove any Path.cwd() fallback.
- Use only settings.INVENTORY_DATA_ROOT.

## PowerShell note: file compare
In PowerShell, `fc` is an alias for Format-Custom.
Use:
- cmd /c fc .\disk.json .\api.json
or
- Compare-Object (Get-Content .\disk.json) (Get-Content .\api.json)
