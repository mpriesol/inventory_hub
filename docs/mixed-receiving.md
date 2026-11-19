# Zmiešaný príjem podľa EAN

Stránka `/receiving` podporuje jednu pracovnú dávku naprieč dodávateľmi a faktúrami. Sken sa uloží ako rozpracované priradenie faktúrového riadka. Fyzické množstvo, FIFO a skladová dostupnosť sa zmenia až po zobrazení súhrnu a jednom potvrdení celej dávky.

## Postup

Mobilná kamera používa zadný fotoaparát a umožní prisvietenie, ak ho zariadenie podporuje. Po načítaní sa pozastaví do stlačenia „Skenovať ďalší kus“; opakované video snímky tak nenavyšujú množstvo. USB/Bluetooth skener a ručný vstup s Enter fungujú bez tohto kroku.

1. Otvoriť alebo obnoviť rozpracovanú dávku v zvolenom sklade a skenovať EAN. Každý nový fyzický sken má nové UUID. Množstvo je vo fakturovanej jednotke; výslovne nastavený prepočet nákupného balenia sa zachová.
2. Systém vyberie najnovšiu vhodnú faktúru podľa overeného dátumu dokladu. Iné vhodné faktúry sú viditeľné a priradenie možno pred potvrdením zmeniť. Chýbajúce alebo rovnaké dátumy pri viacerých kandidátoch vyžadujú výber; dátum uploadu ani poradie názvov nenahrádza dátum dokladu. Viaceré riadky na jednej faktúre vyžadujú samostatnú voľbu, pretože môžu mať rozdielnu cenu.
3. Rozpracované skeny rezervujú zostávajúce fakturované množstvo aj voči inej dávke. Rezervácia faktúrového riadka nie je rezervácia fyzického skladu. Zrušenie skenu alebo presunutie na inú faktúru uvoľní pôvodnú kapacitu a zachová audit.
4. Chýbajúcu kartu pripraviť cez existujúci produktový editor. Použije sa presný zdroj daného dodávateľa a spoločné kontroly EAN/SKU. Uloženie karty nevytvorí skladový pohyb. Konflikt identity alebo chýbajúca nákupná cena bránia potvrdeniu dávky.
5. Súhrn zobrazí faktúry, naskenované množstvo, prepočet na základné jednotky, potvrdený sklad pred/po a prepojené e-shopy. Neoverené množstvo má hodnotu neznámu, nie nula. Jedno potvrdenie zaúčtuje všetky riadky dávky v jednej transakcii.

Pred založením dávky možno zvoliť aktívny sklad; predvolený je sklad označený ako hlavný. Jedna faktúra naďalej patrí jednému skladu. Zmena skladu počas jej prijímania nie je implicitné rozdelenie faktúry medzi sklady.

Nahratie faktúry je dostupné aj pred založením dávky v zdieľanom modale, bez odchodu z aktuálnej práce. História dokončených dávok je samostatný rozbaliteľný prehľad; nahrávanie ju neotvára. Po začatí alebo obnovení príjmu sa zaostrí EAN vstup s bežným vnútorným odsadením. Chýbajúci doklad ponúka nahratie alebo výslovnú ručnú výnimku. Klient zachová UUID a celý payload neistej ručnej operácie či odstránenia konceptu a pri obnove zopakuje tú istú operáciu.

Zoznam dokladov je vo [Faktúrach](invoice-archive.md). Tam je aj stav a odkaz na súvisiacu príjemku. Starý prehľad `/receiving/invoices` presmeruje na `/invoices` a zachová návrat do dávky, ak ho odkaz obsahuje.

### Výslovný príjem bez faktúry

Ak faktúra chýba, obsluha môže výslovne potvrdiť **Pridať bez faktúry**, uviesť dôvod a prípadne doloženú nákupnú cenu v EUR bez DPH. Chýbajúca cena zostáva `NULL` s príznakom `unknown`; predbežná cena je `provisional`. Táto výnimka obchádza chýbajúci doklad, nie konflikt skladovej identity. Produkt musí mať jednoznačnú existujúcu fyzickú skladovú kartu, overenú spoločným resolverom podľa EAN alebo presného SKU. Množstvo je v základnej skladovej jednotke karty.

