# Inventory Hub — prehľad projektu

Inventory Hub je interná aplikácia pre **BIKETREK**, **xTrek** a predajňu. Obsahuje správu dodávateľov a faktúr, príjem, prehľad skladu, dodávateľský katalóg a viacero integračných ciest s Upgates. Kompletný centrálny sklad vrátane predaja zo všetkých kanálov, rezervácií, FIFO a tabuľkovej editácie je ďalším cieľom, nie dokončenou funkciou celého systému.

**Posledné porovnanie s kódom:** 23. 9. 2026, `main` na commite `951684b6c63cf94e6ffe5705ab95e86ee8a3c493`. Tento stav vychádza z aktívneho kódu a workflow v repozitári. Nepotvrdzuje aktuálny obsah produkčnej DB, celú serverovú konfiguráciu ani funkčnosť všetkých obrazoviek v prehliadači. Pri ďalších zmenách aktualizuj stav a rozsah overenia.

## Kde začať

| Zdroj | Úloha |
| --- | --- |
| [AGENTS.md](AGENTS.md) | Rozsah práce, platnosť udelených oprávnení a pravidlá zmien. |
| Tento dokument | Mapa projektu, hranice implementácie a prevádzkové súvislosti. |
| [Dodávateľský katalóg](docs/supplier-catalog.md) | Kontrakty feedov, identity, náhľadov, importu a obnovy. |
| [AI obsah](docs/ai-content.md) | Príprava a kontrola obsahu, existujúce produkty, pravidlá, fronta a konfigurácia. |
| [PR kontroly](.github/workflows/ci.yml) | Automatické testy a izolovaná testovacia databáza. |
| [Build a deployment](.github/workflows/build.yml) | Skutočný automatický postup nasadenia po zmene `main`. |

Dokumentácia je mapa; pri rozhodovaní over aktívny kód. Staré datované súbory a kópie nepovažuj za používané iba preto, že sú v repozitári. Tabuľka alebo tlačidlo samy osebe nedokazujú dokončený pracovný postup.

## Prevádzkový kontext

