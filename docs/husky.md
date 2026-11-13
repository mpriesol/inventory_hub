# Husky SK

Husky uses the existing supplier catalog, supplier availability worker and protected
supplier configuration. There is no new scheduler, schema migration, dependency,
physical stock writer or automatic shop publication.

## Configuration in Hub

1. Open **Dodávatelia → Husky → Obecné → Husky: katalóg a B2B**.
2. **Doplniť oficiálne XML zdroje** adds missing `products` and `stock` endpoints;
   it preserves existing sources, credentials, supplier identity and invoice settings.
   Save the supplier configuration. The official sources are
   `https://www.huskysk.sk/feed/partners.xml` (catalog) and
   `https://www.huskysk.sk/data/partners/Katalog_export.xml` (public stock).
   They are linked on the public website, **Pre partnerov → Synchronizácia skladu**:
   <https://www.huskysk.sk/synchronizacia-skladu>. The page labels them
   **XML skladu nájdete tu** and **XML rozšírená štruktúra tu**; a link in the
   signed-in B2B menu is not required.
3. To read wholesale quotes/stock too, enter the B2B username/password and enable
   **Čítať aj Husky B2B**. These are stored under `adapter_settings.husky`, with the
   existing write-only password/history protection and administrator save boundary.
   A browser sign-in alone does not configure the server worker.
4. Confirm `purchase_price_basis`: `net`, `gross`, or leave `unknown`. Until confirmed,
   the discounted price is retained as raw supplier evidence only, with a visible
   warning; it is never silently assigned to purchase net. Currency is EUR and VAT
   uses the supplier's configured `vat_rate`. The export's historical `VAT/VALUE`
   is not the destination VAT. Public retail prices are never purchase prices.
5. Open **Nastavenia → Dostupnosti a prenos zásob**, Husky. Select **Produkty**.
   Default interval is **86 400 seconds / 24 hours**, default freshness 48 hours.
   Husky permits 1 hour to 7 days; other suppliers' daily/manual-only limits remain.
   Save and enable scheduled reads. **Načítať dostupnosť teraz** and the catalog's
   refresh button both queue the same serialized worker. A queued response is not
   reported as a completed refresh. The synchronization card shows run outcome,
   errors, warnings, timestamps and next run.

The deployment runner now uses a guarded installer for the unchanged migration 034.
Its delivery table is created atomically with its one-time daily scheduling policy;
once present, that policy is not replayed. This preserves a saved shorter Husky
interval across deployments. First installation still executes the original SQL
under its original advisory lock. No deployed SQL or old installer is edited.

Enabling a schedule is an explicit live configuration action, not a code migration.
With B2B disabled, XML still refreshes the full catalog, but wholesale availability
remains unknown and the run reports the missing B2B connection. Public retail
stock flags are retained only as raw evidence, never as B2B availability. With B2B enabled,
any login/transport/structure failure rejects the whole candidate snapshot.

## Source contract

| Source field | Meaning |
| --- | --- |
| Catalog `CODE` | Exact supplier catalog identity; joins stock `KODVYROBCE`. Never replace an existing Hub SKU. |
| `PRODUCTNO` | Separate source manufacturer-code candidate / B2B registration number. Joins the B2B `Reg. číslo` only when unique. Native source values do not bypass the existing verified manufacturer identity requirement for new Hub allocation. |
| `MASTER_PRODUCT_ID`, `PARAM` | Explicit family and color/size choices. Singleton families become fixed attributes. Names do not create variants. |
| `EAN` | Deduplicated and checksum validated. Invalid values stay in source XML, not identity matching. Shared EANs block the affected import candidates. |
| `IMAGES/IMAGE/DOWNLOAD_FROM` | Original images, preferred to the 200 px thumbnail fields. |
| `COMMON_PRICE_VAT`, `PRICE_VAT` | Gross reference retail and current public retail quote; EUR Decimal values. Missing prices remain null. |
| Stock `SKLADEM` | Public retail boolean, retained as raw evidence only. It proves neither B2B availability nor a wholesale quantity. |
| B2B `Skladom` | Exact `N ks`, minimum `>N ks`, or unknown dash. `>10 ks` proves at least 11 whole pieces, not exactly 10. |
| B2B `Cena so zľavou (EUR)` | Supplier purchase quote, usable as purchase net only with an explicitly configured price basis. |

Wholesale availability uses B2B evidence exclusively until a feed is explicitly
verified as a wholesale stock source. Neither a positive nor a negative public
flag can fill a missing B2B row, an unknown dash, or a disabled B2B connection. Unmatched/ambiguous B2B registration
numbers stay unknown. The fast lead time requires at least two B2B pieces;
a single piece or unknown B2B stock uses **overíme**. Under the owner's clarification
on 13 November 2026, B2C availability and retail-store counts are not B2B stock
and must not drive wholesale availability.
Expired observations project as unknown. Import lead times also check freshness,
and Husky publication previews cannot outlive their fresh evidence.

