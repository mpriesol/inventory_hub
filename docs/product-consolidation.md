# Reviewed consolidation of one physical product identity

The operator can retire duplicate Hub cards after establishing that they describe
the **same complete retail unit**. One current card then owns all explicitly
reviewed shop listings. The original product IDs, SKUs, identifiers, balances and
historical movements remain on their original records.

This V1 changes current identity ownership only. It does not add quantities,
transfer stock or costs, confirm a count, create FIFO layers, delete native
products, send shop updates, or enable a collector or publisher. For example,
historical unconfirmed quantities of 3 and 2 remain 3 and 2 on their respective
original IDs. They do not become a confirmed quantity of 5 on the surviving card.
Current stock still requires the existing separately confirmed opening or
receiving workflow.

## Physical identity and allowed scope

The keeper and every duplicate must be `item_type=stock`, `stock_unit=pcs`. The
keeper must be active. The keeper must have exactly one verified GTIN identity;
equivalent valid UPC/EAN representations count as one identity. Every other
verified barcode must belong to that same identity. Unrelated owners, including
inactive owners, remain conflicts. Missing or contradictory physical variant
evidence requires an independent review before using this workflow.

The review records public manufacturer or other primary source URLs, a concrete
description of the complete physical unit, and the reason the existing cards
refer to it. A common name alone is insufficient. The server also compares
available canonical axes on the cards and fresh native listings. Any conflicting
common color, size or other canonical axis blocks the operation. It never
invents an EAN, manufacturer code or conversion factor.

Each sales target has factor **1**. A complete sealed pair or complete cookware
set can be one stock piece when that is the unchanged retail unit. Consolidating
two listings for that same complete set does not split it into its components.
This factor-1 alias case is distinct from the reviewed loose-piece multiples
described in [shared stock sales](shared-stock-sales.md). When several targets
exist in one shop, their mappings use the same explicit shared-sale mechanism,
each with its own native identity and a factor of 1. A single target stays an
ordinary mapping. Existing shared groups cannot be consolidated or extended by
V1; use their dedicated review workflow.

Existing aliases cannot be reversed, extended or chained by V1. A previously
retired card cannot become a keeper. Every existing mapping of every selected
card must be enumerated, including unlisted mappings. A duplicate with a scoped
`supplier_sku` identifier is unsupported in V1: deployment migration032 still
validates original supplier ownership, and its deployed SQL is unchanged.
Keeper supplier aliases and bindings remain supported. Existing pending pushes,
non-unit factors, unsupported shop platforms and inactive shop integrations
block the operation.

## API contract

All endpoints require operator access. Cookie writes retain the shared
same-origin protection. Responses have `Cache-Control: no-store`.

| Endpoint | Effect |
| --- | --- |
| `POST /api/product-consolidations/preview` | Reads exact current native identities and stores a durable review. |
| `GET /api/product-consolidations/{id}` | Returns the original review and any completed result. |
| `POST /api/product-consolidations/{id}/apply` | Rechecks the frozen review and atomically records local ownership changes. |

A preview contains one `keeper_product_id`, one to nine `retired_product_ids`,
the exact mode `identity_only_preserve_unconfirmed_history`, and two literal
boolean confirmations: `same_physical_retail_unit_confirmed=true` and
`legacy_initial_history_preserved=true`. It also includes `identity_evidence`,
up to 50 `mapping_actions`, and up to 20 `additional_sales`.

Each existing mapping has one explicit action:

| Action | Required evidence and result |
| --- | --- |
| `retain` | The exact current mapping already belongs to the keeper. Its native kind, IDs, code and parent remain unchanged. |
| `bind` | Name the exact live native leaf. Preserve the original mapping as an immutable retired row and create a new current mapping on the keeper. |
| `retire_absent` | Prove the old exact native claim absent from both active and archived partitions. Preserve the mapping without creating a replacement. |
| `retire_unverified_code_absent` | Explicitly retire a standalone historical claim whose normalized and preserved legacy IDs are all SQL NULL, after proving its exact code absent from both namespaces and archive partitions. The old native identity remains unverified. |

