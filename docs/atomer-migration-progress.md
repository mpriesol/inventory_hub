# Stav migrácie z Atomeru

V **Obchody → xTrek → Stav migrácie z Atomeru** (`/migration/atomer-progress`) nahrajte celý súbor buyers.xml. Každý predajný kód má vlastný riadok. Riadok s prázdnym PARENT_ID alebo odkazom na seba je samostatný produkt. Rovnaké identity s opakovaným kódom sa počítajú raz. Rozporné zdrojové identity sa zobrazia na kontrolu.

**Obnoviť údaje obchodov a Hubu** načíta aktuálne produkty a podrobnosti variantov xTrek cez existujúce API napojenie. Pri otvorení prehľadu staršieho ako 24 hodín sa kontrola spustí automaticky. Po migračnej dávke spustite obnovenie hneď. Novší export Atomeru treba nahrať. Dátum načítania zdroja je samostatný od času kontroly. Príslušný čas kontroly označuje začiatok lokálnej transakcie. Vzdialené stránky sa sťahujú následne. Rozpor medzi dvomi vzdialenými čítaniami alebo chyba nedokončenej stránky ponechá posledný dokončený beh.

xTrek sa porovnáva s konkrétnym variantom alebo samostatným produktom podľa kódu, platného GTIN a prípadne úplných parametrov v jednoznačnej rodine. Zhodu blokujú protirečivé identifikátory, značky, typ alebo parametre. Chýbajúca zhoda vyžaduje posúdenie pred založením. Stav **Karta a väzba xTrek overené** znamená zhodu predajnej identity a natívnej väzby na skladovú kartu. Kontrola cien, obsahu a fyzického skladu má samostatné postupy.

**BIKETREK** zobrazuje väzbu tej istej karty evidovanú v Hube, vrátane natívneho ID produktu a pri variante aj ID variantu. Rozlišuje potvrdenú kartu bez väzby, neznámu kartu a konflikt. Natívne ID, kód a materský produkt sa porovnávajú aj s aktuálnym katalógom BIKETREK. Rozbaliteľné rodiny ukazujú stav každej karty a odkazy do administrácie a e-shopu z katalógu. V prehľade možno kombinovať vyhľadávanie a filtre typu, stavu xTrek a väzby na BIKETREK.

Chránené `GET /api/migration/atomer-progress` vracia posledný úspešný beh a stránku položiek. `POST /api/migration/atomer-progress/refresh` prijíma multipart s `expected_run` (0 pre prvý beh), voliteľným `source_file` a voliteľným `only_if_stale`. Ukladajú sa iba identity, odtlačok zdroja a výsledky. Migrácia 037 pridáva tri tabuľky do PostgreSQL. Zdrojové riadky, identifikátory obchodov a stav migrácie patria do súkromnej databázy. Beh sa sprístupní naraz po úplnom dokončení. Súbežné alebo oneskorené požiadavky staršími údajmi neprepíšu nový výsledok.

Vydanie spúšťa existujúce nasadenie. Pri návrate aplikácie na predchádzajúcu verziu ponechajte pridané tabuľky a výsledky na mieste. Samotné nasadenie nespúšťa skladovú operáciu ani zápis do e-shopu.
