# Zookee

Native catalog, independent availability and PDF invoice adapters are implemented in code. Production configuration and matching results must be verified separately after deployment. Private feed URLs and customer invoices belong in supplier configuration/storage, never in Git.

## Sources and money

The `products` source uses parser `zookee` and the rich `ITEMS/ITEM` export (feed 15). `kod` is the supplier SKU; `ID` is a separate source identifier. Explicit `ITEMGROUP_ID`, `ITEMGROUP_TITLE` and `VARIABLE_PARAMS` preserve real size/colour families. A lone family member is normalized by the existing singleton logic. `STAGE=draft` is not a stock flag.

The `stock` source uses parser `zookee` and the `export/item` export (feed 5). `code` is the supplier SKU; `inStock` is supplier quantity. Missing/invalid quantities and uncoded rows remain diagnostic failures, never confirmed zeros. A rich-catalog refresh also downloads the configured stock source through the existing bounded downloader and includes stock-only identities without replacing rich descriptions/variants. Stock-only records can be linked to existing cards; publication of a new product requires complete catalog evidence. A mapped/custom replacement catalog does not implicitly fetch a second source.

On 19 October 2026 the supplied snapshots contained 7,145 rich rows (7,130 with supplier codes), and 11,725 stock rows (11,707 with supplier codes). Their union contained 11,768 coded products. These are source snapshot counts, not own warehouse cards or physical quantities.

`PRICE`/`RRP` in the rich export are Czech retail prices; source VAT comes from `VAT`. They remain **CZK** and are never treated as EUR or acquisition costs. A different shop currency is blocked by existing validation. The stock export's `Cena-bez-dph` is not accepted as a purchase cost. No exchange rate or commercial discount is invented. Source texts remain Czech and use the existing reviewed Slovak preparation workflow.

Supplier identity is the raw supplier SKU scoped to Zookee and its explicit `product_supply_sources` link. Existing canonical SKUs remain unchanged when another supplier is attached; source prefix configuration does not own or rename a Hub card.

### Missing catalog images

Optional `adapter_settings.catalog_image_fallback` uses `{ "enabled": true, "shop_code": "biketrek", "brands": ["TOPEAK", "SENSOR"] }`. Catalog list/detail and new preparation selections fill only empty image lists from cached content of an explicitly linked stock card in that shop. The exact mapped code, native product/variant ID, parent identity, brand and compatible verified EANs must agree. Ambiguous native targets are skipped. Variant photographs take precedence; parent photographs are inherited only when the shop explicitly enables that behavior. Only HTTPS image URLs without embedded credentials are accepted.

This read-only projection records shop/code/native ID/cache timestamp in mapping provenance and displays an image-source warning. It survives supplier refreshes through the existing shop cache and configuration, without rewriting supplier snapshots. It makes no Upgates request or product/stock write, does not replace supplier images, and does not make an incomplete stock-only record publishable.

## Invoice upload

Use **Príjem → Nahrať faktúru pre príjem**, select Zookee and upload the original PDF with its single embedded `.isdoc`. `/suppliers/zookee/upload-invoice` validates the ISDOC supplier registration, document type, EUR currency, line identities, quantities, VAT, unit-cost precision and totals. Credit notes, scans without ISDOC, multiple attachments and unsupported/inconsistent invoices fail visibly.

For a foreign-currency ISDOC, `LineExtensionAmountCurr / InvoicedQuantity` is the EUR net acquisition cost. The local `UnitPrice` must not be interpreted as EUR. The normalized receiving CSV includes raw supplier SKU and EAN. Storage, locking, index updates, duplicate detection and conflict protection reuse the existing invoice upload implementation. Uploading creates no receipt session, movement or shop product. The supplied historical example was verified locally, not physically received into production.

## Existing BIKETREK products

Optional `adapter_settings.existing_card_matching` uses `{ "enabled": true, "shop_code": "biketrek", "brands": ["TOPEAK", "SENSOR"] }`. The existing manual **repair supplier links** action additionally considers listed cards in that shop with the same brand and either an exact verified leaf `code_supplier` or a unique verified EAN match. It reuses the shared identity resolver, rejects conflicting identities/ambiguous offers, and keeps existing disabled links and policies. It does not guess numeric suffixes, compare titles, rename canonical SKUs or copy barcodes. Ordinary catalog refresh does not broaden discovery to all brand/EAN matches.

