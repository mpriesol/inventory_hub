# Inventory Hub — prehľad projektu

Inventory Hub spája produkty, dodávateľské doklady a fyzický sklad pre **BIKETREK** a **xTrek**. Technické kódy e-shopov zostávajú `biketrek` a `xtrek`.

Tento prehľad opisuje aktuálnu implementáciu v repozitári. Výsledok konkrétneho nasadenia patrí k jeho Actions behu; výsledok migrácie alebo externého zápisu k uloženému auditu danej operácie. Historické implementačné poznámky sú dostupné v Git histórii.

## Kde začať

- [AGENTS.md](AGENTS.md): pravidlá práce, platné oprávnenia, doménové hranice a overovanie.
- [Inštalácia, aktualizácia a obnova](api/POSTGRES_SETUP.md): prostredie, poradie migrácií a obnova oboch úložísk.
- [CI a nasadzovanie](docs/ci-deployment.md): úplnosť testov, zostavenie obrazov, deployment a diagnostika.
- [Priority a zostávajúca práca](docs/mvp-progress.md): otvorený produktový rozsah a prevádzkové overenie.

## Aktuálny stav funkcií a obrazoviek

„Implementované“ znamená existujúcu cestu v kóde. Dostupnosť konkrétnej integrácie závisí aj od jej konfigurácie a dát.

