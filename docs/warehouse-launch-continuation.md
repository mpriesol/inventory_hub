# Warehouse workflow continuation

This public record describes implementation and recovery mechanics only.
Current operational scope, quantities, source snapshots, request UUIDs, receipts,
private configuration and execution progress belong in the owner's private
continuation package. Do not reconstruct them from a chat or publish them here.

## Implementation

- Repository: https://github.com/mpriesol/inventory_hub. Follow AGENTS.md and do
  not use OLD_inventory_hub.
- [PR #185](https://github.com/mpriesol/inventory_hub/pull/185) implements invoice
  processing without stock effects, reversible document/receipt archival,
  guarded receipt corrections and owner-adoption batch primitives. It also
  contains supplier availability and order collection fixes.
- [PR #186](https://github.com/mpriesol/inventory_hub/pull/186) adds the narrowly
  scoped manual recovery of an absent DELETE notification. It uses the existing
  sync journal and requires no additional database migration.
- Check each PR's actual head, complete CI and deployment result before using
  its new routes. A merge or successful build does not prove data operations ran.

## Resume without chat history

1. Read AGENTS.md, git status and the current main/feature heads. Preserve any
   unfinished changes and avoid a second competing implementation.
2. Obtain the owner's latest private continuation text and recovery bundle.
   They contain the exact frozen operation plans and applied/not-applied state.
   Use authorized credentials supplied separately; no token belongs in Git or
   the portable bundle.
3. Read current API revisions and recover every interrupted operation by its
   original request UUID before any new write. Never create a replacement UUID
   merely because the response was lost.
4. Use the frozen archival scope in receipt, invoice, then source-entry order.
   Preserve original documents, stock movements, FIFO and settlement history.
   Verify active-list, quantity and payment postconditions from fresh reads.
5. Finalize source identities and units before quantity adoption. Freeze every
   per-card UUID offline, save the plan and manifest in the private handoff,
   then apply bounded batches with previews and committed-outcome verification.
   Unknown quantities or costs must not silently become zero.
6. Verify collection and webhook outcomes independently from stock processing
   and publication. Runtime access, shared-channel readiness and maintenance
   assertions must reflect observed facts, not assumptions.
7. Save each milestone and its recovery instructions in the private checkpoint.
   Keep public documentation limited to implementation and generic workflows.

## Related technical documentation

- [Invoice archive and settlements](invoice-archive.md)
- [Receiving identity and recovery](receiving-identity.md)
- [Stock corrections](stock-adjustments.md)
- [Stock tracking](stock-tracking.md)
- [Order automation and notification recovery](order-automation.md)
- [Order launch setup](order-launch-setup.md)
- [Stock publication](stock-publication.md)
- [Regular stock synchronization](stock-sync.md)

Preserve existing authorization boundaries. Missing server access is not a
reason to bypass protected configuration through a new workflow. Reversible
archival is not a full database/files backup, and owner adoption is not a claim
that every item was physically counted.