The historical `TOPEAK-` → `ZOO-` migration has been retired. Supplier relationships use database links and do not require renaming existing shop or Hub codes. Ordinary Upgates PUT/import matches by `code`; it is not a proven rename API. Existing mapped-SKU guards remain in force.

## Scoped automatic availability

Optional `adapter_settings.availability_publication` has the same explicit enabled/shop/brand structure. It defaults to absent/off. Set labels under `adapter_settings.availability`: `orderable = "do 5 dní"`, `unavailable = "vypredané"`, `unknown = "overíme"`.

Enable the existing supplier availability schedule separately. The supplier worker processes up to 20 exact mapped, linked leaves per idle tick after fresh observations exist. For a positive existing Upgates stock quantity, availability remains `SKLADOM`; otherwise fresh positive/negative supplier evidence selects the configured label. Unknown/stale evidence sends nothing. Only `availability` and product/parent codes are in the payload: own stock, reservations, prices and basket settings are not written.

Batch reads reuse the existing bounded stock reader. One availability-only PUT groups sibling variants under their verified parent; complete native-ID acknowledgements and per-leaf readback are required. Partial or malformed acknowledgements fence every affected intent without automatic retry. This path reuses editor publication records, identity checks, shared API admission, committed sending intents, single PUT, readback and explicit uncertain-write resolution. The observed shop quantity participates in stale/readback checks. An interrupted ready request can continue; a sending/uncertain request is fenced and never automatically repeated. Per-product publication history remains available through the existing editor, and **Nastavenia → Synchronizácia dostupnosti** displays enabled scope and aggregate results. Full stock authority and AI availability writes cannot compete with an active scoped supplier policy.

Only mappings with a valid native ID and an exact canonical leaf code enter automatic availability batches. Unverified mappings require the normal explicit shop-link identity review and cannot make an otherwise verified batch incomplete. After a verified link is saved they become eligible for the same fresh observation if no attempt exists. The settings overview counts the latest outcome per card/channel within the configured shop, retaining priority for any unresolved sending/uncertain intent. Historical attempts remain in product history.

To stop writes, disable `availability_publication.enabled` in supplier configuration. To stop source reads, disable its schedule. Neither action changes stock history. Enabling a schedule is different from pressing **Spustiť teraz**; setup must respect an owner's instruction to wait for the scheduled trigger.

## Verification

Synthetic tests cover CZK/EUR separation, explicit families, stock-only identities, malformed/unknown stock, PDF attachment validation and duplicate upload. Receiving and publication database cases use isolated PostgreSQL in CI. UI interaction tests cover invoice selection, first-scan UUID handoff, remount recovery and closed-receipt review. No private feed URLs or customer documents are test fixtures.

### Retired code migration: history and lost-response recovery

New `TOPEAK-` → `ZOO-` renames and the related one-off native-ID repair are disabled. Zookee settings show a retirement notice and read-only historical results. New shop relationships use the shared explicit product-link workflow and verified native IDs without changing the stock card SKU.

Admin-only `GET /supplier-availability/zookee/legacy-codes` returns `retired: true`, reason `code_rename_retired`, an empty candidate list and up to 100 saved historical results. `GET /supplier-availability/zookee/legacy-code-results/{request_id}` reads an original committed result by UUID; an unknown UUID returns 404 `code_rename_result_missing`. Reading history does not require the former prefix configuration.

The old single-item POST accepts only a previously completed UUID for the same product and returns its original result. A fresh UUID returns 410 `code_rename_retired`; reusing another operation's UUID returns a conflict. The old batch POST similarly returns saved results or a `code_rename_retired` failure per fresh row. These routes make no remote calls or local writes.

The former operation committed its local changes, audit and result together and had no durable pending rename phase. Existing aliases, audits, issued orders and the stock ledger remain intact. General publication records in `sending` or `uncertain` state retain their original recovery workflow; reading rename history does not complete or retry them.
