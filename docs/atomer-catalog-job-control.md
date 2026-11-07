# Explicit control of one unsent catalog job

An operator can withdraw one unexecuted catalog plan while its campaign is
operator-paused. This is a lifecycle action with an immutable receipt. It does
not declare the underlying product migrated, delete review history, reconcile
an uncertain write or change an inventory quantity.

## Read the exact scope

`GET /api/migration/atomer-progress/catalog-engine/campaigns/{campaign_id}/jobs/control?family={family_key}`

The operator-only, no-store read uses a read-only repeatable-read transaction.
It returns the snapshot hash, selected codes, campaign/job revisions, current
state, proof counts, blockers and separate `eligible` / `replan_eligible` flags.
It makes no remote request and does not call recovery helpers that update
journals. The response omits full source documents and stored review text.

Eligibility requires the exact current source/run and every sibling review
revision to agree with the saved guards. The job must be `pending`, its document
must be SQL NULL, and the complete receipt must have a known no-effect shape.
Only optional scheduling isolation metadata and explicit `dispatched: false`
are admitted. Unknown keys, content intents, native origins, created or reused
card IDs, ambiguous effects, prepared/sending/observation stages and incomplete
evidence are blockers. Existing proof records must be explicit blocked/excluded
diagnostics; a source-card receipt cannot contain a card or native identity.
For this initial contract, the source-card reason must be the known pre-write
`atomer_source_card_requires_review` rejection. Unknown reasons and failed
readback are blocked. Raw inactive/current products are also checked for an
earlier Atomer allocation under any family leaf SKU, including original SKUs in
immutable editor audit; a later null-ID diagnostic or SKU rename cannot hide
that allocation. Unknown job columns require a reviewed contract update.
The scope is bounded to 500 leaves and 5 MB of evidence.

## Withdraw with explicit comparison and swap

`POST /api/migration/atomer-progress/catalog-engine/campaigns/{campaign_id}/jobs/withdraw`

```json
{
  "request_id": "d81fe206-a083-4db2-9119-96ec58be55c4",
  "family_key": "v:EXAMPLE-FAMILY",
  "expected_snapshot_hash": "<hash from the current control read>",
  "selected_codes": ["EXAMPLE-LEAF"],
  "reason": "This exact unsent plan is handed to manual review.",
  "confirmed": true
}
```

The server takes the remote worker, review, identity and source worker advisory
locks and the shared source-refresh fence before any change. A worker's session lock remains effective across its
intent commits and HTTP calls. Campaign/job/proof row locks use NOWAIT; a busy
row or worker returns a conflict and rolls back. The full fresh snapshot and
selected codes must match the request while the campaign remains paused.

Withdrawal changes the job to `excluded`, the local source lane to `blocked`,
and sets a sticky `operator_withdrawn` flag with a revision and audit UUID. The
original complete job, proofs, source rows and review guards are retained in an
immutable database audit. Existing proof and review rows remain unchanged.
The response is returned only after the job and audit commit together.

The UUID replays its original result even after later replanning. Reusing it
with a different request fails. Read the durable receipt at:

`GET /api/migration/atomer-progress/catalog-engine/job-control-requests/{request_id}`

## Sticky suppression and explicit replanning

Campaign resume, registration, source discovery, family expansion, retries,
source-card recovery and quota scheduling cannot revive a withdrawn job.
Normal candidates exclude it; a database trigger independently rejects any
unaudited modification. Proof writers acquire a shared lock on their parent
job and reject withdrawn jobs, including a new proof inserted concurrently
with withdrawal. Existing proof rows remain immutable while withdrawn.

`POST /api/migration/atomer-progress/catalog-engine/campaigns/{campaign_id}/jobs/replan`
uses the same request shape and a newly read hash. It is available only while
the campaign remains paused and the full source and review guards still agree.
It clears scheduling errors/attempts and schedules a fresh pending plan with
no document and an empty receipt. It never restores an outbound payload.
The campaign stays paused; normal resume is a separate explicit action.
Changed source/reviews or any unknown effect block this narrow replan path.

## Deployment and compatibility

Migration 049 adds the job control columns, immutable audit table and SQL
fences. It withdraws no jobs. The regular migration runner installs it before
workers start, and the API image includes the SQL file. Existing consolidation
guards remain unchanged: an actually withdrawn job is excluded; other paused,
prepared or uncertain jobs remain pending dependencies.

Keep migration 049 and its audit during rollback. Older workers do not know the
sticky flag and may hit its SQL fence; use a compatible forward fix. Do not drop
the control tables or clear a flag to restart work.