Ručný riadok sa uloží do tej istej dávky, viditeľne označený bez faktúry. Má vlastné UUID, dôvod, pôvodcu a nákladový stav. Skontroluje sa v spoločnom súhrne a zaúčtuje spolu s faktúrovými riadkami v jednej transakcii. Používa rovnaký `fifo.write_documented_receipt` ako existujúci samostatný FIFO príjem; nevytvára fiktívnu faktúru ani príjemkovú reláciu. Neúspešná položka vráti späť celú dávku. Následné nahratie faktúry automaticky neprepojí tento ručný pohyb ani nezníži fakturovaný zostatok: rovnaké už prijaté fyzické kusy sa nesmú naskenovať druhýkrát. Doplnenie doloženej nákupnej ceny môže použiť existujúcu revíziu nákladu FIFO.

### Odstránenie prázdnych konceptov a história

Odstránenie prázdnej otvorenej dávky je dôvodová, vratná archivácia. Dávka zmizne z bežného zoznamu, pôvodné UUID a výsledky operácií zostanú zachované. Nový sken do archivovanej dávky je zakázaný, replay pôvodného skenu ostáva dostupný. Dávka s čakajúcimi aktívnymi skenmi sa archivovať nedá: najprv treba výslovne zrušiť jej priradenia.

Osobitná archivácia dokončenej dávky nemení faktúry, množstvá, FIFO ani účtovné pohyby. Je to vyčistenie zobrazenia, nie opätovné otvorenie faktúry alebo vynulovanie fyzického skladu. Oprava už zaúčtovaných množstiev naďalej používa kontrolované kompenzačné pohyby v existujúcom postupe opráv príjemiek.

Faktúra sa automaticky uzavrie iba keď je celé fakturované množstvo preukázateľne zaúčtované. Čiastočná faktúra zostane rozpracovaná. Aj historická dokončená čiastočná príjemka môže prijať zvyšné kusy: pôvodný príjem sa nestornuje, neprijíma znova a neprepisuje. Archivované, stornované a výslovne spracované bez skladu zostávajú blokované.

Výslovne identifikované služby (napríklad doprava) nevytvárajú pohyby a neblokujú fyzické dokončenie faktúry. Neznámy alebo nepriradený riadok sa za službu nepovažuje.

Už predané alebo neprítomné kusy sa nedopĺňajú. Historické skeny zo starého nedokončeného postupu nie sú účtovaním: pred použitím tejto faktúry treba jej pôvodnú príjemku dokončiť alebo vynulovať rozpracované skeny existujúcim postupom. Systém uvedie konkrétny doklad a dôvod `mixed_receiving_legacy_pending`; staré množstvá nepreúčtuje ani nezmaže automaticky. Zmeny pôvodnej príjemky počas čakajúcej dávky sú blokované.

## Sklad, e-shopy a obnova

Obidva postupy používajú rovnaký writer `services/receiving_posting.py`, resolver `receiving_scan_identity.py`, FIFO, register začiatku evidencie a ochrany publikovania. Žiadny nový skladový engine nevzniká. Dodávateľská dostupnosť sa nepripočítava k fyzickým kusom.

Existujúci trigger z migrácie 036 zaznamená zmenu dostupného zostatku do `stock_sync_dirty`; existujúci delta_v1 worker ju odošle iba pri splnených nastaveniach a autorite konkrétneho e-shopu. Príjem nič nezapína. Súhrn rozlišuje chýbajúci variant, vypnutý prenos, čakajúci prenos a chybu; samotné prepojenie nie je potvrdenie úspešného vzdialeného zápisu. Automatika neisté vzdialené zápisy neopakuje naslepo.

UUID výsledok finalizácie je uložený v rovnakej transakcii ako pohyby. Opakovanie vráti rovnaký výsledok bez druhého príjmu. Opakovanie skenu zachová jeho pôvodnú operáciu a zobrazí aktuálny súhrn. UUID s iným obsahom sa odmietne. Zmena skladu, ceny, identity, dávky alebo príjemky po náhľade vyžaduje nový súhrn.

