# Produktový editor / Product editor

## Návod pre obsluhu

Editor je lokálna tabuľka všetkých evidovaných produktov BIKETREK a xTrek vrátane položiek bez skladovej bilancie. Riadok predstavuje jeden fyzický produkt alebo variant s vlastným spoločným SKU. Pokladňový parent „xTrek“ neurčuje skupinu na hromadnú úpravu.

Uloženie mení ručné údaje v Hube. **Názvy, predajné ceny a viditeľnosť sa týmto editorom zatiaľ neposielajú do Upgates.** Označenie „uložené“ alebo API stav `saved_unpublished` znamená lokálne uloženú požadovanú hodnotu. Nie je to čakajúca synchronizácia ani potvrdenie zmeny e-shopu. Samostatné publikovanie vlastných zásob má iný postup v [stock-publication.md](stock-publication.md).

### Načítanie a výber

1. Otvorte produktový editor a odomknite ho existujúcim operátorským tokenom.
2. Nastavte hľadanie, značku, zalistovanie v e-shope a prípadne sklad. Potvrďte načítanie. Hľadanie prehľadáva SKU, názov, čiarové kódy a dodávateľské kódy; pri názvoch podporuje slovenskú a českú diakritiku.
3. Vyberte zobrazenie **Spoločné**, **BIKETREK** alebo **xTrek**. Zmena zobrazenia nemení vybraný sklad.
4. Počet produktov na stránku je 25, 50 alebo 100. Usporiadanie môže byť podľa lokálnej rodiny → farby → veľkosti, SKU alebo názvu. Veľkosti sa radia XS → S → M → L → XL → XXL; číselné veľkosti/SKU prirodzene, napríklad 2 pred 10. Zoradenie sa vykoná na serveri ešte pred stránkovaním.

Pri načítaní a uložení je uvedený účinok **Hub · čítanie** alebo **Hub · zmena**. Obe akcie používajú iba uložené údaje; nevolajú Upgates. V detaile produktu je odkaz **História pohybov**, ktorý otvorí skladovú históriu s filtrom na presné SKU.

Výber bez konkrétneho skladu zobrazuje katalóg. Množstvá sa v tomto režime nesčítavajú cez sklady. Umiestnenie a minimum možno meniť až po výslovnom výbere skladu. Zmena skladového filtra je pri rozpracovaných úpravách blokovaná; najprv ich uložte alebo zahoďte.

### Čo možno upravovať

| Oblasť | Polia | Význam |
| --- | --- | --- |
| Spoločné | Názov, značka, interná poznámka | Ručné hodnoty konkrétneho fyzického riadka. Zmena názvu neprepisuje názov rodiny ani ostatné varianty. |
| Variant | Základná predajná cena, sadzba DPH, poznámka | Predajná cena je EUR s DPH. Sadzba DPH je samostatný ručný údaj; chýbajúca sadzba sa neodhaduje. |
| Vybraný sklad | Umiestnenie, minimálne množstvo | Platí len pre zvolený sklad. Samotné uloženie nevytvorí skladovú bilanciu. |
| BIKETREK / xTrek | Požadovaný názov, predajná cena, viditeľnosť | Samostatné ručné hodnoty e-shopu, zatiaľ bez publikovania. |

Niektoré polia, napríklad interná poznámka, DPH a poznámka variantu, sú predvolene skryté. Zapnete ich cez výber stĺpcov. SKU, EAN, dodávateľské kódy, variantné atribúty a skladové množstvá sú v tabuľke iba na čítanie. Nákupná cena je odvodená zo skladu; jej zmena patrí do kontrolovaného [postupu nákupných cien](fifo.md).

### Stĺpce a produktové rodiny

