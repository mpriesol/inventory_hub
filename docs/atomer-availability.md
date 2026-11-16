# Atomer: denný export dostupnosti

Stránka `/sync/atomer` pripravuje CSV pre **ručný import do pôvodného Atomeru xTrek**. Export nemení množstvá v Hube, neodosiela požiadavky na zápis do Atomeru ani Upgates a nezakladá produkty. Technický kód `xtrek` a jeho existujúce Upgates pripojenie zostávajú zachované.

## Zdroj a pokrytie

Správca zadá buyers XML do osobitnej súkromnej konfigurácie `shops/xtrek/config.json`, vetva `atomer_availability.export_url`. Voliteľné `admin_export_url` dopĺňa plný administratívny CSV export. URL sa cez API nevydávajú; prehliadač dostáva iba príznak nakonfigurovania. Žiadna adresa s privátnym kľúčom nie je predvoľbou v Gite.

Buyers parser používa presné `PRODUCT_ID`, `PARENT_ID`, `EAN`, `PRODUCT_NAME`, `MAIN_IMAGE`, `AVAILABILITY` a `VISIBILITY`. Nepoužíva `COUNT_ON_STOCK` ako vlastnú zásobu. Každý nový náhľad stiahne zdroj znovu; to nie je dôkaz, že Atomer práve nanovo vygeneroval obsah. Porovnáva sa zdroj s aktuálnym Hubom, nikdy predchádzajúce CSV s Hubom. Nevyskytujúci sa produkt sa nepovažuje za skrytý, nulový ani odstránený.

Administratívny export môže byť UTF-8 alebo CP1250; duplicitné nesúvisiace charakteristiky neprekážajú. Používa existujúci parser a katalógové číslo. Očakáva `Kat. číslo`, `Kat. číslo rodiča`, `Interné číslo`, `Viditeľný`, `Názov tovaru`, `Dodacia doba` a ideálne `EAN kód` a `Obrázky`. Zdroj musí obsahovať aj materské riadky. Voľbu **celý katalóg** treba zvoliť iba pri exporte vrátane skrytých produktov a variantov. Hub cez existujúci index overí aj referencie rodičov interným ID; v CSV zachová presnú pôvodnú rodičovskú bunku. Historické rodiče bez adresovateľného katalógového čísla a rozdielne raw spellings zostanú blokované, bez náhrady SKU interným ID. Neplatné/quarantined riadky bránia prijatiu zdroja ako celého katalógu. Hub skontroluje existenciu rodičov; sám nedokáže dokázať úplnosť externého katalógu. Náhľad uvádza aj počet blokovaných zdrojových riadkov; úplný katalóg neznamená, že každý jeho riadok možno bezpečne exportovať. Aktuálne administratívne obchodné hodnoty majú prednosť pred periodickým buyers súborom; rozpor EAN ostáva blokovaný.

CSV možno aj nahrať cez `POST /api/atomer-availability/source/admin`; uloží sa súkromne v PostgreSQL. Nahratý podklad je použiteľný 24 hodín. Starší podklad sa na stav dostupnosti/viditeľnosti nepoužije a výsledok uvedie upozornenie. Automaticky stiahnutý admin export má prednosť. Prázdna hodnota `admin_export_url` odstráni toto voliteľné napojenie; prázdne `feed_url` zachová buyers URL.

Kontrola dodaného buyers zdroja 16. 11. 2026 našla 13 314 riadkov a hodnotu viditeľnosti 1 pri každom. Väčšina rodičov nemala vlastný riadok. Je to pozorovanie konkrétneho zdroja, nie dôkaz úplnosti alebo všeobecná vlastnosť každého buyers exportu. Bez plného administratívneho zdroja náhľad explicitne oznamuje neoverené pokrytie skrytých produktov a viditeľnosti chýbajúcich rodičov. Rodič sa zobrazí ako kontext, ale nevymyslí sa jeho stav a neexportuje sa zápis na neoverený kód.

## Výpočet a manuálna kontrola

Párovanie používa spoločný resolver presného SKU, historických aliasov a overených GTIN. Nejednoznačné kódy, konflikty EAN, poškodené identity a duplicitné zdrojové kódy zostávajú uvedené s dôvodom. Blokovaný riadok nebráni exportu ostatných overených riadkov. Servisné položky a predajné balenia bez Atomer väzby na potvrdený prevodný faktor sa neexportujú.

Dostupná vlastná zásoba je potvrdený stav konkrétneho zvoleného skladu mínus rezervácie mínus karanténa. Neoverený stav nie je nula. Ak vlastná zásoba nie je potvrdená, riadok ostáva blokovaný. Pri kladnej vlastnej zásobe je návrh `SKLADOM` a viditeľnosť 1, vrátane overeného skrytého rodiča. Pri potvrdenej nule sa použije aktuálna spoločná projekcia dostupnosti dodávateľa, inak `overíme`. Nulová zásoba automaticky neskrýva produkt ani nemení jeho aktivitu.

