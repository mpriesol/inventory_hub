# Objednávková automatika — vývojársky kontrakt

Tento dokument dopĺňa [doménové pravidlá](central-stock.md) a [používateľskú príručku](order-workflows.md). Opisuje implementáciu v tejto vetve; výsledky konkrétneho CI behu a nasadenia treba uvádzať samostatne. Syntetické testy ani úspešný deploy nie sú potvrdením aktivácie obchodov alebo testu na živých objednávkach.

## Zodpovednosti

- Zberač ukladá obmedzené hlavičky do `order_inbox` a vlastné kontrolné body. Nesmie volať skladový ledger.
- Spracovateľ objednávok rozhoduje podľa úplného čerstvého zdroja, potvrdenej politiky a účinných prevádzkových nastavení. Vykonáva lokálnu rezerváciu, uvoľnenie alebo jeden výdaj.
- `order_stock_ledger.py` zostáva spoločným miestom výpočtu alokácií a zápisu pohybov pre ručné aj automatické spracovanie. Nevolá Upgates a necommitne transakciu.
- Existujúci ručný postup používa nemenný náhľad a osobitné potvrdenie fyzického výdaja. Automatika potrebuje vlastnú zaznamenanú autorizáciu politikou; nesmie predstierať, že používateľ ručne potvrdil každý riadok.
- `stock_projection.py` ostáva čítacím návrhom. Kontrolované odoslanie počas údržby je samostatný workflow opísaný v [stock-publication.md](stock-publication.md); tento procesor zásoby do e-shopov neposiela.

Všetky nové používateľské API používajú `operator_access` a `Cache-Control: no-store`. V prehliadači zostáva token iba v pamäti. Worker nepoužíva požiadavku z prehliadača ako trvalú autorizáciu; používa uložený explicitný režim, cieľ a revízie.

## Prevádzkové nastavenia

Definície sú v `stock_settings_types.py`, modely v `stock_settings_models.py`, vyhodnotenie v `services/stock_settings.py`.

`stock_warehouse_settings` drží predvolené hodnoty, revíziu a `processing_paused`. `stock_shop_settings` drží čiastkové `overrides`, revíziu, výslovný režim, aktivačné časy, autorizovanú revíziu stavovej politiky a fingerprint API cieľa. Samostatné `processing_retry_after_at` uchováva prevádzkovú prestávku po limite Upgates. Žiadny záznam znamená zabudované predvolené hodnoty a režim `manual`.

Efektívna hodnota vzniká v poradí **zabudovaná hodnota → sklad → výnimka e-shopu**. Chýbajúci kľúč v `overrides` znamená dedenie; nejde o nulovú hodnotu. Režim a oprávnenie na výdaj sa nededia. API vracia aj pôvod každej hodnoty (`warehouse`/`shop`) a `configuration_hash` zahŕňajúci účinné hodnoty, revízie a autorizačné údaje.

`effective` navyše vracia `processing_ready`, bezpečný `processing_error` a spoločný `retry_after_at`. Sú to lokálne diagnostické údaje bez skúšobného volania Upgates. Režim môže zostať `fulfill`, aj keď zmenená politika/cieľ, pauza alebo prestávka ďalší pokus blokujú. Prevádzková prestávka a diagnostické polia nemenia konfiguračný hash ani aktivačné časy.

| Kľúč | Predvolené | Rozsah |
| --- | --- | --- |
| `poll_interval_seconds` | 300 | 60–86 400 |
| `reconcile_interval_hours` | 24 | 1–168 |
| `overlap_minutes` | 10 | 5–1 440 |
| `reconcile_window_days` | 7 | 1–30 |
| `max_pages_per_pass` | 100 | 1–100 |
| `run_timeout_seconds` | 180 | 30–180 |
| `retry_base_seconds` | 300 | 60–3 600 |
| `retry_max_seconds` | 3 600 | 300–86 400 |
| `processing_batch_size` | 20 | 1–100 |
| `processing_retry_minutes` | 5 | 1–1 440 |
| `full_order_check_hours` | 24 | 1–168 |

Vstupy sú striktne celočíselné, bez neznámych kľúčov. `retry_max_seconds >= retry_base_seconds` platí aj po zlúčení výnimiek. Zmena skladu overí zdedené kombinácie všetkých dotknutých e-shopov. Limity sú kontrolované serverom, nielen formulárom.

| API | Účel |
| --- | --- |
| `GET /stock-settings/options?shop_code=...` | Politika, sklady, predvoľby, výnimky a účinné nastavenia e-shopu. |
| `GET /stock-settings/warehouse?warehouse_code=...` | Nastavenie konkrétneho aktívneho skladu. |
| `POST /stock-settings/warehouse` | Celé `values`, `processing_paused`, `expected_revision`, skutočné `confirmed=true`. |
| `POST /stock-settings/shop` | Čiastkové `overrides`, režim, očakávaná revízia e-shopu aj skladu a potvrdenie. |