- Hranicu hlavičky potiahnite myšou. Na zameranej hranici fungujú šípky doľava/doprava po 10 px (Shift po 40 px), Home/End pre minimum/maximum a dvojklik pre predvolenú šírku. Šírku 80–640 px možno zadať aj číselne v ponuke **Stĺpce**.
- V ponuke **Stĺpce** možno zapnúť/vypnúť polia a meniť ich poradie šípkami. SKU zostáva viditeľný ako prvý stĺpec. Obnovenie predvoleného nastavenia nemení rozpracované úpravy.
- Viditeľnosť, poradie a šírky sa pamätajú iba v konkrétnom prehliadači. Nejde o nastavenia používateľského účtu. Do tohto úložiska sa neukladajú tokeny, údaje produktov ani rozpracované hodnoty. Pri zablokovanom úložisku zostáva nastavenie funkčné pre otvorenú stránku.
- Samostatné stĺpce **Farba** a **Veľkosť** používajú existujúce pomenované atribúty (slovenské, české a anglické názvy). Ostatné parametre s pôvodnými názvami zobrazí stĺpec **Parametre variantu**. Chýbajúce údaje sa nedopĺňajú odhadom. Cez výber stĺpcov sú dostupné aj EAN, dodávateľské kódy, prepojené e-shopy, karanténa a nákupná hodnota skladu.
- Pri zoradení podľa rodín možno rozbaliť/zabaliť iba skutočnú lokálnu rodinu. Hlavička výslovne uvádza počet variantov **na tejto stránke**; rodina môže pokračovať na ďalšej stránke. Výber rodiny označí iba jej načítané varianty, nikdy vzdialeného pokladňového parenta ani nenačítané produkty.
- Zabalenie rodiny odznačí skryté varianty, zachová ich rozpracované úpravy a upozorní na ne v hlavičke. Výber celej stránky označuje len viditeľné fyzické riadky. Uloženie stále uloží všetky rozpracované produkty, aj tie na inej stránke alebo v zabalenej rodine.

Prázdna editovaná bunka odstráni ručnú hodnotu a obnoví dedenie. Spoločný názov a značka potom používajú importovaný produkt. Názov e-shopu dedí spoločný názov a jeho požadovaná cena základnú predajnú cenu variantu. Viditeľnosť bez ručnej hodnoty používa zachytenú viditeľnosť e-shopu. Minimum bez override používa existujúce minimum skladovej bilancie; ak bilancia neexistuje, zostáva neznáme. Umiestnenie a poznámky bez ručnej hodnoty zostávajú prázdne.

Zachytená cena e-shopu sa v detaile ukazuje oddelene s **neznámym daňovým základom**. Importované pole nemusí jednoznačne rozlišovať cenu s DPH a bez DPH, preto sa automaticky nepoužíva ako základná požadovaná cena. Nulová cena je platný výslovný údaj; prázdna cena je neznáma/dedená hodnota. Ceny majú najviac dve desatinné miesta, minimum je nezáporný celý počet kusov.

### Klávesnica, vkladanie a hromadné zmeny

- Šípky presúvajú aktívnu bunku. Enter, F2 alebo dvojklik otvoria úpravu; písanie do upraviteľnej bunky ju tiež otvorí.
- Enter potvrdí bunku do lokálneho konceptu. Tab / Shift+Tab potvrdí a presunie sa na ďalšiu / predchádzajúcu upraviteľnú bunku. Escape zruší práve otvorenú úpravu.
- Zo schránky možno vložiť obdĺžnik buniek oddelených tabulátormi, napríklad z tabuľkového editora. Vkladanie začína v aktívnej bunke a rešpektuje aktuálne poradie viditeľných stĺpcov a viditeľných fyzických riadkov. Zabalené varianty a hlavičky rodín sa neprepisujú. Celý vložený blok sa odmietne, ak presahuje aktuálnu stránku, obsahuje zamknutý stĺpec alebo neplatnú hodnotu. Nevkladá sa automaticky na ďalšiu stránku.
- Hromadné nastavenie platí iba pre označené fyzické riadky aktuálnej stránky. Nevyhľadáva ďalšie varianty parenta a nemení neoznačených súrodencov.
- Rozpracované úpravy môžu zostať cez prepínanie stránok a zobrazení; najviac 100 produktov naraz. Zmeny sú dovtedy iba v pamäti otvorenej stránky. Tlačidlo Uložiť potvrdí aktuálnu dávku na serveri.

### Konflikt alebo prerušené uloženie

Server overuje revíziu ručných údajov aj pôvodné importované hodnoty. Ak ich medzičasom niekto zmenil, daný riadok vráti konflikt. Ostatné platné riadky dávky sa môžu uložiť; neúspešné zostanú rozpracované. V detaile porovnajte aktuálny stav servera s vlastným návrhom. Výslovné potvrdenie nového základu ponechá vlastný návrh na ďalšie uloženie; konflikt sa automaticky neprepisuje.

Ak sa stratí odpoveď na uloženie, použite obnovu výsledku. Server drží trvalý výsledok pod UUID požiadavky. Ak výsledok ešte neexistuje, editor umožní poslať tú istú požiadavku s tým istým UUID. Opakovanie nevytvorí druhú úpravu ani duplicitný audit. Počas nejasného výsledku editor blokuje ďalšie úpravy.

UUID rozpracovaného odoslania a neuložené koncepty si aktuálne pamätá iba otvorená stránka. Obnovenie stránky, zatvorenie alebo zamknutie ich odstráni; serverový výsledok a audit už uložených zmien zostávajú. Pri nejasnom výsledku preto najprv použite obnovu v otvorenom editore. V detaile je posledných 20 záznamov ručných zmien a oddelený panel **Sklad a nákupné ceny**.

