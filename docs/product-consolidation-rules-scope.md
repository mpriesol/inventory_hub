# Rule proposals in consolidation dependency checks

The dependency parser recognizes the reviewed `kind=rules` AI proposal
workflow separately from product content jobs. It validates the complete saved
job shape, the rule or category snapshot, the frozen default policy, generation
metadata and the exact `ProposedInstructions` output model. A kind label, an
archived flag or a completed status alone is insufficient.

`propose_rule` creates an estimate, and the generation worker produces a rule
proposal for review. `accept_proposal` checks the published rule version and
saves a **new unpublished rule-book version**. Publishing that version is a
separate operator action; it does not execute a product job. A rule's declared
product scope and prose therefore provide context, without authorizing writes
to that product. The parser never calls the rule publication service.

Product source facts, native update previews, import-draft links, automatic
publication instructions, a non-null publication preview ID, product checks,
mixed product output and unknown fields keep the dependency hold. Native
reference-product snapshots inside category proposals are also outside this
initial contract. The strict proposal output cannot pass the `Content` model
used by product import drafts. This boundary matters because some shared
content/draft readers do not reject a job solely by its kind.

Recognized proposal stages include an interrupted or failed provider request;
these are requests to generate rule text, with no product intent fields.
Product jobs in an uncertain state retain their existing checks. Unknown AI
kinds remain unsupported. Reopened or otherwise changed proposals containing
additional product-workflow fields also remain on hold, even when an added
field is null.

The full job row remains in the inventory and its frozen hash. Apply repeats
the same parser under the existing database write fences, so a changed
proposal invalidates its prior preview. All import drafts and catalog journals
are still scanned independently. This feature does not cancel jobs, publish
rules, repair stored documents, change stock or make any remote request.
