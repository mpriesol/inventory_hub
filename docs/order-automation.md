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

`order_stock._allowed` oddeľuje povolené operácie od orientačného návrhu podľa názvu. `PaymentSuccessful` povoľuje rezerváciu, nikdy výdaj; `Custom` povoľuje výslovne potvrdenú rezerváciu alebo výdaj podľa ID stavu. Natívny `Received` zostáva rezerváciou s existujúcou osobitnou podmienkou plateného dokončeného pokladňového predaja. `Sent` povoľuje výdaj, `Canceled` uvoľnenie. Rozpoznané vratky/reklamácie aj v dlhších názvoch a neúspešné/zrušené platby zostávajú na kontrolu; rovnaké rozpoznanie používa aj uzavretie skladovej výnimky po výdaji. Neznáme systémové typy nezískajú oprávnenie. Uložená akcia sa overuje aj pri spracovaní. Nové možnosti nemenia existujúce mapovanie ani nezačínajú automatiku.

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

Objednávkový čítač navyše povoľuje presný historický alias iba s úplným auditom
overeného premenovania pre rovnaký shop, pripojenie, mapovanie a natívnu
identitu. Rovnaký postup sa používa v ručnom náhľade aj procesore; nemení
časové hranice automatizácie ani režim skladu. Viac v [prevode SKU](sku-migration.md).

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

### Skladový podklad pre pravidelný prenos

`services/order_sync_readiness.py` odlišuje stav úlohy od účinku už zapísaného do skladu. Všetky bežné kontroly čerstvosti, autority a predajných kanálov zostávajú povinné. Výnimku z požiadavky `completed` má iba `retry/order_processing_backorder` s doloženou aktuálnou rezerváciou. Kontrola viaže inbox, úlohu, konfiguráciu, zdroj a skladovú revíziu objednávky. Znovu overí identitu riadkov a čítací plán ledgeru; alokácie sa nesmú meniť. Nový príjem alebo uvoľnenie kusov preto zablokuje prenos, kým čakajúcu rezerváciu doplní spracovateľ. Čítanie nevytvára zostatky, rezervácie ani pohyby.

Pravidelný publisher pri príprave odoslania drží zámky v poradí identita → e-shop → sklad cez kontrolu podkladu a finálnu projekciu. Túto kontrolu opakuje aj po vzdialenom čítaní pred uložením `sending`; pred sieťovým volaním zámky uvoľní commitom. UI diagnostika bez zápisového zámku je informatívna; nie je oprávnením na odoslanie.

### Potvrdenie skladovej výnimky po výdaji

`services/order_exceptions.py` poskytuje osobitný postup pre `review` po vydaní nezmeneného obsahu, napríklad následné storno alebo vratku. Náhľad načíta aktuálnu objednávku, identitu a číselník stavov. Potvrdenie načíta podklad znovu, overí hash, platnosť náhľadu, inbox/generáciu, politiku, konfiguráciu, cieľ, skladovú revíziu a zobrazené doklady výdaja/vratiek. Vyžaduje výslovné potvrdenie skladovej evidencie a neprázdny dôvod; vykonávateľ pochádza z autentizovanej požiadavky.

| API | Kontrakt |
| --- | --- |
| `POST /order-processing/jobs/{id}/resolution-preview` | `{request_id}`; čerstvé čítanie Upgates a uloženie náhľadu pod týmto UUID. |
| `GET /order-processing/resolutions/{id}` | Čítacie obnovenie náhľadu alebo potvrdeného výsledku. |
| `POST /order-processing/resolutions/{id}/resolve` | `{preview_hash, confirmed:true, stock_accounted:true, reason}`; čerstvé overenie a audit bez skladových pohybov. |

Audit používa existujúci `OrderStockPreview` s `trigger:exception_resolution` a `action:resolve_exception`. Je oddelený od bežných náhľadov výdaja a nemožno ho vykonať bežnou cestou `order-stock/.../apply`. Pôvodný `stock_snapshot`, `stock_issue_result` a nemenné pohyby zostávajú zachované. Toto potvrdenie neúčtuje vratku, nevracia peniaze a nemení objednávku v e-shope. Fyzický návrat sa naďalej zapisuje cez existujúce FIFO vratky; historický výdaj bez FIFO alokácií neposkytuje vymyslený údaj o vrátenom množstve.

