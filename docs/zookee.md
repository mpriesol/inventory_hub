# Zookee

Native catalog, independent availability and PDF invoice adapters are implemented in code. Production configuration and matching results must be verified separately after deployment. Private feed URLs and customer invoices belong in supplier configuration/storage, never in Git.

## Sources and money

The `products` source uses parser `zookee` and the rich `ITEMS/ITEM` export (feed 15). `kod` is the supplier SKU; `ID` is a separate source identifier. Explicit `ITEMGROUP_ID`, `ITEMGROUP_TITLE` and `VARIABLE_PARAMS` preserve real size/colour families. A lone family member is normalized by the existing singleton logic. `STAGE=draft` is not a stock flag.

The `stock` source uses parser `zookee` and the `export/item` export (feed 5). `code` is the supplier SKU; `inStock` is supplier quantity. Missing/invalid quantities and uncoded rows remain diagnostic failures, never confirmed zeros. A rich-catalog refresh also downloads the configured stock source through the existing bounded downloader and includes stock-only identities without replacing rich descriptions/variants. Stock-only records can be linked to existing cards; publication of a new product requires complete catalog evidence. A mapped/custom replacement catalog does not implicitly fetch a second source.

On 19 October 2026 the supplied snapshots contained 7,145 rich rows (7,130 with supplier codes), and 11,725 stock rows (11,707 with supplier codes). Their union contained 11,768 coded products. These are source snapshot counts, not own warehouse cards or physical quantities.

`PRICE`/`RRP` in the rich export are Czech retail prices; source VAT comes from `VAT`. They remain **CZK** and are never treated as EUR or acquisition costs. A different shop currency is blocked by existing validation. The stock export's `Cena-bez-dph` is not accepted as a purchase cost. No exchange rate or commercial discount is invented. Source texts remain Czech and use the existing reviewed Slovak preparation workflow.

The authoritative prefix is `adapter_settings.mapping.postprocess.product_code_prefix = "ZOO-"`. Existing canonical SKUs remain unchanged when another supplier is attached.

### Missing catalog images

Optional `adapter_settings.catalog_image_fallback` uses `{ "enabled": true, "shop_code": "biketrek", "brands": ["TOPEAK", "SENSOR"] }`. Catalog list/detail and new preparation selections fill only empty image lists from cached content of an explicitly linked stock card in that shop. The exact mapped code, native product/variant ID, parent identity, brand and compatible verified EANs must agree. Ambiguous native targets are skipped. Variant photographs take precedence; parent photographs are inherited only when the shop explicitly enables that behavior. Only HTTPS image URLs without embedded credentials are accepted.

This read-only projection records shop/code/native ID/cache timestamp in mapping provenance and displays an image-source warning. It survives supplier refreshes through the existing shop cache and configuration, without rewriting supplier snapshots. It makes no Upgates request or product/stock write, does not replace supplier images, and does not make an incomplete stock-only record publishable.

## Invoice upload

Use **Príjem → Nahrať faktúru pre príjem**, select Zookee and upload the original PDF with its single embedded `.isdoc`. `/suppliers/zookee/upload-invoice` validates the ISDOC supplier registration, document type, EUR currency, line identities, quantities, VAT, unit-cost precision and totals. Credit notes, scans without ISDOC, multiple attachments and unsupported/inconsistent invoices fail visibly.

For a foreign-currency ISDOC, `LineExtensionAmountCurr / InvoicedQuantity` is the EUR net acquisition cost. The local `UnitPrice` must not be interpreted as EUR. The normalized receiving CSV includes raw supplier SKU and EAN. Storage, locking, index updates, duplicate detection and conflict protection reuse the existing invoice upload implementation. Uploading creates no receipt session, movement or shop product. The supplied historical example was verified locally, not physically received into production.

## Existing BIKETREK products

Optional `adapter_settings.existing_card_matching` uses `{ "enabled": true, "shop_code": "biketrek", "brands": ["TOPEAK", "SENSOR"] }`. The existing manual **repair supplier links** action additionally considers listed cards in that shop with the same brand and either an exact verified leaf `code_supplier` or a unique verified EAN match. It reuses the shared identity resolver, rejects conflicting identities/ambiguous offers, and keeps existing disabled links and policies. It does not guess numeric suffixes, compare titles, rename canonical SKUs or copy barcodes. Ordinary catalog refresh does not broaden discovery to all brand/EAN matches.

A requested `TOPEAK-` → `ZOO-` shop-code migration is a separate operation. Ordinary Upgates PUT/import matches by `code`; it is not a proven rename API. Do not use a changed code to create another product, delete/recreate listings, or bypass the mapped-SKU editor guard. Verify a supported administrative rename and reconcile every shared shop mapping before changing canonical Hub identity.

