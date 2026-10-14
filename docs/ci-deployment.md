# CI coverage and deployment timings

This document describes the workflow contracts in code. It does not certify a
particular commit's CI result or deployment; use that commit's Actions runs.
Sources: [PR checks](../.github/workflows/ci.yml),
[release workflow](../.github/workflows/build.yml),
[test runner](../scripts/ci_backend_tests.py) and
[migration runner](../api/inventory_hub/migrate.py).

## Pull request checks

Every PR targeting `main`, including a documentation-only PR, runs backend and
frontend checks. A newer commit cancels superseded checks for the same PR.

Backend discovery remains `api/tests/test*.py`. Four parallel jobs each use a
separate PostgreSQL 16 service and Python 3.12. Modules stay together to preserve
class/module fixtures and discovery order within each module. The runner balances
modules by the duration hints in `scripts/ci_test_weights.json`; unknown modules
receive a fallback estimate and are included automatically. Hints are not a test
allowlist. Initial hints come from the 13 October 2026 baseline run below.

The existing required check name **`backend-compile`** remains the final gate. It
requires all four jobs to succeed and verifies their JSON reports: identical full
discovery inventories, every discovered ID selected and executed exactly once,
all reports present, successful outcomes and no skipped tests. A missing report,
discovery error, skipped database case or failed fixture blocks the gate. CI
requires a localhost database whose name ends in `_catalog_test`; the original
test-specific database guards still apply. Never run these tests against a live
warehouse database.

Pip downloads are cached against `api/requirements.txt`. The frontend retains its
npm cache, build and all 21 jsdom interaction suites. No application tests are
removed. The ordinary serial `unittest discover` command in [AGENTS.md](../AGENTS.md)
remains useful for local diagnosis; disclose database skips in local results.

## Reading the measurements

Each backend job uploads `backend-test-<shard>` with a JSON report retained for
**14 days**. Reports contain test IDs, outcomes and elapsed seconds, with separate
per-test setup, test-body, teardown and cleanup timings. The job and final gate
summaries show totals and the slowest tests/modules. Shared class/module fixtures
and runner overhead contribute to suite time, not the per-test phase totals.

Release summaries show measured GitHub job durations and build/deploy step
durations. Concurrent jobs overlap: do not add their durations to estimate wall
time. The deployment log records `DEPLOY_TIMING` for `pull`, `migrations`,
`restart`, `readiness` and `cleanup`; migration logs also report each module's
duration. Read actual outcomes as well as durations. A best-effort timing summary
must not turn a failed build or deployment into success.

Use several successful runs to assess a change; runner load, downloads and cold
caches affect individual results. Update balancing hints only from measured
reports, keeping the coverage gate intact. Reports contain timing/identity data,
not invoice documents, production data or credentials.

## Image build and production deployment

API and frontend images build in parallel with distinct Docker cache scopes
`api` and `frontend`. Cache absence only causes rebuilding. A first run under a
new scope can be cold; compare subsequent equivalent runs before claiming the
steady-state improvement.

Pushes to `main` trigger release unless every changed file matches `**/*.md` or
`docs/**`. This path exception does not weaken PR checks. Manual dispatch is
restricted to `main` and accepts `deploy` (default `true`). With `deploy=false`,
the workflow builds/pushes only `sha-<commit>` tags and cache, leaves the production
`main` tags unchanged, and skips the SSH/deploy job. This supports a build-only
cache benchmark. Ordinary releases publish both `main` and SHA tags.

The entire release workflow uses a common concurrency group with
`cancel-in-progress: false`, including image publication. An active release is
not interrupted by a newer run. Do not use cancellation to speed up deployment.

The server pulls images, then executes `python -m inventory_hub.migrate` in one
temporary API container. Its explicit list invokes twenty-two upgrade
modules, `005` through `026`, in their existing order. Each module retains its
own transaction and advisory lock; this is not one combined database transaction.
The first failure stops the runner and prevents the service restart. Already
committed migrations remain applied. No schema SQL is changed by this optimization.
New migrations require explicit runner and image-packaging updates.

After successful migrations, the existing `--force-recreate` for `api`,
`frontend-build` and `caddy` remains. Both public health routes must return HTTP
200 in the same readiness round within the shared 60-second limit. Requests are
capped at five seconds; response bodies and credentials are not logged. Image
cleanup runs only after readiness succeeds. Preserve the existing Compose secret
overlay and supplier credential volume during deployment or recovery.

On failure, use the failing phase/module and Actions outcome to diagnose before
retrying. Do not edit deployed SQL or erase warehouse data. Reverting this pipeline
change reverts orchestration, not already committed migrations; the migration
modules remain independently callable. See [OVERVIEW.md](../OVERVIEW.md#produkčné-nasadzovanie-a-diagnostika)
for the production and rollback boundaries.

## Measured baseline before optimization

On 13 October 2026, [PR #68 checks](https://github.com/mpriesol/inventory_hub/actions/runs/38393069125)
took **8:08**, and its [release](https://github.com/mpriesol/inventory_hub/actions/runs/38394154054)
took **4:47**, including workflow overhead. Backend unittest execution reported
1,213 tests in 438.421 seconds, with no database skips. Log-derived allocation was
approximately 432 seconds for 383 database cases and six seconds for 830 other
cases; this is not a direct measurement of fixture-only time.

The release's API build/push step took 2:26, frontend build/push 0:34, and server
deployment 1:15. The original builds were sequential. Migration execution used
nineteen container starts and took approximately 33 seconds. These are historical
baseline values, not measurements of the optimized implementation. The proposed
6–8-minute combined target is an estimate until comparable runs establish it.


Od 14. 10. 2026 readiness používa `inventory_hub.security_check` v API kontajneri proti verejnému HTTPS. Overuje zdravú DB, anonymné HTTP 401 pre sklad a konfiguračné cesty, autorizovaný stav AI a HTTP 404 pre raw konfiguráciu aj po prihlásení. Kľúč sa neobjaví v argumentoch ani logu. Kontrola je čítacia, nepovoľuje presmerovania a opakuje sa najviac počas existujúceho 60-sekundového okna. Anonymné HTTP 200 pre `/ai-content/status` už nie je úspešný stav.
