# Oprava a archív príjemky

Príjemka sa opravuje cez **Oprava a archív príjemky** v jej detaile. Každá akcia potrebuje dôvod, najskôr zobrazí náhľad a až po jeho potvrdení uloží výsledok. V zozname dokončených faktúr tlačidlo otvorí tento detail; samotné otvorenie stránky nič nemení.

Rozbaliteľná **História úprav príjemky** ukazuje posledných 50 dokončených akcií s dôvodom, autorom, časom a presným skladovým dopadom. Nezobrazuje technické uložené snapshoty ani nesúvisiace doklady.

| Akcia | Zmena zásob | Výsledok príjemky |
| --- | --- | --- |
| Znovu otvoriť | Odpočíta presne pôvodný príjem, ak sú jeho kusy stále nedotknuté. | Pozastavená príjemka, ktorú možno upraviť a opäť dokončiť. |
| Stornovať príjem | Rovnaká kontrolovaná kompenzácia pôvodného príjmu. | Zrušená príjemka. |
| Archivovať | Žiadna. | Skrytá v bežnom zozname, zachovaná s pôvodným stavom a históriou. |
| Obnoviť z archívu | Žiadna. | Pôvodný stav a časy. Dokončená príjemka sa obnoví ako dokončená. |

Po storne možno príjemku neskôr znovu otvoriť; pôvodný príjem sa druhýkrát neodpočítava. Rozpracovanú príjemku možno archivovať bez dokončenia a bez naskladnenia. Prepínač **Zobraziť archivované** sprístupní archív. Archív nie je oprava množstva: zásoby upravuje iba výslovné storno alebo bežná skladová korekcia.

## Kedy sa dá príjem bezpečne stornovať

Náhľad ukáže aktuálne množstvo, odpočítané množstvo, výsledné množstvo a dostupné množstvo po odpočítaní rezervácií a karantény. Používa základnú skladovú jednotku vrátane uloženého prepočtu balenia.

Storno vyžaduje aktuálny potvrdený sklad s FIFO a úplné pôvodné vrstvy príjemky. Zablokuje sa, ak boli kusy vydané, premiestnené, vrátené cez odvodenú vrstvu, patria do staršieho obdobia evidencie, chýba pôvodný pohyb alebo by výsledok nepokryl rezervácie či karanténu. Zámok publikovania skladu platí aj pre storno. Keď už na príjem nadväzujú ďalšie operácie, použije sa bežná inventúrna korekcia alebo fyzická vratka; história sa neprepisuje.

Pôvodné pohyby, riadky, skeny a FIFO vrstvy zostávajú zachované. Storno pridáva záporné pohyby `ADJUSTMENT_OUT` s odkazom `receiving_reversal`, UUID operácie, dôvodom a autentifikovaným autorom. Odoberá konkrétnu pôvodnú vrstvu, nie náhradné kusy z inej dodávky. Neznáma cena zostáva neznáma; nepremieňa sa na nulu. Cache poslednej nákupnej ceny už nepoužíva stornovaný príjem.

## Opakovanie požiadaviek a súbežné zmeny

Náhľad trvá 30 minút a obsahuje hash presného stavu príjemky, množstiev a príslušných FIFO vrstiev. Zmena medzi náhľadom a potvrdením vyžaduje nový náhľad. Operácia má UUID a zachováva pôvodný request aj výsledok. Opakovanie po strate spojenia vracia uložený výsledok; nevytvára druhý pohyb.

Rozhranie uloží presnú požiadavku ešte pred odoslaním. Pri neznámom výsledku ponúkne opakovanie tej istej požiadavky a zablokuje ďalšie úpravy príjemky. Samotná HTTP 200 odpoveď bez správneho UUID a príjemky sa nepovažuje za potvrdenie.

Znovu otvorená príjemka má nový `posting_revision`. Dokončenie posiela `expected_posting_revision`; stará alebo neúplná požiadavka z predchádzajúceho cyklu sa odmietne. Nové pohyby používajú kľúč `receiving:{session_id}:{line_id}:r{revision}`. Pôvodný cyklus ponecháva existujúci kľúč `receiving:{session_id}:{line_id}`.

