# Identita produktov a automatické SKU

Rozhodnutie vlastníka z 23. 10. 2026: nové fyzické produkty používajú presne
`<dvojpísmenová predpona výrobcu>-<kód výrobku u výrobcu>`. Napríklad výrobné
kódy `00042` pod značkami Topeak a Shimano vytvoria `TP-00042` a `SH-00042`.
Register výrobcu zaručuje jedinečnosť dvojpísmenovej predpony. Hub nepoužíva
číselný rad, náhodnú príponu ani dlhší prefix pri kolízii.

Jedna konkrétna predajná jednotka má jednu kartu Hubu s interným `products.id`.
Cudzie kľúče spájajú sklad, ponuky dodávateľov, e-shopy a doklady. Existujúca
karta si ponechá SKU. Aj produkt, ktorý už existuje v e-shope a až teraz sa
preberá do Hubu cez overený Upgates import, si ponechá svoj pôvodný kód.
Bežná aktualizácia značky alebo pripojenie ďalšieho dodávateľa ho neprečísluje.

## Výrobný a dodávateľský kód

| Údaj | Význam |
| --- | --- |
| Kód výrobku u výrobcu | Presný kód konkrétneho výrobku, variantu a predajného balenia vydaný výrobcom. Zdroj nového SKU. |
| Kód dodávateľa | Vlastný katalógový kód ponuky konkrétneho dodávateľa. Ukladá sa samostatne a páruje iba v rozsahu dodávateľa. |
| EAN / UPC / GTIN | Čiarový identifikátor na dohľadanie a overenie identity. Nenahrádza výrobný kód pri pridelení nového SKU. |
| Rodičovský kód | Označuje rodinu variantov. Nie je kódom konkrétnej skladovej jednotky. |

Rovnaký výrobok môže mať u dvoch dodávateľov rôzne kódy. Zhodný dodávateľský
kód sám osebe nepreukazuje rovnaký výrobok. Názov poľa `MPN`, `SKU` alebo
`manufacturer_code` automaticky nepotvrdzuje jeho význam. Kódy zostávajú
textom, vrátane úvodných núl, interpunkcie a veľkosti písmen.

## Automatické vytvorenie z feedu

V spoločnom mapovaní dodávateľa sa osobitne mapujú **Kód dodávateľa** (`code`)
a **Kód výrobku u výrobcu** (`manufacturer_code`). Pri výrobnom poli treba
raz potvrdiť, že obsahuje kód konkrétneho variantu a balenia vydaný výrobcom.
API ukladá `manufacturer_code_confirmed=true` na príslušnej väzbe mapovania.

Potvrdená väzba vyžaduje skutočné zdrojové pole. Nie je prípustná vygenerovaná
konštanta, predvolená náhrada ani prepis kódu transformáciami; dovolené je iba
odstránenie okrajových medzier. Potvrdenie patrí spoločnému mapovaniu, nie
obsahovej odchýlke jedného e-shopu. Revízia, cesta poľa a presná načítaná hodnota
ostávajú v existujúcom `mapping_provenance.fields.manufacturer_code`.

Bežný refresh rovnakého mapovania zachová potvrdenie. Chýbajúce zdrojové pole
neprevezme starý/neoverený údaj ako nový dôkaz. Príprava skontroluje zhodu
pôvodného zdroja, potvrdenej hodnoty a aktuálneho riadka; úprava výrobného kódu
alebo značky nemôže potichu použiť staré potvrdenie. Zmena zdroja, cieľa alebo
transformácie v editore zruší potvrdenie mapovania.

Z potvrdeného výrobného kódu a registrovanej značky Hub automaticky pripraví
SKU a uloží dôkaz identity pri vytvorení karty. Obsluha nezadáva SKU pre každý
výrobok a nestará sa o jeho jedinečnosť. Dodávateľský kód sa nezmení a cez
`product_supply_sources` zostáva naviazaný na svoju ponuku.

## Ručné overenie a existujúce produkty

Bez potvrdeného výrobného poľa možno pri príprave novej karty skontrolovať
výrobcu, opraviť výrobný kód a výslovne potvrdiť konkrétny variant/balenie.
Jednoduchý formulár uchová operátorské potvrdenie; nevydáva ho za dokumentáciu
výrobcu. Úprava výrobného kódu pred založením vyžaduje nové potvrdenie.
Chýbajúci výrobný kód blokuje iba nové fyzické pridelenie. Existujúcu kartu
možno dohľadať nezávislou jednoznačnou väzbou, overeným čiarovým kódom alebo
existujúcim SKU aj bez nového výrobného dôkazu.

Overený produktový import z Upgates je samostatný postup na prevzatie produktov,
ktoré už v e-shope existujú. Vytvorí prípadnú chýbajúcu Hub kartu s pôvodným
vzdialeným kódom a uloží natívne produktové/variantové ID. Nie je to pridelenie
nového výrobného SKU. Katalógový `shop_matches` zo starej cache sám osebe nie je
pokyn na vytvorenie alebo premenovanie karty. Už obsadený cieľ pri publikovaní
musí prejsť existujúcimi kontrolami; Hub ho neprepíše novým kódom.

Služba musí mať výslovné `item_type=service`. Používa existujúci registrovaný
servisný kód a dvojpísmenový priestor služby; nepotrebuje vymysleného výrobcu.
Nevytvára fyzické množstvo, rezerváciu ani FIFO. Typ služby sa nesmie použiť
ako obchádzka pre fyzický tovar bez výrobného kódu.

## API a kontrola zápisu

`POST /api/sku-policy/preview` prijíma `sku_identity` so značkou, presným
`source_code` a významom kódu. Pre nové fyzické produkty musí byť
`code_kind=manufacturer`, `leaf_confirmed=true` a neprázdny `source_ref`.
Dodávateľské a GTIN identity zostávajú použiteľné na overenie historickej karty,
nevytvoria však nové fyzické SKU. Samotný text `sku` slúži na vyhľadanie
existujúcej karty, nie na obídenie pravidla.

Náhľad je čítací. Založenie zopakuje kontrolu pod spoločným
`IDENTITY_WRITE_LOCK`; UUID chráni opakované uloženie po výpadku. Pridanie
ponuky a aliasu je atomické. Konflikt značky, výrobného kódu, čiarového kódu
alebo vlastníka zastaví zápis. Pôvodné dôkazy, skladové pohyby a FIFO ostávajú.

Nové produkty používajú rovnaké pridelené Hub SKU v BIKETREK aj xTrek.
Existujúce overené e-shopové mapovania môžu mať vlastné pôvodné kódy.
Pred odoslaním sa kontroluje obsadenosť produktových aj variantových kódov
samostatne v každom cieľovom e-shope. Pridelenie alebo uloženie karty samo
nevytvára zásobu, e-shopový produkt ani fyzický príjem.
