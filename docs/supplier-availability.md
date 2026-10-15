# Dodávateľské dostupnosti

## Pre obsluhu

V obrazovke **Dostupnosti a synchronizácia** má každý dodávateľ samostatný zdroj, interval sťahovania, platnosť údajov a vypínač automatického načítavania. Nové nastavenia sú vypnuté. Najprv vyberte nakonfigurovaný feed a uložte nastavenia; potom môžete spustiť načítanie ručne aj bez zapnutia automatiky. Spustenie zaradí úlohu do frontu. Obrazovka zobrazuje prebiehajúci beh, posledný úspech, chybu a ďalší termín.

Interval je od 5 minút po 7 dní. Platnosť údajov musí pokryť aspoň jeden interval; najviac môže byť 30 dní. Predvolené hodnoty sú 1 hodina a 6 hodín. Čas posledného úspechu je začiatok prijatého načítania, nie okamih neskoršieho uloženia. Pri staršom feede tak údaje nedostanú umelo dlhšiu platnosť.

**Minimálne pokrytie** kontroluje pokles počtu riadkov. Nevie dokázať úplnosť prvého feedu ani zachytiť všetky výmeny položiek pri nezmenenom počte. Predvolene musí nový feed obsahovať aspoň 100 % počtu položiek z posledného úspechu. Ak dodávateľ legitímne zúži ponuku, môžete tento prah znížiť a spustiť nový pokus. Vynechané položky sa nikdy automaticky nemenia na nulu: posledná prijatá dostupnosť ostáva platná len do pôvodného termínu. Prázdny, chybný alebo podlimitný feed sa odmietne celý a posledný úspech sa zachová.

Dostupnosť dodávateľa je oddelená od fyzického skladu. Hodnota `6+` znamená najmenej šesť kusov, údaj áno/nie vyjadruje dostupnosť bez presného počtu. Ani jeden nevytvára príjem alebo vlastnú zásobu. Na e-shop sa publikuje iba vlastné overené voľné množstvo; dodávateľ môže ovplyvniť dodaciu dostupnosť.

Text dodacej dostupnosti sa upravuje v konfigurácii konkrétneho dodávateľa v `adapter_settings.availability.orderable`. Ak chýba, použije sa **do 5 dní**. Vlastná voľná zásoba má prednosť a výsledok je **SKLADOM**. Keď je vlastné overené voľné množstvo nulové a čerstvý dodávateľ potvrdí dostupnosť, použije sa jeho nastavený text. Bez takého potvrdenia sa použije **overíme**; zákazník stále môže objednať. Zlyhanie sťahovania nie je dôkaz nulovej zásoby.

Pri viacerých dodávateľoch sa použije prvý čerstvý dostupný zdroj podľa primárneho zdroja a priority. Vypnuté alebo neobjednateľné väzby sa nepoužijú. Samotné vypnutie automatického sťahovania neruší už získané údaje; tie dožijú svoju platnosť.

Čerstvý feed musí byť prepojený s konkrétnym skladovým produktom. Jednoznačné väzby sa dopĺňajú pri importe; pre už stiahnuté údaje slúži lokálne tlačidlo **Prepojiť dostupnosti** pri dodávateľovi. Nesťahuje feed ani nevolá Upgates. Tabuľka rozlišuje chýbajúcu väzbu, konflikt identity, chýbajúce pozorovanie a starý údaj. Podrobnosti vrátane uzamknutia prefixu a pravidla `PL-` + dodávateľský kód sú v [dodávateľskej identite](supplier-identity.md).

V produktovej tabuľke sa **množstvo u dodávateľa** a **dodávateľská dostupnosť** opierajú o rovnaké pozorovanie. Potvrdená čerstvá nula sa zobrazuje ako nedostupnosť; chýbajúci alebo starý údaj ostáva nepotvrdený. Text „overíme“ je konzervatívny náhradný text pre publikáciu, nie informácia o prebiehajúcom načítaní. Skutočný beh a zaradenie do frontu majú samostatný stav. Vlastná zásoba sa do týchto dodávateľských stĺpcov nepripočítava.

