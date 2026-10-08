# Product feed mapping

The **Produktový feed** workspace configures how a supplier's XML, CSV or JSON becomes normalized supplier catalog data. It does not create physical stock or publish products to a shop. The separate unified import workspace prepares editable product drafts and sends explicitly reviewed selections.

## Operator flow

1. Open the supplier's **Produktový feed** page. The last retained source is analyzed automatically after sign-in; a missing source prompts an upload or catalog download. Upload saves an artifact only; inspection and preview do not import products or save mapping changes.
2. Choose a record path when the feed has several possible record arrays. XML and JSON use literal slash-separated field names. Repeated lists are traversed in order. XML attributes appear under `@attributes`; mixed text appears as `#text` and complete inner markup as `#inner_xml`. Namespace-qualified XML paths retain the namespace URI, including its slash characters.
3. Under the shared feed mapping, select a source field using its name and sample values, then connect it to the destination field. Drag-and-drop and ordinary selection provide the same operation. Advanced literal paths, constants and transformations remain available. Preview shows real sample values and errors before saving. Unknown source fields remain available; mapping a field does not discard the rest of the record.
4. Choose a target shop to map categories. The searchable supplier list is populated from the analyzed records, including category labels paired with source codes where available. Connect a supplier category to an active leaf in the shop tree. The import workspace expands this leaf into its ancestors. Generic feeds must also map their source category into the normalized category/code field; category rules match those normalized values.
5. Save the mapping. Saving a definition does not rewrite existing catalog rows or prepared drafts. Use the explicit **Použiť na katalóg** action to reprocess the retained source with the saved revision, or refresh the supplier catalog to download/process a new source.
6. Prepare an import from the supplier catalog. Source and mapping revisions are frozen in the new draft. Manual and accepted AI edits belong to that draft and are not erased by a later feed refresh.

Existing Paul Lange and Northfinder suppliers keep their native identity, grouping, price inheritance and warning rules. Their mapping starts with this native parser; user bindings overlay only destinations explicitly configured. The invoice-column **Mapovanie** tab remains a different feature. Supplier credentials and connection configuration remain in the existing supplier configuration; product mapping definitions and revision history live in PostgreSQL.

An explicit non-native record path selects generic parsing even for an existing supplier; it requires its own SKU/name bindings. This makes an intentionally changed feed layout configurable without silently ignoring the chosen record path.

An ordinary new supplier needs no parser code: create its supplier configuration and product prefix, upload an XML/CSV/JSON sample, map at least supplier SKU and product name, save, and explicitly apply the sample to the catalog. Subsequent catalog refreshes use the configured remote source when present, otherwise the retained uploaded source. Native Northfinder preserves parent context and actual variants. Generic feeds require an explicit source group identifier and variant-parameter mappings; grouping by similar product names is never inferred.

## Supported data and transformations

Destinations include supplier SKU and manufacturer code, all source EANs, name/brand, description and short description, SEO title/description/URL, source category identity, images, dynamic parameters, explicit variant axes/group identity, purchase/recommended/discount price bases, currency/VAT, supplier availability observations, and named custom fields. Shop-specific mappings cannot change shared supplier identities or availability observations.

`parameter:<name>` maps one source value or list into a named product parameter. A binding for `parameters` or `variant_attributes` uses a repeated source list with separate relative name/value paths. `meta:<key>` maps a custom field; the review flag `validation_required` remains reserved. Remote publication supports only fields represented by its validated import contract; internal IDs, physical stock and review flags cannot be mapped into arbitrary outgoing payloads.

The parameter picker discovers paired parameter names/values in repeated feed records, including Paul Lange's `DYN_PARAMS/PARAM` and `STA_PARAMS/PARAM`. A selected parameter is addressed by its exact name and relative name/value paths, not its array position. Optional `param_match_name` extracts only that named parameter before applying transformations. Reordered or absent parameter entries cannot borrow another parameter's value. A named mapping overlays the destination name and preserves other source parameters; it does not remove the original supplier name automatically.

Target suggestions combine names read from the selected Upgates shop with approved parameters in published Hub category rules. The category/profile selector filters suggestions, including known values and units from those rules; it does not make a field binding conditional on a product category. Bindings still apply to the selected supplier/feed/shop scope. Upgates names are cached for a day and can be refreshed explicitly. If the remote read fails, the UI reports that limitation and published rule suggestions remain available. Reading names does not create parameters or change their visibility/category assignment in Upgates.

Simple transforms are trim, HTML-to-text, split, join, literal replacement, prefix/suffix, value lookup, decimal conversion, multiplication, rounding and truncation. They are a bounded declarative list, not Python/JavaScript expressions or a general XPath engine. A vertical bar separates source paths when a destination intentionally combines lists, such as `IMGURL|IMAGES/IMGURL`. Multiple source values for a scalar field require an explicit Join or a more specific path. Unknown lookup values are preserved unless the operator explicitly maps them.

Missing values stay missing; explicit zero is not replaced by a default. Source SKU/EAN values must be strings so leading zeros are retained. Invalid numbers and ambiguous source identities produce visible errors. No physical quantity is created from supplier stock. Net/gross derivation requires an explicit mapped or configured VAT rate; a new generic supplier without it receives `missing_vat`, and derived prices remain absent. Existing native-parser VAT behavior is preserved.

