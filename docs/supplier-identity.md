# Identita produktu a dodávateľské väzby

Každý tovar alebo služba má jedno interné `products.id`. Existujúce SKU zostáva
prevádzkovým kódom karty; dodávateľ ani e-shop nemusia používať rovnaký textový
kód. Nové karty používajú [spoločnú politiku identity](sku-policy.md) bez povinného
značkového či dodávateľského prefixu. Úvodné nuly dodávateľských kódov sa zachovajú.

## Jeden zdroj pre každý údaj

| Údaj | Autoritatívny zdroj |
| --- | --- |
| Identita skladovej karty | `products.id` a jej explicitné identifikátory |
| Aktuálna dodávateľská väzba | `product_supply_sources` → `supplier_products.id` |
| Kód a katalógová ponuka dodávateľa | `supplier_products`, identita `(supplier_id, supplier_sku)` |
| Čerstvá dodávateľská dostupnosť | `supplier_availability_observations` pre rovnakú dvojicu dodávateľa a kódu |
| Pôvod založenia karty | `created_from_source`, `source_supplier_product_id`; nemení aktuálnu väzbu |
| Vlastná zásoba a nákupná cena prijatého tovaru | Potvrdená skladová evidencia a FIFO |

Viacerí dodávatelia môžu ponúkať tú istú skladovú kartu. Jedna konkrétna ponuka
má najviac jedného vlastníka v Hube; chráni to aj databázový unikátny index.
Priorita, primárny zdroj, objednateľnosť a vypnutie patria konkrétnej väzbe.
Príjmy si zachovávajú vlastné nákupné ceny. Cena v aktuálnom katalógu neprepisuje
historické FIFO náklady.

Staré polia `products.supplier_id`, `supplier_price`, `supplier_stock`,
`supplier_availability`, `supplier_feed_data` a `supplier_feed_synced_at` sa
nepoužívajú ako aktuálny dodávateľský zdroj. Nasadenie ich nemaže ani z nich
nevytvára fyzickú zásobu. Aktívne aplikačné čítania používajú relačné väzby.
Pôvodné SQL pohľady `v_product_with_identifiers`, `v_product_inventory` a
`v_stock_alerts` zostávajú tvarovo zachované s výslovným komentárom, že ich
historické dodávateľské polia nie sú autoritatívne. Aktívny kód ich nepoužíva;
aktuálny sklad a dostupnosť vracajú aplikačné projekcie s kontrolou potvrdenia
a čerstvosti.

## Prepojenie existujúcej karty

V detaile karty otvorte **Dodávatelia**, vyhľadajte konkrétnu položku dodávateľa
a potvrďte [náhľad prepojenia](supplier-card-links.md). Karta si zachová ID,
SKU, obsah, e-shopové väzby aj sklad. Náhľad a potvrdenie nepotrebujú prefix
v konfigurácii. Rozdielne platné EAN-y, známa odlišná veľkosť či farba a iný
vlastník dodávateľskej ponuky zostávajú konfliktom.

Pri založení karty z katalógu vznikne skutočná dodávateľská väzba spolu so
scoped identifikátorom v jednej transakcii. Faktúrový alias bez katalógovej
ponuky môže zostať samostatným identifikátorom na vyhľadanie príjemky; sám
nepredstavuje dodávateľskú dostupnosť.

Čítanie feedu alebo obnovenie e-shopu kontroluje iba existujúce väzby a môže
opraviť chýbajúci identifikátor tej istej ponuky. Nezakladá nové väzby podľa
SKU, prefixu, pôvodu založenia ani osamoteného aliasu. Odstránená väzba sa tak
pri ďalšom feede neobnoví sama. Vypnuté väzby sa automaticky nezapnú.

Tlačidlo **Prepojiť dostupnosti** je výslovná lokálna oprava. Môže doplniť
chýbajúcu väzbu podľa už overeného dodávateľského identifikátora; pri osobitne
nakonfigurovanom vyhľadávaní aj podľa presnej identity konkrétnej e-shopovej
položky a overených variantových údajov. Samotná podobnosť názvu, text SKU
alebo dodávateľský prefix nie je dôkaz. Pri konflikte sa väzba nezapíše.

Táto oprava nesťahuje feed, nevolá e-shopy, nemení cenu ani sklad. Opakovanie
neduplikuje vzťah. Stav dostupnosti naďalej rozlišuje chýbajúcu väzbu, konflikt,
chýbajúce pozorovanie a starý údaj. Oprava väzby nepredĺži platnosť pozorovania.

## API a transakcie

`services/supplier_links.py` a explicitné prepojenie používajú spoločný
`IDENTITY_WRITE_LOCK` aj resolver. Zápis väzby a dodávateľského identifikátora
je atómový; kód zostáva textom v rozsahu konkrétneho dodávateľa.

Chránený `POST /api/supplier-availability/{supplier}/links/reconcile` prijíma
`after_product_id` a `limit` 1–500. Odpoveď obsahuje `scanned`, `linked`,
`existing`, `skipped`, `conflicts`, `skipped_details` a pokračovací kurzor
`next_after_product_id`. Konflikt sa nezmení na úspech výberom prvej zhody.

Staré nastavenie a register prefixov zostávajú dostupné pre existujúce
konfigurácie a staré pripravené operácie. Neurčujú súčasného dodávateľa karty
a nové dodávateľské prepojenie ich nečíta ani neuzamyká.

## Migrácia a overenie

Migrácia `032_supplier_authority.sql` jednorazovo prevedie iba jednoznačné
existujúce zdrojové odkazy a dodávateľské identifikátory na `product_supply_sources`.
Ak viac kariet tvrdí rovnaký pôvod alebo si dôkazy odporujú, nevytvorí medzi nimi
nové prepojenie. Pôvodné údaje zostanú na kontrolu. Existujúci konflikt vlastníkov
aktuálnej väzby alebo jej dodávateľského aliasu zastaví migráciu s konkrétnou
chybou; nič sa neprepojí potichu.

`schema_migrations` uchováva iba číslo dokončenej migrácie a čas. Opakované
nasadenie už neobnovuje väzby z historického pôvodu. Unikátny index
`uq_supply_source_owner` chráni jednu dodávateľskú ponuku pred dvomi Hub kartami.
Migrácia nemení stock ledger, FIFO, ceny, nastavenia automatizácie ani e-shopy.
Pri návrate kódu ponechajte pridané vzťahy a index; preferujte opravu dopredu.

Databázové regresie overujú jednorazový prevod, zachovanie vypnutej väzby,
konfliktné vlastníctvo, opakovanie po odpojení a nulové skladové zápisy. Ďalšie
testy pokrývajú čerstvosť, identifikátory, viac dodávateľov a explicitnú opravu.
Používajú iba izolovaný localhost `*_catalog_test`; ich výsledok a produkčné
nasadenie sa uvádzajú samostatne.
