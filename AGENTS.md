# BikeTrek Inventory Hub – Agent Playbook (Codex/LLM)

This repository contains the BikeTrek Inventory Hub: a FastAPI backend + React/Vite/Tailwind frontend.
Agents (Codex) should follow this playbook to avoid regressions and repeated path/config issues.

## Read these files first (in order)
1. docs/ARCHITECTURE.md
2. docs/CONFIGS_AND_PATHS.md
3. docs/TROUBLESHOOTING.md
4. docs/SUPPLIERS/paul-lange.md (when touching supplier logic)

## Non-negotiables (hard rules)
1. Single source of truth for runtime data root:
   - INVENTORY_DATA_ROOT MUST be read from api/.env via api/src/inventory_hub/settings.py.
   - NEVER derive inventory-data path from Path.cwd() or relative working directory.
2. On config read failures:
   - Do NOT silently return blank/default configs.
   - API must return clear errors (404 missing file, 422 invalid JSON, 500 unexpected).
3. Do not commit secrets or runtime data:
   - Never commit api/.env, inventory-data, *.db, tokens, API keys.
4. UI/code language:
   - Frontend UI and code should be in English.
5. Preserve backward compatibility where feasible:
   - If introducing new config keys, keep older ones readable or provide migration notes.

## How to work in this repo (agent workflow)
When implementing changes:
1. Identify target file(s) and current behavior.
2. Propose minimal, testable changes with clear rationale.
3. Update docs if you change paths/config layout or endpoints.
4. Add/adjust tests where practical.
5. Produce a PR with:
   - Summary of changes
   - How to run locally (commands)
   - Risk assessment + rollback notes

## Quick sanity checks (must pass)
Backend:
- GET /health returns {"status":"ok"}
- GET /shops/{shop}/config returns the full config from disk (not just defaults)
- No creation of <repo>/api/inventory-data by accident

Frontend:
- VITE_API_BASE points to backend
- Config modal shows errors instead of clearing/resetting to empty

## Local run (baseline)
Backend (Windows PowerShell):
- cd api
- copy .env.example -> .env and set INVENTORY_DATA_ROOT
- run: uvicorn main:app --reload --port 8000

Frontend:
- cd frontend
- npm install
- npm run dev
