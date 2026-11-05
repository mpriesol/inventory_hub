# Stav migrácie z Atomeru

V **Obchody → xTrek → Stav migrácie z Atomeru** (`/migration/atomer-progress`) nahrajte celý súbor buyers.xml. Každý predajný kód má vlastný riadok. Riadok s prázdnym PARENT_ID alebo odkazom na seba je samostatný produkt. Rovnaké identity s opakovaným kódom sa počítajú raz. Rozporné zdrojové identity sa zobrazia na kontrolu.

**Obnoviť údaje obchodov a Hubu** načíta aktuálne produkty a podrobnosti variantov xTrek cez existujúce API napojenie. Pri otvorení prehľadu staršieho ako 24 hodín sa kontrola spustí automaticky. Po migračnej dávke spustite obnovenie hneď. Novší export Atomeru treba nahrať. Dátum načítania zdroja je samostatný od času kontroly. Príslušný čas kontroly označuje začiatok lokálnej transakcie. Vzdialené stránky sa sťahujú následne. Rozpor medzi dvomi vzdialenými čítaniami alebo chyba nedokončenej stránky ponechá posledný dokončený beh.

xTrek sa porovnáva s konkrétnym variantom alebo samostatným produktom podľa kódu, platného GTIN a prípadne úplných parametrov v jednoznačnej rodine. Zhodu blokujú protirečivé identifikátory, značky, typ alebo parametre. Chýbajúca zhoda vyžaduje posúdenie pred založením. Stav **Karta a väzba xTrek overené** znamená zhodu predajnej identity a natívnej väzby na skladovú kartu. Kontrola cien, obsahu a fyzického skladu má samostatné postupy.

**BIKETREK** zobrazuje väzbu tej istej karty evidovanú v Hube, vrátane natívneho ID produktu a pri variante aj ID variantu. Rozlišuje potvrdenú kartu bez väzby, neznámu kartu a konflikt. Natívne ID, kód a materský produkt sa porovnávajú aj s aktuálnym katalógom BIKETREK. Rozbaliteľné rodiny ukazujú stav každej karty a odkazy do administrácie a e-shopu z katalógu. V prehľade možno kombinovať vyhľadávanie a filtre typu, stavu xTrek a väzby na BIKETREK.

Chránené `GET /api/migration/atomer-progress` vracia posledný úspešný beh a stránku položiek. `POST /api/migration/atomer-progress/refresh` prijíma multipart s `expected_run` (0 pre prvý beh), voliteľným `source_file` a voliteľným `only_if_stale`. Ukladajú sa iba identity, odtlačok zdroja a výsledky. Migrácia 037 pridáva tri tabuľky do PostgreSQL. Zdrojové riadky, identifikátory obchodov a stav migrácie patria do súkromnej databázy. Beh sa sprístupní naraz po úplnom dokončení. Súbežné alebo oneskorené požiadavky staršími údajmi neprepíšu nový výsledok.

Vydanie spúšťa existujúce nasadenie. Pri návrate aplikácie na predchádzajúcu verziu ponechajte pridané tabuľky a výsledky na mieste. Samotné nasadenie nespúšťa skladovú operáciu ani zápis do e-shopu.

## Rozhodnutia, návrhy a pokyny

V časti **Produkty a varianty** otvorte **Riešiť** pri celom produkte alebo na riadku jedného variantu. Zvoľte rozsah predaja, zapíšte internú poznámku alebo upravte návrh ďalšieho kroku. **Uložiť** zaradí nový neprázdny pokyn do časti **Pokyny pre AI**. **Označiť ako hotové** uzavrie danú položku ručne. Filter **Hotové** umožní opätovné otvorenie. Rozsah predaja sa používa pri posudzovaní požadovaných väzieb. Ručne uzavretý produkt pokrýva aj svoje varianty. Aktívny pokyn variantu treba najprv dokončiť alebo stiahnuť. Poznámky a rozhodnutia majú vlastnú revíziu a audit a pretrvajú obnovenie katalógu.

**Na rozhodnutie o odstránení** má stĺpec **Odstrániť?** pre jednotlivé varianty xTrek z posudzovaného zoznamu. Zoznam sa nahráva súkromne cez **Nahrať podklady** ako JSON pole s reťazcovými ID produktu a variantu (`product_id`, `variant_id`), kódmi `source_parent`, `parent`, `code`, hodnotou `ean` a mapou `axes` (názov parametra na zoznam hodnôt). Nová kontrola porovná tieto identity so živým katalógom. Pri rozhodnutí **Nie** sa posúdenie bez ďalšieho pokynu uzavrie. Pri **Áno** vznikne úloha k presnému variantu. Pri zmene identity, stavu skladu, väzby alebo zdrojovej zhody sa staré schválenie zneplatní. Pred externým odstránením treba potvrdiť neprítomnosť aj medzi skrytými položkami v úplnom Atomeri, overiť objednávky, rezervácie, rozpracovanú synchronizáciu a obnoviteľnú zálohu pre dané ID. Zmena rozsahu predaja alebo voľný text samy osebe neudeľujú súhlas na odstránenie.