When SEO fallback is enabled, empty SEO title uses the product name (up to 80 characters) and empty SEO description uses plain text from short/long description, falling back to the name (up to 160 characters). No external facts, keywords or product claims are invented. Explicit mapped SEO wins over the fallback.

## Persistence and provenance

Additive migration `021_product_feed_mapping.sql` creates:

- `product_feed_mappings`: current definition and positive revision, unique per supplier + feed key + shop code. An empty shop code identifies shared normalization.
- `product_feed_mapping_revisions`: immutable definition snapshots per mapping/revision. Compare-and-swap requires the last observed revision; concurrent edits return `feed_mapping_conflict`.

Original uploads remain private artifacts under `suppliers/<supplier>/feeds/mapping/<feed>/<sample-id>.feed`. Downloaded sources retain their normal feed paths. `SupplierFeed.mapping_config` retains source/run metadata; it is not the authoritative editable definition. Normalized catalog rows include mapping revision and field provenance; full detail includes original source fields. Compact catalog pages omit long source/provenance data.

Applying a new shared definition requires a fresh catalog run. A draft request against a catalog with an older mapping revision returns `feed_mapping_stale`, so changed code/price mappings cannot silently coexist with an old identity/search index. Reprocessing uses the usual atomic catalog transaction and preserves existing row IDs for unchanged supplier SKUs. Failures retain the previous successful catalog. Changing shared identity mappings can create different supplier identities; it never renames existing central products or changes stock/history.

Shop mapping is applied to a copy when preparing a new draft. It never writes source rows, previously saved drafts, manual overrides or Upgates. Drafts retain both scope revisions and provenance. Category precedence is manual draft selection, then shop mapping, then optional AI choice when unmapped. Missing/deleted target categories are checked by the import workflow before publication.

## API

All routes require operator access and return `Cache-Control: no-store`. Prefix: `/api/suppliers/{supplier}/feed-mapping`.

| Method | Suffix | Behavior |
| --- | --- | --- |
| GET | empty | `feed_key`, optional `shop`; current revision/definition, destinations and base definition |
| PUT | empty | `{feed_key,shop,expected_revision,definition}`; save definition only |
| GET | `/inspection` | Inspect retained source or `sample_id`; optional format/record-path/CSV settings |
| GET | `/parameter-options` | `shop`, optional `refresh`; read shop parameter names and published category registries |
| POST | `/upload` | Multipart file plus format/record settings; retain sample and inspect |
| POST | `/preview` | `{feed_key,shop,definition,sample_id?,limit}`; up to 20 mapped sample products and row errors |
| POST | `/remap` | `{feed_key,expected_revision,sample_id?}`; explicitly reindex source with saved base definition |

`MappingDefinition` contains `format` (`auto`, `xml`, `csv`, `json`), `record_path`, CSV delimiter/encoding, `bindings`, `category_rules`, and `seo_fallback`. Bindings contain destination `target`, source path or constant, an optional missing-value default, transforms, optional parameter name/value paths and `param_match_name`. Shop-specific category rules contain `source` and `target_code`. Inspection retains the original `fields`/`source_categories` contract and adds structured category options and named parameter options for the visual picker.

Automatic XML/JSON detection looks inside wrappers for product collections rather than treating a container with a sibling category dictionary as a product. The Paul Lange `SHOP/SHOPITEMS/SHOPITEM` layout remains compatible with its native parser. Equally plausible collections require an explicit record path; the analyzer must not silently combine products with category or parameter dictionaries.

Sources are bounded to 100 MB, 100,000 product records, 24 structural levels and 4,096 distinct field paths. Source examples are bounded snippets; all original data remains in the artifact. XML external entities/DTDs are rejected; JSON non-finite numbers and ambiguous record arrays are rejected. CSV supports UTF-8 (with optional BOM), Windows-1250 and ISO-8859-2, with explicit or detected delimiter. Mapping definitions are limited to 1 MB and do not execute code.

## Deployment and verification

Package and run `inventory_hub.product_feed_mapping_migrate` after migration 020 and before starting the updated API. Replaying the additive migration is safe. It does not activate catalog scheduling or shop stock/cost publication. On application rollback retain the mapping tables, history and source files; rolling back does not reverse explicitly imported supplier data.

The visual mapping/parameter-discovery extension requires no new migration or server configuration. Existing definitions remain valid. Once a definition uses `param_match_name`, older application versions cannot parse it; prefer a forward fix, or remove/replace these bindings through the current version before an application rollback. Do not delete mapping history or source artifacts.

Pure tests cover original-field preservation, PL/Northfinder compatibility, inherited prices and duplicate blockers, generic XML/CSV/JSON, repeated parameters, value transforms, explicit zero/missing VAT, malformed and ambiguous identities, SEO and parser limits. Guarded PostgreSQL tests cover revision conflicts/history, explicit remapping, shop isolation, read-only previews and a new supplier imported from an uploaded generic feed without parser code. They require an isolated localhost database ending in `_catalog_test`; no test uses production credentials or writes shop products.

The 2026-10-08 extension was checked with focused source/parameter regression tests and UI interactions: wrapped Paul Lange XML with a sibling taxonomy, Northfinder's joined category path, ambiguous collections, reordered named parameters, automatic read-only inspection, click/drop mapping, leaf-category selection, parameter registry refresh, unsaved changes and delayed responses across supplier switches. UI interaction tests use synthetic data and jsdom; production layout and retained source data require a separate browser check. No AI generation is required for mapping analysis or preview.