Opakované potvrdenie rovnakého náhľadu a payloadu vráti uložený výsledok. Odlišný payload sa odmietne. Spracovateľ pri ďalšej úplnej kontrole uzná iba stále platný audit toho istého podkladu; nová hlavička, zdroj, identita, politika alebo konfigurácia vyžadujú novú kontrolu. Pravidelný prenos overuje rovnakú väzbu, nestačí samotný stav úlohy `completed`. Zmazané alebo konfliktné objednávky ani zmenený vydaný obsah nemožno týmto potvrdením obísť.

Táto oprava nepridáva migráciu, historické automatické potvrdenia ani zapnutie synchronizácie. Stará verzia môže bezpečne obnoviť prísnejšie blokovanie; nemaž audit ani pohyby a uprednostni opravu vpred.

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

## Webhooky objednávok a menej častý zber zmien (28. 10. 2026)

Aditívna migrácia `035_order_webhooks.sql` pridáva výslovne zapínateľné nastavenia
`order_webhook_settings` a trvalú frontu `order_webhook_events`. Migrácia nezapína
zber, rezervácie, výdaj ani skladovú autoritu. Administrátorské ovládanie je na
stránke objednávkového inboxu; nastavenia a výsledky sa čítajú z Hubu. Konfigurácia
alebo obnova registrácie zaradí osobitnú úlohu s volaniami Upgates.

| API | Význam |
| --- | --- |
| `GET /order-webhooks/status?shop_code=...` | Uložené nastavenie, stav registrácie, počty čakajúcich/chybných oznámení a posledných 50 výsledkov. Bez čítania Upgates. |
| `POST /order-webhooks/configure` | `{shop_code,expected_revision,confirmed:true,enabled,public_base_url,catchup_interval_seconds}`. HTTPS základ verejného Hub API napr. `https://hub.example/api`; interval 300–86400 sekúnd, predvolene 3600. Zapnutie vyžaduje už povolený kolektor rovnakého cieľa. |
| `POST /order-webhooks/register` | `{shop_code,expected_revision,confirmed:true}` zaradí overenie a doplnenie/opravu troch vlastných registrácií. |
| `POST /order-webhooks/retry` | Rovnaké potvrdenie zaradí chybné alebo odložené oznámenia na nové čítanie. Neodstraňuje skladové ani identitné výnimky. |

Všetky tieto ovládacie cesty vyžadujú administrátora a `no-store`. Jediné výnimky
z operator-session hranice sú presné `POST /webhooks/upgates/orders/create`,
`update` a `delete`. Každá overuje samostatné Basic prihlasovanie: názov e-shopu
a serverom generované 384-bitové tajomstvo. DB obsahuje iba SHA-256 odtlačok;
verzovaný súbor s pôvodným tajomstvom ostáva na existujúcom súkromnom trvalom
`SUPPLIER_CREDENTIALS_ROOT` s právami 0600. Bežné API, UI ani logy ho nevracajú.
Upgates registrácia používa dokumentované prihlasovanie v HTTPS URL. Pole
`project_name` nie je autentizácia a nepáruje skladové identity.

Príjem má limit 2 MB a 5000 položiek. Uloží iba číslo objednávky, typ a čas
udalosti, odtlačok a stav spracovania; neprijíma skladové množstvá ani osobné údaje
ako autoritatívne fakty. HTTP 204 nasleduje až po commite. Spracovanie sa zmestí
do vlastného dvojsekundového limitu vrátane DB; pri zlyhaní odpovie 503, aby
Upgates mohol opakovať. Upgates považuje aj prekročenie svojho trojsekundového
limitu za doručenie, preto zostáva nevyhnutný nezávislý úplný zber zmien.