Skladový údaj bez bilancie a preukázaného pohybu je **neznámy**, nie nula. Potvrdená nulová zásoba sa zobrazuje ako nula. Dostupné množstvo odpočítava rezervácie aj karanténu. Pri neúplnom alebo predbežnom ocenení editor nevydáva celkovú nákupnú hodnotu a priemer za definitívnu cenu.

## Developer contract

Implemented modules are `product_editor_models.py`, `product_editor_types.py`, `services/product_editor.py`, `routers/product_editor.py`, and migration `012_product_editor.sql`. The React page uses `/api/product-editor`; the backend router prefix is `/product-editor`. All routes require the shared operator-access dependency and return `Cache-Control: no-store`. This shared credential is **not user-level RBAC** and the audit does not identify individual people.

### API

| Method / path | Contract |
| --- | --- |
| `GET /product-editor/options` | Active Upgates shops, active warehouses, effective brands, page sizes, `currency: EUR`, `price_basis: incl_vat`, `external_write_enabled: false`. |
| `GET /product-editor/products` | Server-side `q`, `brand`, `shop_code`, `warehouse_code`, `page`, `page_size` (25/50/100), `sort` (`group_sku`, `sku`, `name`) and `direction` (`asc`, `desc`). Returns `items`, `total`, paging and warehouse context. |
| `GET /product-editor/products/{id}` | Same effective row plus the latest 20 manual audit entries; optional `warehouse_code`. |
| `POST /product-editor/save` | Explicitly confirmed, bounded batch of up to 100 whitelisted row patches. |
| `GET /product-editor/saves/{request_id}` | Exact durable response for a completed UUID; missing result is 404. |

A save body contains `request_id` (UUID), optional `warehouse_code`, literal boolean `confirmed: true`, and `changes`. Each change contains `product_id`, `expected_revision`, `snapshot_hash` and only the modified scopes:

```json
{
  "common": {"name": "Example", "brand": null, "internal_note": "Shelf label"},
  "variant": {"sale_price_gross": "29.90", "vat_rate": null, "note": null},
  "warehouse": {"location": "A-2", "min_quantity": "2"},
  "shops": {"biketrek": {"name": null, "sale_price_gross": "31.90", "visible": false}}
}
```

Absent fields are unchanged; explicit null deletes that override. A warehouse patch requires an explicit active warehouse. Shop keys must identify active Upgates shops. Names are at most 500 characters, brand/location 100, notes 4000. Money uses nonnegative decimal **strings**, at most two decimal places and a maximum of `9999999999.99`; VAT is nullable or a string from 0 through 100 with at most two decimal places. Minimum quantity is a whole string from 0 through 999999999. The UI normalizes decimal commas; the API accepts dot-decimal canonical syntax. Boolean and numeric coercion, extra fields, malformed Unicode and control characters are rejected. The encoded batch is bounded to 1 MB.

Results have `status: completed`, `external_write_enabled: false`, and per-row `status` values `saved`, `conflict`, `invalid` or `missing`, plus sanitized errors and an optional effective `row`. Top-level invalid input rejects the request; row validation preserves valid rows. Duplicate product IDs in one batch are invalid. No-op patches return the current row without incrementing revision or creating an audit entry.

Rows expose canonical identity/group/attributes, barcode and supplier-code observations, a bounded HTTPS image URL, revision/source hash, effective `common`, `variant`, selected `warehouse`, stock, per-shop effective/observed/override values, and raw whitelisted `overrides`. Only canonical `Product.group_id` determines local grouping. Shop parents, including large POS umbrellas, never expand the selected IDs. Images are extracted from already stored data; no upstream request is performed by the service.

`group_sku` sorting aggregates canonical attributes once, then orders each family by colour, normalized apparel size rank, natural size, remaining attributes and stable SKU/ID. Colour aliases are `farba`, `barva`, `color`, `colour`; size aliases are `velkost`, `velikost`, `size` after case/accent/space normalization. Recognized clothing sizes XXXS/3XS through XXXXXL/5XL precede other naturally ordered sizes. It is applied before `OFFSET`/`LIMIT`; the browser does not reorder a paginated subset.

`productEditorGrid.ts` persists only versioned, whitelisted column keys and bounded integer widths under `product-editor-columns-v1` in localStorage. Invalid JSON/version falls back to defaults; unknown/duplicate keys are removed, missing order keys appended, and SKU is always first/visible. Preferences survive token changes without persisting protected rows or drafts. Family expansion is transient, derived solely from canonical IDs on the loaded page, and introduces no extra API request. Keyboard/TSV operations resolve against the exact current visible physical row and column identities. Saved or uncertain requests retain their frozen original body regardless of presentation state.

