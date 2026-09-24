# Dodávateľské dostupnosti

## Pre obsluhu

V obrazovke **Dostupnosti a synchronizácia** má každý dodávateľ samostatný zdroj, interval sťahovania, platnosť údajov a vypínač automatického načítavania. Nové nastavenia sú vypnuté. Najprv vyberte nakonfigurovaný feed a uložte nastavenia; potom môžete spustiť načítanie ručne aj bez zapnutia automatiky. Spustenie zaradí úlohu do frontu. Obrazovka zobrazuje prebiehajúci beh, posledný úspech, chybu a ďalší termín.

Interval je od 5 minút po 7 dní. Platnosť údajov musí pokryť aspoň jeden interval; najviac môže byť 30 dní. Predvolené hodnoty sú 1 hodina a 6 hodín. Čas posledného úspechu je začiatok prijatého načítania, nie okamih neskoršieho uloženia. Pri staršom feede tak údaje nedostanú umelo dlhšiu platnosť.

**Minimálne pokrytie** kontroluje pokles počtu riadkov. Nevie dokázať úplnosť prvého feedu ani zachytiť všetky výmeny položiek pri nezmenenom počte. Predvolene musí nový feed obsahovať aspoň 100 % počtu položiek z posledného úspechu. Ak dodávateľ legitímne zúži ponuku, môžete tento prah znížiť a spustiť nový pokus. Vynechané položky sa nikdy automaticky nemenia na nulu: posledná prijatá dostupnosť ostáva platná len do pôvodného termínu. Prázdny, chybný alebo podlimitný feed sa odmietne celý a posledný úspech sa zachová.

Dostupnosť dodávateľa je oddelená od fyzického skladu. Hodnota `6+` znamená najmenej šesť kusov, údaj áno/nie vyjadruje dostupnosť bez presného počtu. Ani jeden nevytvára príjem alebo vlastnú zásobu. Na e-shop sa publikuje iba vlastné overené voľné množstvo; dodávateľ môže ovplyvniť dodaciu dostupnosť.

Text dodacej dostupnosti sa upravuje v konfigurácii konkrétneho dodávateľa v `adapter_settings.availability.orderable`. Ak chýba, použije sa **do 5 dní**. Vlastná voľná zásoba má prednosť a výsledok je **SKLADOM**. Keď je vlastné overené voľné množstvo nulové a čerstvý dodávateľ potvrdí dostupnosť, použije sa jeho nastavený text. Bez takého potvrdenia sa použije **overíme**; zákazník stále môže objednať. Zlyhanie sťahovania nie je dôkaz nulovej zásoby.

Pri viacerých dodávateľoch sa použije prvý čerstvý dostupný zdroj podľa primárneho zdroja a priority. Vypnuté alebo neobjednateľné väzby sa nepoužijú. Samotné vypnutie automatického sťahovania neruší už získané údaje; tie dožijú svoju platnosť.

Ručné spustenie počas prebiehajúceho načítania pripraví jeden ďalší beh. Opakované kliknutia nevytvárajú paralelné sťahovania. Zmena konfigurácie počas sťahovania zneplatní rozpracovaný výsledok a vyžiada nový beh.

## Pre vývojárov

Implementované v kóde: migrácia `014_supplier_availability.sql`, ORM `supplier_availability_models.py`, služby `supplier_availability.py`, `supplier_availability_source.py`, `supplier_availability_worker.py` a chránený router. Stav nasadenia je vedený v `mvp-progress.md`; samotná prítomnosť tejto dokumentácie nedokazuje zapnutie automatizácie na produkcii.

API používa existujúci operator token a `Cache-Control: no-store`:

| Metóda a cesta | Účel |
| --- | --- |
| `GET /supplier-availability` | Zoznam konfigurácií a prevádzkového stavu bez prihlasovacích údajov zdrojov |
| `PUT /supplier-availability/{supplier}` | Uloženie s `expected_revision`; striktne validované `enabled`, `feed_key`, `interval_seconds`, `freshness_seconds`, `min_coverage_percent` |
| `POST /supplier-availability/{supplier}/run` | Trvalé zaradenie ručného behu s `expected_revision`; odpoveď 202 |

Revizia 0 znamená ešte neuložené nastavenia. Interval a plánovanie sú v PostgreSQL `supplier_availability_settings`; prihlasovacie údaje, parser a texty dostupnosti ostávajú v existujúcom súborovom dodávateľskom configu. Každý beh zachová hash celého configu a jeho revíziu. Pred prijatím sa znovu overia. Do auditu sa neukladá URL ani autentizácia.

Worker používa jeden PostgreSQL session advisory lock naprieč replikami. Krátka transakcia pripraví `SupplierFeedRun`, potom sa transakcia ukončí. Stiahnutie a parsovanie bežia mimo transakcie; druhá krátka transakcia atómovo prijme všetky pozorovania a audit. Ukončený či prerušený beh sa neopakuje pod rovnakým identifikátorom. Zostatkové `running` behy ďalší vlastník locku označí ako prerušené. Chyby obsahujú stabilné kódy, nikdy plnú výnimku s možnými tajnými URL.

Downloader je spoločný s katalógom, s limitom 100 MB a pri automatike aj kontrolou celkového času. Používajú sa existujúce listing parsery Paul Lange a Northfinder; minimálny Paul Lange stock XML potrebuje iba `ITEM_ID` a `STOCK`/`STOCK_EXTERNAL`. Northfinder používa už podporovaný formát rodičov a variantov. Iný formát potrebuje explicitný parser. Nenačítava sa generický neoverený XML mapping.

`supplier_availability_observations` uchováva identitu `(supplier_id, supplier_sku)`, presnosť množstva, pozorovanie, expirácie a odkaz na `SupplierFeedRun`. `SupplierFeedItemRaw` archivuje iba potrebné skladové fakty. Katalógové názvy, ceny, obrázky a jeho snapshot/run ID sa pri tomto procese neprepíšu. Na identitu fyzického produktu sa používajú existujúce `ProductSupplySource` a ako záloha `Product.source_supplier_product_id`; žiadna podobnosť názvu ani globálne EAN párovanie.

`await project(db, product_ids, at=None)` vracia dostupnosť pre každé požadované product ID. Platnosť vyhodnocuje pri každom čítaní. Pravidelný prenos zásob musí preto opakovane prejsť mapované položky aj bez nového feedu: samotné uplynutie platnosti môže zmeniť dodaciu dostupnosť. Skrátenie TTL platí okamžite, predĺženie neoživuje pôvodne expirované pozorovania. Množstvo dodávateľa sa nikdy nepripočíta k vlastnému.

Overenie: lokálne testy kontrolujú presnosť množstiev, odmietnutie neplatných hodnôt, expiráciu, minimálny stock XML a ochranu API. Databázové testy majú guard na izolovaný localhost `*_catalog_test` a overujú opakovateľnosť migrácie, atómové prijatie, zachovanie posledného úspechu, zmenu konfigurácie počas behu, front a zakázané väzby. Plný výsledok CI a nasadenie sa zaznamenávajú samostatne.

Migrácia je aditívna, nevkladá aktivačné nastavenia a nemení skladový denník. Pri problémoch vypnite automatiku; údaje ostávajú k dispozícii na kontrolu. Nepoužívajte spätný zásah do fyzických zásob ako opravu dodávateľského feedu.
