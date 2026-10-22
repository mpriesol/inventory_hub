# Typ karty, skladové jednotky a nákupné balenia

Karta v `products` má vlastný typ `item_type`: `stock` je fyzický tovar,
`service` je služba. Typ neurčuje názov, kategória ani prefix SKU. Služba môže
mať identifikátory, dodávateľa a väzbu na e-shop. Po jednoznačnom rozpoznaní
sa v objednávke označí `non_stock` a nevytvorí rezerváciu, pohyb ani FIFO
vrstvu. Neznámy alebo konfliktný kód sa tým nestane službou. Ručné riadky
bez identity naďalej zostávajú mimo skladu podľa existujúcich pravidiel.

`stock_unit` je základná jednotka fyzickej zásoby:

| Hodnota | Význam | Povolené množstvo |
| --- | --- | --- |
| `pcs` | Jeden kus | Celé číslo |
| `pair` | Jeden kompletný predajný pár | Celé číslo |
| `pack` | Jedno kompletné predajné balenie | Celé číslo |
| `m` | Meter | Najviac tri desatinné miesta |

Pár rukavíc predávaný ako jeden pár preto nemusí mať skladový prepočet na
dve rukavice. Balenie evidované a predávané ako celok je jedna skladová
jednotka. Definícia sa nastavuje pri založení karty alebo v detaile produktu.
Po skladovom pohybe, rezervácii, potvrdení evidencie alebo nenulovom zostatku
sa typ a jednotka uzamknú. Zmena jednotky nie je premenovanie starej zásoby.
API aj databázové ochrany odmietnu pokus spätne zmeniť jej význam.

## Nákup v inej jednotke

Príjemka uchováva fakturované množstvo a pôvodnú jednotkovú cenu. Na
konkrétnom priradenom riadku možno nastaviť nákupnú jednotku a výslovný
faktor **nákupná jednotka → skladová jednotka**. Príklad: dva nákupné balíky
po desať kusov, cena 50 EUR za balík, znamenajú príjem 20 kusov a náklad
5 EUR za kus. Faktúrový riadok zostane 2 × 50 EUR.

`POST /suppliers/{supplier_code}/receiving/sessions/{session_id}/lines/{line_index}/unit`
prijíma `purchase_unit` (`pcs`, `pair`, `pack`, `m`) a
`purchase_to_stock_factor` ako kladný desatinný reťazec s najviac tromi
desatinnými miestami. Index riadka je od nuly. Odpoveď má `success`, `line`
a `summary`, rovnako ako zmena množstva. Vyžaduje priradenú fyzickú kartu
a otvorenú príjemku. Dokončený doklad sa nemení.

Riadok ukladá aj cieľovú `conversion_stock_unit`; zmena definície karty pred
dokončením vyžaduje nové overenie prepočtu. DTO vracia `item_type`,
`stock_managed`, `purchase_unit`, `purchase_to_stock_factor`, `stock_unit`,
`stock_quantity`, `stock_unit_cost` a prípadnú `unit_conversion_error`.
Nákupná cena sa delí faktorom a zaokrúhli na existujúcu presnosť FIFO,
štyri desatinné miesta, HALF_UP. Prepočet sa odmietne, ak by zmenil centový
súčet riadka, zaokrúhlil kladný náklad na nulu alebo zaokrúhlil množstvo.
Neznáma cena zostáva neznáma; fyzický faktúrový príjem naďalej vyžaduje
známu cenu. Riadok služby môže byť uzavretý bez skladového nákladu.

Prijaté množstvo aj prírastok skenera zostávajú v nákupnej jednotke príjemky.
Pri faktore 10 znamená jeden prírastok jedno nákupné balenie, teda desať
skladových kusov. Pri počítaní jednotlivých kusov treba zadať zodpovedajúce
nákupné množstvo; samotný EAN nepreukazuje, či bol skenovaný kus alebo balík.
Náhľad ukazuje výsledné skladové množstvo pred dokončením.

Faktor sa nikdy neodvodzuje zo starého `product_supply_sources.pack_size`,
názvu produktu ani PDF jednotky. Predvolený faktor 1 znamená rovnakú
nákupnú a skladovú jednotku. Parsery zachovávajú pôvodné fakturované údaje;
obsluha nastaví rozdielnu nákupnú jednotku pred zaúčtovaním. Opakované
dokončenie vracia pôvodný výsledok a nevytvorí druhý prepočítaný príjem.

## Podporované skladové operácie

Počiatočný stav, príjem, FIFO a fyzický prepočet podporujú všetky uvedené
skladové jednotky. CSV počiatočného stavu používa `ks` pre `pcs`, ďalej
`pair`, `pack` a `m`; jednotka musí súhlasiť s kartou. Zmiešaný súhrn má
`unit: mixed`, `summary.quantity: null` a samostatné `quantity_by_unit`.
Množstvá rôznych jednotiek sa nesčítajú do spoločného počtu kusov.

Aktuálny objednávkový a publikačný kontrakt Upgates naďalej používa celé
predajné jednotky s API jednotkou `ks`. Pre karty `pcs`, `pair`, `pack`
znamená jedna objednaná jednotka jeden konkrétny predajný kus/pár/balenie.
Iné vstupné jednotky sa neprekladajú odhadom. Karty `m` sa na skladové
objednávkové spracovanie a publikovanie množstva do e-shopu výslovne
odmietnu, aj keď je ich aktuálne množstvo celé. Vratky viazané na predajný
výdaj zostávajú v pôvodnom kontrakte celých predajných jednotiek.

## Migrácia a overenie

Migrácia 031 pridáva typ/jednotku karty a údaje prepočtu na príjemkový
riadok. Existujúce karty dostanú `stock`/`pcs`, riadky faktor 1. Žiadne
historické množstvo ani cena sa neprepočíta. Rozšíria sa iba známe kontroly
jednotiek počiatočného stavu. Databázové triggery blokujú fyzické pohyby,
zostatky a rezervácie služieb a zmenu použitej skladovej definície.

Regresie pokrývajú presnosť prepočtu, pôvodné faktúrové hodnoty, opakované
zaúčtovanie, služby s natívnou e-shopovou identitou, konfliktné kódy,
zmenu cieľovej jednotky a desatinný počiatočný stav. Lokálne prešli čisté
testy; databázové scenáre vyžadujú izolovanú PostgreSQL databázu v CI.
Tento text neznamená aktiváciu skladovej autority ani vykonaný živý príjem.