The stock DTO preserves decimal strings and nulls: `known`, quantities including `qty_quarantined`, `avg_cost`, `total_value`, `valuation_complete`, `known_value`, `provisional_value`, `unknown_qty`, `provisional_qty`. A missing warehouse or unverified balance does not manufacture zero stock. For FIFO, unknown/provisional costs or layer/balance coverage disagreement suppress definitive average/total valuation. Partial known/provisional values remain distinct.

### Ownership, concurrency and recovery

Imported `Product`, `ShopProduct.shop_price`, content snapshots, supplier feeds, identifiers and stock history remain source records. Manual values live in separate overrides; subsequent imports cannot overwrite them. `effective(db, product_ids, warehouse_code=None)` supplies full selected display rows (maximum 100 IDs). `effective_names(db, product_ids=None)` is the lightweight common-name/brand override lookup used by existing stock views; it does not read shop parent content. Consumers needing warehouse minimum use the explicit override with fallback to `StockBalance.min_quantity`.

The snapshot hash covers imported display/identity facts, observed shop values, selected warehouse and its inherited minimum. Dynamic stock quantities/costs are excluded; override concurrency is guarded separately by revision. Saves acquire a request-specific advisory lock, the shared product identity write lock, the selected warehouse lock, sorted product locks and relevant mapping/content locks before re-reading facts. Each changed row uses a savepoint. Audit and the completed batch result commit with the successful overrides. This is an optimistic compare-and-swap, not a last-write-wins replacement.

The first accepted UUID stores its normalized request hash and exact result. Same UUID/same body replays that result; same UUID/different body returns `product_editor_request_reused` (409). A replay remains the original result even after later source or override changes. Browser recovery is currently limited to the open page; the durable API result itself survives restarts.

### Additive schema 012

| Object | Fields and invariant |
| --- | --- |
| `product_editor_overrides` | `product_id` primary key/FK, positive `revision`, whitelisted `data` JSONB, `updated_at`. JSON scopes are `common`, `variant`, `warehouses` keyed by warehouse code, and `shops` keyed by shop code. |
| `product_editor_saves` | UUID-text `request_id` primary key, `input_hash`, durable `result` JSONB, `created_at`. |
| `product_editor_audit` | ID, save/product foreign keys, positive revision, before/after JSONB, timestamp; unique `(product_id, revision)`. |
| `product_editor_natural_key(text)` | Immutable SQL tokenization helper for natural numeric/text ordering. |

Migration 012 creates these objects without altering product, balance or movement history and supports reruns. `python -m inventory_hub.product_editor_migrate` uses the shared migration advisory lock. Deployment wiring runs 011 before 012; test schemas using the editor need the FIFO tables too.

There is no product-editor publisher or editor-owned sync queue in this increment. Existing AI selected-field publication does not provide a safe substitute for price/visibility publication and cannot be treated as a general leaf editor, especially under POS umbrellas. A future publisher needs a frozen desired-field preview, exact remote leaf identity, explicit price-list/VAT semantics, narrow writes, readback and uncertain-write recovery.

### Verification and remaining limits

Local verification on 2026-09-23: 13 pure/API tests passed, covering validation, inheritance, source hashes, access checks, sanitized failures and PostgreSQL query compilation. Twenty guarded PostgreSQL cases were present but skipped locally because an isolated test database was unavailable; they exercise paging/search, selected-leaf reads, source preservation, CAS/concurrency, replay, partial success, valuation and migration reruns in CI. Frontend interaction tests are maintained separately in `frontend/tests/product-editor-ui.cjs`; they do not prove browser layout or live Upgates delivery.

On 2026-09-24, the expanded product-editor interaction suite and Vite build passed locally. It covers mouse/keyboard resizing, accessible reordering, persistence across page remount, malformed preference recovery, named attributes, family selection/collapse, draft preservation, TSV against reordered columns, quarantine read-only behavior and the movement-history link, alongside existing save/conflict/recovery cases. Thirteen pure/API tests passed; 21 guarded PostgreSQL tests, including a new 32-variant colour/size pagination scenario, were skipped locally pending an isolated database. No browser layout or live shop delivery was verified. The full legacy TypeScript check still reports unrelated repository errors and existing ES2020/Array.at compatibility errors; Vite build success is not a claim of a clean global type check.

Current limits include per-product common values rather than editable family records, no identity/EAN/image editor, no editor-specific shop publisher, shared operator access rather than individual roles, and SK/CZ accent folding rather than a universal linguistic search index. Supplier-specific and imported observations remain separate from manual ownership.
