# Unified stock card — implementation checkpoint

## Approved scope (2026-11-09)

The owner approved implementation of one stock card throughout the Hub. The
card opens above the current screen, preserves its dimensions when changing
sections, and works on phones. Sections: Overview, Stock, Prices and FIFO,
E-shops, History. Existing direct product URLs remain usable.

The product information order is Supplier, Brand, EAN, Supplier SKU, Warehouse
location, Type / stock unit. Include the actual supplier product link when
available. Show canonical Hub identity, SKU, source/shop codes, product image,
shop/admin links, stock, FIFO costs, and existing audited editing tools.

Selling prices can be published to BIKETREK, xTrek, or both. Each selected
shop gets an actual preview and an independent result. Keep existing
publication recovery and concurrency rules; never resend a successful or
uncertain request automatically. Do not change stock authority, synchronization
activation, or production stock as part of this UI change.

## Work in progress

- Branch: `codex/unified-stock-card`, based on `dc2289b6` (main / PR #187).
- API: canonical card reads by ID and SKU, supplier URLs, description and
  navigation link overrides using the existing editor revision/audit flow.
- UI: shared responsive `StockCard`, embedded existing stock/FIFO/link tools,
  and a selected-shop price publication panel.
- Integration: replace the separate stock/detail implementations and connect
  invoice, receiving, history and product links to the same card.
- Validation pending: backend tests, frontend component/integration tests,
  typecheck/build, CI, review, deployment and browser checks.

## Continue safely

Read `AGENTS.md`, `OVERVIEW.md`, `docs/product-editor.md` and this document.
Inspect the branch and working tree before editing; more implementation may
already have landed after this checkpoint. Product editor and publication
contracts must remain backward compatible. Tests use synthetic fixtures or a
guarded local `_catalog_test` database. Production verification is read-only;
no live product or price is authorized as a test target.

When the implementation is complete, replace this progress section with the
final architecture, behavior, verification results, and deployment reference.