Catalog rows, expanded variants and product detail show **Dostupnosť neznáma**
for public-only snapshots and explain that B2B stock is unverified. They never
show public True/False as wholesale stock. B2B values such as `4 ks` and `>10 ks`
retain their original meaning. Expired quantities and lead times are not displayed
as current. Historical public-only observations are also projected as unknown in
catalog/API reads, product stock views, import availability and the publication
queue; no historical evidence is rewritten and no data migration is needed.

Product content and original image links come primarily from the official product
feed; B2B supplies wholesale offers. The connector does not scrape B2C product
pages or retail stores. A future missing-content/image fallback may use relevant
B2C information after feed and B2B sources, but must not use B2C stock.

Read-only browser verification on 13 November 2026 confirmed the signed-in
`/objednavky` table and **Iba tovar na sklade → OK** filter: it showed exact piece
counts and `>10 ks`, not only product membership. The connector reads the same
stock column across the unfiltered catalog, so out-of-stock/unknown rows remain
observable; absence from a filtered list is never inferred to be zero.
Browser authentication does not save credentials for the server worker. Automatic
wholesale reads require the saved Hub configuration described above. Browser
verification is not a claim that a server-side B2B refresh has completed; inspect
the current configuration and the latest completed run for that evidence.

Read-only search and pager postbacks preserve the observed `cboPobocka` branch
selection, including the empty **Vyberte pobočku** placeholder, and the listing's
hidden `ctl00$ctl33` form field from the latest response. This is a narrow allowlist
for the observed listing form, not permission to copy every control. The connector
never chooses another branch, copies cart controls, persists form state, or relaxes
the expected-page and duplicate-code checks. Synthetic transport tests exercise
both empty and selected branches and updated page state across a two-page read.

The catalog and public stock exports may be published at different times. A catalog
code absent from stock remains in the catalog with an explicit unknown public
observation and warning, never an inferred zero; a verified B2B quote still takes
precedence. A stock export with no catalog matches is rejected. The first live
read on 13 November 2026 exposed this case: 3,129 catalog rows, 9,940 stock rows,
and one catalog code missing from stock. These are observations, not fixed counts.

## Boundaries, atomicity and recovery

The scraper creates an ephemeral HTTP session, authenticates at the fixed HTTPS
Husky host, clears search/stock/promotion filters and traverses the observed
ASP.NET GridView pagination. Only login, search, tree selection and pagination
fields are submitted. Quantity/cart/order buttons are never copied. Redirects
are confined to login/home/catalog; POST redirects are not replayed. TLS is
verified. Limits: one request per second, 2 MB per HTML page, 250 pages and a
480-second collection deadline. No response HTML, cookie, token or submitted
password is persisted or logged. A 429, challenge, expired session, changed table,
repeated page/code or unexpected value stops the read with a fixed error code.

XML downloads use the existing bounded downloader in a temporary directory. All
three sources are parsed before acceptance. The existing worker lease serializes
reads across replicas; cancellation retains the lease until the bounded HTTP
thread closes. Supplier/configuration revision is rechecked under the catalog
lock. Supplier catalog rows, raw per-item evidence, quote prices and availability
observations are committed in one transaction through the shared catalog upsert.
Existing source identities and unrelated attributes are retained; no Hub cards,
stock movements, receipts, FIFO costs or shop writes are created by the read.

Husky uses the configured coverage threshold as an acceptance gate for catalog
size, public stock export size and, after the first successful B2B read, B2B size. A smaller snapshot below
that threshold keeps the previous success, reports failure and retries only on
the next scheduled/manual run. Default 100% is conservative; review legitimate
supplier removals before lowering it. Explicit unknown B2B observations replace
old wholesale claims with unknown; they never become zero. Arbitrary custom
product feed mappings are rejected by the combined worker rather than silently
ignored. Source raw XML remains available in catalog detail; ephemeral downloads
are not retained as remapping sources.

To pause, disable the Husky schedule. Disabling B2B switches the next successful snapshot to catalog-only data with
unknown wholesale availability. A failed enabled B2B read does not
silently switch sources. Revert through the normal PR/deployment workflow if
needed; database and files remain compatible. No deployed migrations are changed.

## Verification

`test_husky.py` covers XML identities/GTIN, images, Decimal prices, uncertain VAT
basis, public vs wholesale stock, safe form fields/pagination and secret redaction.
`test_husky_db.py` uses an isolated localhost `*_catalog_test` database to check
atomic catalog/observation writes, replay, revision fencing, coverage rejection,
unchanged physical stock, new/updated/unchanged catalog counts and missing-price semantics. Existing catalog and
availability tests cover the shared upsert and scheduling regressions.
The database tests also exercise rollback of a failed 034 installation and repeated
deployment after an hourly Husky schedule is saved, partial catalog/stock joins
and rejection of a shrinking stock export without losing the prior snapshot. They
also verify that legacy public True/False observations cannot enter publication
as wholesale availability, while actual B2B quantities remain usable.
`frontend/tests/supplier-config-ui.cjs` covers the actual Husky form and source
preservation; existing availability/catalog suites cover queue/schedule behavior.
These tests do not authenticate to production B2B. Deployment and an authenticated
live B2B pass must be reported separately from parser/test success.
