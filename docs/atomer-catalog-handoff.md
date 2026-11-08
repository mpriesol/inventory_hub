# Retained Atomer job handoff for manual identity migration

This separate operator action permanently stops one paused catalog job while
preserving its recorded local effects. It supports known created/reused source
cards and preflight mapping conflicts that cannot use the original
[unsent withdrawal](atomer-catalog-job-control.md). It does not merge cards,
complete a source/native/content migration, reconcile an uncertain native
request, or change physical quantities, prices or stock history.

The feature is implemented in code with synthetic PostgreSQL coverage. Those
tests do not establish that any particular live family is eligible or has been
handed off. Every live action requires its own complete current preview and
explicit confirmation.

## Preview the complete family

`GET /api/migration/atomer-progress/catalog-engine/campaigns/{campaign_id}/jobs/handoff-control?family={family_key}`

This operator-only, `Cache-Control: no-store` GET runs in a read-only
repeatable-read transaction. It has no durable preview row, recovery side
effect, source refresh or native request. Its response includes:

- `schema`, `campaign_id`, `family_key`, `stage`, `job_revision` and
  `operator_paused`.
- `scope_codes`: the exact union of saved guards, current siblings, source-card
  receipts and proofs, including excluded/blocked/done siblings and historical
  codes no longer in the current family. `selected_codes`, `saved_codes` and
  `current_codes` show the distinct selections.
- `review_changed`, acknowledging a valid current review change without
  overwriting any original guard or review.
- `local_effects`: counts of retained source rows, proof rows, sticky-created
  source rows, reused source rows and blocked source rows. These are receipt
  counts, not a reconstructed count of all lifetime allocations.
- `retained_card_ids` and `mapping_count`, when the identity closure is complete.
  The list includes old/retired IDs and all discovered conflicting/current
  owners. It does not choose a canonical card or assert physical equivalence.
- `writer_inventory`: complete manifests, counts and blockers for current
  writer documents, with `complete` distinct from the bounded diagnostic
  projection flags. Timing/cache telemetry is excluded from confirmation hashes.
- `eligible`, `complete`, `snapshot_hash` and structured `blockers`. An incomplete
  scan or timeout never yields an applicable confirmation. A timeout has a null
  hash; size/schema errors can instead return an explicit HTTP conflict code.

The response always says `historical_native_effects: "not_determined"`,
`source_publication_completed: false` and `content_publication_completed: false`.
An eligible GET is a reviewable candidate. The POST must reproduce its proof
under the worker/writer fences before any state transition.

### Supported current state and retained history

The campaign must be operator-paused, with the exact current source/run/admin
hashes. The job must be `pending`, its document must be **SQL NULL**, its receipt
must be the literal JSON object `{}`, and it must have control revision zero,
no control UUID and `operator_withdrawn=false`. JSON `null` is not SQL NULL.
Attempt counters may be positive; an HTTP error or `native_origin=null` is not
used as negative evidence of native effects.

Every retained row is checked against the current known producer schema.
Ready source receipts must carry the complete local card identity and source
fields. Partial proofs must have `verification_scope=source_card_only`.
Known nested `source_card` and `creation_evidence` records remain intact. The
source writer's sticky `created=true` is accepted after reuse only when the
retained creation proof identifies that same local card. Blocked source rows
support the precise preflight reasons `atomer_source_card_requires_review` and
`local_mapping_conflict`; other reasons and incomplete/unknown history hold.

Prepared/sending/observe/content/related documents, nonempty receipts,
historical reset/replan lineage, unknown producer fields and uncertain writes
are outside this handoff class. This contract does not add an override to
withdrawal or to native recovery. Existing uncertain writers must be resolved
through their own original operations.

### Current identity and writer closure

The snapshot retains the complete original campaign/job/reviews/proofs/source
rows. Allocation and immutable rename-audit queries cover the full union of
family codes, not only the current source family.

Raw products, all identifiers and SKU identities, every sale mapping (including
hidden/inactive/retired mappings), source/cached native anchors and canonical
aliases are expanded to a fixed point. Both ends of an alias and every
conflicting owner are included. Raw records are never redirected or rewritten.
Source and target names are both retained. GTIN owner discovery uses the shared
checksum/equivalent EAN/UPC rules; supported compound barcodes retain every
member and the original value. Unsupported separators hold. Missing or
inconsistent included mapping anchors return an explicit blocker with mapping
ID/shop; this endpoint does not manufacture a parent/native ID or perform a
native read to repair it.

The existing exhaustive typed scanner examines import drafts, AI jobs, Atomer
jobs, consolidation documents and complete current filesystem catalog journals,
including their linked evidence. Only this independently validated candidate
receives a context-only exception, bound to its exact campaign/family,
PostgreSQL row SHA-256 and own proof hash **inside the complete enumeration**.
No exception is inferred from sampled records. Other matching or unknown
operations still block. Ordinary consolidation scans use the unchanged default
path and do not receive this exception.

