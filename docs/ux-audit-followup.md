# UX audit: realizované úpravy

Východiskový audit: `main` / `e7ff080df9306a05f84a31045fd119abb2a8e02d`.
Implementačná vetva: `codex/ux-audit-parameter-mapping`. Zmeny sú aditívne v API/UI,
bez migrácie, zmeny pravidiel AI, aktivácie skladovej synchronizácie či opravy
produkčných skladových pohybov. Stav nasadenia určuje výsledok build/deploy workflow.

| Bod | Zmena a aktívne miesto |
| --- | --- |
| F01 | `/stock`: spoločná ochrana SPA navigácie a reloadu zahŕňa aj otvorenú bunku a neisté uloženie. Zamknutie vyžaduje potvrdenie zahodenia. |
| F02 | `/stock`: zmenené, ešte nepoužité filtre majú výrazné hlásenie a tlačidlo Použiť filtre; starý výber nemôže spustiť hromadnú operáciu. |
| F03 | `/stock`: po otvorení odomknutej stránky sa načítajú všetky karty, vrátane nepotvrdených zásob. Aplikované filtre, stránka, výber a zvislý posun sa vrátia v tej istej karte prehliadača. Pri zmene prihlasovacieho kontextu sa vymažú. Neuložené produktové hodnoty ani token sa do tohto kontextu nezapisujú. |
| F04 | `/stock`: pohľady Sklad, Produkty a ceny, E-shopy využívajú existujúce poradie/šírky stĺpcov. Nový predvolený pohľad dáva fyzické množstvá hneď za identitu. Existujúce osobné nastavenia zostávajú zachované. Horné ovládanie je kompaktnejšie. |
| F05 | `/product-import`: Spracovať podľa pravidiel a viditeľný rozsah všetkých riadkov/cieľov upozorňujú na možné automatické uloženie a publikovanie podľa politiky ešte pred AI. Samostatné označenie riadkov obmedzuje iba hromadné úpravy. |
| F06 | AI detail/príprava: preložené kódy s konkrétnym názvom parametra, diagnostika očakávanej a aktuálnej hodnoty a odkaz na opravu. Príjem zachová konkrétny dôvod serverového odmietnutia vrátane riadka. |
| F07 | `/product-import`: výber rozlišuje počet na stránke a mimo nej. Predvolená hromadná úprava má rozsah iba viditeľného označenia; celý výber je explicitná voľba. Spoločné rodinné polia upozorňujú na propagáciu na súrodencov. |
| F08 | Navigácia Príprava produktov, zoznam s názvom/SKU, serverové vyhľadávanie a filtre Rozpracované/Vyžadujú pozornosť/Dokončené/Všetky. Zobrazuje sa najnovších 100 výsledkov hľadania. |
| F09 | `/receiving/:invoiceId`: nákupná cena bez DPH na riadku, mena a sklad z uloženého príjmu, súhrn množstva/hodnoty a dôsledkov pred každým zaúčtovaním. Neznáma cena sa nezobrazuje ako nula. Hodnota riadka sa počíta Decimal na serveri. |
| F10 | Príjem: HTTP 409 s kandidátmi otvorí výber faktúrového riadka podľa názvu, množstva a ceny. Sken zostáva vo fronte. Až po dokázanom odmietnutí môže dostať `line_id`; po neistom výsledku sa opakuje nezmenený payload a UUID. |
| F11 | `/shops`: konkrétne pracovné odkazy s uvedeným smerom prenosu. Všetky tri objednávkové obrazovky zdieľajú navigáciu a odovzdávajú zvolený e-shop. |
| F12 | Spoločný terciárny text používa svetlejší token `#aaa69d`; tmavé pozadia a ostatné stavové farby zostávajú zachované. |
| F13 | `/stock` a nový súhrn príjmu: lokalizované čísla a dátumy, množstvá bez umelých koncových núl, ceny s vhodnou zobrazovacou presnosťou. Editované zdrojové reťazce a identifikátory sa neprepisujú. |
| F14 | Archív: Nahrať doklad do archívu s vysvetlením a odkazom na príjem so zvoleným dodávateľom. Príjem: Nahrať faktúru na príjem. |
| F15 | Filter značky zjednocuje iba zobrazované možnosti a vyhľadávací kľúč, vrátane medzier a veľkosti písmen. Pôvodné názvy značiek v dátach sa nemenia. |

## Parametre Northfinder a ostatných dodávateľov

`FARBA` → `Farba` a `VELKOST`/`VEĽKOSŤ` → `Veľkosť` sa rozpoznajú cez
normalizovaný názov v schválenom registri konkrétnej kategórie. Rovnaké pravidlo
platí pre ľubovoľný registrovaný parameter. `choice` sa stáva variantovým
parametrom aj pri rozdieloch v diakritike, medzerách a veľkosti písmen.

Pri sémantickom premenovaní, napríklad `Colour` → `Farba`, AI uvedie explicitný
`source_name` zo zdrojových variantových atribútov. Validator overí zdroj,
konzistentnosť mapovania, rozsah a presnú hodnotu pre konkrétny produkt. Dve osi
jedného SKU sa nesmú zlúčiť, už rozpoznaná os sa nesmie presmerovať a AI nemôže
zmeniť variantové hodnoty, rodinu, SKU či EAN. Nejasný význam ostáva dôvodom na
kontrolu, nie na vymyslenie údaja. Ostatné nevariantové parametre AI vypĺňa podľa
registra a jeho hodnôt z dodaných/overených faktov.

Schválené názvy sa prenášajú aj do variantov výstupného Upgates payloadu a do
importnej tabuľky. Pôvodný feed ostáva nemenný. Opätovné prevzatie AI rozpozná
predchádzajúce vlastné zmeny cez existujúci odtlačok a snímku; ručné bunky zachová.
Nové voliteľné `source_name` pri `null` nemení serializáciu ani odtlačok starých
obsahov. Staré úlohy nezískavajú novú automatickú publikačnú politiku.

Starý zablokovaný výsledok možno v AI detaile **Znovu overiť obsah bez AI**.
Tento krok nevytvára nové platené generovanie ani neschvaľuje publikovanie.
Následné schválenie/prevzatie a odoslanie rešpektujú uloženú politiku a pôvodnú
prípravu; nevytvárať duplicitný koncept na obídenie kontroly.

## Overovanie

Syntetické testy pokrývajú názvy a sémantické mapovanie, odmietnutie kolízií a
zmenených variantových hodnôt, export, opakované prevzatie AI, ručné úpravy,
navigačnú ochranu otvorenej bunky, viditeľný/skrytý hromadný výber a sken s
nejednoznačným riadkom a stratenou odpoveďou. Databázové testy pridávajú
vyhľadávanie príprav a filtrovanie aliasov značiek bez zmeny pôvodných dát.
Plné databázové kontroly vyžadujú izolovaný localhost `*_catalog_test`; výsledok
lokálnej sady s preskočenými DB testami nenahrádza povinný CI gate.

Používateľské overenie po nasadení: nájsť neaktívnu neprepojenú kartu, upraviť
bunku a skúsiť odísť, upraviť cenu iba viditeľnému výberu, skontrolovať Northfinder
parametre a dokončiť syntetický príjem s dvoma riadkami rovnakého EAN. Zaznamenať
čas, návraty, chyby a pochopenie dopadu; dokument neuvádza vymyslené merania.
