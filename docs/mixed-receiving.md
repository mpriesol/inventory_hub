# Zmiešaný príjem podľa EAN

Stránka `/receiving` podporuje jednu pracovnú dávku naprieč dodávateľmi a faktúrami. Sken sa uloží ako rozpracované priradenie faktúrového riadka. Fyzické množstvo, FIFO a skladová dostupnosť sa zmenia až po zobrazení súhrnu a jednom potvrdení celej dávky.

## Postup

Mobilná kamera používa zadný fotoaparát a umožní prisvietenie, ak ho zariadenie podporuje. Po načítaní sa pozastaví do stlačenia „Skenovať ďalší kus“; opakované video snímky tak nenavyšujú množstvo. USB/Bluetooth skener a ručný vstup s Enter fungujú bez tohto kroku.

1. Otvoriť alebo obnoviť rozpracovanú dávku v zvolenom sklade a skenovať EAN. Každý nový fyzický sken má nové UUID. Množstvo je vo fakturovanej jednotke; výslovne nastavený prepočet nákupného balenia sa zachová.
2. Systém vyberie najnovšiu vhodnú faktúru podľa overeného dátumu dokladu. Iné vhodné faktúry sú viditeľné a priradenie možno pred potvrdením zmeniť. Chýbajúce alebo rovnaké dátumy pri viacerých kandidátoch vyžadujú výber; dátum uploadu ani poradie názvov nenahrádza dátum dokladu. Viaceré riadky na jednej faktúre vyžadujú samostatnú voľbu, pretože môžu mať rozdielnu cenu.
3. Rozpracované skeny rezervujú zostávajúce fakturované množstvo aj voči inej dávke. Rezervácia faktúrového riadka nie je rezervácia fyzického skladu. Zrušenie skenu alebo presunutie na inú faktúru uvoľní pôvodnú kapacitu a zachová audit.
4. Chýbajúcu kartu pripraviť cez existujúci produktový editor. Použije sa presný zdroj daného dodávateľa a spoločné kontroly EAN/SKU. Uloženie karty nevytvorí skladový pohyb. Konflikt identity alebo chýbajúca nákupná cena bránia potvrdeniu dávky.
5. Súhrn zobrazí faktúry, naskenované množstvo, prepočet na základné jednotky, potvrdený sklad pred/po a prepojené e-shopy. Neoverené množstvo má hodnotu neznámu, nie nula. Jedno potvrdenie zaúčtuje všetky riadky dávky v jednej transakcii.

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
| `POST /{id}/scans/{scan}/reassign` | S novým UUID zmeniť faktúru pred účtovaním. |
| `POST /{id}/scans/{scan}/cancel` | S novým UUID zrušiť čakajúce priradenie. |
| `POST /{id}/preview` | Aktuálny súhrn a hash potvrdenia. |
| `POST /{id}/finalize` | UUID, `preview_hash`, výslovné `confirmed:true`; zaúčtovať raz. |

Migrácia **054_mixed_receiving.sql** pridáva `mixed_receiving_batches`, `mixed_receiving_scans`, nemenné `mixed_receiving_requests` a nemenné `mixed_receiving_postings`. Neaktivuje autoritu, neprijíma staré údaje a nemení existujúce pohyby. Modul `inventory_hub.mixed_receiving_migrate` musí byť v explicitnom runneri a SQL v obraze.

Zámky: UUID operácie → dávka → dodávatelia v poradí ID → príjemky → faktúry → identita → skladové bilancie/FIFO. Sken aj ručná zmena dodržiavajú existujúci dodávateľský/príjemkový zámok. Uložené potvrdenia obsahujú iba potrebný výsledok operácie; rastúci súhrn sa nekopíruje do každej historickej požiadavky.

Databázové regresie `test_mixed_receiving_db.py` overujú opakovaný čiastočný príjem, dokončené čiastočné doklady, súbeh skenov/finalizácie, uvoľnenie kapacity, výber dátumu a cenového riadka, prepočty balení, obnovu, rollback celej dávky a jednoznačný exportný rozsah. `test_mixed_receiving_routes.py` overuje autorizáciu a zákaz cache. Ide o izolované testy, nie potvrdenie produkčného príjmu alebo externých skladových zápisov.