Opravy používajú [existujúce storno a znovuotvorenie](receiving-recovery.md). Každý batch posting má väzbu na faktúrový riadok a revíziu príjemky. Oprava zahŕňa pôvodné aj nové dávkové pohyby danej revízie; spotrebovaná alebo rezervovaná FIFO vrstva sa nesmie potichu nahradiť inou zásobou.

## API a údaje

Autorizované cesty pod `/api/receiving/batches`:

| Cesta | Úloha |
| --- | --- |
| `POST /` | Založiť dávku s UUID a skladom. |
| `GET /`, `GET /{id}` | Obnoviť dávku a načítať aktuálny súhrn. |
| `POST /{id}/scan` | Uložiť sken; voliteľne vybrať dodávateľa, faktúru a riadok. |
| `GET /{id}/manual-product?code=...` | Čítacie overenie konkrétnej karty pred výnimkou bez faktúry. |
| `POST /{id}/manual-scan` | UUID, kód, množstvo, dôvod, `confirmed_without_invoice:true`, voliteľná cena a nákladový stav. |
| `POST /{id}/scans/{scan}/reassign` | S novým UUID zmeniť faktúru pred účtovaním. |
| `POST /{id}/scans/{scan}/cancel` | S novým UUID zrušiť čakajúce faktúrové alebo ručné priradenie. |
| `POST /{id}/archive`, `POST /{id}/restore` | Vratné skrytie/obnovenie dávky s UUID a dôvodom; `empty_only:true` chráni odstránenie prázdneho konceptu. |
| `POST /{id}/preview` | Aktuálny súhrn a hash potvrdenia. |
| `POST /{id}/finalize` | UUID, `preview_hash`, výslovné `confirmed:true`; zaúčtovať raz. |

Migrácia **054_mixed_receiving.sql** pridáva `mixed_receiving_batches`, `mixed_receiving_scans`, nemenné `mixed_receiving_requests` a nemenné `mixed_receiving_postings`. Neaktivuje autoritu, neprijíma staré údaje a nemení existujúce pohyby. Modul `inventory_hub.mixed_receiving_migrate` musí byť v explicitnom runneri a SQL v obraze.

Migrácia **056_receiving_manual.sql** aditívne dopĺňa archivačné údaje dávky, oddelené `mixed_receiving_manual_scans` a nemenné väzby `mixed_receiving_manual_postings` na skutočné pohyby. Povinné faktúrové väzby pôvodných skenov sa neuvoľňujú. Migrácia sama nič nearchivuje ani neprijíma.

Ručné skeny používajú existujúcu databázovú ochranu pred zápisom na vyradenú skladovú identitu. Ich prítomnosť na niektorej zo zlučovaných kariet blokuje konsolidáciu aj po zrušení skenu; história, zaúčtovanie a FIFO sa pri zlučovaní nepresúvajú ani nemažú. Prázdna tabuľka ručných skenov konsolidáciu neblokuje.

Zámky finalizácie: UUID operácie → dávka → dodávatelia v poradí ID → príjemky → faktúry → identita → aktívny sklad → skladové bilancie/FIFO. Založenie a obnovenie otvorenej dávky zamyká iba UUID a sklad pred zápisom dávky, nie identitu. Zdieľaný riadkový zámok skladu bráni súbežnej deaktivácii počas vytvorenia alebo potvrdenia príjmu. Sken aj ručná zmena dodržiavajú existujúci dodávateľský/príjemkový zámok. Uložené potvrdenia obsahujú iba potrebný výsledok operácie; rastúci súhrn sa nekopíruje do každej historickej požiadavky.

Databázové regresie `test_mixed_receiving_db.py` overujú opakovaný čiastočný príjem, dokončené čiastočné doklady, súbeh skenov/finalizácie, uvoľnenie kapacity, výber dátumu a cenového riadka, prepočty balení, obnovu, rollback celej dávky a jednoznačný exportný rozsah. `test_mixed_receiving_routes.py` overuje autorizáciu a zákaz cache. Ide o izolované testy, nie potvrdenie produkčného príjmu alebo externých skladových zápisov.
