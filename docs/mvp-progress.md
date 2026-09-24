# MVP — rozhodnutia a stav pokračovania

Aktualizované 24. 9. 2026. Vlastník odsúhlasil implementáciu podľa odpovedí k 30 bodom a N1–N8. Základ: `main c2a32ac` (PR #33). Tento súbor odlišuje rozpracovanie, overenie a nasadenie; plán nie je hotová funkcia.

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
| A | Identita príjmu/importov, request ID skenera, opravy prehľadu, označenie akcií a história pohybov | Rozpracované na `codex/mvp-clarity-receiving`; zatiaľ bez overeného nasadenia |
| B1 | Šírky/poradie/stĺpce tabuľky, variantné parametre a práca so skupinami | Rozpracované súbežne s A |
| B2 | Editor → náhľad → publikovanie vybraných polí do konkrétneho shopu | Čaká; dnešné uloženie zostáva lokálne |
| C | Scheduler feedov, čerstvosť, dostupnosti, automatický dávkový skladový prenos a porovnávanie | Čaká; maintenance publisher nie je bežná automatika |
| D | Dokončenie jednoduchého príjmu/dokladov, skutočné tržby a hrubá marža, malý servisný výdaj | Čaká; nákladové vrstvy už existujú |
| E | Pilot aktuálneho rozsahu a spoločný prechod | Čaká |

## Pokračovanie po prerušení

1. Over aktuálny remote main, otvorený PR a pracovnú vetvu; zachovaj rozpracované zmeny.
2. Prečítaj tento súbor, AGENTS, OVERVIEW a dokumentáciu dotknutých modulov.
3. Doplň výsledky testov a číslo PR/commit až po skutočnom vykonaní. Nenasadené funkcie neoznačuj ako dostupné na produkcii.
4. Dokonči prvý súdržný balík, over ho v CI s PostgreSQL a cez relevantné UI testy, potom merge/deployment podľa už udeleného oprávnenia.
5. Ďalšie balíky rieš postupne v samostatných PR. Nasadenie aplikácie nie je aktivácia skladovej autority ani povolenie hromadnej zmeny živých zásob.

Prvý balík rezervuje migráciu `013` pre idempotentné požiadavky skenera; musí byť zapojená do obrazu a deploymentu pred reštartom API. Záznam o overení a nasadení sa doplní pri dokončení.
