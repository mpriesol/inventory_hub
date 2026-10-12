# Supplier attribution in Upgates

Catalog imports record the configured supplier name in the product custom field
`supplier_name` (label **Dodávateľ**), for both feed-only and AI preparation. The
configured name overrides generated supplier text and is also attached to every
imported variant. It does not change the existing canonical product code or our
physical stock.

Preview reads the field definition without writing to Upgates. Publication may
create only this missing field, verifies the definition, and respects existing
`input` / `textarea` types and shared or per-language values. A changed definition
requires a fresh preview. Import readback confirms the supplier value for the
parent and each selected variant; a missing or different value is not reported
as a confirmed import. Historical previews without this field retain their
existing recovery behavior.

This custom field is **not** the native **Dodávateľ** relation in Upgates. The
[product API documentation](https://docs.upgates.com/api-reference/produkty)
documents `supplier` in GET responses, but the POST/PUT schemas do not expose it
or `supplier_id`. The writable `code_supplier` is the supplier's product code,
not its name. The [XML product format](https://docs.upgates.com/import-export/xml/products)
likewise documents only `SUPPLIER_CODE`.

The [documented native supplier workflow](https://www.upgates.cz/a/jak-funguji-dodavatele)
creates the supplier under **Nastavenia → Produkty → Dodávatelia**, then selects
it in the product's **Popis** tab, or uses a bulk edit. The API importer does not
send unsupported native supplier fields or claim that a custom metadata value
has established this relation. Native association must be completed through a
supported Upgates workflow.
