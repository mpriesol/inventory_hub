# Originálne predajné doklady z Upgates

Hub archivuje už vystavené doklady e-shopu. Vystavovateľom zostáva e-shop;
import nevystavuje faktúry, neposiela e-maily, nezapisuje platby do e-shopu ani
nemení objednávky, fyzický sklad alebo FIFO. Predajné doklady sa ukladajú do
spoločného archívu cez `services/invoice_archive.py` a migráciu `033`.

Implementované v kóde 22. 10. 2026. Syntetické transportné a servisné testy
prešli lokálne; tento záznam nepotvrdzuje živé stiahnutie z BIKETREK ani stav
produkčnej databázy. PostgreSQL kontroly archívu a autentizácie sú samostatnou
súčasťou CI.

## Podporovaný zdroj

[Oficiálna referencia Upgates](https://docs.upgates.com/api-reference/faktury)
uvádza čítanie `GET /api/v2/invoices` po stranách do 100 dokladov a originál
`GET /api/v2/invoices/{invoice_number}/pdf`. Zdrojovým identifikátorom je
presné `invoice_number`; archív ho odlišuje podľa `shop_id`. Podporované typy
sú faktúra, dobropis a účtenka.

Používa sa existujúca konfigurácia príslušného e-shopu a spoločný API rozpočet
`BudgetSession`. Je potrebný aktívny e-shop s `platform=upgates` a platným
HTTPS API pripojením. xTrek vedený ako Atomer sa odmietne s konkrétnou chybou;
jeho prípravné Upgates pripojenie sa nepovažuje za zdroj živých predajov.

## Ručné načítanie a pokračovanie

Prihlásený operátor volá `POST /api/invoices/sync/shop/{shop_code}`:

```json
{
  "date_from": "2026-10-01",
  "date_to": "2026-10-31",
  "page": 1,
  "offset": 0
}
```

Dátumy určujú **čas vytvorenia v zdroji**, nie dátum splatnosti ani deň
poslednej platby. Ide o celé dni v `Europe/Bratislava` so správnym prechodom
letného času. Rozdiel hraníc smie byť najviac 366 dní. Na obnovu platobného
stavu staršieho dokladu treba znovu vybrať obdobie jeho vytvorenia.

Jedno volanie načíta jednu zdrojovú stranu a najviac desať originálnych PDF:
najviac jedenásť pokusov o externé GET. Nemá neobmedzenú slučku ani automatické
opakované sieťové požiadavky. Odpoveď obsahuje `imported`, `unchanged`, `failed`,
jednotlivé `items`, `remote_requests`, počet zdrojových strán a `next`.
`unchanged` znamená existujúcu obchodnú identitu dokladu; pozorovanie úhrady
alebo nová binárna verzia originálu sa pritom môžu doplniť.

Pre pokračovanie klient zachová rovnaké dátumy a prenesie celý objekt `next`
(`page`, `offset`, `page_token`). Token overuje pripojenie, obdobie a poradie
čísel na aktuálnej strane. Pri zmene strany sa import zastaví chybou
`invoice_source_page_changed`; obsluha začne znovu od prvej strany daného
obdobia. Už uložené doklady sa tým neduplikujú. API neposkytuje nemenný
celkový zoznam počas stránkovania; pri súbežných zmenách zdroja možno znovu
prejsť celé zvolené obdobie.

Každý doklad má vlastný commit. Pri výpadku PDF alebo vyčerpaní API rozpočtu
zostávajú predošlé doklady uložené a `next` ukazuje na nedokončený doklad.
`stopped_error` a prípadné `retry_after` vysvetľujú zastavenie. Neplatné údaje
alebo konflikt už archivovaného dokladu sú vo výsledkoch jednotlivých položiek;
ďalšie položky môžu pokračovať. Po oprave sa obdobie načíta znovu.

## Pôvodné údaje a prepojenie produktu

Archív zachová pôvodnú menu, presné desatinné sumy, znamienka dobropisu,
jednotky, položky a celý zdrojový JSON. Chýbajúca suma ostáva neznáma;
chýbajúca mena sa nenahrádza EUR. Údaje mimo podporovanej presnosti alebo
rozsahu sa odmietnu viditeľne, bez zaokrúhlenia či skrátenia identifikátora.
Zdrojové číslo dokladu má aktuálne limit 100 znakov existujúceho poľa archívu.

Pri zdrojovom riadku typu `product` sa karta smie priradiť iba cez jednu
presnú, aktívnu väzbu `shop_products` v tom istom e-shope. Väzba musí mať
overené natívne ID produktu a pri variante aj variantu. Rozdiel veľkosti
písmen, viac vlastníkov alebo chýbajúce natívne ID znamenajú nepriradený
riadok. Hub SKU sa globálne neporovnáva a chýbajúci EAN sa nevymýšľa.
Poplatky, doprava a ďalšie typy riadkov sa podľa náhodnej zhody kódu nepriraďujú.

Uložené `match_evidence` obsahuje ID väzby, karty a e-shopu, zdrojový kód,
natívne ID a čas pozorovania. Ide o overenie **aktuálnej väzby v čase prvého
importu**, nie o dôkaz vlastníka kódu v ľubovoľnom historickom okamihu.
Opakovaný import toto priradenie ani pôvodné riadky neprepisuje. Import
nevytvára skladovú kartu, nový identifikátor ani dodatočné skladové pohyby.

## Opakovanie, originály a úhrady

Jedinečná zdrojová identita a databázový zámok bránia dvojitému zápisu aj pri
súbežnom načítaní. Zmena obchodných údajov, účastníkov alebo riadkov rovnakého
zdrojového dokladu vyvolá `invoice_source_changed`; staré údaje sa neprepíšu.
Presný obsah PDF je uložený podľa SHA-256. Ak e-shop pri nezmenených obchodných
údajoch znovu vytvorí PDF napríklad s označením úhrady, pribudne samostatná
verzia originálu; prvý hlavný originál zostáva zachovaný.

Zdrojový stav platby je samostatné pozorovanie. Zo známej celkovej sumy a
známeho zostatku sa odvodí uhradená suma ich rozdielom, vrátane znamienka
dobropisu. Samotné `paid_yn=false` neznamená nulovú úhradu. Pri explicitnom
`paid_yn=true` a známej celkovej sume môže zdroj potvrdiť úplné vyrovnanie.
Rozpor alebo preplatok sa neoreže na nulu. Dátum úhrady sa uloží do
`source_paid_date`; z dátumu sa nevyrába presný čas `source_paid_at`.
Opakované načítanie obnoví tieto pozorovania, ale nevytvára ručnú platbu
ani nemení miestne platobné udalosti.

## Transport a overenie

PDF sa sťahuje len z pevnej autentizovanej API cesty. Zdrojové
`invoice_pdf_url` sa uchová ako údaj, nikdy sa nenasleduje. Presmerovania sú
zakázané a TLS overovanie povinné; pripojenie sa medzi zoznamom a PDF nesmie
zmeniť. JSON má limit 8 MB, PDF 25 MB; čítanie je časovo ohraničené.
Kontroluje sa MIME typ, PDF hlavička a koncová značka. Binárne PDF sa
neprepisuje ani nenahrádza vlastným vygenerovaným dokladom.

Zdrojové telá, zákaznícke údaje a prihlasovacie údaje sa nezapisujú do
diagnostických logov ani chybových odpovedí. Router vyžaduje operátorský
prístup, pri cookie požiadavkách aj existujúcu CSRF ochranu, a vracia
`Cache-Control: no-store`. Sťahovanie uložených originálov používa spoločnú
autentizovanú hranicu súborov archívu.

`test_shop_invoice_source.py` a `test_shop_invoice_sync.py` overujú hranice
stránkovania, desaťdokladový limit, obnovenie po výpadku, presné sumy a meny,
platobné pozorovania, jednoznačné produktové väzby, odmietnutie iných typov
riadkov, zmenu obchodných údajov, pevné API cesty, autentizačné chyby,
presmerovania a neplatné alebo príliš veľké PDF. Syntetické testy nenahrádzajú
overenie dostupnosti konkrétneho účtu Upgates a reálnych dokladov.
