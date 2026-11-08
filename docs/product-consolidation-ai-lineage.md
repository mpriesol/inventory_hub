# Historical and replaced AI jobs in consolidation checks

The read-only dependency parser recognizes three additional writer contracts.
Every row remains in the complete inventory and its frozen hash. These checks
do not cancel jobs, change stored documents, publish content or contact a shop.

## Explicitly replaced draft jobs

`product_import.prepare_ai` replaces the selected rows' `ai_job_id` bindings and
the complete automation job list. `apply_ai` only selects current row bindings;
`advance_ai` additionally requires membership in that list. A replaced job is
recognized only when no current row links to it, its original source IDs are
still present, every selected row has a different current automation binding,
and each original fact matches the current row's AI baseline identity. Missing
backlinks without these replacement proofs remain unverified.

The complete current draft is still parsed, including unrelated rows, effective
import identities, original sources, baselines and publication history. Its
resolver queries and publication journal references remain part of the proof.
An uncertain generation status or a paused automation flag does not grant an
exception. A publication preview or automatic publication instruction on the
old job excludes this replacement proof. Partial replacement, mixed update
intent, unknown automation fields and changed baselines remain blocked.

## Legacy shop estimates

Older shop-content estimates omitted `source_parameters_loaded`. The current
AI start and shop-source comparison writers reject that source version and
require a fresh preparation. The parser recognizes only an estimate with no
output, product checks, approval, publication or update intent, with the exact
frozen snapshot digest and matching factual parent identity.

This does not discard the native identity: the complete parent and every
variant still contribute their normal possible-write scope. A matching parent
or sibling therefore remains a dependency even when the job is archived.
Prepared, sending or uncertain updates do not receive this legacy exception.

## Retired Sloger supplier probes

The historical `supplier_lookup` writer made a bounded, read-only supplier
observation; its current service only reads saved audits. Product AI entry
points reject this kind. The parser validates its complete job and context
schema, lookup lifecycle, costs, usage, events, diagnostic and strict
`SlogerObservation` result. The result must match the requested supplier SKU
and cannot pass the product-content model used by import drafts.

Supplier SKU and observation EAN values are audit context here, not product
write targets. A kind label alone is insufficient: product context fields,
publication IDs, product checks, mixed content output and unknown nested fields
remain blocked, including empty or null intent fields. Other supplier job
schemas remain unsupported.

Apply repeats these proofs under the existing input fences. Any changed job,
draft binding, baseline, publication or supplier audit invalidates the frozen
dependency evidence and requires a new preview. Stock and history guards are
unchanged.