## Faktúry a jednorazový archív testov

Najprv sa archivuje príjemka a potom jej faktúra. Obnova má opačné poradie: najprv faktúra, potom príjemka. Faktúra označená ako archivovaná alebo spracovaná bez skladového pohybu nesmie začať, obnoviť, znovu otvoriť ani dokončiť príjem. Dokončená archivovaná príjemka zachováva dôkaz pôvodného naskladnenia. Aktuálne spracovanie faktúry pritom posudzuje nearchivované príjemky: po archivácii starého testovacieho príjmu možno historickú faktúru výslovne označiť ako spracovanú bez nového skladového pohybu. Pred obnovou starej príjemky sa musí toto manuálne spracovanie faktúry znovu otvoriť.

Pri jednorazovom uprataní testov možno rovnaké API použiť na vopred zvolený zoznam príjemiek. Každá dostane vlastný audit a archivácia má nulový skladový dopad. Prijatie aktuálnych množstiev ako počiatočného stavu je samostatná skladová operácia. Staré faktúry sa na tento účel opakovane nedokončujú. Pôvodné súbory a index sa pri oprave príjemky neprepisujú; zoznam faktúr dopĺňa aktuálny stav čítaním z databázy.

## API

Všetky operácie vyžadujú oprávnenie operátora a odpovede sa necachujú. Cesty sú pod `/api`:

- `POST /suppliers/{supplier}/receiving/sessions/{id}/operations/preview` s `{request_id, action, reason}`.
- `GET /suppliers/{supplier}/receiving/sessions/{id}/operations` vracia posledných 50 dokončených záznamov histórie v danej príjemke.
- `GET /suppliers/{supplier}/receiving/sessions/{id}/operations/{request_id}` vracia náhľad a prípadný dokončený výsledok.
- `POST /suppliers/{supplier}/receiving/sessions/{id}/operations/{request_id}/apply` s `{preview_hash, confirmed:true}`.
- Zoznam `/suppliers/{supplier}/receiving/sessions?include_archived=true` zahŕňa archivované príjemky.
- Čítanie `/suppliers/{supplier}/invoices/{invoice}/active-session?include_closed=true` vie otvoriť aj uzavretú príjemku bez zmeny stavu.

Akcie sú `reverse`, `reopen`, `archive` a `restore`. Výsledok uvádza `stock_effect`, `impacts`, `movement_ids`, nový stav a revíziu príjemky. Pôvodná statusová cesta `/invoices/{invoice}/reopen` už vracia `receiving_recovery_required` a príjemku nemení.

Migrácia **052_receiving_operations.sql** pridáva len auditnú tabuľku a ochranu histórie. Nevykonáva storno, archiváciu, otvorenie ani zmenu zásob. Spúšťací modul je `inventory_hub.receiving_operation_migrate`. Dokončené audity nemožno zmeniť ani odstrániť; obnovenie príjemky je nová auditovaná operácia.

Zámky majú spoločné poradie s faktúrami: UUID operácie → dodávateľ → príjemka → súvisiaca faktúra → identita produktu → skladové bilancie/FIFO. Zachová sa tak rovnaké poradie pri skenovaní, dokončení aj oprave príjemky.

## Overenie

`test_receiving_operations_db` overuje presné FIFO storno, prepočet balení, neznáme ceny, rezervácie, spotrebované vrstvy, zmenu po náhľade, opakovanie a súbeh požiadaviek, vrátenie transakcie po chybe, nemenný audit, opätovné dokončenie a nulový skladový dopad archívu/obnovy. `test_receiving_operations_routes` kontroluje skutočne zaregistrované API, oprávnenia, povinný dôvod a výslovné potvrdenie. `frontend/tests/receiving-recovery-ui.cjs` overuje náhľad, uchovanie UUID po strate odpovede, obnovenie požiadavky po načítaní stránky, blokovaný/neaktuálny náhľad a otvorenie archívu bez zápisu.
