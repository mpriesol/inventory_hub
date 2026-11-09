# Warehouse launch and correction work — continuation record

Updated: 2026-11-09 09:37 UTC. Status: PR #185 passed all CI and merged as
`fd45035a3f1af21eaaa7830118b49e58b6da571b`; deployment and the new API routes are verified.
Order policies, read-only collection and webhook registration are configured.
Test document reset, stock adoption and live stock authority have NOT yet been
applied. Current costs from nine evidenced test FIFO roots were revised to
unknown with audit, without changing quantities or movement history.

This is the durable continuation record for the owner's 9 November instruction.
Read it together with AGENTS.md; do not depend on a previous chat.

## Repository and work location

- Repository: https://github.com/mpriesol/inventory_hub
- Base production commit: 50cc0f3b85534b715ad315f5218b010c75f1414d.
- Merged feature branch: codex/warehouse-launch-recovery (PR #185).
- Focused follow-up: codex/webhook-delete-recovery. It adds an audited manual
  decision for an absent DELETE notification using the existing sync journal;
  there is no additional database migration.
- Do not use OLD_inventory_hub. Do not redo the completed card consolidation.
- Before resuming, inspect branch/main heads, open PRs, CI/deployment, and the latest
  section below. A commit or green CI is not evidence that live operations ran.

## Owner decisions (confirmed 9 November 2026)

1. Implement the outstanding warehouse and basic supplier-invoice launch work.
   Keep it simple; one-off data preparation uses the existing API/service layer.
2. Adopt existing own-stock quantities once, then correct differences during
   inventory counts. Record this as an owner-adopted operational baseline, not a
   claim that every product was physically counted.
3. For an actual conflict between Atomer and Upgates own-stock quantities, use
   Atomer. For other unresolved quantity conflicts use the owner's explicit
   fallback of 5 base units. This is not a default for every missing/zero product
   and does not justify guessing identity or sale-to-base-unit factors.
4. The previously mentioned 5-versus-8 quantities were an illustrative example,
   not an identified product.
5. Goods in the unfinished receipts have physically arrived. All pre-existing
   receipts and invoices are test work and should be reset for a fresh start.
   Historical supplier invoices must not be replayed into stock: most goods have
   already sold, and the current quantity will be established by the baseline and
   later inventory counts.
6. Supplier invoices need an explicit “processed without stock effect” action.
   Require a nonblank operator note, show the effect, record the audit, and allow
   an audited reversal of the processing state.
7. Operators must be able to correct states in both directions in the affected
   managers, including stock effects where applicable. Show what happened and
   what the correction will do. Correct immutable stock/payment ledgers with new
   compensating events. Do not quietly edit historical movements.
8. Undocumented payments will be entered manually. Allow corrections in both
   directions through the settlement ledger; unknown is not zero.
9. Sales documents remain in Upgates. Do not make Hub sales invoicing a launch
   requirement.
10. BIKETREK and xTrek share the main warehouse. Existing accepted order rules:
    reserve on received/payment/awaiting pickup; issue once on dispatch or
    completed handover; cancel before issue releases reservation; cancel after
    issue/returns require explicit review/physical return.

## Authorization and safeguards

- Implementation, feature branch, reviewed PR, merge and deployment are covered
  by the current request and standing owner permissions recorded in AGENTS.md.
- The owner specifically authorizes resetting the pre-existing test invoices and
  receipts. Prepare an exact snapshot/manifest and verify recoverable backup
  before destructive reset. Archive/void with preserved original evidence is
  preferred when it achieves a clean operational start.
- Preserve product identity, original document bytes, scanner UUID results,
  immutable stock/FIFO/settlement history, reservations and uncertain-write
  fences. Never reset unrelated orders/products.
- Receipt reversal must account for downstream FIFO consumption, reservations,
  quarantine and cost evidence. If a direct reversal cannot be valid, show the
  exact dependency and supported corrective workflow.
- Do not automatically activate live Hub stock authority merely by deploying
  code. Prepare concrete policy/cutoff/baseline and verify all shared channels
  and competing writers before the separate live activation step.
- Never put credentials, production invoice/customer data, private source URLs,
  database dumps or server configuration secrets into this public repository.
  A different account must obtain its own authorized credentials.

## Work packages and ownership

| Package | Current status | Files/owner during implementation |
| --- | --- | --- |
| Invoice processing without stock, undo and manual settlements | Merged in PR #185; full CI passed; deployment verified | Invoice services/routes/UI/tests; migration 051 |
| Receipt correction/reversal and clean-start primitives | Merged in PR #185; full CI passed; deployment verified | Receiving/FIFO services/UI/tests; migration 052 |
| Topeak availability and source identity recovery | Merged in PR #185; full CI passed; deployment verified | Availability batch reader, diagnostics and pinned read-only Atomer source export |
| Order setup | Policies, read-only collection and webhooks configured; collector failure under investigation | See checkpoints and docs/order-launch-setup.md |
| One-off baseline/reset, full CI and release | Not applied | Primary agent |
| Portable handoff and this record | Being checkpointed | Primary agent |

Avoid shared-file conflicts: primary agent owns migrate.py, Dockerfile, CI,
OVERVIEW.md and AGENTS.md integration. Invoice agent coordinates both locale files.

## Implementation and operational acceptance

- Invoice “processed without stock” requires a note, creates no stock movements,
  is idempotent and reversible with history, and is separate from payment status.
- Receipt correction has a before/after preview and explicit reason; repeat
  requests cannot duplicate stock; consumed stock cannot be silently recreated.
- Manual payment and reversal preserve exact currency/decimal arithmetic,
  unknown balances and concurrent-change protection.
- Existing product/card identity invariant remains true for every native shop
  leaf; no duplicates introduced.
- Opening adoption handles confirmed/unconfirmed rows and existing test effects
  explicitly, without adding historical receipts twice. Costs remain unknown
  where evidence is absent.
- Topeak diagnostics distinguish feed parse failures from publication failures,
  current mapping scope from historical outcomes, and pre-write rejection from
  uncertain writes.
- Order collection/webhooks are configured together, with a fresh cutoff and
  current remote statuses. Old orders are not replayed into the new stock start.
- Run meaningful targeted tests and the required CI coverage gate on a dedicated
  localhost database ending _catalog_test. Never test against production.
- Verify deployed code and every live operation separately. Preserve request IDs,
  manifests, counts and applied/not-applied evidence in private operational
  checkpoints. Never retry a timed-out mutation without reading its outcome.

## Exact continuation procedure

1. Read AGENTS.md, this record and git status. Fetch branch/main and check PR/CI.
2. Preserve unfinished changes; do not start a second competing implementation.
3. Inspect changed feature docs/tests, migration runner/image packaging and
   private operational checkpoint (obtain it from the owner if using another
   account). Never infer “applied” from code availability.
4. Finish/review the three work packages, reconcile their contracts and run
   isolated validation.
5. No full server backup/access has been verified. The scoped reset uses tested
   reversible archival with preserved originals and ledgers, not deletion. API
   exports are recovery evidence, not a database-plus-files backup. Destructive
   reset remains outside this operational procedure.
6. Freeze exact test-reset scope and opening source manifest before any reset.
   Apply only once using stable UUIDs and verify postconditions.
7. Prepare order/webhook settings and stock baseline. Record any final live
   activation/access prerequisite explicitly.
8. Update this record, portable handoff, PR/deployment links and operation
   receipts after each milestone.

## Checkpoints

- 2026-11-09: main inspected at the base commit above; clean local feature branch
  created. Implementation assigned. No production data writes by this work.
- 08:37 UTC: work checkpoint pushed as
  `e85806d75233be65433fd16c3a99497d499a99c5`; this is WIP, not a release.
- Fresh native product snapshots completed about 08:29 UTC. Every one of the
  42,375 current sales leaves had exactly one canonical card. Null stock remains
  unknown. Historical source links absent from current snapshots are retained
  for history and must not be used as current publication proof.
- 08:41–08:42 UTC: both shops' accepted status policies, collector revision 1 and
  webhook revision 1 were configured through the API. Policies start at
  `2026-11-09T08:41:06.146665Z` (BIKETREK) and
  `2026-11-09T08:41:37.784409Z` (xTrek). Both remain in manual processing mode;
  stock publication/automatic issue was not enabled. Do not repeat initial
  configure requests with an empty expected revision: read current state first.
- Both sets of create/update/delete callbacks were registered. A real BIKETREK
  callback arrived at 08:44:03 UTC and completed at 08:44:04 UTC. No xTrek callback
  delivery had yet been observed. Registration is not proof of delivery.
- 08:58 UTC: both collectors repeatedly failed before page 1 with
  `order_collection_source_unavailable`; no completed full scan. Investigate
  timestamp wire precision and verify a successful full scan after the fix.
  Do not call collection ready solely because webhook delivery works.
- Local PostgreSQL 16.2 verification passed all SQL 001–052 and the actual
  41-module migration runner. Targeted invoice, receiving, availability and
  adoption tests passed. Full PR CI and production release remain pending.
- Private portable handoff files are `Hub_pokracovanie_20261109.txt` and
  `Hub_obnova_20261109.zip`. The latter holds operation intents/results and
  source snapshots, never the API token or a repository copy. Obtain the latest
  versions from the owner when continuing in another account.
- Completed feature checkpoint:
  `2bf023c302c0e114599f43b466c81e38eccc32ac`,
  [PR #185](https://github.com/mpriesol/inventory_hub/pull/185).
  First full CI run 42590664662 passed the frontend but found four receiving
  integration cases in three test fixtures missing the invoice schema. Those
  fixtures now include 003/033/051/052; all four affected cases passed in isolated
  reruns. Full CI must pass on the final updated head before merge.
- The follow-up also preserves original CP1250 source-export bytes without
  declaring an invented UTF-8 charset; the four export tests passed again.
- 09:10–09:11 UTC: fresh API reads confirmed all three runtime write switches
  (regular stock, maintenance publication and FIFO cost publication) are false.
  Both stock-sync authorities/schedulers remain off with zero baselines. These
  env flags have no API setter; deployment preserves existing server values.
  Authorized runtime configuration access is still required for activation.
  The owner's current request covers activation work; do not invent a new
  generic approval request. Do not bypass unavailable server access with a new
  deployment workflow. External writer/checkout pause must be verified before
  claiming the baseline maintenance prerequisites.
- 09:12–09:15 UTC: used the existing FIFO cost-revision API for exactly nine
  current roots proven to originate from the frozen test receipts. All are now
  `unknown`/NULL at cost revision 1 with the original prices retained in audit.
  Readback verified 54 units, zero reserved/quarantined, nine confirmed cards
  and unchanged movement history (2,746 rows, latest ID 3,164). Current valuation
  is unknown, not zero. This operation did not archive documents or adopt stock.

## Next concrete work

1. Preserve the reviewed collector, source-archive and UI recovery fixes already
   in the branch. Do not start competing implementations.
2. Pass full CI on the final PR head, review and merge/deploy under standing
   permission. Verify deployed routes separately.
3. Use the pinned Atomer admin source export to resolve remaining source IDs;
   produce the exact quantity selection manifest with provenance. No guessed
   identities/units, no zero for missing source rows, no duplicated shared stock.
4. Finish exact receipt scope across all suppliers. Archive frozen test receipts,
   then invoices, then the two malformed source test entries; compare unchanged
   stock/settlement history and active-list postconditions. Preserve operation
   UUIDs and read back uncertain outcomes before any retry.
5. Adopt approved quantities through bounded stock-adjustment batches, preserving
   unknown costs and existing receipt effects. Verify identities and quantities.
6. Recheck only known pre-write Topeak failures after the availability fix;
   complete collector scans and prepare cutover/baseline before live authority.


## Follow-up checkpoint: absent DELETE notification

- Full CI [42592384918](https://github.com/mpriesol/inventory_hub/actions/runs/42592384918)
  passed all four backend shards, complete-coverage gate and frontend/UI on
  `a8e85f45d95f12ee6921febec92a9b63e2c32d02`. PR #185 was merged under the
  standing permission; [build/deploy 42594100734](https://github.com/mpriesol/inventory_hub/actions/runs/42594100734)
  completed successfully, including migrations 051/052, health/authentication and SPS checks. New availability diagnostic API returned HTTP200.
- A real BIKETREK DELETE notification remains on review because successful
  active and deleted Upgates queries no longer return the order. No matching
  local order, inbox/job/native history or issue was found in the private audit.
  Do not silently ignore it or fabricate an order. The follow-up adds a required
  note, server-verified absence/history preview, apply/readback and audited
  reopen; newer notification generations remain protected. It does not advance
  collection checkpoints or change stock. See docs/order-automation.md.
- Follow-up targeted backend and UI tests passed, including concurrent UUID
  recovery, event generation changes, reopening and recovery after a newer
  event replaces the visible notification. Independent backend review passed.
  Full follow-up CI/deployment is still required.
- The private stock runner now requires `--prepare-plan` to freeze every request
  UUID offline. Persist that exact plan and final source manifest before any
  `--apply`; resume original IDs after interruption. Never derive applied state
  from an earlier chat, a code commit or a successful deployment.
