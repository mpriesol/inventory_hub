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
An eligible withdrawal also identifies its `withdrawal_basis`; a blocked job
or a replan does not receive a positive withdrawal classification.
It makes no remote request and does not call recovery helpers that update
journals. The response omits full source documents and stored review text.

### Optional retained-history diagnostics

Append `&include_evidence=true` to the same GET to inspect the captured evidence
for that exact campaign and family. Authorization, `Cache-Control: no-store`,
the read-only repeatable-read transaction and the original summary are unchanged.
The helper projects the single existing snapshot; it issues no additional query,
native request, recovery call or write. `snapshot_hash`, eligibility, blockers,
withdrawal and replan still use the original control contract.

`evidence` contains the full captured-snapshot manifest and typed projections of
the campaign, current head, job, saved guards, current sibling reviews, catalog
proofs, source-card receipts and allocation/audit query results. Every record has
a `canonical` manifest (`sha256`, UTF-8 `bytes`, JSON `type`) and projected
`fields`. The encoding is explicitly sorted-key, compact canonical JSON, not
PostgreSQL's JSONB text or original wire bytes. The captured snapshot digest is
the existing `snapshot_hash`; the older allocation-audit `row_hash` remains its
explicitly labeled PostgreSQL MD5 projection.

The projection preserves SQL NULL versus JSON `null` for the job document,
attempt/control counters, dispatch flags, content/related intents, native IDs,
card IDs, link IDs and previous receipts/documents. Created and reused source
receipts, nested `creation_evidence` and `source_card` history remain distinct;
none of these alone proves an absence of past enrichment or native writes.
Known source visibility fields and barcode-correction/sentinel identities are
typed too. Free review/error/contact text, URLs, connection fingerprints and
general campaign configuration values are redacted to exact manifests.
Uninterpreted native content bodies produce an explicit hold, even though their
complete captured bytes are hashed. Unknown fields never pass through: their
key and value have separate canonical manifests, with a hashed key segment in
the diagnostic path so a credential placed in a field name cannot leak.

The scope lists every saved and current row, including excluded, blocked and
done siblings, historical `previous_guards`, and retained proof/source-card
codes. Native parent/variant and card references retain their exact paths and
types. `source_allocation_query` labels the unchanged query's precise
`current_family_codes_only` scope and lists any historical union codes it did
not cover. That gap is an explicit hold; the GET does not silently broaden the
withdrawal query or claim missing allocation history was inspected.

The projection admits at most 500 entries per collection, a 500-code family
union, 5 MB of canonical input and 5 MB of output, with bounded nesting, node and
problem counts. A proof/source query returning its 501st sentinel is explicitly
incomplete; its hash describes only the captured selection, not every database
row. Oversize projections are omitted under a hold instead of returning a
truncated clean result. Missing/unknown/malformed schema fields include a path,
type and manifest; their values are not exposed.

`complete: true` / `review_state: typed_snapshot` means only that the captured
lifecycle projection is understood and fits these limits. Configuration and
free-text manifests remain intentionally redacted. This is **not** a complete
historical event log, a native readback, a settlement certificate or new mutation
permission. `historical_native_effects` remains `not_determined` in every case.
Overwritten older rows cannot be reconstructed by this endpoint. Jobs with
earlier effects continue to fail the original unsent-withdrawal checks. The
separate [retained-job handoff](atomer-catalog-handoff.md) has its own complete
current-writer proof, explicit local-effect acknowledgement and terminal audit;
this diagnostic projection alone does not authorize that action.

Eligibility requires the exact current source/run and every sibling review
revision to agree with the saved guards. In the original `unsent_pending`
class, the job must be `pending`, its document must be absent, and the complete
receipt must have a known no-effect shape.
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

### Existing targets awaiting their first reconciliation

`pre_reconcile_existing_targets` recognizes the narrow `cards` state written
directly by `_prepare` when every selected xTrek target already has a native
identity in the current report. At least one xTrek target must be selected;
every such target needs a positive native product ID and a positive variant ID
when its kind is `variant`. IDs are exact positive integers or canonical decimal
strings, as written by the progress report. This proves a lifecycle branch, not
the physical identity or completed migration of those products.

The document must be actual SQL NULL (JSON `null` is not sufficient), the full
receipt must be exactly `{}`, and there must be no catalog proof or source-card
row. Both attempt counters and the job control revision must be integer zero;
the control UUID must be NULL and the source lane must still be `pending`.
All original source, sibling review, schema, allocation and rename-history
checks still apply. The snapshot hash includes the SQL-NULL fact; obtain a
fresh control read before requesting withdrawal.

Native observation retains its document and receipt. Card reconciliation and
its card receipts/proofs commit together; quota failure rolls back partial
local changes before rescheduling. Neither path is silently treated as this
untouched branch. Even a previously withdrawn and replanned job with a newly
empty receipt has a nonzero control revision and cannot enter this class.
Unknown or partly reconciled `cards` jobs remain blocked.

Withdrawing this class hands an unsent plan to explicit manual work while the
campaign remains paused. It does not mark any source item done or any product
migrated. The existing withdrawal, immutable audit and replan transitions are
otherwise unchanged; this extension adds no SQL migration or native writes.

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