Pracovník otvorí čakajúci pokyn, prevezme ho a vykoná iba kroky v potvrdenom rozsahu cez štandardné postupy Hubu. Výsledok po kontrole zapíše cez **Potvrdiť overený výsledok**. Pri chýbajúcich podkladoch zvolí **Uložiť dôvod blokovania**. Prerušené spracovanie zostáva viditeľné. Pred opakovaním treba preveriť prípadné uskutočnené zápisy. Čítanie zoznamu pokynov je bez vedľajších účinkov. Súkromné API poskytuje `GET /api/migration/atomer-progress/work`, `GET /api/migration/atomer-progress/targets` a revízne podmienené `POST /api/migration/atomer-progress/work`.

## Zachovanie údajov Atomeru

Hromadné ponechanie používa súkromný backendový `POST /api/migration/atomer-progress/targets/keep`. Na prvej strane nefiltrovaného zoznamu rozhodnutí s voľbou **Nerozhodnuté** vracia rozhranie `targets` presnú mapu natívnych kľúčov a revízií v poli `keep_selection`. Odošlite ju ako `revisions` spolu s príslušným `run_id`. Rovnaký postup používa tlačidlo **Ponechať nerozhodnuté varianty**. Hub zapíše rozhodnutie **Nie**, stav **Hotové** a audit pre každý variant v jednej databázovej transakcii. Už uložené ponechanie možno overiť opakovanou požiadavkou. Zmenená revízia, chýbajúci cieľ alebo aktívny pokyn zastaví celú dávku.

**Aktivita v Atomeri** číta viditeľnosť zo zdroja. Rodina ukazuje stav samotného materského produktu. Po rozbalení sa ukáže stav variantu a samostatne stav jeho rodiča. Pri samostatnom produkte platí viditeľnosť jeho vlastného riadka. Chýbajúca hodnota je **Neuvedené**, protirečivé podklady sú **Rozpor v zdrojoch**. Stav rodiča sa neodvodzuje zo súčtu viditeľných variantov. Viditeľnosť neurčuje množstvo ani objednateľnosť.

Do prehľadu možno nahrať pôvodný buyers XML a **export tovarov z administrácie Atomeru (CSV)**. Výber produktov stačí na doplnenie viditeľnosti a príznakov pri uvedených kódoch. Variant s katalógovým číslom a uvedeným rodičom sa zobrazí aj vtedy, keď v XML chýba. Materské riadky dopĺňajú stav rodiny. Hub ukáže počet kódov v CSV a počet predajných položiek pridaných oproti XML. Zdrojová zhoda chráni existujúci variant pri posudzovaní odstránenia.

Voľbu **CSV obsahuje celý katalóg vrátane skrytých produktov a variantov** zaškrtnite iba pri takomto exporte. Hub ešte overí pokrytie kódov z XML a existenciu riadkov rodičov. Pri výbere zostáva neprítomnosť položiek neoverená. Samostatný kód mimo XML bez odkazu na rodiča možno zaradiť ako predajnú položku až po identifikovaní všetkých rodičov v celom katalógu.

Hub archivuje pôvodné súbory v súkromnej databáze vrátane opakovaných charakteristík. Párovanie používa katalógové číslo a zachováva hodnoty príznakov **Odporúčaný**, **Akcia**, **Výpredaj** a **Novinka** vrátane nuly. Pri obnovení prehľadu Hub znova použije naposledy uložené CSV pre aktuálny buyers XML. Nahratím nového CSV sa pre tento beh vyberie nová verzia. Pri výmene XML nahrajte zodpovedajúce CSV spolu s ním. Pri migrácii overte v cieľovom obchode viditeľnosť rodiča aj variantu, príznaky, obdobia platnosti, ceny, zásoby a obsah.

Odkaz na rodiča môže obsahovať aj jeho interné číslo z Atomeru. Pri jednoznačnej zhode Hub zaradí varianty do existujúcej rodiny a zachová jej pracovné pokyny. Opakovaný kód s rozdielnymi záznamami označí na kontrolu. Pri rozdielnych hodnotách viditeľnosti zobrazí rozpor. Riadok s poškodeným identifikátorom a čitateľným interným číslom ostane v pôvodnom archíve. Prehľad uvedie jeho interné číslo a do opravy zablokuje spracovanie odstránení. Pri odlišnom kóde v xTrek môže údaj o aktivite pochádzať z jedinečného EAN variantu v rovnakej zdrojovej rodine.

Migrácia 038 pridáva súkromné tabuľky na podklady, kontroly, rozhodnutia a audit a voliteľný odkaz na CSV pri behu. Predchádzajúce behy sú čitateľné. Pri návrate aplikácie ponechajte všetky tabuľky, podklady a rozhodnutia na mieste.

