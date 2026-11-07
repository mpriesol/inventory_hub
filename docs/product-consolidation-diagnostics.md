# Read-only consolidation dependency diagnostics

`GET /api/product-consolidations/{id}/dependency-diagnostics` explains the current
dependency matches for an existing consolidation preview. Operator access and
`Cache-Control: no-store` are required, as for the consolidation itself.

The server derives the scope from its saved preview. There is no request body,
caller-selected table, arbitrary file path, exclusion list or approval token.
The endpoint does not change eligibility, retire a row, cancel work, settle a
journal, retry a request, call BIKETREK or xTrek, or change any existing guard.

## Response and interpretation

The response identifies the consolidation ID, original preview hash/status,
frozen scope hash and observation times. `frozen_blockers` are the original
review's blockers; `current_blockers` are the existing scanner's current results
for that same scope. `matcher_policy` explicitly identifies the base dependency
scanner without optional keeper-only invoice/receiving exemptions. Such an
opt-in request is reported but its exemption helper is not invoked here; these
base blockers can therefore be more conservative than that optional workflow.
It deliberately does not turn an explanation into an apply
decision. A new preview and its normal apply checks remain authoritative.

For matching import drafts, AI jobs, catalog jobs and consolidation previews,
`documents` includes:

- Exact table and primary-key identity, current full-row MD5, and matching count.
- RFC6901 `matched_paths`, the exact matching identity value, matching rule and
  context label. These labels do not grant an exception.
- Selected identity fields and intent states only. Full documents, descriptions,
  prompts, prices, invoice contents and credentials are not returned.
- For catalog jobs with complete guards, the selected code set calculated by the
  existing engine selection rule, with its scope. Paused work remains unsettled.

The current conservative scanner compares all exact string values. A parent code
can therefore match a shop display label in another document. It also compares
unqualified `product_ids` and pooled native IDs. Diagnostics exposes those paths
and their context; it does not silently reinterpret supplier IDs as Hub IDs,
remove parent references, or assume a match is a false positive.

For filesystem catalog journals, the response keeps the matched base-document
paths distinct from merged sidecar intent states, with hashes and per-journal
completeness. It calls only the existing pure file loader, never the normal
`import_result` recovery function: that function can mark an interrupted queued
or running job as failed during a normal polling read.

## Consistency, bounds and limits

The request begins a PostgreSQL repeatable-read, read-only transaction before
loading the saved preview. No row/advisory lock or commit is required. Filesystem
records are separate observations and are checked against the scanner's hash and
a second read. A changed, oversized, malformed or symlinked journal is incomplete.

Responses are bounded to 100 rows per supported table, 100 journal explanations,
4 MB per document, 30 MB per database/file explanation phase, 500 matches and
2000 identity fields per document.
`complete=false` and explicit reasons expose truncation or unsupported schemas;
an omitted record never means absence. Non-document dependency blockers remain
visible in `current_blockers` without exposing their underlying business data.

The route is additive. It needs no database migration, configuration flag,
background worker or UI change. Tests use synthetic identities and local
PostgreSQL only. No test proves that a particular production document is safe to
ignore; that requires reviewing the actual returned identity/intent evidence.