Fronta zlučuje oznámenia podľa `(shop_id, order_number)`. Rovnaké čakajúce
udalosti sa zlúčia a staršie sa ignorujú. Upgates neposiela jednoznačné ID
doručenia; rovnaký čas a stav môžu znamenať druhú zmenu položiek v tej istej
sekunde. Preto rovnaké oznámenie počas alebo po čítaní vyvolá nové overenie a
prebudí aj existujúcu spracovateľskú úlohu. Novšia generácia zostane čakajúca aj
po neskorom výsledku staršej požiadavky. Opakované overenie nevytvára druhý
skladový účinok vďaka idempotentnému spracovateľovi. Samostatný worker s PostgreSQL poradným
zámkom číta najviac 100 dotknutých čísel objednávok naraz, overí členstvo odpovede,
UUID a aktuálne metadáta a použije spoločné `order_collection.save_entries`.
Chýbajúce aktívne objednávky overí aj v zmazaných. Zmazanie zostáva na kontrolu;
nikdy nevytvorí automatickú vratku. Po reštarte sa nedokončené čítanie obnoví.
Dočasné chyby majú rastúci odklad, po ôsmich pokusoch zostanú viditeľné na kontrolu.
Spracovateľ inboxu ďalej používa úplný čerstvý zdroj a existujúce aktivačné hranice.

Registrátor pred každým zápisom načíta aktuálne webhooky cez existujúce API
prístupy. Stabilné názvy patriace danej inštalácii umožnia po prerušení nájsť
už vytvorený hook; nejasné duplicity zastavia registráciu. Pred každým vytvorením
sa uloží trvalý zámer. Po neistom výsledku ďalšie čítanie môže potvrdiť existujúci
hook, ale jeho neprítomnosť neoprávňuje opakovať POST: výsledok zostane viditeľne
neistý. Zrušenie workeru počká na dokončenie sieťového vlákna pred uvoľnením
spoločného zámku. Cudzie webhooky sa
nemenia. Zodpovedajúce registrácie sa znova nezapisujú. Pri vypnutí sa vlastné
hooky deaktivujú, nevymazávajú. Overenie registrácie prebieha raz za hodinu;
upstream údaj o úspešnom doručení nenahrádza lokálny príjem.

Úspešne registrovaný webhookový režim používa nastavený interval úplného delta
zberu, predvolene raz za hodinu. Pri neoverenej registrácii zostáva pôvodné
pravidelné čítanie. Po kontrolnej rekonciliácii nasleduje delta okamžite, aby sa
hodinový interval nepredĺžil striedaním dvoch režimov. Webhook sám nikdy neposunie
`cursor_at` ani `last_completed_at` kolektora. Podmienky úplnosti pred publikovaním
zásob sú opísané v [pravidelnom prenose](stock-sync.md); známe čakajúce alebo
nevyriešené oznámenia ostávajú blokujúcim podkladom.

Tento kontrakt opisuje implementáciu a syntetické testy. Registrácia konkrétnej
produkcie, jej aktivačné hranice a overený živý objednávkový cyklus sa dokladujú
osobitne. Referencie: [udalosti objednávok](https://docs.upgates.com/webhooks/orders),
[príjem a timeouty](https://docs.upgates.com/webhooks/intro),
[správa webhookov](https://docs.upgates.com/api-reference/webhooky).

Pri úprave existujúcej registrácie sa pred PUT uloží aj jedinečné ID pokusu a
odtlačok celého zamýšľaného zápisu. Vymaže ich iba presná overená odpoveď tohto
pokusu. Pri prerušení, chybnej odpovedi alebo strate potvrdenia zostane PUT
blokovaný aj po zmene nastavenia; ani zhodný GET nedokazuje, že staršia požiadavka
už nemôže neskoro prepísať novší stav.

Administrátor môže výnimočne použiť
`POST /order-webhooks/resolve-registration` s
`{shop_code,expected_revision,confirmed:true,event,intent_id,remote_request_finished_confirmed:true,note}`.
Pred potvrdením musí mimo samotného GET overiť u Upgates, že pôvodná požiadavka
už bola úplne dokončená. Panel vyžaduje osobitné potvrdenie a opis dôkazu
(10–2000 znakov). API overí aktuálnu revíziu a presné ID zablokovaného pokusu;
počas aktívneho workeru uzavretie nepovolí. Pridá nový záznam do
`order_webhook_resolutions` s administrátorom, časom a pôvodným odtlačkom,
uvoľní iba príslušný blok a zaradí nové bežné overenie registrácie. Pri neistom
vytvorení cez POST môže bežné overenie nájsť jednoznačný vlastný webhook.
Ak sa nevytvoril, rovnaké výslovné uzavretie preukázateľne dokončenej pôvodnej
požiadavky umožní po novom GET bezpečne vytvoriť chýbajúci webhook. Nejde o
automatické opakovanie neistého vytvorenia.