Odkaz pri produkte otvorí nastavenia dodávateľa cez `/settings/availability?supplier=<code>`. Nastavenia vždy patria celému dodávateľskému zdroju, neukazujú jeden produkt ani zoznam prepojených kódov. Staré odkazy s `sku` naďalej fungujú, ale tento parameter sa ignoruje. Presná produktová diagnostika zostáva na karte produktu. Stránka iba prečíta miestne nastavenia; nič sama neaktivuje. **Načítať dostupnosti** zaradí celý vybraný feed do fronty. Pri čakaní a načítaní sa stav každých päť sekúnd obnovuje čítaním z Hubu; formulárové úpravy ani neistý zápis sa tým nezahodia. **Všetci dodávatelia** vráti úplnú obrazovku nastavení.

Diagnostika rozlišuje tieto príčiny:

| Stav | Význam a ďalší krok |
| --- | --- |
| `unconfigured` | Dostupnosti dodávateľa ešte nemajú uložené nastavenia; vybrať zdroj a uložiť. |
| `not_loaded` | Zdroj je nastavený, ale nebol úspešne načítaný; spustiť ručné načítanie a skontrolovať prípadnú chybu. |
| `missing_observation` | Zdroj už bol načítaný, no pre presný prepojený dodávateľský kód nie je uložené pozorovanie. Samotný stav nedokazuje neprítomnosť v najnovšom externom súbore. |
| `feed_mismatch` | Uložený údaj pochádza z iného feedu než aktuálne zvoleného; skontrolovať zdroj a načítať ho. |
| `configuration_error` | Konfigurácia alebo zvolený zdroj nie je použiteľný; opraviť konfiguráciu dodávateľa. |
| `stale` | Pozorovanie existuje, ale vypršala jeho platnosť; načítať nové údaje. |
| `unavailable_source` | Produkt, dodávateľ alebo väzba je vypnutá/neobjednateľná; skontrolovať existujúce prepojenie. |

Kód v prepojení je raw dodávateľský kód. Napríklad Northfinder `103404-281-103` sa vyhľadáva spolu s identitou dodávateľa; prefix `NF-` ani odlišné Hub SKU nenahrádzajú tento kľúč. Katalógový snapshot množstva, jeho čas stiahnutia ani uloženie dodávateľskej väzby nevytvárajú čerstvé pozorovanie dostupnosti. Nová diagnostika nemení pravidlá publikácie, množstvo vo vlastnom sklade ani konfiguráciu automatiky.

Ručné spustenie počas prebiehajúceho načítania pripraví jeden ďalší beh. Opakované kliknutia nevytvárajú paralelné sťahovania. Zmena konfigurácie počas sťahovania zneplatní rozpracovaný výsledok a vyžiada nový beh.

## Sloger: iba ručné B2B čítanie

Sloger nemá skladový feed. Prihlásenie sa uloží raz v nastavení dodávateľa a na obrazovke dostupností sa používa pevný zdroj **Sloger B2B**. Obsluha nastaví platnosť údajov a spustí ručný refresh. Automatické spúšťanie nie je dostupné: API odmietne jeho zapnutie a worker ignoruje aj prípadnú starú zapnutú hodnotu. Samotné nasadenie alebo uplynutie intervalu nikdy nezačne nový Sloger crawl.

Rozsah tvorí zoznam aktívnych produktov už evidovaných v Hube s aktívnou objednateľnou väzbou na Sloger; zahŕňa aj aktuálne vypredané produkty. Zoznam sa zmrazí na začiatku ručného behu. Celý dodávateľský katalóg sa neprechádza. Staršie jednoznačné zdrojové väzby sa použijú iba tam, kde produkt nemá novšie dodávateľské väzby.

Existujúci trvalý beh uchováva kurzor a počty spracovaných, úspešných a neúspešných SKU. Worker spracúva najviac desať SKU na dávku, v jednej prihlásenej relácii, s odstupom medzi požiadavkami. Spoločný zámok zabráni súbežnému B2B čítaniu s prípravou importu. Pokračovanie už vyžiadaného behu po dávkach nie je pravidelné spúšťanie nového crawlu.