| Oblasť / cesta UI | Implementované | Kontrakt a hranice |
| --- | --- | --- |
| Dashboard `/` | Súhrny skladu a posledné pohyby | [História skladu](docs/stock-history.md); neznámy alebo chybný údaj nie je nula. |
| Sklad `/stock`, detail `/products/:sku` | Jednotný editor, parametre, obrázky, ručné hodnoty a vybrané publikovanie | [Editor](docs/product-editor.md); `/products` presmeruje na `/stock`. Uloženie v Hube je samostatné od odoslania do e-shopu. |
| Väzby a zlučovanie kariet | Náhľad, potvrdenie, konflikty a obnova podľa UUID | [E-shopy](docs/product-links.md), [dodávatelia](docs/supplier-card-links.md), [konsolidácia](docs/product-consolidation.md). Ďalšie podporované zlúčenie nevyžaduje nový PR. |
| Príjem `/receiving` | Faktúra, skenovanie, ručné množstvá, pozastavenie a idempotentná finalizácia | [Identita príjmu](docs/receiving-identity.md), [potvrdený začiatok skladu](docs/stock-tracking.md). Príprava karty sama nevytvára zásobu. |
| História a množstvá `/stock/movements`, `/stock/opening` | Nemenné pohyby, potvrdený počiatočný stav a opravy fyzického počtu | [História](docs/stock-history.md), [opravné pohyby](docs/stock-adjustments.md), [jednotky](docs/stock-units.md). Širšia inventúrna agenda zostáva plánovaná. |
| Nákupné ceny v detaile produktu | FIFO vrstvy, výdaje, fyzické vratky, karanténa a zdokumentované opravy ceny | [FIFO](docs/fifo.md); staré zásoby potrebujú výslovný prechod, chýbajúca cena zostáva neznáma. |
| Faktúry `/invoices` a EAN prehľad `/product-trace` | Externé doklady, úhrady, účtovnícky ZIP a dohľadanie vzťahov produktu | [Archív](docs/invoice-archive.md), [finančné údaje](docs/invoice-finance.md), [e-shopové faktúry](docs/shop-invoices.md). Import faktúry nevystavuje nový doklad ani nemení sklad. |
| Dodávatelia `/suppliers` a katalóg | Konfigurácia, feedy, vyhľadávanie, identita a importné náhľady | [Katalóg](docs/supplier-catalog.md), [dodávateľská identita](docs/supplier-identity.md), [Zookee](docs/zookee.md). Adaptér a podporované polia treba overiť pre konkrétny zdroj. |
| Mapovanie feedu a `/product-import` | Verzie mapovania, zdrojová analýza, uložená editovateľná príprava, ochrana ručných hodnôt a ciele oboch e-shopov | [Mapovanie](docs/product-feed-mapping.md), [importy](docs/product-imports.md), [dodávateľské metadáta](docs/upgates-supplier-metadata.md). |
| E-shopy `/shops` | Zoznam, konfigurácia, Upgates import/export a mapovanie | Aktívna stránka je implementovaná; [existujúce väzby](docs/product-links.md) zachovávajú overené natívne identity. |
| Objednávky `/orders`, `/orders/stock`, `/orders/inbox` | Čítací audit, samostatný zber, náhľad a potvrdené lokálne rezervácie/výdaje | [Postup obsluhy](docs/order-workflows.md), [automatika](docs/order-automation.md), [príprava spustenia a webhookov](docs/order-launch-setup.md), [pravidlá centrálneho skladu](docs/central-stock.md). Automatické spracovanie vyžaduje vlastnú aktiváciu a časovú hranicu. |
| Dostupnosti `/settings/availability` | Čerstvosť dodávateľských údajov, ručné spustenie, intervaly a pravidelný skladový prenos | [Ovládanie](docs/availability-sync.md), [dodávateľské údaje a stránkovaná diagnostika publikovania](docs/supplier-availability.md), [rozdielový prenos](docs/stock-sync.md). Dodávateľská ponuka nie je fyzická zásoba Hubu. |
| Údržbová publikácia `/stock/publication` | Výslovná blokácia skladu, porovnanie, jedno odoslanie a riešenie neistého výsledku | [Údržbový postup](docs/stock-publication.md); odlišný od pravidelného rozdielového prenosu. |
| Ceny `/settings/purchase-costs` | Voliteľný prenos ďalšej FIFO ceny produktu a nákladov vydaných objednávok | [Nákupné ceny](docs/fifo-purchase-costs.md); účtovné tržby a úplná hrubá marža ešte nie sú hotové. |
| AI `/ai-content`, `/settings/ai-content` | Pravidlá, profily, príprava, kontrola a vybrané obsahové aktualizácie | [AI obsah](docs/ai-content.md); uložený koncept, schválenie a skutočné odoslanie sú odlišné stavy. |
| Účty `/login`, `/settings/users` | Meno/heslo, trvalá relácia, správca účtov a kompatibilný operátorský token | [Účty](docs/user-accounts.md), [tajné konfigurácie](docs/configuration-secrets.md). Všeobecné prevádzkové RBAC zostáva plánované. |
| Atomer `/migration/atomer`, `/migration/atomer-progress` | Príprava obsahu/katalógu, pôvodné a aktuálne identity, review, pozastavenie a riadené odovzdanie | [Obsah](docs/atomer-content-import.md), [prehľad migrácie](docs/atomer-migration-progress.md), [riadenie kampane](docs/atomer-catalog-control.md), [riadenie úlohy](docs/atomer-catalog-job-control.md), [handoff](docs/atomer-catalog-handoff.md). Dokončená konsolidácia kariet sama nedokazuje dokončené publikovanie obsahu. |

Zdroj navigácie: [App.tsx](frontend/src/App.tsx) a [exporty stránok](frontend/src/pages/index.ts). Názov súboru `PlaceholderPages.tsx` sám neurčuje stav exportovanej stránky.

## Architektúra a aktívne moduly

React frontend komunikuje s FastAPI cez rovnaký HTTPS pôvod a prefix `/api`. Caddy obsluhuje frontend a reverse proxy. Backend používa Python 3.12, async SQLAlchemy a PostgreSQL 16. Workery bežia v API procese a svoje trvalé stavy ukladajú do PostgreSQL.