## Scoped automatic availability

Optional `adapter_settings.availability_publication` has the same explicit enabled/shop/brand structure. It defaults to absent/off. Set labels under `adapter_settings.availability`: `orderable = "do 5 dní"`, `unavailable = "vypredané"`, `unknown = "overíme"`.

Enable the existing supplier availability schedule separately. The supplier worker processes up to 20 exact mapped, linked leaves per idle tick after fresh observations exist. For a positive existing Upgates stock quantity, availability remains `SKLADOM`; otherwise fresh positive/negative supplier evidence selects the configured label. Unknown/stale evidence sends nothing. Only `availability` and product/parent codes are in the payload: own stock, reservations, prices and basket settings are not written.

Batch reads reuse the existing bounded stock reader. One availability-only PUT groups sibling variants under their verified parent; complete native-ID acknowledgements and per-leaf readback are required. Partial or malformed acknowledgements fence every affected intent without automatic retry. This path reuses editor publication records, identity checks, shared API admission, committed sending intents, single PUT, readback and explicit uncertain-write resolution. The observed shop quantity participates in stale/readback checks. An interrupted ready request can continue; a sending/uncertain request is fenced and never automatically repeated. Per-product publication history remains available through the existing editor, and **Nastavenia → Synchronizácia dostupnosti** displays enabled scope and aggregate results. Full stock authority and AI availability writes cannot compete with an active scoped supplier policy.

To stop writes, disable `availability_publication.enabled` in supplier configuration. To stop source reads, disable its schedule. Neither action changes stock history. Enabling a schedule is different from pressing **Spustiť teraz**; setup must respect an owner's instruction to wait for the scheduled trigger.

## Verification

Synthetic tests cover CZK/EUR separation, explicit families, stock-only identities, malformed/unknown stock, PDF attachment validation and duplicate upload. Receiving and publication database cases use isolated PostgreSQL in CI. UI interaction tests cover invoice selection, first-scan UUID handoff, remount recovery and closed-receipt review. No private feed URLs or customer documents are test fixtures.

### Verified reconciliation after an administrative code rename

A collapsed **Zosúladenie starých kódov TOPEAK- → ZOO-** tool in Zookee's general settings lists remaining old canonical codes. It performs no shop writes. After the administrative product/variant/parent rename, **Overiť v e-shopoch a zosúladiť Hub** reads every mapped new code and requires the same native product/variant ID and compatible barcode in every channel. A partial shop rename, conflicting code, missing native ID or unresolved publication rejects the local change.

For a legacy mapping whose native ID is empty, reconciliation can recover that ID from its exact original cached product/variant. It requires one cached parent and leaf, valid native IDs, compatible barcodes and the same IDs in the fresh shop response. Existing nonempty IDs are never replaced from the cache. Missing/ambiguous cache evidence is rejected before batch API reads; cached identity is locked and rechecked before committing. The audit records the recovery evidence. The UI also displays the stable failure code so an unverified rename can be diagnosed without exposing remote payloads or credentials.

The same review also lists missing native IDs on already linked Topeak/Sensor BIKETREK cards when the explicit Zookee existing-card matching policy enables those brands. An active, same-brand Zookee supplier binding is required; disabled bindings and unrelated cards are excluded. For these unchanged codes (including `SEN-`), the operation verifies all mapped shop identities and restores IDs without renaming the card, adding an alias or changing cached source content. It increments the editor revision and audits `verified_native_id_repair`; successful UUIDs retain read-only replay.

`GET /supplier-availability/zookee/legacy-codes` and admin-only `POST /supplier-availability/zookee/legacy-codes/{product_id}` are restricted to the configured `ZOO-` prefix and the old `TOPEAK-` namespace. POST takes one request UUID. The administrative UI uses `POST /supplier-availability/zookee/legacy-code-batches` with at most 20 distinct product IDs and UUIDs, sharing bounded existing-link reads per shop; every local change remains individually verified and atomic. Successful UUIDs replay without another API read. A successful local transaction preserves the product ID, stock movements, balances, source links and historical orders; updates shop-code mappings/cached identity fields; retains the former code as an internal identifier; increments the editor revision and records an audit/result. Repeating a successful UUID does not repeat remote reads or writes. This is a narrow reconciliation exception; ordinary mapped-SKU editor renames remain blocked.

The tool does not prove that an external administrative rename has happened until its GET verification succeeds. Do not report a migration as finished from the presence of the tool alone.