`additional_sales` names independently verified live leaves with no foreign
current owner. Every target includes `shop_code`, `code`, `parent_code`,
`is_variant`, `native_product_id`, an appropriate `native_variant_id`, and an
integer `sale_to_stock_factor` of 1. A standalone target uses its own code as
`parent_code` and omits `native_variant_id`. Codes and native identities must be
unique within each shop.

A true null-ID mapping can bind only its unchanged exact code, kind and parent,
and discovery must find exactly one matching live leaf. A pinned variant whose
parent ID is missing can obtain its parent from that same fresh exact variant.
Other changes of native identity require proof that the old exact target is
absent. A preserved positive legacy `external_id` can be used only as an old
standalone absence claim; it never certifies current ownership. Admin URLs,
titles and a normalized null ID are not absence evidence. A hidden target that
still exists fails `retire_absent`.

### Historical claims with no recorded native ID

`retire_unverified_code_absent` is a separate action with `target=null`. The
server reads the original mapping by `mapping_id`; the request cannot supply an
old ID or a replacement code. This action requires `is_variant=false`, a null
`parent_code`, null normalized product and variant IDs, and an actual SQL NULL
in the physical legacy `external_id` column. A missing database column, empty
string, invalid value or positive legacy ID blocks this action. The column is
verified through the PostgreSQL catalog before a direct SQL null check. There
is no schema repair or fallback from a missing JSON key.