| Miesto | Úloha |
| --- | --- |
| [api/inventory_hub/main.py](api/inventory_hub/main.py) | Registrácia routerov, autentizačná hranica a životný cyklus workerov. |
| [api/inventory_hub/routers/](api/inventory_hub/routers/), [services/](api/inventory_hub/services/) | HTTP rozhranie a zdieľané obchodné pravidlá. |
| [api/inventory_hub/adapters/](api/inventory_hub/adapters/) | Dodávateľské parsery a B2B konektory. |
| [api/inventory_hub/db_models.py](api/inventory_hub/db_models.py) a ďalšie modely | Relačné entity; samotný model nie je hotový používateľský postup. |
| [frontend/src/App.tsx](frontend/src/App.tsx), [pages/](frontend/src/pages/) | Navigácia a aktívne obrazovky. |
| [frontend/src/api/](frontend/src/api/) | Typované klientské rozhrania; prihlasovanie a spoločný transport chránia súkromné požiadavky. |
| [infra/db-init/](infra/db-init/), [migrate.py](api/inventory_hub/migrate.py) | Nemenné očíslované SQL a explicitné poradie aktualizácií. |
| [.github/workflows/ci.yml](.github/workflows/ci.yml), [build.yml](.github/workflows/build.yml) | Kontroly PR a nasadenie `main`. |

`USE_POSTGRES=true` je aktuálny skladový režim. Legacy režim nie je rovnocenný náhradný sklad pri výpadku databázy. Referenčný Compose obsahuje aj Redis; to nie je dôkaz používania Redis fronty v aktívnej aplikácii.

### Dve úložiská

PostgreSQL obsahuje karty, identifikátory, dodávateľské a e-shopové väzby, doklady, pohyby, FIFO, objednávkové stavy, prípravy a audity operácií. Filesystem obsahuje konfigurácie dodávateľov/e-shopov, zdrojové faktúry, feedy, exporty a niektoré importné denníky. Prihlasovacie údaje sú v samostatnom chránenom mount-e mimo verejných dát.