## Opravy parametrov variantov xTrek

Operátor môže v sekcii **Opravy položiek na kontrolu** nahrať súkromný JSON plán. Pole
`families` obsahuje objekty s `run_id`, `source_sha` a `rows`. Každý riadok určí presný
zdrojový `code`, revíziu rozhodnutia `revision`, pôvodné pomenované osi `before` a vopred
overené priradenie kľúča PARAM k názvu osi v `names`. Hodnoty pochádzajú z uloženého
Atomeru. Voliteľné `blocks` obsahuje `run_id` a `items` s revíziou a konkrétnym
dôvodom pre každý kód. Endpointy pod `/migration/atomer-progress` vyžadujú operátora.

Náhľad prečíta celú rodinu. Potvrdenie zopakuje kontrolu, uloží pôvodné dáta,
pošle vybrané parametre a porovná natívne ID, súrodencov a chránené polia.
Opakovanie uloženého zámeru vykoná iba overenie. Po spracovaní obnovte prehľad.
Rozdiely totožnosti a nejednoznačné EAN vyžadujú samostatný dôkaz a rozhodnutie.

Pri doloženom prečíslovaní toho istého výrobku používa plán `operation: verified_barcode`.
Operátor pripája dokument výrobcu alebo dodávateľa pre presnú veľkosť, starý kód, obidva GTIN
a ID zachovanej karty. Tento postup je obmedzený na variant s nulovou zásobou, bez rezervácie
a s jednoznačným vlastníkom. Nový GTIN sa zapíše len do xTreku a po spätnom overení aj ako
ďalší identifikátor pôvodnej karty. Starší čiarový kód sa ponechá. Iná generácia výrobku
vyžaduje samostatnú položku. Pri menách rodičov kolidujúcich po orezaní sa použije doslovný
odkaz dieťaťa v pôvodnom CSV, ak identifikuje presne jeden rodičovský záznam.

Voľba `fill_existing_card_barcode` dopĺňa len prázdny EAN pri existujúcej natívnej väzbe. Vyžaduje overený kód, ktorý už patrí výlučne tej istej skladovej karte, aktuálne natívne ID a zhodu s Atomerom.

Pre viaceré rodiny možno použiť pole `packs` so skupinami `families`. Kontrola aj zápis
používajú spoločné stránkované čítanie podľa natívnych ID. Jedna skupina obsahuje najviac
25 rodičov a 100 vybraných variantov. Hub pred odoslaním uloží samostatnú úplnú zálohu
každej rodiny. Aj po čiastočnej odpovedi overí rodiny jednotlivo. Obnova neistého zápisu
vykonáva iba čítanie. Odložený zápis možno zopakovať po preukázanom lokálnom odmietnutí
pred odoslaním a opätovnom overení pôvodného stavu.

Ak ten istý presne identifikovaný variant pri doplnení chýbajúceho EAN vyžaduje aj opravu osi, voliteľné pole `names` umožní v rovnakej overenej požiadavke použiť hodnoty parametrov z uloženého zdroja Atomeru.

Obnova prehľadu číta parametre variantov podľa skupín natívnych rodičov. Pre xTrek môže
použiť nedávny úplný katalóg zo serverovej vyrovnávacej pamäte, doplní živé zmenené produkty
s časovým prekrytím a preverí počet rodičov a variantov. Každú rodinu s variantmi načíta
aktuálne podľa ID. Odlišný počet rodičov vyvolá úplné čítanie produktov. Neúplná rodina,
nezhodné natívne ID alebo súbežná zmena zastavia zverejnenie nového prehľadu.

Pri sťahovaní konkrétnych variantov xTrek do skladu sa použije rovnaké doplnenie zmien
nad nedávnym katalógom. Potvrdenie navyše načíta všetkých vybraných rodičov priamo z API.
Následne sa uplatnia obvyklé kontroly duplicít celého katalógu, pôvodného tokenu variantu,
vlastníctva karty a explicitne vybraných položiek. Fyzické množstvá sa týmto postupom nezapisujú.


### Generácie v jednej rodine

Operátor môže na presne vybranej rodine uložiť výrobcom doložené rozlíšenie generácií. Prvá fáza doplní parameter Generácia na vymenované existujúce natívne ID. Druhá fáza pridá nové SKU z archivovaného Atomeru a zaznamená natívne ID pridelené xTrekom. Privátny plán obsahuje revízie, očakávané EAN a úplný zoznam pôvodných variantov rodiča. Obe fázy vyžadujú aktuálny náhľad. Hub zachová zálohu rodiča, pôvodné parametre, HTML zdroja a výsledok spätného čítania. Prírastky skladu z dostupnosti dodávateľa sa spracúvajú samostatne. Pri nejasnom výsledku Hub opakuje iba čítanie.