Úspešné pozorovania dostanú vlastný čas a platnosť. Chybná položka zachová starší údaj len do pôvodnej expirácie a zobrazí sa vo výsledku. Chyba prihlásenia, ochranná výzva alebo nedostupný zdroj zastaví ďalšie čítanie; neúspešné položky sa automaticky neopakujú. Neznáme hodnoty ostávajú neznáme a nevytvárajú nulovú dostupnosť. Prerušený rozpracovaný chunk možno obnoviť ako čítanie; potvrdené dávky sa neopakujú.

Rovnaká služba uloží dodávateľské pozorovania aj pri príprave importu, aby sa po uložení produktu dali použiť v existujúcom prepočte dostupnosti. Tento čiastkový importný záznam nepredstiera úspech celého ručného refreshu. Sloger B2B nevolá AI, nemení vlastné zásoby, FIFO ani sám neposiela údaje do e-shopov. Dostupnosť dodávateľa **do 2-4 dní** platí iba pri potvrdenej čerstvej dostupnosti; bez nej ostáva **overíme**.

## Pre vývojárov

Implementované v kóde: migrácia `014_supplier_availability.sql`, ORM `supplier_availability_models.py`, služby `supplier_availability.py`, `supplier_availability_source.py`, `supplier_availability_worker.py` a chránený router. Stav nasadenia je vedený v `mvp-progress.md`; samotná prítomnosť tejto dokumentácie nedokazuje zapnutie automatizácie na produkcii.

API používa existujúci operator token a `Cache-Control: no-store`:

| Metóda a cesta | Účel |
| --- | --- |
| `GET /supplier-availability` | Zoznam konfigurácií, prevádzkového stavu a `latest_run` so súhrnom posledného behu; bez prihlasovacích údajov zdrojov |
| `GET /supplier-availability/{supplier}/runs/{run_id}/failures` | Stránkovaný zoznam preskočených riadkov konkrétneho dodávateľa; `offset`, `limit` 1–100 (predvolene 50), kód, pôvodná hodnota a dôvod |
| `GET /supplier-availability/{supplier}/observation?sku=<raw>` | Miestna diagnostika presnej katalógovej položky: stav, raw kód, zdroj, časy a odporúčaný ďalší krok; bez downloadu alebo zápisov obchodných údajov |
| `PUT /supplier-availability/{supplier}` | Uloženie s `expected_revision`; striktne validované `enabled`, `feed_key`, `interval_seconds`, `freshness_seconds`, `min_coverage_percent` |
| `POST /supplier-availability/{supplier}/run` | Trvalé zaradenie ručného behu s `expected_revision`; odpoveď 202 |
| `POST /supplier-availability/{supplier}/links/reconcile` | Lokálna dávka prepojenia existujúcich údajov; `after_product_id`, limit 1–500, konflikty a pokračovací kurzor |

Revizia 0 znamená ešte neuložené nastavenia. Interval a plánovanie sú v PostgreSQL `supplier_availability_settings`; prihlasovacie údaje, parser a texty dostupnosti ostávajú v existujúcom súborovom dodávateľskom configu. Každý beh zachová hash celého configu a jeho revíziu. Pred prijatím sa znovu overia. Do auditu sa neukladá URL ani autentizácia.

Worker používa jeden PostgreSQL session advisory lock naprieč replikami. Krátka transakcia pripraví `SupplierFeedRun`, potom sa transakcia ukončí. Stiahnutie a parsovanie bežia mimo transakcie; druhá krátka transakcia atómovo prijme platné pozorovania a audit všetkých preskočených položiek. Ukončený či prerušený beh sa neopakuje pod rovnakým identifikátorom. Zostatkové `running` behy ďalší vlastník locku označí ako prerušené. Chyby obsahujú stabilné kódy, nikdy plnú výnimku s možnými tajnými URL.

