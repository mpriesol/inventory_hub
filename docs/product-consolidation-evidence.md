# Selected AI dependency evidence

The operator-only `GET /product-consolidations/{uuid}/dependency-evidence`
accepts one to eight distinct `job_ids` query values, each a lowercase UUID32.
It is an inspection endpoint, not a preview, clearance, recovery or apply action.
It does not change consolidation eligibility or run the global dependency scan.

One PostgreSQL `REPEATABLE READ READ ONLY` transaction reads the consolidation
header, the exact selected AI jobs and their complete current import drafts.
It follows the jobs' staging authorizations and authoritative per-shop draft
publication references, including drafts referenced by those journals. Selected
jobs' current draft backlinks are reported separately. There is no arbitrary
table, SQL or filesystem-path parameter and no native/provider request.
Backlinks are inspected in these explicitly linked drafts only; this is not a
global reverse-reference search of every draft or every AI job.

Database rows are supplied in `raw_json`, with a SHA256 of that exact UTF-8 SQL
JSON text. This preserves all stored keys and numeric precision. Journal base,
result and event files are supplied as exact `raw_utf8` with separate presence,
length and SHA256 fields. No ordinary public AI/draft reader or `import_result`
is called: those readers can perform interruption recovery writes.

Only explicitly linked journal IDs are read. All directory components and
files are opened without following symlinks. Every observed file set is read
again at the end; a changed file, missing base, malformed or incomplete event,
unknown link shape or exceeded bound yields `complete=false` and an explicit
problem. No partial body is silently truncated. Multishop aggregate preview IDs
are not mistaken for importer files; the complete target publication map is
the executor's authority.

Limits are eight jobs, sixteen linked drafts, thirty-two journal sets, sixteen
million bytes per stored document/file, thirty-two million aggregate admitted raw bytes,
and forty million serialized response bytes. Sensitive configuration/credential
keys, credentials in URLs and private-key material prevent that document from
being returned. Configuration, connection settings and arbitrary documents are
never loaded. Responses inherit the protected router's `no-store` handling.
The byte limits bound evidence payloads, not peak process memory: parsing,
serialization and the second observation require additional representations.

`complete=true` describes this selected evidence scope only. It does not prove
global disjointness, identify the cause of a changed backlink, settle an
uncertain operation or authorize consolidation. The DB snapshot is consistent;
the filesystem proof consists of two bounded observations and is not an atomic
filesystem snapshot or a publication lock. Changes after observation remain
possible and must be handled by the ordinary preview/apply guards.