Neexistujúci nastavovací riadok má verejnú revíziu `0`. Konflikt verzií vyžaduje nové čítanie; posledný zápis nesmie potichu prebiť iného operátora. `fulfill` vyžaduje `fulfillment_confirmed=true` pri každom uložení e-shopového nastavenia v tomto režime.

## Zber, prehľad úloh a explicitné načítanie

| API | Kontrakt |
| --- | --- |
| `GET /order-collection/status?shop_code=...` | Stav zberu, čakajúca jednorazová požiadavka a účinné prevádzkové nastavenie. |
| `POST /order-collection/refresh` | `{shop_code, expected_revision, confirmed:true}`; odpoveď `202` zaradí jeden čítací pokus aj pri `enabled=false`. Nezapína automatický režim. |
| `GET /order-processing/jobs?shop_code=...` | Bezpečné uložené výsledky; `limit` 1–100 (predvolene 50), `offset` 0–1 000 000. Odpoveď `{jobs,total,limit,offset}`. |
| `POST /order-processing/refresh` | `{shop_code, confirmed:true}`; odpoveď `202` s `{scheduled,mode,external_write_enabled:false}`. Prebudí existujúce nerunning úlohy a objaví oprávnené záznamy v aktuálnej dávke. Vyžaduje už povolenú, nepozastavenú automatiku. |

Posledná cesta nie je ručné potvrdenie výdaja, nevykonáva synchrónny výdaj a neobchádza aktivačné hranice. Nie je totožná s tlačidlom „Načítať zmeny teraz“, ktoré používa kolektorovú cestu. Uvedené cesty sú backendové; frontend ich volá cez aplikačný prefix `/api`.

Kolektor spotrebuje `manual_requested_at` až pri trvalom založení behu. Neúspešný jednorazový beh je skončený pokus; pri vypnutom pravidelnom zbere vyžaduje po prestávke nový explicitný request. Nová samostatná požiadavka zadaná počas behu zostáva zachovaná. Neplatná konfigurácia pred spustením čakajúcu požiadavku nespotrebuje. Uložená revízia a hash sa overujú pred stránkou, po nej aj pri dokončení; ich zmena zneplatní rozpracovaný beh bez posunu kontrolného bodu.

Kontrolný bod sa posunie až po úplnom aktívnom aj zmazanom priechode. Už zapísané čiastkové metadáta môžu zostať v inboxe. Upgates stránkovanie nie je nemenný snapshot a nemá hornú hranicu času zmeny; presah delta čítania a striedané kontrolné intervaly vzniku znižujú riziko vynechania, negarantujú okamžité zachytenie každej súbežnej úpravy.

## Autorita a časové hranice

| Režim | Automatické lokálne operácie |
| --- | --- |
| `manual` | Žiadne. Existujúci ručný postup zostáva dostupný. |
| `reserve` | Povolená rezervácia/úprava a storno pred výdajom. Výdaj vyžaduje ručný postup. |
| `fulfill` | To isté a oprávnený fyzický výdaj podľa potvrdených stavov. |

Pri prechode `manual → reserve/fulfill` sa `automation_starts_at` nastaví na aktuálny serverový čas. Pri vstupe do `fulfill` z iného režimu sa podobne nastaví `issue_starts_at`. Odchod z `fulfill` jeho čas vymaže; opätovné zapnutie znamená novú hranicu. Zmena hodnôt v rovnakom režime hranice zachová. Pôvodný `OrderStockPolicy.starts_at` sa nikdy neprepisuje.

Hranice sa porovnávajú s **časom vzniku objednávky**. `updated_at` nedokazuje nový fyzický predaj: stará odoslaná objednávka môže neskôr dostať iba administratívnu úpravu. Staršie objednávky zostávajú na ručné spracovanie; tento balík nesmie vykonať skrytý spätný výdaj.

Aktivácia automatiky viaže `authorized_policy_revision` a fingerprint na existujúcu kolektorovú konfiguráciu rovnakého cieľa. Fingerprint identifikuje HTTPS adresu a prihlasovacie meno, neobsahuje API kľúč. Výmena kľúča sama nemení identitu; iný obchod alebo používateľ nie je automaticky ten istý zdroj.

Kolektorová konfigurácia vznikne potvrdeným zapnutím zberu alebo prvou jednorazovou požiadavkou; samotné čítanie prehľadu ju nevytvorí. Automatika nepotrebuje `collector.enabled=true` na opakovanú kontrolu už známych úloh. Bez pravidelného alebo ďalšieho jednorazového zberu však nepribúdajú nové hlavičky.