Downloader je spoločný s katalógom, s limitom 100 MB a pri automatike aj kontrolou celkového času. Používajú sa existujúce listing parsery Paul Lange, Northfinder a SK Profi Bike; minimálny Paul Lange stock XML potrebuje iba `ITEM_ID` a `STOCK`/`STOCK_EXTERNAL`. Northfinder používa samostatné čítanie dostupnosti zo všetkých fyzických variantov (alebo samostatných produktov) v podporovanom XML. Nevyžaduje ceny ani názov a rešpektuje nakonfigurované tagy; dostupnosť sa neodvodzuje zo súčtu rodiča. Iný formát potrebuje explicitný parser. Nenačítava sa generický neoverený XML mapping.

SK Profi Bike používa `catalog_parser: "sk-profi-bike"`: plný produktový aj zjednodušený skladový XML číta `products/product/options/option/kod` a `sklad`. Anonymné položky bez kódu sa preskočia a uvedú v prehľade chýb; duplicitné identity odmietne spoločná validácia po riadkoch. Záporné alebo neplatné množstvo je v prehľade preskočených riadkov; neprepíše posledné platné pozorovanie. Neprítomnosť SKU nie je dôkazom nulovej zásoby. Pri overení 13. 10. 2026 skladový feed neobsahoval 141 položiek aktuálneho katalógu, preto možno pre úplné pokrytie zvoliť ako zdroj dostupnosti `products`. Ide iba o dodávateľské pozorovania; tento refresh neobnovuje katalógové ceny, nevytvára príjem a neposiela zásobu do e-shopu. Automatika sa zapína samostatne v existujúcom nastavení.

`supplier_availability_observations` uchováva identitu `(supplier_id, supplier_sku)`, presnosť množstva, pozorovanie, expirácie a odkaz na `SupplierFeedRun`. `SupplierFeedItemRaw` archivuje iba potrebné skladové fakty. Katalógové názvy, ceny, obrázky a jeho snapshot/run ID sa pri tomto procese neprepíšu. Na identitu fyzického produktu sa používajú existujúce `ProductSupplySource` a ako záloha `Product.source_supplier_product_id`; žiadna podobnosť názvu ani globálne EAN párovanie.

`await project(db, product_ids, at=None)` vracia dostupnosť pre každé požadované product ID. Polia `status`/`reason` rozlišujú príčinu, `availability_state` rozlišuje potvrdenú dostupnosť, nedostupnosť a neznámy stav. Kontext zahŕňa `supplier`, `supplier_name`, `supplier_sku`, `feed_key`, `observation_feed_key`, `last_success_at`, sanitizovaný `last_error`, `refresh_state` a `action`, keď sú známe. Medzi viacerými zdrojmi má prednosť čerstvá potvrdená dostupnosť, potom iné čerstvé pozorovanie, starý údaj a nakoniec diagnostika; v rovnakej skupine sa zachová primárny zdroj a priorita. Platnosť vyhodnocuje pri každom čítaní. Pravidelný prenos zásob musí preto opakovane prejsť mapované položky aj bez nového feedu: samotné uplynutie platnosti môže zmeniť dodaciu dostupnosť. Skrátenie TTL platí okamžite, predĺženie neoživuje pôvodne expirované pozorovania. Množstvo dodávateľa sa nikdy nepripočíta k vlastnému.

Overenie: lokálne testy kontrolujú presnosť množstiev, odmietnutie neplatných hodnôt, expiráciu, minimálny stock XML a ochranu API. Databázové testy majú guard na izolovaný localhost `*_catalog_test` a overujú opakovateľnosť migrácie, atómové prijatie, zachovanie posledného úspechu, zmenu konfigurácie počas behu, front a zakázané väzby. Plný výsledok CI a nasadenie sa zaznamenávajú samostatne.

Migrácia je aditívna, nevkladá aktivačné nastavenia a nemení skladový denník. Pri problémoch vypnite automatiku; údaje ostávajú k dispozícii na kontrolu. Nepoužívajte spätný zásah do fyzických zásob ako opravu dodávateľského feedu.

