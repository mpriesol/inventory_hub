# Stav migrácie z Atomeru

V **Obchody → xTrek → Stav migrácie z Atomeru** (`/migration/atomer-progress`) nahrajte celý súbor buyers.xml. Každý predajný kód má vlastný riadok. Riadok s prázdnym PARENT_ID alebo odkazom na seba je samostatný produkt. Rovnaké identity s opakovaným kódom sa počítajú raz. Rozporné zdrojové identity sa zobrazia na kontrolu.

Pri aktívnej alebo dokončenej kampani s rovnakými zdrojovými súbormi tabuľka a filtre priebežne zobrazujú potvrdené skladové karty a väzby. Karta sa ukáže hneď po overení svojho ID a SKU v Hube. Samostatne sa zobrazuje natívne potvrdený cieľ, jeho väzba na kartu a prípadná nedokončená kontrola obsahu. Súhrn nad tabuľkou má vlastný dátum pôvodnej kontroly. Ručné rozhodnutia a zásoby majú vlastné postupy.

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
Pri záverečnom čítaní Hub porovná čas zmeny vrátane časového pásma s časom začatia kontroly.
Ak vzdialenú kontrolu zastaví chyba, pri upozornení v prehľade sa zobrazí aj jej kód.

Pri sťahovaní konkrétnych variantov xTrek do skladu sa použije rovnaké doplnenie zmien
nad nedávnym katalógom. Potvrdenie navyše načíta všetkých vybraných rodičov priamo z API.
Následne sa uplatnia obvyklé kontroly duplicít celého katalógu, pôvodného tokenu variantu,
vlastníctva karty a explicitne vybraných položiek. Fyzické množstvá sa týmto postupom nezapisujú.


### Generácie v jednej rodine

Operátor môže na presne vybranej rodine uložiť výrobcom doložené rozlíšenie generácií. Prvá fáza doplní parameter Generácia na vymenované existujúce natívne ID. Druhá fáza pridá nové SKU z archivovaného Atomeru a zaznamená natívne ID pridelené xTrekom. Privátny plán obsahuje revízie, očakávané EAN a úplný zoznam pôvodných variantov rodiča. Obe fázy vyžadujú aktuálny náhľad. Hub zachová zálohu rodiča, pôvodné parametre, HTML zdroja a výsledok spätného čítania. Prírastky skladu z dostupnosti dodávateľa sa spracúvajú samostatne. Pri nejasnom výsledku Hub opakuje iba čítanie.

Po prevzatí nových listov cez skladový import overí voľba `finishGenerationCards` natívne ID, jednoznačného vlastníka EAN a väzbu na konkrétnu skladovú kartu. Doplní chýbajúce potvrdené osi novej karty a uzavrie riadok s ID karty aj obchodu.

Ak vytvorenie variantu ovplyvní cenu rodiča, samostatná obnova porovná konkrétne nové ID, pôvodné listy a uložené ceny. Upraví len preukázaný rozdiel. Hmotnosť na dopravu prevádza z kg na celé gramy a pri oprave dostupnosti posiela iba dostupnosť. Výber týchto presných ID možno odovzdať spoločnému skladovému importu.

Natívny číselník dostupností sa dá prečítať cez `availability-options` pre jednotlivý dokumentovaný typ alebo konkrétne ID. Výslovne zvolené ID musí súhlasiť s názvom z Atomeru aj pri priamom overení v obchode. Ak je preukázaná iba nezhoda dostupnosti, `pendingGenerationCards` zaznamená obe ID aj obe hodnoty, uloží osi variantu a ponechá riadok blokovaný.

Operator-approved proposals can be previewed and saved together with the private repair-plan control. Each new item receives its own instruction and audit event. Previously managed subjects, parent instructions, withdrawals and deletion decisions are excluded. A separate verification action checks current Hub links against a recent published catalog run and records native IDs. Retrying generation availability after the shop stock policy changes is limited to the old journal, unchanged source archives, exact existing cards and current review revisions. A dispatched request is observed before any further action.

## Durable catalog migration

Automatic catalog and source-card workers start only when the server setting
`ATOMER_CATALOG_WORKER_ENABLED=true` is explicitly configured. It defaults to
`false`; after the API restarts, neither Atomer worker resumes stored campaigns.
Campaigns, work ownership, source archives, write intents and receipts remain
intact. Other Hub workers and explicit operator actions keep their existing
behavior. Re-enabling requires a deliberate setting change and API restart;
review any intervening XML imports before resuming the saved campaign.

The authenticated report can register a group of exact families or the complete approved catalog from the pinned buyers and administrative CSV archives. The operator can see registration, pending numbers, verification, explicit exclusions, partial results, blocking reasons and the next retry in the report. A preview is read-only. Apply registers the work in PostgreSQL. When enabled, the API worker resumes that work after restart and stores the source fingerprint, recorded intent and observed native IDs. A job that reached the send boundary is checked against the shop before further writes.

The source adapter verifies visibility and commercial fields separately for parents and sellable leaves. Original HTML, option mappings and unmapped source values are retained privately. Hub card bindings are checked against the exact xTrek leaf and, when available, the matching BIKETREK leaf. A native product ID by itself does not complete a family. Per-family observed evidence is available through the authenticated catalog-engine/family endpoint. Product creation and card linking do not imply physical stock initialization. Native stock authority is configured in the separate stock workflow.

For generation repairs the private plan also supports a read-only observation of an existing journal. It can show which upstream field changed and compare the parent and native variant stock representations before any repair.

### Evidence for each migration stage

The source-card worker can prepare a missing sellable card from the pinned administrative export while Upgates is busy. It checks existing cards, SKU aliases, barcode ownership, shop bindings and operator decisions in a database transaction. The separate receipt records the card ID and whether that worker created or reused it. Shop verification continues against the same card. A full item receipt requires the applicable native IDs and a successful comparison of the mapped content. The dashboard counts each stage separately.

For an existing xTrek product, the worker reads the native family, compares the selected Atomer fields and records the intended changes before dispatch. Readback checks the original IDs and protected siblings. When an API limit is reached the Hub records the next allowed attempt. An uncertain write goes to readback. Exact revision tokens on individual families guard reconsideration after new evidence. Supplier barcode corrections, category mappings and conflict evidence are stored with the private campaign.

An explicitly selected missing category can be restored under its verified native parent after a full-tree lookup. Upgates assigns the ID and the next lookup confirms its code, parent and menu setting before product use. Manual closures, store-specific decisions and financial or quantity discrepancies retain their own status.

### Retrying a product or a card

The campaign status gives each delayed phase its next retry time and reason. It reports native requests separately from work postponed locally by the shop quota. Priority products can share a native create request, and an interrupted write stays in observation until its outcome is known. Category restoration has a separate retry schedule.

Submitting new manufacturer evidence queues only matching unresolved or blank local cards for another check. The original creation receipt stays available across retries and reindexing the same source. A card made by the migration can receive a previously missing GTIN once the source row, official evidence, card ID and ownership checks agree. The per-family detail distinguishes the local card receipt from the later shop and content receipts.

The standalone-to-variant repair requires an exact existing parent, positive identification of its former sellable and verified warehouse balances before reusing that parent and card. A private plan records the expected native IDs, operator revisions and original product before the write. Its readback verifies sibling identity and the allocation of any original stock.
