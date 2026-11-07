# Atomer campaign pause and resume

The operator API can pause one registered catalog campaign and later resume its
previous scheduling state. This controls the campaign worker and its local
source-card worker. It does not edit products, shop listings, stock, review
decisions, job stages, frozen requests, verification receipts or quota deadlines.

These endpoints require the existing operator authentication and return
`Cache-Control: no-store`. Cookie-authenticated mutations retain the existing
same-origin request checks. They make no Upgates requests.

## Read and compare the current campaign

`GET /api/migration/atomer-progress/catalog-engine/campaigns/{campaign_id}/control`

The response contains the exact campaign ID, scheduling state, `operator_paused`,
control `revision`, `previous_state`, `updated_at`, existing `next_at`, failure
count and source run IDs. The campaign ID is its existing 64-character lowercase
hexadecimal identifier. Copy both the revision and timestamp into the mutation;
a stale value returns `409 catalog_control_changed` without changing the campaign.

## Pause

`POST /api/migration/atomer-progress/catalog-engine/campaigns/{campaign_id}/pause`

```json
{
  "request_id": "bd40dfd2-5ee7-435c-9777-36fcdb40ab75",
  "expected_revision": 0,
  "expected_updated_at": "2026-01-01T00:00:00+00:00",
  "reason": "Reviewed maintenance of this exact campaign.",
  "confirmed": true
}
```

The UUID and timestamp above are illustrative. Use a new request UUID and the
actual values returned by the read endpoint. The reason must contain 10–1000
characters without control characters. Confirmation must be the JSON boolean
`true`; unknown fields are rejected.

A successful response is returned only after the pause and its immutable audit
receipt commit together. The service acquires the remote worker's advisory lock,
the shared review mutex and the identity writer lock before checking the campaign
row and its compare-and-swap values. The remote worker holds its session lock
across intent commits and HTTP, so pause cannot acknowledge quiescence midway
through a remote operation. Lock contention returns one of `catalog_worker_busy`,
`catalog_review_busy` or `catalog_identity_busy` with HTTP409; it does not interrupt
the worker or start an automatic retry.

Pause accepts running, waiting or automatically paused campaigns. It preserves the
previous state, sets `state=paused` and `operator_paused=true`, and advances the
control revision. Ordinary catalog preview/apply cannot wake or revise an
operator-paused campaign: it returns `catalog_operator_paused`. Both worker lanes
exclude paused campaigns. After a local worker rollback, even retry metadata is
written only after reacquiring the review mutex and rechecking the campaign.

This is a campaign-specific control. Another independently authorized campaign
and other Hub operations retain their own normal controls.

## Resume

`POST /api/migration/atomer-progress/catalog-engine/campaigns/{campaign_id}/resume`

Use the same body shape with a new UUID and a fresh control read. Resume restores
the saved scheduling state and clears the operator pause. It preserves the exact
retry deadline, failure count, problem, selection and all jobs. A previously
waiting campaign continues to wait until its existing deadline. A campaign that
was already automatically paused returns to that state; resume alone does not
reset its failure or authorize a new request. A different active campaign blocks
resume with `catalog_other_campaign_active`.

Resume does not enable workers that are disabled by the existing deployment
setting. A new pause of an already operator-paused campaign and resume of a
campaign without an operator pause are explicit conflicts.

## Audit, recovery and unresolved jobs

The mutation response includes the original request hash, actor, reason, before
and after control state, and job-stage counts. Read the committed receipt through
`GET /api/migration/atomer-progress/catalog-engine/control-requests/{request_id}`.
Retrying the exact same UUID and payload returns that original receipt, even if a
later control operation changed the campaign again. Reusing the UUID for a
different campaign, action or payload returns `catalog_control_request_conflict`.
Busy or stale requests do not create a completed receipt.

Pause does not settle prepared, sending, observed, blocked or uncertain jobs.
Their frozen documents and historical receipts remain intact and continue to
block operations that depend on those identities. Consolidation's matching-job,
financial and other dependency guards are unchanged. There is no job cancellation
or exclusion action in this contract.

Additive migration048 installs the pause columns, consistency checks and immutable
control journal. It does not pause or resume an existing campaign. The normal
ordered migration runner executes it after047, and the API image packages its
SQL. Replaying048 preserves all campaign and audit data. An older application
cannot change an operator-paused campaign to running while the database pause
constraint remains active; deploy a compatible forward version before resuming
normal lifecycle controls.