Zmena stavovej politiky vyžaduje novú autorizáciu nastavenia. Webová platba sama nie je výdaj. Pravidlá pre `Sent`, Odoslaná/Vyzdvihnutá a dokončený platený `cash-register` predaj sa používajú zo spoločného skladového rozhodovania. Vratky, reklamácie, chybné identity a nepodporované stavy zostávajú na kontrolu.

## Súbežné zmeny nastavení

Skladové nastavenie sa zapisuje pod zámkom `Warehouse FOR UPDATE`; následné čítanie výnimiek nepridáva zámky `Shop`. E-shopové nastavenie používa poradie `Shop → OrderStockPolicy → Warehouse → nastavenia`. Zámok nadradeného skladu chráni aj prvé vloženie jeho zatiaľ neexistujúceho nastavovacieho riadka.

`effective(..., lock=True)` predpokladá už zamknutý Shop/stavovú politiku a zamyká sklad i nastavovacie riadky. Spracovateľ po sieti znovu overí revízie/hash a pauzu v transakcii skladového zápisu. Pauza alebo nová autorizácia nesmie ponechať starému rozpracovanému pokusu právo na výdaj.

`processing_paused` pozastavuje iba automatické lokálne spracovanie skladových objednávok. Nie je všeobecným zámkom fyzického skladu: nemení čítací zber ani oprávnenie existujúcich ručných príjmov, počiatočného stavu a ručne potvrdených objednávok.

Sieťové čítanie sa nevykonáva pod zámkami konfigurácie, objednávky alebo bilancií. Nové mutujúce cesty musia zachovať poradie zámkov, vrátane implicitných FK zámkov pri vkladaní detských riadkov.

## Úplný zdroj, opakovanie a nedostatok zásoby

Hlavička v inboxe nie je skladový podklad. Spracovateľ musí načítať úplnú aktuálnu objednávku a číselník stavov; overiť číslo aj očakávané UUID a fingerprint skutočného použitého klienta. Zmazanie ani prázdna odpoveď sa nemenia na storno.

Riadky sa znovu párujú spoločným resolverom. Jednoznačné kanálové mapovanie alebo presné spoločné SKU môže pokryť aj pokladňový variant pod iným rodičom. Konfliktné/neznáme kódované riadky blokujú celú operáciu. Manuálne riadky bez skladovej identity a zľavy sa vylúčia viditeľne; nesmú sa vytvoriť náhradné produkty.

Nedostatok zásoby nie je dôvod na záporný sklad ani čiastočný výdaj. Objednávka môže dostať čiastočnú rezerváciu s nekrytou časťou; výdaj musí pokryť všetky jej skladované riadky. Rezervácie iných objednávok zostávajú zachované.

Nový príjem môže zmeniť výsledok bez zmeny zdrojového hashu objednávky. Automatika preto musí kontrolovať čakajúce a nekryté objednávky aj po nezmenenom načítaní hlavičky. Rovnaký čas hlavičky tiež nedokazuje nezmenené riadky; slúži na objavenie kandidátov, nie na trvalé potlačenie úplného čítania.

Nevykonávať zamykanie spracovateľských úloh z príjmovej transakcie po zamknutí bilancií. Opačné poradie `úloha → bilancia` v spracovateľovi by vytvorilo deadlock. Priebežné opakovanie čakajúcich úloh je jednoduchší základ; prípadné budúce udalosti o príjme potrebujú vlastný transakčný kontrakt.

Nezmenená plne rezervovaná objednávka nepotrebuje zvyšovať `stock_revision` pri každom načítaní. Nezmenený dokončený výdaj vracia uložený výsledok. Zmenený vydaný obsah/identita alebo následné storno ostávajú na kontrolu bez ďalšieho `SALE_OUT` alebo automatickej vratky.

## Trvalá evidencia a migrácia

`009_stock_automation.sql` pridáva dva nastavovacie modely a `order_processing_jobs`. Existujúce kolektorové nastavenie rozširuje o `manual_requested_at`; beh má navyše príznak `manual`, `configuration_hash` a `configuration_snapshot`. Jednorazová požiadavka sa spotrebuje ako samostatný čítací beh a nemení pravidelné `enabled`.

Spracovateľská úloha má unikátne `(shop_id, source_uuid)`, väzbu na inbox, číslo objednávky, hash pozorovania a generáciu. Stavy sú `pending`, `running`, `completed`, `retry`, `review`; ďalší termín a časy pokusov sú trvalé. Ukladajú sa bezpečný chybový kód, posledný zdrojový/konfiguračný hash, výsledok a odkaz na auditný náhľad. Stav `completed` pri jednej kontrole neznamená, že sa zdroj už nikdy nekontroluje.

