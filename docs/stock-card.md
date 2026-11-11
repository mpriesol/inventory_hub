# Unified stock card

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

## Implementation and entry points

Delivery branch: `codex/unified-stock-card`, based on `dc2289b6` (PR #187).
Implementation and release evidence: [PR #188](https://github.com/mpriesol/inventory_hub/pull/188).

`StockCard.tsx` owns the single dialog and effective editor detail. Its portal
is attached to `document.body`, outside animated layout wrappers and table
rows. The desktop shell keeps a fixed width and height across sections;
only its contents scroll. Below 640 px it fills the viewport, with touch-sized
controls and horizontal section navigation. The Hub layout uses a mobile
menu below 801 px, including focus restoration and an inert background.

At widths up to 640 px and heights up to 520 px, a compact header and a single
warehouse/quantity row leave more room for the scrolling content. Section,
close, footer and form controls retain their mobile touch sizes; ordinary
portrait and desktop layouts are unchanged. The vertical section navigation
scrolls when a short viewport cannot fit all tabs. Overview and editing also show
the same stored supplier when only an inactive source exists, explicitly
labelled as an inactive source without changing its mapping. The source can be
inactive because of its supplier, supplier product, or link; the label does not
claim which of those caused the state.

### Desktop layout refinement (2026-11-11)

The owner requested a larger desktop card with smaller stock totals and a
routine overview that fits in one view. Branch: `codex/stock-card-desktop-layout`.
At widths above 800 px the fixed shell grows to at most 1440 × 960 px, bounded
by the viewport with a 16 px outer margin. The product header, inline stock
strip and footer use less vertical space. Unknown stock is shown neutrally;
the available-stock accent is reserved for an actual known quantity.

At widths of at least 1000 px, the six product fields retain their original
order in three columns. Supplier availability follows those fields; description,
internal note and parameters sit side by side. Product editing uses the same
three-column field order, with description and note next to each other. E-shop
details and FIFO summaries also use the available desktop width. The saved Hub
selling price appears once in the card; the standalone publication panel still
shows its own price by default. Preview, confirmation and recovery remain in
the same persistent panel instances.

The target is a complete routine overview on a desktop or laptop, not clipping
arbitrarily long descriptions, parameters, validation messages or history. Those
remain reachable in the existing content scroll container. Narrow layouts,
mobile touch targets, the short-phone shell and scrollable vertical navigation
remain supported. Desktop screens up to 800 px high use tighter content spacing
without reducing text or control sizes. No API, stock calculation, mutation or schema changes belong
to this layout refinement.

`StockCardLink.tsx` opens this same component above the parent page on an
ordinary click. Modified clicks keep a real URL for a new tab. Invoice and
receiving drafts remain mounted. Receiving and stock history links pass their
actual warehouse; an unspecified warehouse uses the configured default,
while an explicit empty warehouse remains catalog-only.

`ProductDetailPage.tsx` handles the stable `/products/card/:productId` URL and
the existing `/products/:sku` URL with the same component. The stock grid
opens it directly by ID. Invoice, receiving, movement, product trace and Atomer
links all use `StockCardLink`. The former independent legacy detail view and
its broad Upgates push control have been removed.

The five sections embed existing guarded stock/FIFO, supplier, shop-mapping,
SKU-preview and selected-field publication components. Their standalone
forms retain their prior behavior. Metadata edits use the existing editor
save endpoint and revision/source compare-and-swap. Grid drafts keep the
card read-only; the grid's conflict/rebase controls remain under its save bar.

`onChanged(row)` is a **canonical-product invalidation**, sent immediately
after a confirmed change. Its snapshot can predate the post-write reload.
The grid re-reads the row in its own warehouse, ordering overlapping reads
per product. Links defer parent refresh until the card closes. Closing during
the card's own reload therefore cannot hide a confirmed change from its parent.

## Shared API and identity

Both read routes return the same effective editor detail:

- `GET /api/product-editor/products/{id}` resolves a retired ID to its canonical card.
- `GET /api/product-editor/products/resolve?sku=...` resolves exact current or
  historical internal Hub SKU. It rejects ambiguous identities and never
  treats an unscoped supplier SKU or EAN as a Hub SKU.

The read model includes `suppliers[]`, safe source product URLs, effective
description, and explicit `warehouse_context`. Internal exact-ID reads used
by writers stay strict, so read navigation cannot retarget a mutation.
Retired shop mappings are excluded from current card mappings.

Description and e-shop navigation URLs use existing JSON overrides; there is
no schema migration. URLs never change native product/variant mappings.
The actual source URL is required for a supplier product link. Source HTML
is presented as text, not executed. Unknown quantities and costs remain
unknown, including during a required refresh after a stock change.

## Price publication and recovery

`ProductPricePublicationPanel.tsx` publishes only `sale_price_gross`. The user
saves the Hub price, selects one or both shops, requests exact live previews
and explicitly confirms sending. Per-shop overrides, verified VAT/base price
and retained discounts are visible. Each target has its own durable operation;
partial success does not replay the successful target. Shared stock sale
representations require their own supported pricing workflow and are not guessed.

Uncertain publications use existing read/reconcile endpoints and in-flight
fences. Publication history remains bounded to 20 records, prioritizing
`sending`/`uncertain` so another shop's newer previews cannot hide recovery.
Metadata-save recovery persists the frozen UUID/body in session storage
scoped by user, canonical product and warehouse; it verifies storage before
dispatch and never accepts a late result into a different card context.

## Verification

New frontend suites are registered in `.github/workflows/ci.yml`:

```sh
npm --prefix frontend run typecheck
npm --prefix frontend run build
node frontend/tests/stock-card-ui.cjs
node frontend/tests/stock-card-navigation-ui.cjs
node frontend/tests/product-price-publication-ui.cjs
node frontend/tests/product-editor-ui.cjs
```

Existing FIFO, stock adjustment, supplier/shop linking, invoices, receiving,
movement history, product trace, Atomer and Upgates-import suites cover the
affected entry points and embedded compatibility. Backend tests cover source
selection, safe URLs, description precedence/reset, CAS/audit, canonical
aliases, strict retired writes, ignored retired mappings and pending
publication discovery. CI executes database tests against guarded localhost
PostgreSQL and rejects skipped coverage.

DOM tests cover interaction and shared-shell identity; they do not measure
rendered geometry. Actual browser/deployment verification belongs to the
implementation PR and subsequent layout PRs with their Actions runs. Check the
current desktop-layout branch/PR for the 2026-11-11 refinement. Production
verification is read-only, including mobile
layout checks; no product or price is authorized as a live test target.

## Continue safely

Read `AGENTS.md`, `OVERVIEW.md`, `docs/product-editor.md` and this document.
Inspect the relevant implementation/layout PR, its current head, checks,
merge/deployment state and the working tree before editing. PR #188 contains
the original implementation; `codex/stock-card-desktop-layout` is the later
desktop refinement. Do not repeat already completed work. Product editor and publication
contracts must remain backward compatible. Tests use synthetic fixtures or a
guarded local `_catalog_test` database. Production verification is read-only;
no live product or price is authorized as a test target.

If the PR is still open, complete remaining failed checks/review and use the
normal authorized merge/deployment workflow. If it is merged, verify its
release run and the live Hub before claiming delivery. Do not activate stock
authority or restart the separate warehouse-launch operations under this UI task.