## Čiastočné prijatie a automatika

Platný dokument sa spracuje po položkách. Súhrn obsahuje `total`, `accepted`, `rejected`, `unknown`, počty dôvodov a upozornenia. Chybný kód či množstvo sa preskočí; pri duplicitnom kóde sa preskočia všetky jeho výskyty, bez sčítania alebo výberu posledného riadka. Neznáme množstvo nie je nula. Preskočené alebo chýbajúce položky si ponechajú pôvodné pozorovanie aj pôvodnú expiráciu, ktorá sa nepredĺži. Po jej uplynutí dostupnosť ostáva neznáma. Platná nula sa prijíma.

Pokles počtu položiek pod `min_coverage_percent` teraz vytvorí upozornenie, nezablokuje prijatie platných údajov. Základ porovnania sa pri takom poklese nezníži. Rozbitý XML, nedokončený download, zakázaná štruktúra, prázdny/prekročený zdroj alebo zmena konfigurácie počas načítania zostávajú dôvodom odmietnutia celého behu. Ak nie je použiteľná ani jedna položka, beh sa označí ako neúspešný, uloží podrobnosti a nezmení posledný úspech. Ďalší plánovaný termín sa posunie aj po zlyhaní, bez okamžitej slučky opakovaní.

Súhrn je v existujúcom `SupplierFeedRun.error_details.availability_report`. Preskočené riadky sú terminálne auditné záznamy `SupplierFeedItemRaw`, nikdy front pre katalógový import. UI vracia iba ohraničené polia kódu/množstva/dôvodu, nie URL zdroja či prihlasovanie. Staršie behy bez súhrnu nevymýšľajú historické počty. Nová migrácia nie je potrebná.

Worker sa spúšťa v životnom cykle API pri `USE_POSTGRES`. Zapnutý zdroj s uplynutým `next_run_at` prejde rovnakým parserom a prijatím ako ručný beh. Nové behy ukladajú `trigger: scheduled|manual`. Databázový test vykonáva dva automatické cykly Northfinder s chybným aj platným riadkom, overí posun termínu, nulové opakovanie pred termínom a aktualizáciu bežnej existujúcej produktovej karty. Nevytvára fyzický sklad ani nevolá Upgates.

Produkčné dôkazy po nasadení možno prečítať bez zápisov a bez sťahovania feedu:

```sh
docker compose exec -T api python -m inventory_hub.supplier_availability_diagnostics northfinder
```

Príkaz používa SQL transakciu iba na čítanie. Zobrazí nastavenie, posledné tri behy a samostatné podmienky prenosu do BIKETREK/xTrek. Úspešné opakované `scheduled` behy dokazujú dodávateľ → Hub. Podmienky e-shopu sú len kontrolou konfigurácie, nie dôkazom odoslania každého produktu: každý produkt potrebuje správnu väzbu, potvrdený vlastný sklad a platný východiskový stav delta prenosu. Nasadenie tejto opravy nemení `STOCK_SYNC_WRITE_ENABLED`, skladovú autoritu ani automatické objednávky. Bez aktuálnych produkčných dôkazov nemožno tvrdiť overený prenos Hub → e-shopy.


## Cyklo Žitný

Natívny parser `cyklo-zitny` načítava dostupnosti z kompletného zákazníckeho exportu verzie 3. Identita je `code`, množstvo `store_count`. Skrátený export má iba interné `id` a `delivery_date`; nemožno ho pripojiť ako zoznam dodávateľských SKU ani z neho vyrábať počty. Pre `stock` nastavte rovnaký kompletný URL ako pre katalóg. Skladový parser nezávisí od cien a názvov. Platné nuly sa prijímajú, chybné riadky majú bežnú diagnostiku a expiráciu; neprítomnosť položky nevytvára nulu. Konfigurácia ani automatika sa nezapínajú samotným nasadením parsera. Postup a cenové základy sú v [dodávateľskom katalógu](supplier-catalog.md#cyklo-zitny-customer-discount-export).
