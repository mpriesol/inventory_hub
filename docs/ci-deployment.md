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
npm cache, build and all configured jsdom interaction suites. No application tests are
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
temporary API container. Its explicit list invokes twenty-nine upgrade
modules, `005` through `033`, in their existing order. Each module retains its
own transaction and advisory lock; this is not one combined database transaction.
The first failure stops the runner and prevents the service restart. Already
committed migrations remain applied. No schema SQL is changed by this optimization.
New migrations require explicit runner and image-packaging updates. The warehouse
model changes add `030_shop_identity.sql`, `031_item_units.sql` and
`032_supplier_authority.sql`; they retain historical data and leave publication
authority switches unchanged. After new unit/identity writes, recovery uses a
compatible forward fix, not old writers that ignore these fields.

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

## SPS proxy network (23 October 2026)

The separately deployed SPS receiver uses the external Docker network
`sps-proxy` with the alias `sps-receiver`. Caddy must join that network as well as
the existing Hub network. The incident's supplied server output showed healthy
SPS on `sps-proxy,sps_default`, but recreated Caddy only on `inventory-hub-net`;
Caddy logged `lookup sps-receiver ... no such host` for `/sps/`.

The release-generated `docker-compose.ai-content.yml` now also adds Caddy's
`sps_proxy` network, preserving its base networks through Compose merge. The
production reference includes the same attachment. Changing only the reference
would not fix the server: deployment uses the server's own `docker-compose.yml`.
Before pulling images, migrating or recreating services, deployment checks that
`sps-proxy` exists and that the merged Compose configuration is valid. It does
not create, remove or reconfigure the SPS stack/network. After Hub readiness, a
bounded read-only HTTPS request checks `/sps/healthz`; failure marks deployment
failed and skips image cleanup, without automatically rolling back services.

For manual Hub recreation, include the generated overlay, exactly as the release
does: `docker compose -f docker-compose.yml -f docker-compose.ai-content.yml ...`.
A bare Compose invocation against an older server base file can omit the SPS
attachment. For immediate recovery of the incident's running Caddy, the scoped
command is `docker network connect sps-proxy inventory-hub-caddy-1`; it does not
restart containers, but does not persist into a replacement container by itself.
The SPS alias and its application remain owned by the SPS deployment.

No migration or application behavior is changed by this fix. The normal release
still recreates API, frontend builder and Caddy; PostgreSQL, Redis and SPS are not
recreated by this change. A rollback must retain the network attachment or
`/sps/` can fail again after Caddy recreation. Repository configuration and local
validation alone do not prove production deployment; verify the release run and
the live SPS route separately.

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

Migration033 (`033_invoice_archive.sql`) adds the external document archive, indexed invoice items and append-only settlement evidence. It is packaged and explicitly run after032; it does not download documents or issue invoices. CI also runs `invoices-ui.cjs` and `product-trace-ui.cjs`.
