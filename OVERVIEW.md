# Inventory Hub — prehľad projektu

Inventory Hub je interná aplikácia pre **BIKETREK**, **xTrek** a predajňu. Obsahuje správu dodávateľov a faktúr, príjem, prehľad skladu, dodávateľský katalóg a viacero integračných ciest s Upgates. Obsahuje lokálne spracovanie objednávok, kontrolované skladové operácie, FIFO a tabuľkový editor. Obsahuje aj oddelené plánované čítanie dodávateľských dostupností, pravidelný prenos zásob a potvrdené odoslanie vybraných produktových polí. Prevzatie skladovej autority zostáva výslovne zapínateľné; nasadenie kódu nie je ostrý pilot.

**Posledné porovnanie s kódom:** 24. 9. 2026, dostupnosti, pravidelný prenos a jednotná tabuľka nad `main 6bdfd71` (nasadený PR #34). Aktuálny stav overenia a nadväzujúce priority sú v [MVP postupe](docs/mvp-progress.md). Popis kódu nepotvrdzuje obsah produkčnej DB ani celú serverovú konfiguráciu.

## Bezpečnostná hranica API (14. 10. 2026)

Všetky prevádzkové API aj dokumenty vyžadujú prihlásenie; verejné sú iba presné bootstrap autentizačné cesty a sanitizovaný health check. Cookie zápisy majú spoločnú CSRF ochranu, integračné nastavenia spravuje administrátor a uložené tajomstvá sa nevracajú do formulárov. Dokumenty majú osobitnú cestovú allowlist ochranu aj po prihlásení. Deployment overuje anonymné odmietnutie aj autorizovaný prístup; skladové autority/automatizácie sa tým nezapínajú. Podrobný kontrakt a rotácia údajov: [docs/user-accounts.md](docs/user-accounts.md). Stav implementácie treba odlíšiť od potvrdeného výsledku konkrétneho nasadenia.

## Kde začať

Skladová tabuľka a detail ponúkajú opravu chýbajúcich odkazov cez čítanie presného prepojeného produktu; náhľad už prepojených kariet má dokončenie bez nového zápisu väzieb. Dodávateľské párovanie umožňuje výslovne doplniť overené EAN do prázdnej karty s kontrolou konfliktov a auditom. Dostupnosť uvádza konkrétny dôvod chýbajúceho údaja a otvorí nastavenie správneho dodávateľa a kódu. Tieto zmeny nepotrebujú migráciu a neaktivujú skladové zápisy ani plánovanie. Konkrétne produkčné nastavenie zdroja zostáva samostatným overením. Podrobnosti: [editor](docs/product-editor.md), [väzby e-shopov](docs/product-links.md), [dodávateľské väzby](docs/supplier-card-links.md), [dostupnosti](docs/supplier-availability.md).

Existujúcu skladovú kartu možno ručne prepojiť s odlišným kódom dodávateľského katalógu cez náhľad a potvrdenie. Príprava nových kariet upozorňuje na zhodu EAN/kódov aj podobnosť názvu/SKU a umožňuje vybrať existujúcu kartu. Potvrdené prepojenie zachová SKU, obsah, zásoby a históriu. Implementácia, kontrola duplicít a aditívna migrácia 028 sú opísané v [dodávateľských väzbách kariet](docs/supplier-card-links.md); produkčné overenie konkrétnej väzby je samostatný krok.

Príjem faktúry sa prepája s rovnakou prípravou produktov cez **Založiť skladové karty**. Položky rozlišujú existujúcu kartu, základnú kartu z príjmu, produkt iba vo feede, nenájdený produkt a nejasnú či neoverenú zhodu. Vlastný potvrdený sklad sa zobrazuje samostatne. Vybrané položky možno doplniť z feedu, ručne alebo voliteľne cez AI a vrátiť sa k tej istej príjemke so zachovanými množstvami. Uloženie kariet nemení zásoby ani faktúrové ceny; fyzický príjem zostáva samostatným potvrdením. [Postup a API](docs/receiving-identity.md), [spoločný editor](docs/product-imports.md).

Príprava importu podporuje samostatné ciele **BIKETREK** a **xTrek** pri každom produkte, predvolený e-shop dodávateľa a uloženie iba do Hubu. Každý cieľ má vlastný náhľad a trvalý výsledok; úspešné alebo neisté odoslanie sa nenahrádza novou dávkou. AI v tejto príprave vždy pracuje s feedom a oficiálnymi zdrojmi. Existujúca overená identita si zachová SKU aj pri pridaní ďalšieho dodávateľa. Odkazy na produkt a administráciu pochádzajú z odpovede Upgates. Podrobnosti: [príprava importu](docs/product-imports.md), [dodávateľské vlastné pole](docs/upgates-supplier-metadata.md).

Sloger používa priame B2B čítanie ako bežný zdroj dát bez AI. Prihlásenie sa uloží raz v nastavení dodávateľa; príprava importu potom po dávkach načíta presné vybrané SKU, EAN-y, nákupnú ponuku a dostupnosť. Po zlyhaní môže obsluha výslovne pokračovať iba s údajmi z feedu; chýbajúca nákupná cena a neoverená dostupnosť zostanú neznáme. Dokončená alebo takto preskočená príprava umožní úpravy a voliteľné AI podľa spoločných nastavení. Čítač rozlišuje spoločnú cenu rodiny a vlastné ceny jednotlivých variantov. DMOC z feedu ostáva predvolenou predajnou cenou. [Ručný refresh dostupností](docs/supplier-availability.md) prechádza iba aktívne produkty evidované v Hube s väzbou na Sloger, vrátane nulovej vlastnej zásoby; automatické spúšťanie je pre Sloger zakázané aj na serveri. Dodávateľské údaje nemenia vlastné množstvá ani FIFO. Podrobnosti: [príprava importu](docs/product-imports.md), [oddelenie B2B od AI](docs/ai-content.md).

Chránené GET/PUT `/api/suppliers/sloger/lookup/credentials` vracajú iba stav a meno, nikdy heslo. Tajomstvá ostávajú v samostatnom trvalom API-only úložisku. Pôvodné výsledky AI testu sú čitateľné, ale jeho platený POST je zrušený (410). Presný návrat prihlásenia na `/shop/` s HTTP 404 môže raz pokračovať cez overenú `/sk/b2b` bez opakovania POST; pred vyhľadávaním je povinný dôkaz prihlásenia. Spoločná služba serializuje B2B čítanie naprieč importom a dostupnosťami. Tieto zmeny nevyžadujú SQL migráciu; stav implementácie nie je dôkazom produkčného overenia všetkých tokov.

Aktuálna oprava evidencie pridáva [potvrdený začiatok skladu](docs/stock-tracking.md) (migrácia 019). Historické importy a testy zostávajú neoverené; aktívne množstvá, rezervácie a synchronizácia vyžadujú nový potvrdený príjem alebo fyzický počet pre konkrétne SKU/sklad. V nasledujúcich starších popisoch má tento kontrakt prednosť pred odvodzovaním potvrdenia zo samotnej existencie pohybov.

Výbery skladu v editore, histórii, počiatočnom stave, FIFO a nastaveniach používajú pri prvom načítaní serverový aktívny `is_default` sklad. Uložený kontext e-shopu alebo dávky a ručný výber majú prednosť; **Všetky sklady** zostávajú súhrnným pohľadom. Predvolenie výberu nemení zásoby, potvrdenie evidencie ani povolenia automatických zápisov.

| Zdroj | Úloha |
| --- | --- |
| [AGENTS.md](AGENTS.md) | Rozsah práce, platnosť udelených oprávnení a pravidlá zmien. |
| Tento dokument | Mapa projektu, hranice implementácie a prevádzkové súvislosti. |
| [Dodávateľský katalóg](docs/supplier-catalog.md) | Kontrakty feedov, identity, náhľadov, importu a obnovy. |
| [Mapovanie produktových feedov](docs/product-feed-mapping.md) | Zdrojové polia, transformácie, parametre a kategórie podľa dodávateľa a e-shopu. |
| [Príprava importu](docs/product-imports.md) | Uložený editovateľný náhľad, voliteľné AI, uloženie do Hubu a samostatné odoslanie do e-shopu. |
| [Prepojenie existujúcich kariet](docs/product-links.md) | Jednotlivé aj hromadné párovanie s existujúcimi produktmi e-shopov, náhľad, konflikty a obnova. |
| [Dodávateľská identita](docs/supplier-identity.md) | Uzamknuté prefixy, spoločné SKU a doplnenie väzieb dostupností z už načítaných údajov. |
| [Centrálny sklad](docs/central-stock.md) | Schválené obchodné pravidlá, prvý bezpečnostný balík a hranice ďalšej implementácie. |
| [Objednávky — návod pre obsluhu](docs/order-workflows.md) | Prvé spustenie, nastavenia, ručná kontrola a riešenie výnimiek. |
| [Automatika — vývojársky kontrakt](docs/order-automation.md) | Konfigurácia, fronta, transakcie, API a obnova. |
| [AI obsah](docs/ai-content.md) | Príprava a kontrola obsahu, existujúce produkty, pravidlá, fronta a konfigurácia. |
| [Nákupné ceny v e-shopoch](docs/fifo-purchase-costs.md) | Prenos ďalšej FIFO ceny produktu, nákladov vydaných objednávok a konkrétnej historickej opravy. |
| [PR kontroly](.github/workflows/ci.yml) | Štyri paralelné backendové skupiny s vlastnou DB, kontrola úplnosti a frontendové testy. |
| [Build a deployment](.github/workflows/build.yml) | Paralelné zostavenie obrazov a serializované nasadenie po zmene `main`; dokumentačné zmeny sú vynechané. |
| [CI a meranie nasadenia](docs/ci-deployment.md) | Pokrytie testov, časové reporty, cache a vykonanie migrácií. |

Dokumentácia je mapa; pri rozhodovaní over aktívny kód. Staré datované súbory a kópie nepovažuj za používané iba preto, že sú v repozitári. Tabuľka alebo tlačidlo samy osebe nedokazujú dokončený pracovný postup.

AI registre podporujú zdieľané bloky pre vybrané profily, označené návrhy parametrov, nepovinné čiastočné údaje a rozsah podľa skutočnej variantovej osi. Presné zadanie možno stiahnuť bez generovania; detaily a hranice sú v [AI obsah](docs/ai-content.md). Nové úlohy zostavujú obsahové zadanie z relevantných overených kapitol, s auditom vynechaných technických a nesúvisiacich blokov; staré uložené úlohy zostávajú zmrazené. Nasadenie kódu samo nepublikuje súkromnú knihu pravidiel.

AI podklady majú explicitný formát URL, kompatibilitu so starším prefixom `official:https://…`, chyby po jednotlivých riadkoch a opravu zdroja, citátu aj tvrdenia v detaile úlohy. Feedové citáty tolerujú typografické spojovníky a presné dvojice názov: hodnota z parametrov rovnakého produktu. Výhrady k citátom a evidencii otvorenia zdrojov sú neblokujúce upozornenia aj pri automatickom schválení. Chýbajúce rozhodujúce fakty môže prijať ručné schválenie; automatické schválenie ich neprijíma. Technické chyby produktu zostávajú blokujúce. Uloženie konceptu zruší schválenie a vykoná plnú kontrolu bez generovania.

AI úlohy pre spoločnú importnú tabuľku zobrazujú stav obsahu oddelene od skutočného výsledku odoslania. Dokončené importy otvárajú uložený výsledok; opakovaný náhľad ho nenahrádza. Pri zapnutom AI treba pred publikáciou prevziať jeho výsledok do tabuľky alebo AI výslovne vypnúť. Nové prípravy rešpektujú zdedený odhad, schválenie, potvrdenie importu a aktivitu produktu; úplná schválená AI dávka môže pokračovať automaticky cez pôvodný importer. Úprava tabuľky po spustení automatické pokračovanie pozastaví, historické úlohy sa spätne neaktivujú. Staršie importy bez prevzatého AI zostávajú pravdivo označené. Podrobnosti sú v [príprave importu](docs/product-imports.md).

AI produktové názvy, oba popisy a SEO/H1 texty používajú bežné `-`; normalizácia pomlčiek prebehne pri spracovaní AI odpovede, uložení úprav aj zostavení obsahu pre e-shop.

HTML dlhého AI popisu už neblokujú bežné značky ani formátovacie atribúty. Zostáva kontrola aktívneho kódu a vložených dokumentov; uložený výsledok možno znovu overiť bez plateného generovania. Rozsah a postup obnovy sú v [AI dokumentácii](docs/ai-content.md).

Príprava produktov (15. 10. 2026) umožňuje vybrať aj neaktívne a nadradené produktové kategórie, obnoviť ich strom bez straty úprav a otvoriť produkt/fotku priamo pri zaraďovaní. Jediné SKU s pevnou farbou či veľkosťou sa pripraví bez umelej varianty; skutočné varianty sa odosielajú bez hlavného variantu. Nové AI zadanie verzie 4 skracuje názvy a obmedzuje duplicitu parametrov v dlhom popise. Staré dávky a produkty sa spätne neprepisujú. Podrobnosti a hranice sú v [príprave importu](docs/product-imports.md) a [AI obsahu](docs/ai-content.md).

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
| Dashboard `/` | Údaje zo skladu a reálne posledné pohyby | Súčet fyzických, rezervovaných, voľných a karanténnych kusov, nákupná hodnota a rozpracované objednávky evidované Hubom. Žiadne ukážkové udalosti ani vymyslené percentá synchronizácie. Chyba nie je nulový stav. |
| Faktúry `/invoices`, `/invoices/:invoiceId` | Nahrávanie, evidencia, filtre a detail | `routers/invoices.py`, `invoices_unified.py` a príslušné stránky. Nahratie súboru neznamená univerzálne OCR ani rozpoznanie každého formátu. |
| Príjem `/receiving`, `/receiving/:invoiceId` | Príjem naviazaný na faktúru | Skenovanie, množstvá, pozastavenie a finalizácia v `routers/receiving_db.py`. Finalizácia zapisuje nemenné pohyby a FIFO pri nových/aktivovaných zásobách; staršie zostatky zostávajú na váženom priemere do explicitného prechodu. Opakovanie vracia uložený výsledok. Samostatný potvrdený príjem vrátane neznámej ceny je v detaile Nákupné ceny. Nové skeny majú request UUID; replay nepridá druhý kus. [Kontrakt identity a skenera](docs/receiving-identity.md). |
| Sklad `/stock` | Jednotná produktová a skladová tabuľka | Obrázky, parametre, manuálne bunky, označenie zmenenej bunky aj riadka, prítomnosť/odkazy BIKETREK a xTrek, detail po kliknutí na SKU/názov. Samostatné prepojenie jednej alebo vybraných existujúcich kariet s e-shopom cez [náhľad väzieb](docs/product-links.md). Oddelené uloženie v Hube a potvrdené odoslanie vybraných polí. Fyzické množstvo cez [opravný pohyb](docs/stock-adjustments.md). |
| História `/stock/movements` | Chránené čítanie nemennej evidencie | Filtre celého skladu alebo presného SKU, doklad, dôvod, pôvodný náklad a množstvo pred/po. Bez zápisov a Upgates volaní. [Návod a API](docs/stock-history.md). |
| Počiatočný stav `/stock/opening` | Chránený náhľad a zaúčtovanie | Existujúce SKU, fyzicky spočítané celé kusy a explicitná cena EUR bez DPH. Zaúčtovanie vytvorí `INITIAL` pohyby iba bez potvrdeného začiatku evidencie daného tovaru v sklade; neoverený historický zostatok sa auditne uzavrie. Výpadok sa overuje čítaním uloženého výsledku. |
| Dodávatelia `/suppliers` | Implementovaná správa | Aktívny `SuppliersPage.tsx`; nepomýliť so zástupnou funkciou rovnakého názvu. Rozhoduje export v `pages/index.ts`. |
| Katalóg `/suppliers/:supplier/catalog` | Implementovaný dodávateľský katalóg | Vyhľadávanie, filtre, stránkovanie, obrázky, explicitné variantné skupiny, mapovanie zalistovania a importný náhľad. Natívne parsery pre Paul Lange, Northfinder, Sloger a SK Profi Bike; ostatní dodávatelia používajú konfigurovateľné mapovanie. |
| Mapovanie `/suppliers/:supplier/feed-mapping` | Konfigurátor produktového feedu | Automatická analýza XML/CSV/JSON, vizuálne prepojenie polí a kategórií, pomenované parametre feedu → Upgates/publikované registre kategórií, uložené verzie podľa dodávateľa a e-shopu. Odlišné od mapovania faktúr. |
| Import `/product-import` | Jednotný uložený náhľad pre feed a AI | Editácia buniek a hromadné zmeny, ochrana ručných úprav, voliteľné AI pre produktovú rodinu. Uloženie do Hubu je oddelené od potvrdeného založenia v e-shope; bez fyzických skladových pohybov. |
| Shopy `/shops` | „V príprave“ | `ShopsPage` z `PlaceholderPages.tsx`. Konfiguračné API a modaly existujú inde; táto samostatná stránka nie je hotová. |
| Nastavenia `/settings` | AI a prevádzkové nastavenia skladu | `/settings/stock`: predvolené hodnoty skladu, odchýlky e-shopu, intervaly a výslovné režimy automatického spracovania. `/settings/ai-content` zachováva AI nastavenia. |
| Publikovanie `/stock/publication` | Kontrolované zosúladenie vybraných SKU počas údržby | Trvalá blokácia lokálneho skladu, porovnanie presných variantov, jedno odoslanie a obnova nejasného výsledku. Serverový zápis je predvolene vypnutý; pozri [návod](docs/stock-publication.md). |
| AI obsah | Implementovaný samostatný workflow | Pravidlá, profily, príprava, kontrola, náhľad importu a aktualizácia vybraných polí. Limity v `docs/ai-content.md`. |
| Kontrola objednávok `/orders` | Chránený čítací audit | Jedna stránka objednávok, klasifikácia identity a kandidát na operáciu. Nevytvára rezerváciu ani skladový výdaj; používa spoločné prihlásenie alebo operátorský token. |
| Skladové spracovanie `/orders/stock` | Kontrolované rezervácie, uvoľnenie a jednorazový výdaj | Jedna čerstvo načítaná objednávka, potvrdené stavové ID a pevný začiatok evidencie pre každý e-shop. Náhľad a výslovné zaúčtovanie; bez automatického prechodu histórie, pollingu či zápisov do e-shopov. Len celé kusy. |
| Automatický zber `/orders/inbox` | Zapínateľný čítací worker, trvalý inbox a obnova | Hlavičky objednávok od potvrdeného začiatku evidencie, kontrolné prechody a viditeľné chyby. Samotné načítanie nemení rezervácie ani zásobu; otvorenie objednávky vedie na čerstvý potvrdený postup. |
| Návrh zásob v `/orders/inbox` | Čítacia projekcia 1–100 SKU | Vlastné voľné celé kusy z potvrdeného skladu, presné mapovanie jednotlivých variantov vrátane rodiča xTrek. Neznámy stav nie je nula. Bez outboxu a externého odosielania. |
| Automatické skladové spracovanie | Voliteľný samostatný worker a trvalá fronta | Režimy ručne / rezervácie / rezervácie a výdaj, nové objednávky od výslovnej aktivácie, opakovanie nedostatku a kontrola vydaných objednávok. Predvolene vypnuté. |
| Dostupnosti a prenos `/settings/availability` | Oddelené dodávateľské a e-shopové plánovanie | Intervaly a ručné spustenie, platnosť dodávateľských údajov, predvoľby skladu a výnimky e-shopu, výsledky a obnova. Nové plánovanie je vypnuté. [Návod](docs/availability-sync.md), [dodávateľský kontrakt](docs/supplier-availability.md), [pravidelný prenos](docs/stock-sync.md). |
| Inventúry a nové vratky | Širšia agenda zostáva plánovaná | Jednotlivá zdokumentovaná korekcia množstva a existujúce fyzické vratky/nákupné vrstvy sú dostupné; neznamenajú kompletnú inventúrnu agendu. |
| Produkty `/products` | Kompatibilné presmerovanie do Skladu | Jediný editor je na `/stock`, bez duplicitnej položky navigácie. [Postup](docs/product-editor.md). |
| Nákupné ceny `/settings/purchase-costs` | Samostatný zapínateľný prenos FIFO cien | Nastavenia skladu/e-shopu, ručné a pravidelné spustenie, výsledky a neisté zápisy. Produkt používa ďalšiu FIFO vrstvu; objednávka vážený náklad svojich vydaných kusov. Staršie doložené objednávky cez konkrétny náhľad. [Postup](docs/fifo-purchase-costs.md). |
| Nákupné ceny v detaile produktu | Vrstvy, výdaje, vratky a opravy ceny | Kontrolovaný prechod starých zásob, príjem bez faktúry, karanténa, uvoľnenie, opravy s históriou. [Postup](docs/fifo.md). |
| Účty `/login`, `/settings/users` | Implementované meno/heslo a trvalé relácie | Správca spravuje účty, obsluha používa doterajšie chránené funkcie. Operátorský token zostáva pre prvé prihlásenie a integrácie. Úplné rozdelenie prevádzkových oprávnení je plánované. [Postup](docs/user-accounts.md). |

Zdroj navigácie: [App.tsx](frontend/src/App.tsx), [exporty stránok](frontend/src/pages/index.ts), [zástupné stránky](frontend/src/pages/PlaceholderPages.tsx).

## Architektúra a aktívne moduly

Frontend komunikuje s FastAPI cez Caddy. Relačné dáta sú v PostgreSQL; konfigurácie, zdrojové súbory a časť importných stavov vo filesysteme. AI worker, čítací objednávkový worker a oddelený worker lokálneho skladového spracovania bežia v API procese a používajú PostgreSQL. Zber vyžaduje zapnutie alebo jednorazovú požiadavku; skladová automatika vyžaduje samostatnú aktiváciu e-shopu. Prítomnosť Redis v Compose nedokazuje, že ním beží AI alebo všeobecná synchronizačná fronta.

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
| `routers/receiving_db.py`, `routers/stock.py`, `routers/stock_history.py`, `services/stock_history.py` | Príjem, čítanie skladových údajov a nemennej histórie. |
| `routers/opening_stock.py`, `services/opening_stock.py`, `services/stock_balances.py` | Počiatočný stav a spoločné transakčné zámky bilancií s príjmom. |
| `routers/order_stock.py`, `services/order_stock.py`, `order_stock_source.py`, `order_stock_ledger.py` | Potvrdená politika stavov, čerstvý zdroj objednávky, rezervácie a jednorazový výdaj. Posledné dva moduly sú v `services/`. |
| `routers/stock_settings.py`, `services/stock_settings.py`, `routers/order_processing.py`, `services/order_processing*.py` | Dedené nastavenia, samostatná autorizácia automatiky, trvalé úlohy a lokálne skladové spracovanie. |
| `routers/order_collection.py`, `services/order_collection*.py`, `services/stock_projection.py` | Chránené nastavenie, obnoviteľný čítací zber, inbox a lokálny návrh zásob bez externých zápisov. |
| `routers/stock_publication.py`, `services/stock_publication*.py` | Chránené dávky, blokácia skladu a samostatné odoslanie absolútneho stavu počas potvrdenej údržby. |
| `routers/stock_sync.py`, `services/stock_sync.py`, `services/stock_delta.py`, `services/stock_native.py` | Pravidelný `stock_increment`, kontrolné body po údržbe a evidencia natívnych objednávkových účinkov; bez automatickej aktivácie. |
| `routers/upgates_sync.py`, `services/upgates.py` | Načítanie produktov a vybrané operácie ich prenosu. |
| `services/upgates_budget.py` | Spoločný rozpočet a jedna súbežná API požiadavka na HTTPS origin naprieč procesmi, cez samostatné autocommit PostgreSQL spojenie. |
| `routers/catalog.py`, `services/catalog.py`, `services/catalog_import.py` | Katalóg, identita, náhľad a založenie produktov. |
| `routers/feed_mapping.py`, `services/feed_mapping*.py` | Uložené mapovanie feedov a zdrojová analýza. |
| `routers/product_import.py`, `services/product_import.py` | Trvalé náhľady, ručné hodnoty, AI príprava a riadené uloženie/import. |
| `services/identifiers.py`, `adapters/` | Identifikátory a dodávateľské spracovanie. |
| `routers/ai_content.py`, `services/ai_content*.py` | AI príprava, review, aktualizácie obsahu a worker. |
| `routers/invoices.py`, `routers/invoices_unified.py`, `routers/imports.py` | Faktúry a existujúce súborové/CSV postupy. |

`USE_POSTGRES=true` registruje databázový príjem, sklad, katalóg, Upgates API integráciu a AI cesty. Režim `false` používa legacy príjem; nejde o rovnocenný náhradný sklad pri výpadku DB. Nevyvodzuj automatický funkčný failover iba z logov pri štarte.

## Dátový model a úložiská

### PostgreSQL

| Tabuľky | Význam |
| --- | --- |
| `suppliers`, `supplier_feeds`, `supplier_feed_runs`, `supplier_products` | Dodávatelia a normalizované feedové dáta. |
| `product_feed_mappings`, `product_feed_mapping_revisions` | Konfigurácia a nemenné verzie mapovania podľa dodávateľa, feedu a e-shopu. |
| `product_import_drafts` | Uložená zdrojová snímka, editované hodnoty, väzby na AI a výsledok uloženia/importu. |
| `product_groups`, `products`, `product_identifiers`, `product_variant_attributes` | Rodiny, predajné položky, identifikátory a variantné atribúty. |
| `product_supply_sources` | Väzby skladovej položky na dodávateľské ponuky; používané dostupnosťami, importmi a lokálnou opravou párovania. |
| `uploaded_invoices`, `uploaded_invoice_lines` | Nahrané faktúry a položky. |
| `receiving_sessions`, `receiving_lines`, `scan_events`, `receiving_scan_requests` | Príjem a skenovanie. |
| `warehouses`, `stock_balances`, `stock_movements` | Sklady, aktuálne množstvá a nemenná história pohybov. |
| `opening_stock_batches`, `opening_stock_lines` | Uložené náhľady, pôvod, riadky a výsledky zaúčtovania počiatočného stavu. |
| `shops`, `shop_products`, `shop_product_content` | Kanály, mapovania a zachytený obsah z Upgates. |
| `availability_profiles`, `shop_product_availability` | Model dostupnosti; nie dôkaz hotovej automatizácie. |
| `shop_orders`, `shop_order_items`, `reservations`, `order_stock_policies`, `order_stock_previews` | Kontrolované skladové spracovanie objednávok, potvrdené politiky a trvalé náhľady/výsledky. Existujúce nespravované objednávky sa automaticky nepreberajú. |
| `stock_warehouse_settings`, `stock_shop_settings`, `order_processing_jobs` | Predvolené hodnoty, výslovné aktivácie a trvalá fronta lokálneho spracovania. |
| `order_collection_settings`, `order_collection_runs`, `order_inbox` | Aktivácia čítacieho zberu, kontrolné body/behy a stručné hlavičky objednávok; oddelené od skladového zaúčtovania. |
| `stock_native_orders`, `stock_native_events`, `stock_native_lines` | Auditovaný natívny skladový účinok vlastných Upgates objednávok; žiadne fiktívne lokálne príjmy alebo výdaje. |
| `stock_sync_checkpoints`, `stock_sync_baseline_events` | Revízie lokálneho/natívneho stavu a audit počiatočného zosúladenia v údržbe pre rozdielový prenos. |
| `upgates_api_budget` | Zdieľané zostatky API a odloženie požiadaviek; iba hash originu, bez prihlasovacích údajov. |
| `inventory_counts`, `inventory_count_lines`, `shop_sync_outbox` | Modely pre širšie workflow; over ich skutočných producentov a konzumentov. |
| `ai_rule_versions`, `ai_rule_state`, `ai_content_batches`, `ai_content_jobs`, `ai_content_revisions` | AI pravidlá, úlohy, náklady a história kontroly. |

Produkt nemá databázový stĺpec `primary_ean`; identifikátory sú v `product_identifiers`. ORM má kompatibilnú odvodenú vlastnosť rovnakého názvu. Podporované typy a unikátnosť over v modeli a službe identifikátorov. EAN je text a nie je jediným párovacím kľúčom.

Trvalý výber AI modelu pridáva prázdnu tabuľku migráciou 023; deployment ju vykoná po 022 a nemení pôvodné úlohy ani výber modelu.

Mapovanie a náhľady importu pridávajú aditívne migrácie 021 a 022. Obe sú zabalené v API obraze a deployment ich vykoná po 020 pred reštartom API. Nemenia staršie produkty ani pohyby a nespúšťajú automatické AI či publikovanie. Návrat kódu ponechá tabuľky a uložené náhľady; po použití nového postupu uprednostni kompatibilnú opravu vpred.

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

SK Profi Bike má validovaný adaptér textových PDF faktúr cez dodávateľské `upload-invoice` v postupe Príjem. Spracuje aj viacstranové doklady, opakované hlavičky, zalomené názvy, rôzne fakturované jednotky a zľavy vrátane bonusov; overuje strany, poradie položiek a finančné súčty. Uloží PDF, CSV s nákupnými cenami bez DPH po zľave a index, ktorý používa existujúci príjem; nevytvára vlastné skladové množstvá. Používa `pypdf`, bez SQL migrácie. Opakovaný doklad zachová stav, iný obsah pod rovnakým číslom sa odmietne. Obmedzenia formátu, chýbajúce EAN a test príjmu/inventúry sú v [receiving-identity.md](docs/receiving-identity.md#sk-profi-bike-pdf-invoices).

Finalizácia v `routers/receiving_db.py` vytvára `RECEIVING_IN` pohyby, aktualizuje `stock_balances` a podľa pravidiel môže založiť chýbajúcu položku. Má identifikačný kľúč pohybu pre reláciu a riadok, zámok relácie a bilancií a uložený výsledok pre opakovanie. Prijaté riadky bez identity alebo platnej nákupnej ceny blokujú celý príjem; nulové množstvo nevytvorí pohyb. Súborový index faktúry sa označí až po DB commite. Presný kontrakt a dočasné obmedzenie príjmu bez ceny sú v [centrálnom sklade](docs/central-stock.md).

Príjem predvolene zobrazuje zoznam položiek, známe EAN s pôvodom údaja a šípky na ručnú zmenu prijatého množstva. Neúplnú aj nulovú historickú faktúru možno výslovne dokončiť; do skladu vstupuje iba potvrdené prijaté množstvo. Zobrazenie EAN z dodávateľského katalógu je čítanie, nie automatické priradenie identity. Podrobnosti a regresie sú v [kontrakte príjmu](docs/receiving-identity.md).

Nové zásoby bez predchádzajúcich pohybov a explicitne prevedené zostatky používajú **FIFO**. Staršie zostatky zostávajú na pôvodnom váženom priemere do overeného prechodu. Voľné množstvo je fyzické mínus rezervované mínus karanténa. Neznáme ceny zostávajú `NULL`; neúplné ocenenie sa neukazuje ako nula. Podrobnosti prechodu, vrstiev a nákladov sú v [FIFO](docs/fifo.md). Kontrolované spracovanie jednotlivých objednávok rezervuje dostupné kusy, zvyšok eviduje ako nedostatok a pred výdajom vyžaduje celú zásobu pri zachovaní rezervácií ostatných objednávok. Automatické lokálne účtovanie je voliteľné pre každý e-shop; samostatný čítací zberač stále iba zachytáva hlavičky a samostatný procesor načítava celé aktuálne objednávky. Nový `SALE_OUT.total_cost` zachytáva presnú odobratú hodnotu vrátane zaokrúhlenia; historické pohyby sa nedopočítavajú. Podrobnosti sú v [centrálnom sklade](docs/central-stock.md).

`stock_movements` sa pri oprave minulosti nemajú meniť ani mazať; používajú sa korekčné pohyby. Nákupná cena, predajná cena a aktuálna feedová cena majú odlišný význam. Chránený počiatočný stav používa vlastný CSV náhľad a výslovné potvrdenie; nepoužíva množstvá ani predajné ceny e-shopu. Prvá verzia podporuje iba celé `ks`, odmieta existujúcu bilanciu aj predchádzajúci pohyb a nenahrádza historické nákupné vrstvy. Pred fyzickým otvorením treba odsúhlasiť hranicu počítania a rozpracované príjmy podľa [centrálneho skladu](docs/central-stock.md).

## Upgates: samostatné integračné cesty

Označenie „synchronizácia“ nie je zárukou rovnakého správania všetkých tlačidiel:

| Cesta | Čo robí | Hranice |
| --- | --- | --- |
| Legacy CSV | Príprava súborov, načítanie exportu e-shopu a rozdelenie importných výstupov. | Nie je priebežnou API synchronizáciou celého skladu. |
| Upgates → Hub | Náhľad a načítanie produktov, variantov, mapovaní a obsahu; fyzický sklad ani obstarávacie ceny nemení. | `include_stock=true` je odmietnuté; párovanie používa kanálové prepojenie, spoločné SKU a overené čiarové kódy. Rozdielni parenti vrátane pokladňového „xTrek“ sú podporovaní. Obnova zachováva spoločné produktové údaje a môže doplniť chýbajúce overené EAN. Konflikty sa zobrazia; počiatočný stav je samostatná chránená operácia. |
| Hub → e-shop cez `push_products_to_shop` | Prenos vybraných produktov zo zachyteného obsahu. | Vyžaduje výber všetkých prenášaných variantov a preskakuje už namapované položky v cieli; nejde o všeobecnú aktualizáciu existujúcich produktov alebo automatickú stock sync službu. |
| Dodávateľský katalóg → e-shop | Výber, náhľad a založenie nových produktov cez API, skrytých a označených `validation_required=1`. | Neaktualizuje existujúce produkty a neposiela vlastné skladové množstvá. |
| Upgates objednávky → inbox | Zapínateľný pravidelný zber hlavičiek so začiatkom podľa skladovej politiky. | Samotný zber nemení sklad. Voliteľný procesor má vlastnú aktiváciu a pravidlá. Stránkovanie nie je snapshot; prekrývanie a kontrolné prechody zlepšujú zachytenie zmien. |
| Lokálny návrh vlastných zásob | Voľné množstvo potvrdeného skladu a samostatné mapovanie vybraných variantov. | Žiadny PUT, outbox, zmena dostupnosti či viditeľnosti. Vyžaduje úplné účtovanie objednávok pred budúcim publikovaním. |
| AI aktualizácia existujúceho produktu | Porovnanie pred/po a aktualizácia povolených polí s kontrolou identity a výsledku. | Všeobecná aktualizácia obsahu neposiela ceny, vlastné množstvo, aktivitu, identifikátory ani nové varianty. |

Podmienky náhľadov, potvrdenia, kontrol identity, stavu `uncertain` a obnovy sú v [katalógovej](docs/supplier-catalog.md) a [AI dokumentácii](docs/ai-content.md). Existuje aj osobitná obmedzená AI cesta aktualizácie dodávateľskej dostupnosti; jej podmienky neplatia automaticky pre všetky produkty a varianty.

Dodávateľská zásoba je oddelená od našej. `6+` je dolná hranica, nie presný počet. Obnova katalógu a zalistovanie z feedu nevytvárajú fyzický príjem. Texty dostupnosti vlastní `adapter_settings.availability` dodávateľa, s predvolenými `do 5 dní` a `overíme`. AI pravidlá ich neprepisujú; `overíme` je objednateľné. Zachovaj čerstvosť údajov, neznáme hodnoty a pravidlá konkrétneho integračného kontraktu.

Sloger podporuje jeden katalóg z viacerých značkových XML zdrojov (`feeds.sources.products.remote.urls`), s atómovou obnovou a filtrovaním výrobcu. Natívny parser zachováva explicitné varianty a blokuje kolidujúce kódy aj medzi značkami. DMOC je predajná cena s DPH; chýbajúca nákupná cena sa do Upgates neposiela. Karta **Ceny** v nastavení dodávateľa upravuje koeficienty značiek; `1` cenu nemení. Bez nastavenia sa predvolené koeficienty Paul Lange nepoužijú na iného dodávateľa. Zmena nepotrebuje migráciu a sama nezapína dodávateľa ani B2B zber. Konfigurácia a overovanie sú v [dodávateľskom katalógu](docs/supplier-catalog.md#sloger-listing-feeds).

Značkové GET zdroje Sloger sa sťahujú postupne s odstupom jednej sekundy. Pri HTTP 429 sa zdroj skúsi najviac trikrát so zohľadnením `Retry-After`; čakanie dlhšie než 60 sekúnd ukončí obnovu. Časové a veľkostné limity aj zachovanie posledného úspešného katalógu ostávajú v platnosti.

SK Profi Bike (`sk-profi-bike`, prefix `SKP-`) používa natívny XML parser rodičov a variantov, plný produktový aj zjednodušený skladový feed. Nákupná ponuka `voc`/`voc_akcia` je bez DPH, MOC je s DPH; predvolený koeficient je 1. Feedové údaje vstupujú do spoločnej editovateľnej prípravy s voliteľným AI. Existujúce spoločné produkty si ponechajú SKU. Neznáme alebo záporné dodávateľské množstvo sa neprevedie na nulu. Samostatné API objednávok nie je potrebné ani aktivované. Podrobnosti sú v [katalógovej dokumentácii](docs/supplier-catalog.md#sk-profi-bike-listing-feeds).

## AI obsah

Úplné súkromné pravidlá možno pripraviť offline nástrojom `ai_rule_package`; nejde o publikovanie do databázy. Rozsahy, obmedzenia a postup sú v [AI dokumentácii](docs/ai-content.md). Overený návrh prepojenia príjmu s existujúcim importom a prenosom vybraných SKU je v [príjem → Upgates](docs/receiving-upgates-plan.md); samotné prepojenie zostáva plánované.

AI workflow používa existujúci katalóg/importer a vlastné DB tabuľky. Worker beží v API procese s databázovými zámkami. Podporuje prípravu z feedu, existujúce produkty, pravidlá a kategórie, odhad nákladov, revízie a kontrolované odoslanie výsledku.

Oficiálny prieskum môže vyhľadávať weby výrobcov/dodávateľov bez povinného prednastaveného zoznamu domén. Vyhľadávanie nezaručuje dostupnosť správnej hodnoty a chýbajúce povinné parametre naďalej vyžadujú vyriešenie.

AI, audit, skladové spracovanie a ostatné doteraz tokenom chránené funkcie používajú spoločný `access.py`: prihlásený účet s HttpOnly reláciou alebo pôvodný operátorský token. [Účty](docs/user-accounts.md) pridávajú iba rozdiel medzi správcom účtov a obsluhou, nie úplné prevádzkové RBAC. AI model sa dá trvalo vybrať v **Nastavenia → AI obsah produktov → Model a náklady**; serverové `AI_CONTENT_MODEL` je predvoľba, kým sa výber neuloží; bez explicitnej konfigurácie kód používa lacnejší `gpt-6-luna`. Nasadenie neprepisuje uložený model ani serverové nastavenie. Nové úlohy si model zmrazia, existujúce sa neprepnú. Ostatná konfigurácia zostáva `AI_CONTENT_ENABLED`, `AI_CONTENT_ACCESS_TOKEN`, `OPENAI_API_KEY`, `AI_CONTENT_MONTHLY_USD`, `AI_CONTENT_JOB_USD`. Tajné hodnoty do dokumentácie ani logov nepatria.

Nové AI prípravy rozlišujú umiestnenie v kategórii a odborný profil pravidiel. Jednoznačné mapovanie použije profil bez klasifikačného volania; pri zadanej kategórii bez mapovania AI vyberie iba profil. Bez kategórie vyberie koncovú kategóriu aj profil. Ručný odborný profil sa zachová. Pôvodný importer doplní produktových predkov. Rozpočet zahŕňa obe fázy a žiadna sa po nejasnom výsledku automaticky neopakuje. Výsledky sa otvárajú v modálnom okne. Kontrakty, ručné výnimky a aktuálne limity sú v [docs/ai-content.md](docs/ai-content.md).

## Databázové migrácie

V [infra/db-init](infra/db-init) sú tieto SQL súbory:

| Súbor | Úloha |
| --- | --- |
| `001_schema.sql` | Základná relačná schéma, typy a databázové pravidlá. |
| `002_invoice_management.sql` | Rozšírenia fakturačnej evidencie. |
| `003_uploaded_invoices.sql` | Nahrané faktúry a položky. |
| `004_shop_product_content.sql` | Kompletný obsah produktov podľa e-shopu a parent kódu. |
| `005_ai_content.sql` | AI pravidlá, dávky, úlohy a revízie. |
| `006_opening_stock.sql` | Dávky a riadky kontrolovaného počiatočného stavu; nemení existujúcu zásobu. |
| `007_order_stock.sql` | Politiky, náhľady a stav skladového spracovania; presný náklad nového výdaja. Neznáme predajné ceny a mena objednávky smú byť `NULL`. Žiadny historický výdaj ani rezervácia sa nevytvorí migráciou. |
| `008_order_collection.sql` | Aktivácia zberu, trvalé behy a inbox hlavičiek. Migrácia nezapína e-shopy a nemení fyzický sklad. |
| `009_stock_automation.sql` | Dedené prevádzkové nastavenia, jednorazové načítanie a trvalá fronta spracovania. Predvolene ručný režim; žiadne spätné zaúčtovanie. |
| `010_stock_publication.sql` | Kontrolované publikovanie, dávky a trvalé skladové blokácie. Zápis do e-shopov zostáva predvolene vypnutý. |
| `011_fifo.sql` | FIFO vrstvy, alokácie výdajov, prechody, príjmy, vratky, karanténa a opravy ceny. Nullable ocenenie a prepočet generovaného voľného množstva; nezakladá historické vrstvy. |
| `012_product_editor.sql` | Ručné produktové overrides, revízie, uložené výsledky dávok a audit; prirodzené radenie kódov. |
| `013_receiving_scan_requests.sql` | Atómové potvrdenia UUID skenov a rozšírenie compound EAN riadka na 255 znakov. Obnovuje iba odvodený fingerprint a známy faktúrový view so zachovaním definície a prístupov, v transakcii bez CASCADE; nemení pohyby ani nastavenia. |
| `010_stock_publication.sql` | Politiky odosielania, trvalé blokácie skladov, dávky a položky s auditom jedného pokusu. Bez aktivácie odosielania či zmeny zásob. |
| `024_upgates_api_budget.sql` | Spoločná evidencia API rozpočtu; bez aktivácie skladových zápisov alebo nákupu kvóty. |
| `025_stock_native.sql` | Natívne objednávkové účinky s auditom a kontrolou pokrytia; nemení fyzickú skladovú históriu. |
| `026_stock_delta.sql` | Rozdielový protokol, kontrolné body po údržbe a zmrazené podklady pokusov; bez prevzatia starého oprávnenia či automatickej aktivácie. |
| `027_product_links.sql` | Trvalé náhľady a výsledky prepojenia existujúcich kariet s e-shopmi. |
| `028_supplier_card_links.sql` | Trvalé náhľady a výsledky ručného priradenia dodávateľskej položky k existujúcej karte. |

**Deployment spúšťa `005` až `028` v jednom API kontajneri** cez `python -m inventory_hub.migrate`. [Spúšťač](api/inventory_hub/migrate.py) volá moduly v explicitnom poradí; zachováva ich transakcie a DB zámky. `020_user_sessions.sql` vytvára účty, relácie a limity prihlasovania cez [user_sessions_migrate.py](api/inventory_hub/user_sessions_migrate.py). Chyba migrácie preruší nasadenie pred reštartom API; už dokončené migrácie zostanú potvrdené. [API Dockerfile](api/Dockerfile) všetkých dvadsaťštyri upgrade SQL súbory balí do obrazu. Spúšťač nehľadá nové číslované SQL automaticky. Nové `024`–`026` sú aditívne a neaktivujú synchronizáciu.

Adresár `/docker-entrypoint-initdb.d` v referenčnom Compose inicializuje nové databázové úložisko; automaticky neaktualizuje existujúce. Pred upgrade over aplikovanú schému a priprav postup iba pre potrebné chýbajúce zmeny. Pridanie ďalšieho SQL súboru bez zmeny migračného postupu samo nespôsobí jeho vykonanie pri deployi.

Pri čistej lokálnej inštalácii over postupnosť `001`–`028` v izolovanej DB a ukončenie pri SQL chybe. Historická úplná inicializačná cesta nebola počas tejto aktualizácie spustená; izolované CI testy samy nepotvrdzujú celý produkčný bootstrap. Už nasadené migrácie sa spätne neprepisujú.

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

Overovacie príkazy sú v [AGENTS.md](AGENTS.md) a [CI](.github/workflows/ci.yml): kompilácia a `unittest`, frontendový build a jsdom sady vrátane počiatočného stavu, skladového spracovania, čítacieho zberu a návrhu zásob. DB testy preverujú migrácie, rollback, zmeny objednávky, konkurujúce rezervácie, opakovanie výdaja aj súbeh s príjmom. Používajú samostatný lokálny PostgreSQL a `CATALOG_TEST_DATABASE_URL` s názvom DB končiacim `_catalog_test`. Ak sa DB prípady preskočia, uveď to. Interakčné testy neoverujú vizuálny layout v reálnom prehliadači.

CI rozdelí celý backendový zoznam do štyroch paralelných skupín podľa trvania modulov. Každá má vlastný PostgreSQL; záverečná povinná kontrola `backend-compile` odmietne chýbajúce, duplicitné aj preskočené testy. Váhy nemenia výber testov. Súhrny ukazujú najpomalšie testy a čas ich prípravy, vykonania a upratania; JSON reporty zostávajú dostupné 14 dní. Novší commit zruší zastarané kontroly rovnakého PR. Podrobný kontrakt je v [CI a nasadzovaní](docs/ci-deployment.md).

## Produkčné nasadzovanie a diagnostika

Autoritatívny postup je v [build.yml](.github/workflows/build.yml):

1. Push do `main` s aspoň jednou zmenou mimo `**/*.md` a `docs/**`, alebo ručne spustený workflow na `main`, zostaví API a frontend paralelne. Obrazy používajú oddelené cache `api` a `frontend`. Bežné nasadenie publikuje do GHCR tagy `main` a `sha-<commit>`.
2. Deploy job sa pripojí na server a pracuje v `/opt/inventory-hub`.
3. Pripraví Compose overlay pre chránený súbor `ai-content.env`; jeho vytvorenie neznamená vyplnené AI prístupy.
4. Cez `docker compose pull` stiahne obrazy, aplikuje migrácie `005` až `028` jedným spúšťačom v jednom kontajneri a obnoví služby `api`, `frontend-build` a `caddy` cez `--force-recreate` s overlayom.
5. Overuje verejné `/api/health` a `/api/ai-content/status` so spoločným limitom 60 sekúnd. Požiadavky majú najviac 5 sekúnd a kontrola sa pri chybe opakuje s krátkou pauzou; úspech vyžaduje HTTP 200 z oboch ciest v jednom kole. Log obsahuje HTTP stav a návratový kód curl, nie telá odpovedí či prístupy. Neúspešná kontrola ukončí deployment chybou; čistenie nepoužívaných obrazov nasleduje iba po úspechu.

Workflow je serializovaný s vypnutým rušením rozbehnutého nasadenia. Zmeny iba Markdown súborov alebo `docs/**` produkčný workflow nespúšťajú, úplné PR kontroly však zostávajú. Ručné spustenie na `main` s `deploy=false` slúži na meranie zostavenia/cache: publikuje iba SHA tag, nemení produkčný tag `main` a nepripája sa na produkčný server. Predvolená hodnota je `deploy=true`.

Actions súhrn zobrazuje namerané časy zostavenia a nasadenia; deploy log samostatne meria sťahovanie, migrácie, reštart, pripravenosť a čistenie. Každá migrácia vypíše aj vlastný čas. Východiskové meranie a interpretácia sú v [CI a nasadzovaní](docs/ci-deployment.md). Platnosť oprávnenia na merge a živé zásahy rieši `AGENTS.md`; existujúci súhlas sa neopakuje, ale samotný návrh nie je pokynom na nasadenie implementácie.

[infra/docker-compose.prod.yml](infra/docker-compose.prod.yml) je referencia. Server používa vlastný `docker-compose.yml` a deploy pridáva `docker-compose.ai-content.yml`; ich celý aktuálny obsah nebol touto kontrolou repozitára overený. Pred manuálnym zásahom over nasadené súbory a oprávnenia. Starý návod cez `docker compose down` a `up -d` nevystihoval tento postup a nie je štandardným krokom bežného nasadenia.

| Referenčná položka | Význam |
| --- | --- |
| `/opt/inventory-hub` | Serverové Compose súbory a konfigurácia. |
| `/opt/inventory-data` | Aplikačné súbory na hostiteľovi. |
| `/data/inventory-data` | Pripojenie dát v API kontajneri. |
| `postgres`, `redis`, `api`, `frontend-build`, `caddy` | Služby referenčnej zostavy. |
| `ROOT_PATH=/api` | Prefix API za reverse proxy. |
| `ai-content.env` → `/run/secrets/hub-ai.env` | Chránená AI konfigurácia pripojená overlayom. |
| Docker volume `supplier_secrets` → `/var/lib/inventory-hub-secrets` | Trvalé prihlásenie Sloger, iba API; adresár `0700`, súbor `0600`, mimo verejného `/data`; zachovať pri obnove aj rollbacku. |

Diagnostiku začni výsledkom Actions a relevantnými jobmi. [Swagger](https://hub.biketrek.sk/api/docs), [ReDoc](https://hub.biketrek.sk/api/redoc) a [OpenAPI JSON](https://hub.biketrek.sk/api/openapi.json) opisujú nasadenú API verziu, pokiaľ sú dostupné. Logy a detaily kontajnera vyžadujú skutočný serverový prístup; bez neho ich stav netvrď ako overený.

Návrat kódu rieš kontrolovaným revert PR a bežným nasadením. Revert nevracia DB ani už vykonané Upgates zápisy. Taká zmena potrebuje osobitný postup pre dáta a kontrolu spätnej kompatibility.

## Ďalšie kroky centrálneho skladu

Lokálne rezervácie/výdaje, FIFO, editácia produktov a odoslanie podporovaných vybraných polí sú implementované; prevádzkové zapnutie a overenie každého kanála zostáva samostatným krokom. Zostáva najmä:

- rozšírenie podporovaných editovateľných polí a používateľské overenie publikovania na konkrétnom variante;
- kompletné tržby bez DPH, zľavy a refundácie pre výpočet hrubej marže; FIFO zatiaľ poskytuje náklady;
- historické vratky bez FIFO alokácií, odpis poškodeného tovaru, všeobecné merné jednotky a roly;
- prevádzkové zosúladenie objednávok vrátane tvrdých zmazaní a pilot pravidelného prenosu zásob s jednotným rozsahom vybraných SKU.

Po aktivácii FIFO sa nemožno vrátiť k starému kódu váženého priemeru bez kontrolovaného plánu. Ponechaj nové tabuľky aj nemenné pohyby a použi opravu vpred; podrobnosti v [FIFO](docs/fifo.md).

Kontrolované údržbové publikovanie absolútneho množstva je v [stock-publication.md](docs/stock-publication.md); pravidelný prenos používa [rozdielový protokol `delta_v1`](docs/stock-sync.md). Kontrolný bod po výslovne potvrdenej údržbe ukladá lokálne množstvo a auditovaný natívny účinok konkrétneho e-shopu. Prenos `stock_increment` odčíta natívnu zmenu od lokálnej, aby sa vlastná objednávka neodpočítala dvakrát. Neistý PUT sa neopakuje a samotná zhoda aktuálneho množstva ho nevyrieši. Údržbová lokálna blokácia sama nezatvára predaj. Natívne audity, kontrolné body a zámery sa nemažú; návrat k starému absolútnemu pravidelnému zapisovateľovi nie je bezpečný rollback.

Nepodporované alebo nepreukázané natívne objednávkové účinky blokujú bežný prenos. Inkrementálny zber zachytáva aj čerstvé zmeny objednávok vytvorených od roku 1900, ale historické fyzické účtovanie automaticky neprehráva. Živý objednávkový cyklus nového protokolu ešte nie je potvrdený a implementácia neaktivuje produkčné zápisy. [Presné hranice, kapacitný model a pilot](docs/upgates-sync-readiness.md) odlišujú kód, historické čítacie merania a overenie prevádzky.

Nezmenený kontrolný bod a dostupnosť potrebujú 0 vzdialených volaní medzi šesťhodinovými auditmi. Potrebné čítania zostávajú dávkové; model plne zmeneného priechodu je `G + K` (skupiny GET + jednotlivé PUT), bez okamžitého kontrolného GET. Všetky relácie `UpgatesClient.from_shop` navyše zdieľajú limit jednej súbežnej požiadavky na HTTPS origin naprieč procesmi, hodinový/denný rozpočet a `Retry-After`. PostgreSQL výpadok blokuje odoslanie; iba výslovný legacy režim má slabšiu procesnú ochranu. Cudzie integrácie nie sú riadené a kvóta sa automaticky nekupuje.

## Nové oddelené workflow (014–017)

Dodávateľský worker `supplier_availability_worker` uchováva prijaté pozorovania a vyhodnocuje ich platnosť. Zlyhanie alebo neúplný feed nikdy nevynuluje fyzickú zásobu. `stock_sync_worker` prenáša vlastné voľné kusy, dostupnosť a objednateľnosť iba pri výslovne nastavenej skladovej autorite, čerstvých objednávkach a povolenom serverovom zápise `STOCK_SYNC_WRITE_ENABLED` (default false). Údržbový publisher zostáva samostatný.

Pravidelný prenos odlišuje čakajúcu objednávku od nespracovaného skladového podkladu. Zaúčtovaná nekrytá rezervácia neblokuje celý sklad, ak čítací prepočet potvrdí rovnaké alokácie; nový príjem sa najprv pridelí čakajúcej objednávke. Pre vratky a storná po výdaji je v objednávkovom inboxe samostatné potvrdenie skladovej výnimky s nemenným auditom. Nevytvára automatickú vratku a pri zmene podkladu sa zneplatní. Podrobnosti: [prenos zásob](docs/stock-sync.md), [objednávky](docs/order-workflows.md). Táto oprava neaktivuje živú synchronizáciu ani nepotvrdzuje pilot na reálnych objednávkach.

Migrácie `014_supplier_availability.sql`, `015_stock_sync.sql`, `016_product_publication.sql` a `017_stock_adjustments.sql` sú aditívne, zabalené v API obraze a zapojené do deploymentu po `013` pred spustením nového API. Nezapínajú nové plánovanie ani hromadné zápisy. Príslušné zámery, nejasné pokusy a účtovné výsledky sa pri návrate verzie nemažú; pre riešenie nasadenej chyby uprednostni kompatibilnú doprednú opravu.

## Dodávateľské prefixy a väzby dostupností

Etapa `codex/supplier-prefix-availability` zjednocuje prefixy a dopĺňanie dodávateľských väzieb. Nový produkt používa spoločné SKU `prefix + kód variantu dodávateľa`, ale jeho nemenná identita zostáva interné produktové ID. Použitý prefix chráni trvalý filesystem register; existujúce neprázdne prefixy sa konzervatívne uzamknú. Zmena nemení staré SKU ani skladovú históriu.

`services/supplier_links.py` prepája iba jednoznačné lokálne identity. Chránené `POST /supplier-availability/{supplier}/links/reconcile` a tlačidlo **Prepojiť dostupnosti** doplnia väzby aj z už prijatých dát bez nového sťahovania či Upgates volaní. Skladová tabuľka rozlišuje chýbajúcu väzbu a konflikt od chýbajúceho alebo starého pozorovania. Nová SQL migrácia nie je potrebná; etapa nezapína prenos zásob ani cien. Podrobný kontrakt: [supplier-identity.md](docs/supplier-identity.md). Nasadenie a testy sú vedené samostatne v `docs/mvp-progress.md`.

AI content context v2 adds category reference-product snapshots (read-only Upgates preview followed by versioned rule publication), frozen high reasoning for new Luna jobs, usage reasoning-token details and bounded Paul Lange technical-table/document sources. See `docs/ai-content.md` for selection, snapshot compatibility and the remaining single-category-branch limit. No schema migration is required.
AI zadanie verzie 3 zoskupuje parametre vzorových produktov do výstupnej schémy. Nové duše zlučujú iba opakované skupiny štyroch schválených rozmerových parent parametrov, bez nových hodnôt; ostatné konflikty a variantové kontroly zostávajú. Podrobnosti sú v `docs/ai-content.md`.