Spracovateľ používa samostatný PostgreSQL advisory lock `691432113` na vyhradenom spojení počas cyklu; kolektor používa `691432112`. Iba držiteľ spracovateľského zámku obnovuje po páde úlohy `running` na `retry` s kódom `order_processing_interrupted`. API repliky tak nemajú súbežné bežné spracovateľské cykly.

`enqueue` beží oddelene po commite kolektora a serializuje hromadnú prácu na úlohách cez `Shop FOR UPDATE`. `start_job` nárokuje úlohu krátkou transakciou, zvyšuje počet pokusov a uloží hash konfigurácie. Sieťové čítanie potom nemá skladové zámky. `finish_job` používa poradie globálny zámok identity → Shop → politika → Warehouse/nastavenia → úloha → inbox → objednávka/produkty/bilancie. Overí generáciu aj číslo pokusu, aktuálnu konfiguráciu, identitu a čerstvosť zdroja. Novšiu generáciu stará odpoveď nedokončí.

Ledger, výsledok úlohy a dokončený audit sa commitnú spolu. Každá skutočne aplikovaná skladová revízia má `OrderStockPreview` so stavom `completed`, `trigger:"automatic"`, ID úlohy/generácie, úplným DTO skladu, bezpečným zdrojom, plánom a hashmi. `authorization` obsahuje režim, revízie nastavení/politiky, fingerprint a oba aktivačné časy. Nezmenená rezervácia ani opakovaný už vydaný obsah nový audit pohybu nevytvoria. Už dokončený výdaj vracia pôvodný výsledok aj po strate odpovede alebo páde procesu.

Nekryté rezervácie, nedostatok pred výdajom a dočasné chyby idú na `retry` podľa `processing_retry_minutes`. `completed` aj `review` dostanú ďalšiu úplnú kontrolu podľa `full_order_check_hours`; zmenené pozorovanie ich môže prebudiť skôr. Nepodporovaný stav, zmazanie, nejednoznačná identita alebo zmenený vydaný obsah zostávajú na kontrolu bez automatického storna či vratky. `run_timeout_seconds` ohraničuje aj sieť a dokončenie jedného spracovateľského pokusu.

HTTP 429 ukladá prestávku pre celý e-shop, najmenej bežný odstup spracovania a prípadne dlhší sanitizovaný `Retry-After` (najviac sedem dní). Zberač aj spracovateľ rešpektujú spoločný termín pred ďalším čítaním a pri prijímaní odpovede. Pozastavenie, zmena generácie ani explicitné prebudenie termín neskrátia. Zberač pri ostatných chybách používa exponenciálny odstup zo zachovaného snapshotu nastavenia; novšiu konfiguráciu starý neúspešný beh neprepíše.

Migrácia neaktivuje automatické režimy, nezakladá skladové pohyby a nemaže staré dáta. Musí byť zabalená do API obrazu a vykonaná explicitným migračným runnerom po `008` pred spustením nového API/workerov. Samotný nový SQL súbor v inicializačnom adresári nestačí na upgrade existujúcej databázy.

## Overenie a návrat verzie

Pred merge treba overiť aspoň tieto scenáre v izolovanom PostgreSQL a syntetickom zdroji:

- dedenie/výnimky, striktné typy, hranice, konflikt revízií a neplatná zdedená kombinácia;
- nulová autorita po migrácii a ručný režim bez automatického skladového zápisu;
- zapnutie a zvýšenie režimu bez spracovania starých fyzických výdajov;
- zmena/pauza nastavení alebo cieľa medzi sieťovým čítaním a zápisom;
- dve spracovania a ručný/automatický výdaj tej istej objednávky súčasne;
- pád pred a po atomickom commite, nestratená novšia požiadavka;
- nový príjem a opätovné pokrytie nekrytej objednávky pri nezmenenom zdroji;
- zmena riadkov pri nezmenenej hlavičke a ochrana uzamknutého výdaja;
- žiadny Upgates zápis, outbox zásob, záporné množstvo ani vymyslená cena.

Pri návrate verzie najprv vypnúť príslušné automatické režimy/použiť pauzu a overiť výsledky rozpracovaných úloh. Aditívne tabuľky a už zaúčtované pohyby sa ponechajú. Revert kódu nesmie spätne mazať rezervácie alebo obracať ledger. Obnova starého kódu sama nenahrádza kompenzačný skladový postup.

Publikovanie pridáva do `OperationalValues` dedený limit `publication_batch_size` (20, rozsah 1–100) a platnosť porovnania `publication_preview_minutes` (15 minút, rozsah 5–60). Trvalá blokácia skladu pri publikovaní pozastaví aj automatické lokálne spracovanie; prebiehajúca úloha, ktorá na ňu narazí, sa odloží do opakovania. Nejde o bežné nastavenie `processing_paused`.