- Repozitár: [mpriesol/inventory_hub](https://github.com/mpriesol/inventory_hub). Starý `OLD_inventory_hub` sa pri bežnej práci nepoužíva.
- Hub: [hub.biketrek.sk](https://hub.biketrek.sk).
- Podľa posledného zadania vlastníka je BIKETREK produkčný Upgates e-shop na [biketrek.sk](https://www.biketrek.sk); xTrek má testovaciu prevádzku na Upgates. Doména [xtrek.site](https://xtrek.site) podľa tohto zadania zatiaľ nefunguje, čo samo osebe neznamená nefunkčné API.
- Cieľom sú dva Upgates e-shopy a jedna predajňa s fyzickým skladom. Migrácia pôvodného xTrek z Atomeru je prevádzkový kontext; nový Atomer konektor nie je súčasťou aktuálneho skladového cieľa.
- Technické kódy `biketrek` a `xtrek` zostávajú zachované. Nové texty používajú značky BIKETREK a xTrek.
- Konkrétne účty, nastavenia, feedy a údaje sú mimo Git. Použi skutočnú konfiguráciu a over dostupnosť príslušnej integrácie.

## Aktuálny stav funkcií a obrazoviek

„Implementované“ znamená existujúcu cestu v kóde. Funkčnosť konkrétneho dodávateľa alebo e-shopu závisí aj od konfigurácie a dát.

| Oblasť / cesta UI | Stav podľa kódu | Hranice a zdroje |
| --- | --- | --- |
| Dashboard `/` | Implementovaný prehľad | `DashboardPage.tsx`; počítadlo nedokazuje dokončený obchodný proces. |
| Faktúry `/invoices`, `/invoices/:invoiceId` | Nahrávanie, evidencia, filtre a detail | `routers/invoices.py`, `invoices_unified.py` a príslušné stránky. Nahratie súboru neznamená univerzálne OCR ani rozpoznanie každého formátu. |
| Príjem `/receiving`, `/receiving/:invoiceId` | Príjem naviazaný na faktúru | Skenovanie, množstvá, pozastavenie a finalizácia v `routers/receiving_db.py`. Finalizácia zapisuje pohyby a vážený priemer. Všeobecný príjem bez faktúry je cieľ ďalšieho rozvoja. |
| Sklad `/stock` | Čiastočné pracovné rozhranie | `routers/stock.py`, `StockPage.tsx`: stavy, rezervované/voľné množstvo, priemerná cena, detail a Upgates operácie. Chýba editor buniek; stránkovacie tlačidlá a CSV export sú neaktívne. |
| Produkty `/products` | „V príprave“ | `ProductsPage` z `PlaceholderPages.tsx`. Samostatný detail `/products/:sku` už používa produktové komponenty. |
| Dodávatelia `/suppliers` | Implementovaná správa | Aktívny `SuppliersPage.tsx`; nepomýliť so zástupnou funkciou rovnakého názvu. Rozhoduje export v `pages/index.ts`. |
| Katalóg `/suppliers/:supplier/catalog` | Implementovaný dodávateľský katalóg | Vyhľadávanie, filtre, stránkovanie, obrázky, explicitné variantné skupiny, mapovanie zalistovania a importný náhľad. Parsery pre Paul Lange a Northfinder. |
| Shopy `/shops` | „V príprave“ | `ShopsPage` z `PlaceholderPages.tsx`. Konfiguračné API a modaly existujú inde; táto samostatná stránka nie je hotová. |
| Nastavenia `/settings` | Vstup do AI nastavení | `/settings/ai-content` a `/ai-content` zobrazujú AI stránku; nejde o úplnú správu skladu. |
| AI obsah | Implementovaný samostatný workflow | Pravidlá, profily, príprava, kontrola, náhľad importu a aktualizácia vybraných polí. Limity v `docs/ai-content.md`. |
| Objednávky, rezervácie, inventúry, všeobecná sync fronta | Modely / nedokončený celkový workflow | Existencia tabuliek nepotvrdzuje kompletné spracovanie predaja a rezervácií zo všetkých kanálov. |
| FIFO, excelový editor, skladové miesta, roly obsluhy | Ciele ďalšieho návrhu | Nie sú tu deklarované ako hotové funkcie. AI prístupový token nie je všeobecný systém rolí. |

Zdroj navigácie: [App.tsx](frontend/src/App.tsx), [exporty stránok](frontend/src/pages/index.ts), [zástupné stránky](frontend/src/pages/PlaceholderPages.tsx).

## Architektúra a aktívne moduly

Frontend komunikuje s FastAPI cez Caddy. Relačné dáta sú v PostgreSQL; konfigurácie, zdrojové súbory a časť importných stavov vo filesysteme. AI worker beží v API procese a používa PostgreSQL. Prítomnosť Redis v Compose nedokazuje, že ním beží AI alebo všeobecná synchronizačná fronta.

| Vrstva | Technológia |
| --- | --- |
| Frontend | React 18, TypeScript, Vite, Tailwind CSS v4, existujúce UI komponenty a SK/EN preklady. |
| Backend | Python 3.12, FastAPI, SQLAlchemy 2 async a asyncpg. |
| Databáza | PostgreSQL 16. |
| Dodávatelia | Parsery a B2B adaptéry; niektoré používajú Playwright. |
| Prevádzka | Docker Compose, Caddy 2, PostgreSQL a Redis 7 v referenčnej zostave. |

Backendové cesty v tabuľke sú pod `api/inventory_hub/`:

| Modul | Zodpovednosť |
| --- | --- |
| `main.py`, `database.py`, `settings.py` | Routery, životný cyklus API/AI workeru, spojenia a konfigurácia. |
| `db_models.py`, `db_models_ext.py`, `ai_content_models.py` | Hlavné relačné modely; ďalšie modely sú aj v príslušných feature moduloch. |
| `config_io.py`, `config_normalize.py` | Súborové konfigurácie. |
| `routers/receiving_db.py`, `routers/stock.py` | Príjem a čítanie skladových údajov. |
| `routers/upgates_sync.py`, `services/upgates.py` | Načítanie produktov a vybrané operácie ich prenosu. |
| `routers/catalog.py`, `services/catalog.py`, `services/catalog_import.py` | Katalóg, identita, náhľad a založenie produktov. |
| `services/identifiers.py`, `adapters/` | Identifikátory a dodávateľské spracovanie. |
| `routers/ai_content.py`, `services/ai_content*.py` | AI príprava, review, aktualizácie obsahu a worker. |
| `routers/invoices.py`, `routers/invoices_unified.py`, `routers/imports.py` | Faktúry a existujúce súborové/CSV postupy. |

`USE_POSTGRES=true` registruje databázový príjem, sklad, katalóg, Upgates API integráciu a AI cesty. Režim `false` používa legacy príjem; nejde o rovnocenný náhradný sklad pri výpadku DB. Nevyvodzuj automatický funkčný failover iba z logov pri štarte.

## Dátový model a úložiská

### PostgreSQL

| Tabuľky | Význam |
| --- | --- |
| `suppliers`, `supplier_feeds`, `supplier_feed_runs`, `supplier_products` | Dodávatelia a normalizované feedové dáta. |
| `product_groups`, `products`, `product_identifiers`, `product_variant_attributes` | Rodiny, predajné položky, identifikátory a variantné atribúty. |
| `product_supply_sources` | Model väzieb skladovej položky na dodávateľské ponuky. |
| `uploaded_invoices`, `uploaded_invoice_lines` | Nahrané faktúry a položky. |
| `receiving_sessions`, `receiving_lines`, `scan_events` | Príjem a skenovanie. |
| `warehouses`, `stock_balances`, `stock_movements` | Sklady, aktuálne množstvá a nemenná história pohybov. |
| `shops`, `shop_products`, `shop_product_content` | Kanály, mapovania a zachytený obsah z Upgates. |
| `availability_profiles`, `shop_product_availability` | Model dostupnosti; nie dôkaz hotovej automatizácie. |
| `shop_orders`, `shop_order_items`, `reservations`, `inventory_counts`, `inventory_count_lines`, `shop_sync_outbox` | Modely pre širšie workflow; over ich skutočných producentov a konzumentov. |
| `ai_rule_versions`, `ai_rule_state`, `ai_content_batches`, `ai_content_jobs`, `ai_content_revisions` | AI pravidlá, úlohy, náklady a história kontroly. |

Produkt nemá databázový stĺpec `primary_ean`; identifikátory sú v `product_identifiers`. ORM má kompatibilnú odvodenú vlastnosť rovnakého názvu. Podporované typy a unikátnosť over v modeli a službe identifikátorov. EAN je text a nie je jediným párovacím kľúčom.

### Filesystem

Koreň určuje `INVENTORY_DATA_ROOT`. Referenčný produkčný mount je `/opt/inventory-data` na hostiteľovi → `/data/inventory-data` v API kontajneri.

| Relatívna oblasť | Obsah |
| --- | --- |
| `suppliers/<kod>/config.json` | Konfigurácia dodávateľa a adaptérov. |
| `suppliers/<kod>/feeds/` | Zdrojové a konvertované feedy. |
| `suppliers/<kod>/invoices/` | Originálne doklady, spracované súbory a indexy podľa existujúceho workflow. |
| `suppliers/<kod>/imports/upgates/` | CSV výstupy. |
| `shops/<kod>/` | Konfigurácia, exporty/cache a logy e-shopu. |
| `shops/<kod>/catalog-imports/` | Trvalé náhľady, výsledky a importný denník. |

Dodávateľská konfigurácia a relačná identita musia zodpovedať rovnakému kódu. Pri zmene over obe reprezentácie a existujúce vytváracie cesty; nepredpokladaj, že ručný SQL INSERT je vždy potrebný. Súborové indexy a DB nemajú automaticky spoločnú transakciu.

Obnova projektu potrebuje databázu aj relevantné súbory a chránenú konfiguráciu. Existujúci produkčný plán záloh a úspešný restore test táto kontrola repozitára nepotvrdila.

## Príjem, sklad a oceňovanie

Databázový príjem používa cesty pod `/api/suppliers/{supplier_code}/receiving/sessions`. Skenovanie a finalizácia patria ku konkrétnej relácii; všeobecné `/receiving/sessions` z predchádzajúcej dokumentácie nebolo správnym popisom tohto kontraktu.

Finalizácia v `routers/receiving_db.py` vytvára `RECEIVING_IN` pohyby, aktualizuje `stock_balances` a podľa pravidiel môže založiť chýbajúcu položku. Má identifikačný kľúč pohybu pre reláciu a riadok. Pri rozšírení over aj súbeh a obnovu po chybe; samotný kľúč nedokazuje pokrytie všetkých okrajových prípadov.

Aktuálny výpočet príjmu je **vážený priemer**, nie FIFO. Voľné množstvo je rozdiel fyzického a rezervovaného množstva. Zobrazený stĺpec rezervácií neznamená hotové automatické rezervovanie predajov z oboch webov a pokladne.

`stock_movements` sa pri oprave minulosti nemajú meniť ani mazať; používajú sa korekčné pohyby. Nákupná cena, predajná cena a aktuálna feedová cena majú odlišný význam. Počiatočný import zásoby a jej ocenenie treba pred prevzatím úlohy centrálneho skladu osobitne overiť; nenahrádzajú historické nákupné vrstvy.

## Upgates: samostatné integračné cesty

Označenie „synchronizácia“ nie je zárukou rovnakého správania všetkých tlačidiel:

| Cesta | Čo robí | Hranice |
| --- | --- | --- |
| Legacy CSV | Príprava súborov, načítanie exportu e-shopu a rozdelenie importných výstupov. | Nie je priebežnou API synchronizáciou celého skladu. |
| Upgates → Hub | Náhľad a načítanie produktov, variantov, mapovaní a obsahu; má aj cestu inicializácie zásoby. | Nedokazuje bezpečné zlúčenie počiatočného skladu dvoch webov ani históriu nákupov. |
| Hub → e-shop cez `push_products_to_shop` | Prenos vybraných produktov zo zachyteného obsahu. | Aktuálne preskakuje parenty už namapované v cieli; nejde o všeobecnú aktualizáciu existujúcich produktov alebo automatickú stock sync službu. |
| Dodávateľský katalóg → e-shop | Výber, náhľad a založenie nových produktov cez API, skrytých a označených `validation_required=1`. | Neaktualizuje existujúce produkty a neposiela vlastné skladové množstvá. |
| AI aktualizácia existujúceho produktu | Porovnanie pred/po a aktualizácia povolených polí s kontrolou identity a výsledku. | Všeobecná aktualizácia obsahu neposiela ceny, vlastné množstvo, aktivitu, identifikátory ani nové varianty. |

Podmienky náhľadov, potvrdenia, kontrol identity, stavu `uncertain` a obnovy sú v [katalógovej](docs/supplier-catalog.md) a [AI dokumentácii](docs/ai-content.md). Existuje aj osobitná obmedzená AI cesta aktualizácie dodávateľskej dostupnosti; jej podmienky neplatia automaticky pre všetky produkty a varianty.

Dodávateľská zásoba je oddelená od našej. `6+` je dolná hranica, nie presný počet. Obnova katalógu a zalistovanie z feedu nevytvárajú fyzický príjem. Zachovaj čerstvosť údajov, neznáme hodnoty a pravidlá konkrétneho integračného kontraktu.

## AI obsah

AI workflow používa existujúci katalóg/importer a vlastné DB tabuľky. Worker beží v API procese s databázovými zámkami. Podporuje prípravu z feedu, existujúce produkty, pravidlá a kategórie, odhad nákladov, revízie a kontrolované odoslanie výsledku.

Oficiálny prieskum môže vyhľadávať weby výrobcov/dodávateľov bez povinného prednastaveného zoznamu domén. Vyhľadávanie nezaručuje dostupnosť správnej hodnoty a chýbajúce povinné parametre naďalej vyžadujú vyriešenie.

AI má vlastnú konfiguráciu a ochranu prístupovým tokenom: `AI_CONTENT_ENABLED`, `AI_CONTENT_MODEL`, `AI_CONTENT_ACCESS_TOKEN`, `OPENAI_API_KEY`, `AI_CONTENT_MONTHLY_USD`, `AI_CONTENT_JOB_USD`. Tajné hodnoty do dokumentácie ani logov nepatria. Postup konfigurácie a obmedzenia sú v [docs/ai-content.md](docs/ai-content.md).

## Databázové migrácie

V [infra/db-init](infra/db-init) sú tieto SQL súbory:

| Súbor | Úloha |
| --- | --- |
| `001_schema.sql` | Základná relačná schéma, typy a databázové pravidlá. |
| `002_invoice_management.sql` | Rozšírenia fakturačnej evidencie. |
| `003_uploaded_invoices.sql` | Nahrané faktúry a položky. |
| `004_shop_product_content.sql` | Kompletný obsah produktov podľa e-shopu a parent kódu. |
| `005_ai_content.sql` | AI pravidlá, dávky, úlohy a revízie. |

**Aktuálny deployment automaticky spúšťa iba `005`** cez [ai_content_migrate.py](api/inventory_hub/ai_content_migrate.py), pod DB zámkom. [API Dockerfile](api/Dockerfile) tento SQL súbor balí do obrazu. Nejde o všeobecný migrátor číslovaných súborov.

Adresár `/docker-entrypoint-initdb.d` v referenčnom Compose inicializuje nové databázové úložisko; automaticky neaktualizuje existujúce. Pred upgrade over aplikovanú schému a priprav postup iba pre potrebné chýbajúce zmeny. Pridanie `006_*.sql` bez zmeny migračného postupu samo nespôsobí jeho vykonanie pri deployi.

Pri čistej lokálnej inštalácii over postupnosť `001`–`005` v izolovanej DB a ukončenie pri SQL chybe. Historická úplná inicializačná cesta nebola počas tejto dokumentačnej aktualizácie spustená; izolované CI testy samy nepotvrdzujú celý produkčný bootstrap. Už nasadené migrácie sa spätne neprepisujú.

## Lokálny vývoj a overovanie

Použi Python 3.12, Node.js 20 a vlastný PostgreSQL 16. Nastav lokálne `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` a `USE_POSTGRES=true`; nepoužívaj produkčné prístupy. Ďalšie možnosti sú v [settings.py](api/inventory_hub/settings.py).

Z koreňa repozitára, po príprave izolovanej DB a príslušnej schémy:

```bash
python -m pip install -r api/requirements.txt
export INVENTORY_DATA_ROOT=/tmp/inventory-hub-dev
mkdir -p "$INVENTORY_DATA_ROOT"
PYTHONPATH=api uvicorn inventory_hub.main:app --reload --port 8000
```

Dočasný dátový adresár je len na reprodukovateľné lokálne dáta. Pre B2B cesty používajúce prehliadač priprav aj Playwright Chromium; produkčný Dockerfile ho inštaluje.

Frontend v druhom termináli z koreňa repozitára:

```bash
npm --prefix frontend ci
npm --prefix frontend run dev
```

API základ a lokálny proxy over vo [Vite konfigurácii](frontend/vite.config.ts). Konfigurácie ani dáta nevymýšľaj, aby sa aplikácia tvárila funkčne.

Overovacie príkazy sú v [AGENTS.md](AGENTS.md) a [CI](.github/workflows/ci.yml): kompilácia a `unittest`, frontendový build a dve jsdom sady pre katalóg a AI. DB testy používajú samostatný lokálny PostgreSQL a `CATALOG_TEST_DATABASE_URL` s názvom DB končiacim `_catalog_test`. Ak sa DB prípady preskočia, uveď to. Interakčné testy neoverujú vizuálny layout v reálnom prehliadači.

## Produkčné nasadzovanie a diagnostika

Autoritatívny postup je v [build.yml](.github/workflows/build.yml):

1. Push do `main` alebo ručne spustený workflow zostaví API a frontend a publikuje obrazy do GHCR s tagmi `main` a `sha-<commit>`.
2. Deploy job sa pripojí na server a pracuje v `/opt/inventory-hub`.
3. Pripraví Compose overlay pre chránený súbor `ai-content.env`; jeho vytvorenie neznamená vyplnené AI prístupy.
4. Cez `docker compose pull` stiahne obrazy, aplikuje AI migráciu `005` a obnoví služby `api`, `frontend-build` a `caddy` s overlayom.
5. Skontroluje `/api/health` a `/api/ai-content/status` a vykoná existujúce čistenie nepoužívaných obrazov.

Aj dokumentačný merge aktuálne spúšťa tento workflow. Platnosť oprávnenia na merge a živé zásahy rieši `AGENTS.md`; existujúci súhlas sa neopakuje, ale samotný návrh nie je pokynom na nasadenie implementácie.

[infra/docker-compose.prod.yml](infra/docker-compose.prod.yml) je referencia. Server používa vlastný `docker-compose.yml` a deploy pridáva `docker-compose.ai-content.yml`; ich celý aktuálny obsah nebol touto kontrolou repozitára overený. Pred manuálnym zásahom over nasadené súbory a oprávnenia. Starý návod cez `docker compose down` a `up -d` nevystihoval tento postup a nie je štandardným krokom bežného nasadenia.

| Referenčná položka | Význam |
| --- | --- |
| `/opt/inventory-hub` | Serverové Compose súbory a konfigurácia. |
| `/opt/inventory-data` | Aplikačné súbory na hostiteľovi. |
| `/data/inventory-data` | Pripojenie dát v API kontajneri. |
| `postgres`, `redis`, `api`, `frontend-build`, `caddy` | Služby referenčnej zostavy. |
| `ROOT_PATH=/api` | Prefix API za reverse proxy. |
| `ai-content.env` → `/run/secrets/hub-ai.env` | Chránená AI konfigurácia pripojená overlayom. |

Diagnostiku začni výsledkom Actions a relevantnými jobmi. [Swagger](https://hub.biketrek.sk/api/docs), [ReDoc](https://hub.biketrek.sk/api/redoc) a [OpenAPI JSON](https://hub.biketrek.sk/api/openapi.json) opisujú nasadenú API verziu, pokiaľ sú dostupné. Logy a detaily kontajnera vyžadujú skutočný serverový prístup; bez neho ich stav netvrď ako overený.

Návrat kódu rieš kontrolovaným revert PR a bežným nasadením. Revert nevracia DB ani už vykonané Upgates zápisy. Taká zmena potrebuje osobitný postup pre dáta a kontrolu spätnej kompatibility.

## Ďalší cieľ: centrálny sklad MVP

Požiadavky vlastníka na ďalší návrh, nie hotové funkcie:

- spoločný fyzický sklad pre BIKETREK, xTrek a predajňu, s oddeleným zalistovaním a údajmi kanálov;
- predaj a rezervácie z oboch webov a pokladne bez duplicitných pohybov;
- FIFO nákupné vrstvy, náklad predaného tovaru a história, oddelené od predajnej ceny;
- tabuľková editácia produktových údajov, validácie a stav doručenia zmien do každého e-shopu;
- spoľahlivá synchronizácia vlastného voľného množstva a osobitnej dodávateľskej dostupnosti;
- praktické úlohy a oprávnenia obsluhy, postupne skladové miesta a príjem bez faktúry.

Rozsah a poradie určí samostatný návrh MVP. Uprednostni aktívny kód, malé rozšírenia a jasné zodpovednosti. Tieto požiadavky samy neautorizujú implementáciu počas úlohy zameranej iba na dokumentáciu alebo návrh.