Separate schema-discovered JSON/JSONB and product-FK checks cover editor,
stock/FIFO/supplier publication, outbox and repair documents. Known schemas have
explicit terminal contracts; unknown nonempty JSON/JSONB tables hold globally,
even with a completed-looking label or unfamiliar numeric identity key. A
verified repair containing a prepared/sending/uncertain/deferred nested state
still holds. Active catalog campaigns, unresolved category intents and unscoped
stock-sync runs also hold. Immutable local/stock/invoice facts are preserved;
the physical-history eligibility rules for a later consolidation remain separate.

Bounds are explicit: 500 family codes, 501-row proof/source sentinels, 5,000 rows
per identity/writer selection, 32 closure iterations and a 5 MB complete
confirmation preimage. A complete count exceeding a writer bound is reported
as incomplete rather than as a clean truncated selection. Writer manifests hash
fixed-size SHA-256 row hashes of PostgreSQL JSONB text. The existing typed/FS
scanner retains its own row/byte/page/journal limits. The read/proof phase has a
120-second deadline; cancellation rolls the transaction back and cannot continue
using already-released fences.

## Confirm and recover

`POST /api/migration/atomer-progress/catalog-engine/campaigns/{campaign_id}/jobs/handoff`

Send the exact hash and **all** returned `scope_codes` under one new operation
UUID, after reviewing the retained effects and changed reviews:

```json
{
  "request_id": "00000000-0000-4000-8000-000000000001",
  "family_key": "v:SYNTHETIC-PARENT",
  "expected_snapshot_hash": "<64 lowercase hex characters from the fresh GET>",
  "scope_codes": ["SYNTHETIC-LEAF-A", "SYNTHETIC-LEAF-B"],
  "purpose": "identity_migration",
  "acknowledge_preserved_local_effects": true,
  "acknowledge_review_changes": true,
  "reason": "Preserve all local allocations and move this complete family to manual identity work.",
  "confirmed": true
}
```

The example UUID is illustrative. Allocate one real UUID for each reviewed
operation. All three confirmations require literal `true`; integer/string
coercion and additional request fields are rejected. Codes must be distinct.
Changing the family, scope, reason or any payload field under a previously used
UUID is a conflict. A fresh hash or missing code mismatch cannot apply.

The POST claims the remote worker, review, shared identity, refresh and local
worker locks. It takes NOWAIT fences on every scanned identity/source/writer
table and locks the exact campaign/job/proof/source rows. New writer/proof
phantoms and concurrent work cause a retryable conflict before an audit or job
transition. Unrelated stock/invoice history tables are not table-locked simply
because they exist.

After a fresh complete proof matches the review, the same transaction appends
the immutable control request, full before/after job and proof preimage, then
sets only:

- `stage='excluded'`, `source_card_stage='blocked'` and
  `operator_withdrawn=true`;
- the next control revision, exact request UUID and update timestamp.

Original guards, receipt, attempts, error text, schedules, source/proof rows,
reviews, identities, mappings, prices and physical stock history are retained.
The campaign remains paused. A successful response is sent after commit and
reports `current_native_intent="none_under_fenced_complete_scope"`, with no claim
that historical native effects were absent. The response's `status=completed`
means the **handoff operation** committed; source/content/identity migration
completion fields remain false.

After a lost response, use the existing receipt route:

`GET /api/migration/atomer-progress/catalog-engine/job-control-requests/{request_id}`

An exact POST retry returns the saved committed result. If no receipt exists,
retain the original payload while checking the original operation; do not
invent a new UUID to work around uncertainty. A changed snapshot requires a new
review after the old operation is known not to have committed.

The job is terminal: `jobs/replan` returns `catalog_handoff_terminal`. An
independent SQL trigger also refuses direct/no-op updates, deletion or a new
otherwise exact audited replan after handoff. Existing proof/source fences
preserve their original rows. A campaign resume, bulk registration, source
retry or stale background candidate cannot reactivate this job.

## Subsequent migration and deployment

Handoff leaves identity consolidation, existing-native-leaf linking and final
publication verification as separate operations with their normal guards. The
ordinary dependency scanner no longer sees this terminal job as a pending
writer, while all retained proof/card records remain. Existing frozen blocked
consolidation reviews retain their original UUID/hash and must be settled
through the supported discard/recovery and generation-lineage workflow before
a new review. No generic handoff action rewrites them.

Engine status exposes `counts.handoff_families` and `counts.handoff_leaves` in
addition to unchanged proof counts. Preserved partial proofs stay partial;
handoff never creates a verified proof. If resumed remaining work runs out, any
handed-off family keeps the campaign in `review`, with publication explicitly
outstanding, even if every current leaf already has a partial proof.

Migration `050_atomer_catalog_handoff.sql` adds the `handoff` audit action and
terminal SQL protection. It is packaged in the API image and installed after
049 by the normal migration runner. It transitions no jobs, enables no workers
and changes no business/stock rows. Keep 049/050 and their immutable audits on
rollback. The separate terminal trigger survives reinstalling 049's older
generic control function; this prevents an older API from replanning an already
handed-off job. Restore application behavior with a forward fix, not by removing
the fence or editing retained history.