For each shop, the original codes are read through four complete, bounded
[`GET /products/simple` scopes](https://docs.upgates.com/api-reference/produkty-seznamy):
`codes` and `variant_codes`, each with `archived_yn=false` and
`archived_yn=true`. `variants_yn=false` omits sibling payloads; no `active_yn`
filter is applied. The variant-code filter returns the containing product,
so any returned row blocks the action even when the result is a parent, set,
hidden item, differently cased code or unexpected record. All four results
must be complete and empty. Authentication failure, rate limiting, timeout,
incomplete pagination, connection change or exhausted shared request budget
cannot produce an absence proof. These four reads consume the existing maximum
of eight requests per shop and review phase, together with other identity reads.

The preview separates `plan.unverified_code_absence` from native-ID `absence`.
Its observation records the exact codes, four checked scopes, connection
fingerprint, request count and check time. Both preview and completed apply
state `old_native_identity_status="unverified"`,
`native_id_absence_proven=false`, and `exact_code_absence_proven=true` in that
observation. Apply repeats all four reads and stores its fresh observation in
`result.unverified_code_absence`, preserving the original preview separately.

Code absence does **not** establish that an unknown original native record was
deleted: that record could previously have been renamed. The action retires an
unverified local claim after an independent physical identity review. It does
not reconstruct an ID, assign a new native owner or certify full native catalog
coverage. The original code, owner, null IDs, shop observations and history remain
on the retired mapping. Existing dependency and stock guards still apply. A
foreign current mapping claiming the same shop code also blocks retirement,
including a differently cased claim. Local claim changes or a code reappearing
before apply expire the review without an ownership mutation. External absence
observations are point-in-time reads; the API does not offer an atomic catalog
snapshot across the subsequent local transaction.

Use `retire_absent` whenever a preserved exact old native ID is available; its
native-ID absence checks remain unchanged. Neither action deletes a shop item.

Synthetic example, with every mapping explicitly enumerated:

```json
{
  "request_id": "98d2ad5b-c4b5-4257-a221-c985b1b11bc8",
  "keeper_product_id": 101,
  "retired_product_ids": [102],
  "mode": "identity_only_preserve_unconfirmed_history",
  "same_physical_retail_unit_confirmed": true,
  "legacy_initial_history_preserved": true,
  "identity_evidence": {
    "review_note": "Both existing cards describe the same verified manufacturer article and complete retail unit.",
    "physical_unit": "One complete synthetic cookware set",
    "source_urls": ["https://manufacturer.example/products/synthetic-set"]
  },
  "mapping_actions": [
    {
      "mapping_id": 201,
      "action": "retain",
      "target": {
        "shop_code": "biketrek", "code": "SYN-SET", "parent_code": "SYN-SET",
        "is_variant": false, "native_product_id": 301, "sale_to_stock_factor": 1
      }
    },
    {
      "mapping_id": 202,
      "action": "bind",
      "target": {
        "shop_code": "biketrek", "code": "SYN-SET-ALIAS", "parent_code": "SYN-SET-ALIAS",
        "is_variant": false, "native_product_id": 302, "sale_to_stock_factor": 1
      }
    }
  ],
  "additional_sales": []
}
```

Inspect `eligible`, `dependencies.blockers`, the full action plan, exact native
identities and preserved-history hashes before applying. Confirmation sends only
`{"preview_hash":"<exact preview hash>","confirmed":true}`. It cannot change
the keeper, factor, native selection or evidence. Previews expire after 30
minutes. Repeating an identical completed request returns its saved result
without another native read or mutation. Reusing a UUID for a changed request is
rejected. A definitively failed apply expires the prepared review before
returning `known_unapplied=true`; a lost response is recovered by GET or the
identical apply request, not a new operation.

## Dependencies and concurrency

V1 admits only precisely recorded historical `INITIAL` movements from the
Upgates import, with matching source, creator and original SKU reference. It
preserves their full-precision records and all unconfirmed balances unchanged.
Any confirmed tracking, FIFO state/layer/receipt/cutover, reservation,
quarantine, nonlegacy movement, receiving or inventory-count dependency blocks
the group by default. A narrowly verified keeper receipt can use the explicit
option below. Existing order lines by product ID, sale mapping or former/current
sales code also block it. Supplier bindings and editor/SKU overrides can remain
on the keeper; such dependencies on a duplicate require a later supported
design. They are not silently moved.

The dependency inventory reads the installed PostgreSQL catalog. Unknown
nonempty product references, missing required tables, missing or disabled
retired-ID guards, matching unsettled JSON operation documents and active
catalog campaigns block apply. The inspection includes preserved identifier
values, original/current sales codes, mapping IDs and native IDs. Catalog import
files are bounded and read with their durable result/event journals; an old
completed file snapshot cannot hide a later sending or uncertain event. The
review stores counts and hashes instead of copying order or invoice contents.

Fresh native HTTP reads finish before database row locks. Apply then obtains
the shared identity lock, review row, warehouse admission and selected Product
and mapping locks. Physical writers recheck retirement after their Product
lock. Once those row locks are held, short table `SHARE NOWAIT` locks freeze
foreign SKU/GTIN/native/supplier ownership through the final identity scan and
commit. Existing writers cause an immediate
`consolidation_identity_writer_busy` conflict instead of a new blocking lock
cycle. No HTTP request runs inside that database critical section. New writers
can wait briefly until the transaction completes; reads remain available.

Catalog publication also holds the shared identity lock for its final current
owner check and durable sending checkpoint. Its HTTP POST remains outside the
lock. A stale preparation cannot publish a former SKU as a new canonical card
after retirement.

### Explicit keeper invoice and paused receiving preservation

`preserve_keeper_invoice_receiving` is a strict boolean preview option, default
`false`. Omission and explicit `false` retain the original request hash and old
UUID replay behavior. `true` freezes an additional server-generated proof in
the preview. The client cannot specify exempt invoice IDs, receiving lines or
dependency tables. This option changes neither archive matching nor physical
receiving. It does not call reconcile, resume or finalize.

The server scans the complete receiving scope, including NULL-product lines,
raw supplier SKU/GTIN values, stored supplier-product references and referenced
saved drafts. Both the raw resolver used by finalization and the enriched
receiving projection must independently and unambiguously resolve each relevant
line to the keeper before and after the identity alias is created. The stored
line may already point to the keeper or remain NULL. Its session must remain
paused. A reference, candidate, pin or referenced draft identity on a retired
card blocks the group. Missing configuration, incomplete discovery, unresolved
source evidence and relevant warnings also block it. A directly linked import
draft on a related receiving session remains a hold even if it is settled or
its document is empty and contains no selected product token. This option does
not exempt import intentions. Discovery currently bounds
the installation to 1,000 sessions, 20,000 receiving lines and 2,000 selected
invoice claims; exceeding a bound returns a hold rather than a partial scan.

Only parsed purchase invoice claims already pinned to the keeper qualify. They
must have a verified source hash and independently consistent raw identity and
stored supplier source. An optional receiving header link must identify the
same supplier and invoice number in a paused session with a proven relevant
line. It is not treated as a line-to-line foreign key. Any retired invoice
claim remains blocked. The existing guards for orders, tracking, FIFO, reserved
stock, other financial references and active operation documents are unchanged.

The proof stores IDs, counts and full stored-row hashes for the complete relevant
invoice documents and receiving sessions, including all their lines, scan
records, source snapshots and referenced drafts. It does not expose invoice
contents in a preview. Apply compares the proof with the frozen preview, then
repeats both identity resolutions and all preservation hashes after alias
creation. Any difference rolls back the entire consolidation and expires that
review with its existing known-unapplied fence. Original quantities, prices,
costs, product pins, NULLs, session status and timestamps are preserved.

Input tables use `SHARE NOWAIT` after the existing identity/Product lock order.
This prevents a concurrent unbound line or source row appearing between the
final scan and commit. The proof does not acquire receiving row locks, because
receiving writers acquire their session before the shared identity lock.
An already active writer produces a busy conflict rather than a lock cycle.

Supplier configuration is read under a shared filesystem lease on the existing
prefix registry lock, held through proof and commit. The proof never calls the
ordinary loader, which can discover and write registry entries. Normal config
load/save/claim/rotate first try the exclusive lock without waiting. Contention
inside an asyncio event loop returns HTTP409 `supplier_configuration_busy`
before that config operation changes data; synchronous worker threads/processes
retain their serialized waiting behavior. The global response does not assert
that an entire enclosing batch was unapplied: existing per-item journals and
uncertain-write recovery still apply.

This extension requires no SQL migration. Isolated PostgreSQL tests exercise
two keeper invoice claims, a paused NULL-product line, unchanged full rows and
stock, raw/enriched disagreements, retired pins, a new unbound phantom, writer
contention, session/identity lock order and post-alias rollback. A separate
synthetic future receiving test verifies that an explicit later reconcile and
finalize still use the same keeper, quantity, cost and movement UUID. Those
tests do not authorize or perform a production receipt.

## Current reads and historical records

### Typed catalog dependency scope

The dependency inventory parses the known import-draft, AI-job, Atomer-job,
consolidation-review and catalog-journal writer schemas. Hub product IDs,
supplier product IDs, mapping IDs and shop-qualified native product/variant
IDs have separate namespaces. Dictionary keys in an Atomer selection and
dictionary values in a saved Hub-owner map are inspected as identities.
Unknown fields, missing lineage, invalid barcodes and incomplete observations
produce an explicit hold, including unsupported documents otherwise unrelated
to the selected group. Unknown JSON tables keep the conservative existing
reference guard.

A known shop selector's display name and a create-only family container can be
reported as context after all executable leaves, aliases, GTINs and pinned
owners are inspected. An existing-parent content update remains a dependency
of every affected leaf in that shop. A historical other-shop container in an
Atomer receipt can be context only when its exact current code, native leaf,
mapping and Hub owner are independently consistent. Pending Atomer selections
remain blockers; pausing a campaign does not withdraw its jobs.

Staged AI requires the actual stored draft ID, reciprocal row/job links, exact
source selection and the original AI baseline. The complete current draft is
checked because one finished family can advance save/publication for every
row. The authoritative per-shop publications are followed; an aggregate display
preview ID is not mistaken for a missing importer file. Supplier catalog IDs
are resolved through their current source rows and normal identity policy.
Unsaved manufacturer proofs use the same read-only SKU policy as draft save.

Known database documents are read without the old string-only prefilter. Every
complete original row is hashed, including documents classified as disjoint.
The v2 inventory uses deterministic primary-key pages, with identical database
collation in ordering and keyset continuation. It has no global 1,000-job or
20,000-family-leaf cutoff. Each operation loads its complete exact linked
family, draft, supplier/feed source and saved mapping claims. Full-row and
logical-link manifests use length-framed records independent of page size.
Repeated linked rows remain represented for every operation that uses them.

Bounds are 16,000,000 serialized bytes per raw row, 64,000,000 retained input
bytes for an operation and 256,000,000 charged database body bytes per scan.
These are decimal bytes, not resident-memory guarantees. A linked selection
for a single indivisible operation remains limited to 20,000 rows, and distinct
identity queries to 20,000. Charged work includes repeated source/draft reads;
an admitted row can be charged before a cancelled fetch finishes. Overflow
produces an explicit incomplete hold, never a successful prefix of the scope.
Samples share a 1,000,000-byte allowance, normally retain 25 rows per table and
prioritize bounded examples of later unknown/reference reasons. Full counts,
reason histograms and manifests cover every inspected operation. Matched/context
paths are separately limited to 100 per record; truncation changes output only.

Catalog journals use the pure merged base/result/event loader. No recovery
reader, AI public serializer or network request runs during dependency proof.
Base files and sidecars are hashed before and after parsing and database owner
resolution; directory membership is compared too. There are at most 1,000
base journals per shop, 16 MB per file and 64 MB in total. An `invalid` item is
not treated as settled. Complete disjoint create intent may be classified as
context, while matching uncertain/missing/malformed intent remains blocked.
File bytes are separately reported for each of the two observations; they are
not charged as database work. The per-pass byte limit is unchanged. These
numbers describe serialized file sets, not physical I/O or memory consumption.

Preview starts one repeatable-read database snapshot before the first
post-native advisory-lock statement. A serialization race rolls back and
returns a sanitized known-unapplied conflict without repeating native reads.
Only the exact consolidation UUID primary-key collision can replay an already
committed review after rollback and a fresh request-hash check. Apply remains
read committed under its existing complete input fences. A prepared v1 inventory
requires a new v2 review; already completed reviews retain idempotent replay.

The typed scanner has a 120-second deadline, including awaited database reads.
Expiry cancels the in-flight await and explicitly rolls back. Preview returns
`consolidation_dependency_inventory_timeout` with known-unapplied status and
does not save a partial prepared review; apply uses its existing expiry fence.
The diagnostic GET may return captured plain metadata and incomplete progress
after rollback, with no further database or expired ORM reads. The deadline
covers this scanner, not preceding native discovery or unrelated endpoint work.

Apply repeats the full proof under the identity lock and `SHARE NOWAIT` table
fences for the known document/source inputs, then compares it with the preview.
This fences concurrent inserts as well as updates. It does not wait for a
writer that already holds a draft/job row before requesting the identity lock.
A busy writer or changed document expires the apply through the existing
rollback/result protocol. Filesystem checks remain bounded observations;
no filesystem lock is held through the database commit. A new unsent journal
can appear after the last observation. Publication executors still verify the
current canonical SKU and identity before their durable sending intent; an old
retired-SKU plan is not authorized by a stale file. A genuinely new keeper plan
is evaluated by the current executor guards.

The dependency-diagnostics GET includes bounded `typed_scope` classifications
and keeps the legacy matched-word explanation separate. It exposes identity
paths, counts and hashes, not document bodies, prompts or financial contents.
Top-level `complete` describes the legacy explanation;
`typed_scope.projection_complete` separately describes the typed projection.
`typed_scope.inventory_complete` states whether the full typed inventory was
covered. Error-code histograms are deterministic per table and classification;
recognized AI lifecycle labels never substitute for executable-scope proof.
Only the reviewed product-AI, strict rules-only proposal and Atomer document
shapes are understood. A [validated rule proposal](product-consolidation-rules-scope.md)
can save only an unpublished rule version. Other AI kinds or additional Atomer
receipt fields stay on hold until their actual writer contract is reviewed;
this parser is not a generic exemption for them.
This change performs no catalog, stock, invoice, receiving or publisher writes
and needs no new migration.

The diagnostics GET also returns a metadata-only `scope_inventory` before the
bounded loader runs. For every known table it reports the exact selected row
count, total full-row JSON bytes, largest row and each exceeded scanner limit.
The Atomer current-family join has the same separate totals. Fixed lifecycle
buckets distinguish SQL NULL, JSON null, known states and an `other` bucket;
unknown text, prompts and full documents are not returned. The census uses the
same read-only repeatable-read snapshot as the rest of the diagnostics. Its
`complete` flag means the metadata queries completed, and does not make a
blocked review eligible. Base-table bytes are a work lower bound. The separate
family join total repeats family-work per leaf and omits campaign, mapping,
source and draft closures, so it is reference metadata rather than the new
scanner's exact charged work. Current scan limits and actual work are reported
separately; paging changes no identity, lifecycle or financial eligibility rule.

`product_canonical_aliases` is an audited, immutable one-hop map from a retired
ID to its keeper. Current SKU, scoped supplier-code and barcode ownership use
that map before checking for multiple owners. Unrelated inactive products are
still included in conflict detection. An explicit retired `expected_product_id`
is rejected rather than silently rewritten.

Current product lists exclude retired cards. Searching a preserved old SKU or
identifier returns the keeper. Exact historical editor detail remains readable
on the original ID and includes `retired` and `canonical_product_id`. The
original identifiers remain on the retired card; current scanners and
suggestions project their ownership onto the keeper. Supplier relationships
remain on the keeper; V1 blocks supplier aliases on a duplicate. Original product
SKUs stay reserved. The keeper's SKU, physical unit and identity edits require a
separate review after consolidation.

Current ORM mapping queries automatically exclude rows with `retired_at` set.
An explicit historical query can opt in with
`execution_options(include_retired_shop_mappings=True)`. Raw SQL current readers
use the same `retired_at IS NULL` condition. Retired mapping rows cannot be
updated, deleted, revived or used as new current owners. Direct and bulk writes
against retired product IDs are also rejected by database guards after Product
locking, including stale ORM instances. Ledger, FIFO and invoice/history readers
keep original IDs and never aggregate old quantities through canonical aliases.

## Deployment and recovery

Migration `047_product_consolidation.sql` is additive and explicitly packaged
and run after `046` by `python -m inventory_hub.migrate`. The runner now includes
36 upgrade modules. Installing the migration creates the review/alias tables,
mapping retirement fields, current-owner indexes and guards; it does not
consolidate any card or activate stock operations. Required feature tables must
exist before apply. A new product-referencing table added later without a
retired-ID guard causes review to fail closed until its integration is reviewed.

Before first production use, retain a restorable backup, verify the complete
plan and dependencies, and apply one reviewed group. Verify the durable result,
exact current leaf ownership, original stock/history hashes and unchanged
publisher settings before continuing. Busy or unsupported groups remain
unapplied and need a new review after the specific blocker is resolved.

After a completed operation, keep the migration, retired rows, aliases and
immutable review. Do not deploy an older application that treats retired
mappings as current or bypasses the retired-ID guards. There is no generic undo,
quantity transfer or alias-chain workflow in V1; use a reviewed forward fix.
Code tests and a successful migration are not evidence that a production group
has been reviewed or applied.