Náhľad ukazuje zdrojovú hodnotu a návrh pre konkrétne kódy a varianty. Upraviteľná dostupnosť a viditeľnosť platia **iba pre tento export**. Nemenia globálnu politiku, skladovú kartu ani ďalšiu dennú dávku. Technický konflikt sa nedá prekryť manuálnym textom. Pred exportom sa opäť načíta identita, potvrdená zásoba a dodávateľské podklady; ich zmena vyžaduje nový náhľad. Náhľad na export platí 30 minút. Už vytvorený CSV je nemenný a zostáva dostupný aj po vypršaní náhľadu.

Príjem prechádza na tú istú stránku s `?receiving_batch=<uuid>`. Backend z dokončenej dávky príjmu získa sklad a skutočné prijaté ID kariet. Browser nemôže svojvoľne zmeniť rozsah tejto dávky. Produkty neprítomné v zdroji sú explicitne hlásené; nevytvárajú sa automaticky.

## CSV a denný beh

Súbor používa UTF-8 s BOM, oddeľovač `;`, úvodzovky pri všetkých poliach a zachováva pôvodné kódy. Obsahuje iba zmenené overené riadky:

| Stĺpec CSV | Mapovanie v Atomeri |
|---|---|
| Katalógové číslo | Katalógové číslo |
| Katalógové číslo rodiča | Katalógové číslo rodiča |
| Viditeľnosť | Viditeľnosť, hodnoty 0/1 |
| Dodacia doba | Text dostupnosti |

Importný profil musí aktualizovať existujúce produkty/varianty a len tieto polia. Nenastavujte tvorbu nových produktov, zmenu cien ani automatické skrytie tovarov chýbajúcich v CSV. Súbor neobsahuje fyzické počty kusov. Aj nulová delta vytvorí platný súbor s hlavičkou a nulovým počtom zmien. Stiahnutie alebo vytvorenie CSV nepotvrdzuje jeho import do Atomeru.

Po nastavení zdroja a zapnutí denného exportu worker vykoná jednu dávku každých 24 hodín. Prvý beh je naplánovaný ihneď. Manuálny náhľad alebo dokončený príjem používa rovnaké služby. PostgreSQL advisory lock zabráni súbežnému spracovaniu vo viacerých procesoch. Rozpracované čítanie možno po reštarte bezpečne obnoviť, pretože neobsahuje externý zápis. Pripravený automatický náhľad po reštarte pokračuje vytvorením CSV. Chyba zachová posledný úspešný súbor a bezpečný kód dôvodu.

CSV bajty sú uložené transakčne spolu s výsledkom a SHA-256 v PostgreSQL. Súbory staršie ako sedem dní sa odstránia iba ak existuje novší úspešný export; posledný úspešný CSV sa zachová aj pri dlhšom výpadku. Novšia verzia musí pokrývať celý katalóg alebo rovnaký rozsah; príjmový export nezmaže posledný export celého katalógu ani inej dávky. Audit náhľadu a výsledku ostáva zachovaný. Rozhranie pre download používa konkrétne UUID behu, autentifikáciu a `Cache-Control: no-store`.

## API a nasadenie

Všetky cesty začínajú `/api/atomer-availability` a vyžadujú operátora; konfigurácia a admin upload správcu.

- `GET /options`, `POST /configure`: revízne nastavenia; zmena má vlastné `request_id` UUID. Rovnaký posledný request/payload je bezpečne obnoviteľný, zmenený payload alebo neplatná revízia skončí konfliktom.
- `POST /runs`: `request_id`, voliteľné `warehouse_code`, `product_ids` alebo `receiving_batch_id`. ID behu je request UUID. Rovnaký request sa znovu nezaradí.
- `GET /runs`, `GET /runs/{id}?offset=0&limit=100`: história, stav, paginované riadky, číselník textov, súhrn a pokrytie. Každý variant nesie meno/obrázok a aktuálnu viditeľnosť rodiča aj pri rozdelení stránkovaním.
- `POST /runs/{id}/export`: `expected_revision` a `overrides` s `code`, voliteľnou `visibility` a `availability`. Opakovanie rovnakého zámeru vráti uložený výsledok; iný zámer nemení hotové CSV.
- `GET /runs/{id}/download`: nemenné CSV alebo 404 po jeho expirácii.

Migrácia `055_atomer_availability.sql` pridáva tri tabuľky. Modul `atomer_availability_migrate` patrí do explicitného runnera a SQL do API obrazu. Worker `services.atomer_availability_worker.run` sa pripája k existujúcemu lifecycle API. Migrácia sama nepridáva nastavenia, neaktivuje skladovú autoritu a nemení zásoby. Pri návrate kódu tabuľky a výsledky ponechajte; už ručne importované CSV sa návratom kódu nevracia.

Po siedmich dňoch, iba ak existuje novší úspešný export, sa starý celý zdrojový snapshot zhutní na exportované zmeny. Hashy, súhrn, audit, ručné rozhodnutia a dátumy zostávajú; posledný úspešný export zostáva plný. Historický detail uvádza toto zhutnenie.