Dodávateľské zmeny môžu potrebovať databázu aj súbory. Záloha a obnova musia zachovať všetky tieto časti z konzistentného bodu; postup je v [sprievodcovi obnovou](api/POSTGRES_SETUP.md#záloha-a-obnova).

## Identita, sklad a obnova operácií

- Každá predajná položka má jednu kanonickú skladovú kartu. Nové fyzické SKU používa dvojpísmenový prefix **výrobcu** a potvrdený článok výrobcu. Existujúce SKU, zdrojové kódy a overené natívne identity sa zachovávajú. [SKU](docs/sku-policy.md), [dátový model](docs/warehouse-model.md), [náhľad prevodu existujúcich SKU](docs/sku-migration.md).
- Aktívne dodávateľské vlastníctvo určuje `product_supply_sources`. Názov, prefix, podobnosť kódov ani samotný EAN nesmú vytvoriť nepreukázanú väzbu.
- Výslovne overené predajné násobky môžu zdieľať jednu kusovú kartu. Predajné množstvá a fyzické kusy ostávajú oddelené; prepočet sa neodvodzuje z názvu či starého zostatku. [Zdieľané predaje](docs/shared-stock-sales.md), [dávky](docs/shared-stock-sale-batches.md).
- Fyzické zásoby vyžadujú potvrdený začiatok evidencie. Dodávateľský feed, založenie karty, importný zostatok ani faktúra samy nepotvrdzujú prijatý tovar. Desať nových skenovacích UUID znamená desať operácií; opakovanie jedného UUID vracia jeho výsledok.
- Pohyby, FIFO alokácie, pôvodné ceny a audit sa nemenia upratovaním. Opravy množstva sú nové kompenzačné pohyby; neznáma cena je `NULL`, nie nula. Služby nevytvárajú fyzický sklad. [FIFO](docs/fifo.md), [jednotky](docs/stock-units.md).
- Neistý externý zápis sa automaticky neopakuje. Zachovať pôvodnú požiadavku, UUID, dôkazy a blokácie; konkrétnu obnovu určuje dokument daného workflow. Samotná zhoda aktuálneho vzdialeného množstva nevyrieši neistý skladový PUT.

### Zlučovanie a jednorazová migrácia

Bežné **Zlúčiť karty** v produktovej tabuľke aj operátorské API `product-consolidations/one-off/preview`, `one-off/apply` a `one-off/{request_id}` sú súčasťou implementácie. Jednorazová cesta prijíma presný overený zoznam skupín, opätovne kontroluje náhľad pod zámkami a ukladá celú dávku v jednej databázovej transakcii. Nevolá vzdialené API ani nesčítava fyzické množstvá.

Kanonické aliasy, pôvodná história, potvrdenia a výsledky zostávajú zachované. Konsolidácia neruší pozastavenie Atomer kampaní ani ich pôvodné či čiastočné potvrdenia. Podrobný [operátorský kontrakt](docs/product-consolidation.md#one-off-atomic-identity-migration), [pravidlá rozsahu](docs/product-consolidation-rules-scope.md), [dôkazy](docs/product-consolidation-evidence.md), [diagnostika](docs/product-consolidation-diagnostics.md) a [AI pôvod](docs/product-consolidation-ai-lineage.md) zostávajú zdrojom pravidiel a obnovy.

Automatické Atomer workery vyžadujú `ATOMER_CATALOG_WORKER_ENABLED=true`; predvolená hodnota je `false`. Existujúci [export migrácie](docs/migration-export.md) podporuje `local_only=true` bez volania Upgates. Manuálny `scripts/atomer_direct_batch.py` je samostatný obsahový nástroj s vlastným denníkom a [postupom obnovy](docs/atomer-content-import.md), nie všeobecné zlučovanie kariet.

## Databázové migrácie

Repozitár obsahuje 52 SQL súborov `001`–`052`. Aktuálny upgrade runner má 41 modulov: `005`–`038`, potom `046`–`052`; modul `atomer_review_migrate` vykonáva **SQL `038`–`045` spolu**. API obraz balí všetkých 48 upgrade SQL súborov `005`–`052`.

Inicializácia prázdnej DB, aktualizácia existujúcej DB a obnova zálohy majú odlišný postup. Presný stav overenia, zdroje poradia a limity sú v [jednotnom návode](api/POSTGRES_SETUP.md). Už nasadené SQL sa neupravujú a nový súbor sám neznamená zapojenie do deploymentu.

## Produkčné nasadzovanie a diagnostika

`main` je produkčná vetva. Autoritatívny postup je [build.yml](.github/workflows/build.yml): paralelné obrazy, serializovaný deployment, migrácie pred obnovením API a autentizované bezpečnostné overenie verejného HTTPS. Zmeny iba Markdown/docs release nespúšťajú; PR kontroly sa zachovávajú.

Server používa vlastné Compose súbory a generovaný overlay s tajnými konfiguráciami a sieťou SPS. [Produkčný Compose](infra/docker-compose.prod.yml) je referencia, nie dôkaz aktuálneho serverového stavu. Pri diagnostike najprv overiť Actions a chybovú fázu; podrobnosti sú v [CI a nasadzovaní](docs/ci-deployment.md). Oprávnenia pre merge, serverový prístup a živé operácie zostávajú v [AGENTS.md](AGENTS.md).

Návrat kódu nevracia databázové migrácie ani už vykonané externé zápisy. Po použití nových dátových kontraktov treba kompatibilnú opravu vpred. Uchovať blokácie, denníky a výsledky; [postup obnovy](api/POSTGRES_SETUP.md#záloha-a-obnova) nenahrádza overenie konkrétnej operácie.

## Zostávajúca práca

Úplné tržby/marža, širšia inventúrna agenda, nepodporované historické vratky a všeobecné RBAC zostávajú samostatné témy. Existujúce jednotky, účty, FIFO a korekcie tým nie sú označené za nedokončené. [Aktuálne priority](docs/mvp-progress.md) odlišujú produktový rozsah od [prevádzkového pilotu zásob](docs/upgates-sync-readiness.md).

## Rozpracované spustenie a opraviteľnosť

[Priebežné odovzdanie](docs/warehouse-launch-continuation.md) zaznamenáva potvrdené rozhodnutia, stav implementácie, reset testovacích dokladov, prevzatie počiatočných zásob a presný postup pokračovania. Samotná prítomnosť týchto zmien vo vetve neznamená vykonaný reset ani zapnutú skladovú autoritu. Migrácie 051 a 052 pridávajú audit spracovania/archivácie faktúr a opráv príjemiek bez automatickej zmeny skladových množstiev.
