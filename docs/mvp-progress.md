# MVP — rozhodnutia a stav pokračovania

Aktualizované 24. 9. 2026. Vlastník odsúhlasil implementáciu podľa odpovedí k 30 bodom a N1–N8. Základ aktuálnej etapy: `main 6bdfd71` (nasadený PR #34). Tento súbor odlišuje rozpracovanie, overenie a nasadenie; plán nie je hotová funkcia.

## Platné rozhodnutia

- Spoločný kód variantu identifikuje fyzický produkt v oboch e-shopoch. Pokladňový parent xTrek v BIKETREK je iba technické zoskupenie.
- Desať nových skenov rovnakého EAN pridá desať kusov. Retry jedného request ID nepridá nič; rovnaký EAN sa časovo neblokuje.
- Nákupné ceny zostávajú podľa príjmov; výdaj oceňuje najstaršie nevyčerpané príjmy. V bežnom UI použiť Nákupné ceny/Príjmy/Náklad predaja, nie skratku FIFO. Technické názvy zostávajú.
- Priorita: jednotná identita, používateľská tabuľka, nemenná história pohybov za celý sklad aj variant, dodávateľské dostupnosti, pravidelný prenos a hrubá marža.
- Tabuľka: nastaviteľná šírka/poradie/viditeľnosť stĺpcov, uložené preferencie v prehliadači, pomenované variantné parametre, viac obchodných polí. Finálny vizuálny redesign neskôr.
- Každá dátová akcia má ukázať účinok Hub čítanie/zmena alebo Upgates čítanie/zápis, vrátane nadväzujúcej úlohy. Počet API volaní iba meraný alebo podložený odhad; inak neznámy. Lokálny browser request nie je počet Upgates volaní.
- Real-time nie je podmienkou MVP. Intervaly ponechať pre test podľa aktuálnej konfigurácie, neskôr môže používateľ nastaviť 15 minút aj dlhšie. Neaktivovať publikovanie ani meniť live intervaly migráciou.
- Metráž/balenia, účty/RBAC, nové vratky/odpisy, zálohy a N1–N8 odložené. Manuálne rezervácie a veľká inventúrna agenda nižšia priorita. Existujúce ochrany a funkcie zachovať.
- Manuálne bezkódové riadky sú mimo skladu. Odoslaná/Vyzdvihnutá znamená jeden celý uzamknutý výdaj; žiadne čiastočné odovzdanie. Opravy pohybov kompenzáciou, nie editáciou histórie.
- Dostupnosť: vlastná voľná zásoba SKLADOM; čerstvá dodávateľská ponuka konfigurovaná lehota (default do 5 dní); inak objednateľné overíme. Nezamieňať dodávateľský a vlastný sklad.
- Ostré prevzatie spoločného skladu prebehne spolu s vlastníkom po pilote.

## Implementačné balíky

| Balík | Obsah | Stav |
| --- | --- | --- |
| A | Identita príjmu/importov, request ID skenera, opravy prehľadu, označenie akcií a história pohybov | Nasadené cez [PR #34](https://github.com/mpriesol/inventory_hub/pull/34); CI 658 backend testov vrátane PostgreSQL bez preskočenia, build a 14 UI sád; deployment 35989256455 a zdravá DB overené |
| B1 | Šírky/poradie/stĺpce tabuľky, variantné parametre a práca so skupinami | Implementované súbežne s A; interakčné testy prešli |
| B2 | Editor → náhľad → publikovanie vybraných polí do konkrétneho shopu | Implementované v PR #35; overenie a nasadenie podľa záznamu nižšie. Samotné uloženie zostáva lokálne |
| C | Scheduler feedov, čerstvosť, dostupnosti, automatický dávkový skladový prenos a porovnávanie | Implementované v PR #35; prevádzková aktivácia je samostatný krok, predvolene vypnutá |
| D | Dokončenie jednoduchého príjmu/dokladov, skutočné tržby a hrubá marža, malý servisný výdaj | Čaká; nákladové vrstvy už existujú |
| E | Pilot aktuálneho rozsahu a spoločný prechod | Čaká |

## Pokračovanie po prerušení

1. Over aktuálny remote main, otvorený PR a pracovnú vetvu; zachovaj rozpracované zmeny.
2. Prečítaj tento súbor, AGENTS, OVERVIEW a dokumentáciu dotknutých modulov.
3. Doplň výsledky testov a číslo PR/commit až po skutočnom vykonaní. Nenasadené funkcie neoznačuj ako dostupné na produkcii.
4. Dokonči prvý súdržný balík, over ho v CI s PostgreSQL a cez relevantné UI testy, potom merge/deployment podľa už udeleného oprávnenia.
5. Ďalšie balíky rieš postupne v samostatných PR. Nasadenie aplikácie nie je aktivácia skladovej autority ani povolenie hromadnej zmeny živých zásob.

Prvý balík používa migráciu `013` pre idempotentné požiadavky skenera; je zapojená do obrazu a deploymentu pred reštartom API. Záznam o overení a nasadení sa doplní pri dokončení.

## Overenie prvého balíka (24. 9. 2026)

- Lokálny backend po oprave migrácie: 658 testov, bez zlyhania; 251 PostgreSQL prípadov preskočených, keďže lokálna izolovaná DB nie je dostupná. Pred merge musia prejsť v PR CI.
- Vite build a všetkých 14 jsdom sád prešli. Nové regresie skenera navyše overujú prerušené telo HTTP 200, nesúlad UUID, prepínanie relácií počas požiadavky a oneskorený súhrn. Neoverený výsledok zachová pôvodnú požiadavku na retry.
- Nezávislá kontrola našla a oprava pokryla dlhé compound EAN pri ručnej úprave množstva a rozdiel ORM defaultu `None` od explicitne neaktívneho produktu. Objednávkové očakávania zostali zachované.
- Migrácia 013 je zabalená v API obraze a spúšťa sa pred reštartom. Neaktivuje žiadny worker, publikovanie ani nové intervaly. Pri chybe neznámej závislosti sa transakcia vráti späť; potvrdenia skenov sa pri oprave nemažú.
- Reálny vizuálny layout v prehliadači nebol overený. Celkový `tsc` má existujúce chyby aj v starších kópiách; build a cielené interakčné kontroly nie sú tvrdenie o čistej globálnej typovej kontrole.
- [PR #34](https://github.com/mpriesol/inventory_hub/pull/34), prvý CI beh `35986870111`: frontend/build a 14 UI sád prešli; backend 630 z 655 prešlo, 25 príjmových testov zastavila závislosť existujúceho view `v_invoice_lines_detail` na rozširovanom EAN. Oprava migrácie obnovuje tento známy odvodený pohľad v transakcii; tri ďalšie testy overujú existujúce údaje, práva, opakovanie a rollback pri neznámych závislostiach. Opakované CI a deploy musia byť overené pred označením balíka za nasadený. Aktuálny výsledok je pri PR a jeho následnom main workflow; tento záznam zachytáva stav pred merge.

## Aktuálna požiadavka vlastníka (24. 9. 2026)

Vlastník presunul **dodávateľské dostupnosti a pravidelný prenos zásob** pred B2. Súbežne požaduje opravu odchodu z dokončeného príjmu, prázdnych parametrov, jednu tabuľku Sklad/Produkty s obrázkami a stĺpcami oboch e-shopov, manuálnymi bunkami a odoslaním. Pôvodné poradie B2→C už neplatí.

[PR #35](https://github.com/mpriesol/inventory_hub/pull/35), vetva `codex/supplier-sync-unified-stock`: scheduler feedov a čerstvosť, samostatný pravidelný publisher s dedičnými intervalmi a ručným spustením, jednotná tabuľka, vybrané produktové publikovanie a dokumentované korekčné pohyby. Migrácie 014–017. Nasadenie tejto etapy a overenia budú doplnené podľa skutočných výsledkov PR; prítomnosť súborov nie je dôkaz nasadenia.

Naďalej platí automatické schvaľovanie/merge PR a následné nasadenie až do odvolania, výslovne znovu potvrdené vlastníkom. Neaktivuje to samo osebe ostrú skladovú autoritu, dodávateľské intervaly ani hromadné živé zápisy.

Dodatočné kontroly: spoločný trvalý zámok AI a ručného produktového odosielania, výslovné uzavretie neistého AI pokusu, Unicode kolízie SKU a audit prvého fyzického počtu 0 pre dodávateľské produkty. Lokálne prešli build a všetkých 16 UI sád. Databázové scenáre sa overujú v PR CI s PostgreSQL; presný posledný výsledok a nasadenie sú v PR.

Prvý CI beh `36004823470`: frontend prešiel; backend odhalil tri regresie importu parametrov a tri chyby pokračovania synchronizácie po rollbacku položky. Oprava obnovila odmietnutie neplatnej rodiny, obmedzila dopĺňanie kanonických parametrov na presnú zhodu SKU a uchováva identifikátor behu mimo expirovaného ORM objektu. Pôvodné regresné očakávania zostali zachované. Tento výsledok nie je schválením nasadenia; rozhoduje opakované CI opraveného commitu.

## Nákupné ceny produktov a objednávok (24. 9. 2026)

PR #35 bol následne nasadený ako `d8b44e2`; CI `36006570205` prešlo 731 backendových testov vrátane PostgreSQL, build a 16 UI sád. Deployment `36007429688` potvrdil migrácie 014–017 a zdravú databázu.

Vlastník potom výslovne požiadal implementovať odosielanie FIFO nákupných cien. Vetva `codex/fifo-purchase-cost-sync` dopĺňa produktovú cenu najbližšieho kusa po príjme/výdaji a vážený priemer nákladov konkrétnych vydaných kusov v objednávke. Samostatný náhľad umožňuje opravu staršej objednávky z jej pôvodných FIFO alokácií bez nového výdaja. Migrácia 018 je aditívna a predvolene nič neaktivuje. [Používateľský a vývojársky postup](fifo-purchase-costs.md).

Nasadenie a posledné výsledky tejto etapy treba overiť v jej PR/Actions; tento záznam vzniká pri implementácii a sám ich nepotvrdzuje. Oprava dodávateľského párovania diagnostikovaná po PR #35 zostáva samostatná téma; tento balík ju neoznačuje za vyriešenú.

## Prefixy a párovanie dostupností (25. 9. 2026)

Vlastník určil opravu Paul Lange dostupností za bezprostrednú prioritu a schválil implementáciu. Platí spoločné SKU `prefix dodávateľa + jeho kód variantu`, vlastné nemenné interné ID produktu a samostatné dodávateľské kódy. Pri ďalšom dodávateľovi zostáva pôvodné SKU. Prefix je jedinečný, po použití uzamknutý aj voči konfigurácii cez API a obnove histórie. Existujúce neprázdne prefixy sa pri zavedení ochrany uzamknú; staré SKU sa neprečíslujú.

Vetva `codex/supplier-prefix-availability` pridáva register prefixov, jednotný parser, automatické dopĺňanie jednoznačných väzieb a lokálne tlačidlo **Prepojiť dostupnosti** nad už stiahnutými údajmi. Chýbajúca väzba, konflikt, chýbajúce pozorovanie a starý feed sú rozlíšené. Nepridáva SQL migráciu, nezapína prenos zásob/cien ani pilotnú skladovú autoritu. [Návod a kontrakt](supplier-identity.md).

Lokálne prešli priebežné testy prefixov, chráneného opravného API, príjmových pomocných funkcií a tri dotknuté UI sady s buildom. Úplná finálna sada, databázové CI, číslo PR a nasadenie sa doplnia podľa skutočného výsledku; táto implementačná poznámka ich sama nepotvrdzuje. PostgreSQL sa lokálne nepodarilo zriadiť pre obmedzenia prostredia, databázové scenáre preto musí overiť PR CI.


## 25. 9. 2026 — potvrdený začiatok skladu (pripravené, nenasadené)

Vetva `codex/confirmed-stock-start` oddeľuje historické importy/testy od nových potvrdených zásob cez aditívnu migráciu 019. Podrobný postup a hranice: [stock-tracking.md](stock-tracking.md). Register je po nasadení prázdny; historické pohyby ani vrstvy sa nemažú. Nový príjem 3 kusov k starému neoverenému stavu 100 kusov začne evidenciu na 3 kusoch. Potvrdená nula zostáva odlíšená od neovereného stavu vrátane detailu produktu a prenášania zásob/cien.

Lokálne overenie: kompilácia backendu, 503 úspešných testov bez PostgreSQL (335 databázových testov preskočených), frontend build a 9 UI sád: product-editor, stock-history, fifo, opening-stock, stock-adjustments, stock-publication, order-stock, fifo-cost-sync a upgates-import. Databázové scenáre sú doplnené, ale tu neboli vykonané: prostredie nepodporuje lokálneho používateľa na spustenie PostgreSQL. Pred merge musia prejsť izolované PostgreSQL testy v existujúcom PR workflow.

Vzdialená vetva bola založená na `main 45d33c6`. Používateľ 25. 9. v nadväzujúcom chate výslovne potvrdil zápis opravy do `mpriesol/inventory_hub` a postup PR → databázové testy → merge a nasadenie po úspešných kontrolách. Výsledky vzdialených testov a nasadenia treba overiť v príslušnom PR/workflow. Žiadny nový produkčný skladový stav ani prenos do e-shopu nebol vyvolaný.

[PR #39](https://github.com/mpriesol/inventory_hub/pull/39), prvý CI beh `36149351097`: frontend build a všetkých 17 UI sád prešli. Backend vykonal všetkých 838 testov vrátane PostgreSQL bez preskočenia; 835 prešlo. Dve chyby opravuje doplnenie migrácie do testov importu a spustenie viacpríkazovej SQL migrácie priamo cez asyncpg. Tretia odhalila potrebu zachovať ochranu pred nevysvetleným nenulovým stavom po potvrdení nuly: okrem začiatku evidencie sa vyžaduje nový aktuálny pohyb alebo stále nulové množstvá. Pôvodná regresná podmienka zostáva zachovaná. Nové scenáre príjmu 100→3, inventúry, potvrdenej nuly, oddelenia skladov, opakovania a rollbacku prešli. Opakované CI opraveného commitu je podmienkou merge; následný deployment a živé čítanie súhrnu musia potvrdiť nasadenie.
