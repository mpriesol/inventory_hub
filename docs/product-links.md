# Prepojenie existujúcich skladových kariet s e-shopom

Prepojenie priradí existujúcej karte Hubu konkrétny existujúci produkt alebo variant v BIKETREK či xTrek. Nevytvára produkt v e-shope, nemení jeho ceny ani zásoby a nezakladá ďalšiu kartu v Hube. Potvrdenie fyzického skladu zostáva samostatným príjmom alebo inventúrnou opravou.

## Postup obsluhy

1. Otvorte **Sklad**. Pre karty bez potvrdeného príjmu vyberte **Skladová evidencia → Všetky produkty** alebo **Nepotvrdené položky**.
2. Otvorte detail jednej karty alebo označte konkrétne riadky v tabuľke a zvoľte prepojenie s existujúcim produktom e-shopu. Najprv uložte alebo zahoďte rozpracované úpravy buniek.
3. Vyberte cieľový e-shop. Jedna dávka obsahuje 1 až 100 vybraných kariet; ďalší e-shop má vlastný náhľad a výsledok.
4. Pripravte náhľad. Presné spoločné SKU umožňuje automatické nájdenie. Jednoznačný overený EAN môže pomôcť pri kandidátovi známom z uložených údajov e-shopu; nejde o hromadné prehľadanie celého vzdialeného katalógu podľa EAN.
5. Ak sa automatická zhoda nenájde, zadajte konkrétny existujúci kód cieľového produktu alebo variantu a podľa potreby jeho rodiča. Skontrolujte názov, kód, EAN, rodiča a typ položky. Rodič s variantmi nie je samostatná fyzická skladová položka; označenie hlavného variantu na tom nič nemení.
6. Skontrolujte náhľad, označte pripravené riadky a potvrďte prepojenie. Riadky s konfliktom alebo bez nájdeného cieľa sa nemôžu uložiť. Už správne prepojené položky zostanú zachované.

Ak sú všetky vybrané položky už prepojené, náhľad iba zavrite tlačidlom **Hotovo**. Ďalšie potvrdenie väzby nie je potrebné. Pri existujúcej väzbe bez odkazu použite na karte **Načítať odkazy**. Táto akcia overí rovnaký vzdialený produkt alebo variant a doplní jeho dostupné odkazy. Ak ich e-shop neposkytne, rozhranie to uvedie; adresy administrácie ani produktu sa neodhadujú z kódu. Rovnako možno obnoviť odkazy starších väzieb vytvorených skriptom.

Chýbajúca farba alebo veľkosť v odpovedi znamená neznámy údaj, nie dôkaz nesprávneho variantu. Rozhodujú overené identifikátory a explicitný výber; dostupné protichodné údaje sa nesmú ignorovať. Názov, podobnosť názvu ani cena samy neautorizujú prepojenie. Predajné ceny oboch e-shopov sa môžu líšiť.

Náhľad rozlišuje overený čiarový kód Hubu a hodnotu poskytnutú e-shopom. Neplatný čiarový kód zostáva viditeľný aj s upozornením; nepoužíva sa ako overený identifikátor. Chýbajúci údaj znamená, že ho zdroj neposkytol. Pri staršom uloženom náhľade nemusí byť pôvodná neplatná hodnota zachovaná; nový náhľad ju načíta bez opravy alebo zmeny údajov e-shopu.

Ak sa stratí odpoveď pri ukladaní, obnovte výsledok tej istej dávky. Nevytvárajte náhradnú dávku, kým nie je známy výsledok pôvodnej. Opakovanie rovnakého uloženia nezdvojuje väzby; zmena výberu už dokončenej dávky sa odmietne. Pri zmene identity alebo vypršaní náhľadu treba najprv overiť stav pôvodnej dávky a až potom pripraviť nový náhľad.

Pri jednoznačnom odmietnutí platného uloženia server pod spoločným zámkom najprv vráti prípadný už dokončený výsledok alebo trvalo ukončí platnosť náhľadu. Až potom označí chybu `known_unapplied: true`. Rozhranie tak môže povoliť nový náhľad aj po skoršej stratenej odpovedi; oneskorený starý pokus už nemôže zapísať väzby. Samotný stav `prepared` ani sieťová chyba tento dôkaz neposkytujú.

## Kontrakt API

Všetky cesty vyžadujú operátorské prihlásenie, bežnú CSRF ochranu pre cookie relácie a odpovede `Cache-Control: no-store`.

| Cesta | Účinok |
| --- | --- |
| `POST /api/product-links/candidates` | Ponúkne obmedzený zoznam kandidátov z uložených údajov; samotný kandidát ešte neoprávňuje zápis. |
| `POST /api/product-links/preview` | Uloží náhľad vybraných kariet pre jeden e-shop pod klientskym `request_id`. Kontroluje živú vzdialenú identitu cez existujúci limitovaný Upgates klient. |
| `GET /api/product-links/{id}` | Vráti uložený náhľad alebo dokončený výsledok na obnovu po výpadku. |
| `POST /api/product-links/{id}/apply` | Potvrdí presný náhľad a vybranú podmnožinu pripravených riadkov. Väzby zapisuje v jednej transakcii. |
| `POST /api/product-links/refresh` | Pre 1–100 už prepojených kariet jedného e-shopu overí presnú existujúcu väzbu, obnoví lokálne názvy a odkazy a môže doplniť chýbajúce natívne ID rodiča. |

Náhľad uchováva konkrétne SKU, typ produktu/variantu, rodiča, natívne vzdialené ID a identifikačné dôkazy. Pred zápisom sa kontroluje aktuálne spojenie s e-shopom, vzdialená identita aj lokálne vlastníctvo cez spoločný `product_identity` resolver. Zápis používa `IDENTITY_WRITE_LOCK`; existujúca väzba sa potichu nepresmeruje na iný produkt. Konflikt SKU, overených čiarových kódov alebo vlastníkov zastaví zápis.

Vyhľadávanie používa filtrované čítanie vybraných kódov. Upgates rozlišuje filter `codes` pre produkty a `variant_codes` pre varianty. Bežný `/products` nemusí obsahovať variantové parametre; presné varianty sa čítajú cez samostatný `/products/variants` vrátane ich natívneho rodiča. Produktový zoznam môže vynechať obsah variantov, ale samostatná fyzická položka sa prijme iba pri explicitnom potvrdení, že produkt nemá varianty ani nie je súpravou. Chýbajúce parametre sa nedopĺňajú odhadom z rodiča. Jedna fáza používa najviac osem požiadaviek a rešpektuje spoločný API rozpočet; náhľad ani prepojenie neposielajú externý zápis.

Pre varianty sa navyše načítajú presní rodičia cez `codes` s `variants_yn=false`; bežný náhľad preto potrebuje tri GET požiadavky, bez sťahovania súrodencov. Názov a odkazy pochádzajú zo skutočnej odpovede pre daný variant alebo overeného rodiča. `remote.raw_barcodes` zachováva poskytnuté hodnoty, `remote.invalid_barcodes` neplatné časti podľa spoločného rozdeľovača a `remote.eans` iba overené identifikátory. `local_eans` zobrazuje overené hodnoty platnej ručnej úpravy alebo registra Hubu. Tieto nové polia nemusia byť prítomné v starších uložených náhľadoch.

Obnova prijíma `{shop_code, product_ids}` a vracia `{shop_code, remote_requests, rows}`. Každý riadok obsahuje `product_id`, `status` (`refreshed`, ak sú dostupné oba odkazy, inak `incomplete`), `shop_url` a `shop_admin_url`; chýbajúce adresy majú hodnotu `null`. Pred lokálnym zápisom sa pod spoločným zámkom znovu porovnajú všetky vybrané väzby aj aktuálny odtlačok spojenia s e-shopom. Zmena natívneho ID, rodiča, kódu, typu, spojenia alebo lokálnej väzby zápis odmietne. Stratenú odpoveď možno vyriešiť opakovaním obnovy: akcia nevytvára ďalšiu väzbu ani vzdialený zápis.

Potvrdenie prepojenia aj samostatná obnova ukladajú iba pozorovaný názov a odkazy do `_hub_link_metadata` v existujúcej tabuľke `shop_product_content`. Údaje sú viazané na presný natívny cieľ; editor ich použije iba pre zodpovedajúcu väzbu. Existujúci úplný obsah, súrodenci, ceny, zásoby a čas úplného stiahnutia sa zachovajú. Nový záznam obsahujúci iba tieto údaje sa nepovažuje za úplný exportovateľný produkt. Aktuálne chýbajúci odkaz potlačí starý odkaz v editore. Obnova nemení kanonické názvy ani identifikátory Hubu, cieľ väzby, ručné úpravy alebo skladové pohyby. Chýbajúce `external_product_id` variantu doplní iba z čerstvo overenej zhody jeho natívneho ID, kódu a rodiča; už známe ID nesmie nahradiť.

Migrácia `027_product_links.sql` pridáva iba trvalé náhľady a výsledky do `product_link_previews`. Nespúšťa párovanie historických produktov ani nepovoľuje automatizáciu zásob. Nasadenie obsahuje migráciu pred reštartom API. Pri návrate na kompatibilný starší kód možno tabuľku ponechať; nemažte uložené výsledky potrebné na obnovu.

Implementačné a syntetické testy nie sú dôkazom ostrého prepojenia konkrétneho produktu. Výsledok živej dávky sa overuje v jej uloženom výsledku a na karte v Hube.

## Jedna reprezentácia a typované ID (migrácia 030)

Bežný postup priraďuje najviac jednu predajnú reprezentáciu karty v konkrétnom
e-shope. Výslovne overený predaj rôznych počtov rovnakých voľných kusov alebo
presných aliasov celého skladového výrobku s faktorom 1 používa
samostatný [postup zdieľanej kusovej karty](shared-stock-sales.md) s faktorom pre
každú predajnú položku. Bežné párovanie takúto skupinu neprepisuje.
Rovnaký výrobok ako samostatný produkt aj pokladňový variant v BIKETREK je
pri bežnom párovaní konflikt. Obsluha výslovne vyrieši nadbytočný záznam alebo
overí skutočnú skupinu podľa uvedeného postupu. Hub nikdy automaticky nemaže vzdialené produkty ani
nezakladá druhú kartu na obídenie konfliktu.

`shop_products.product_id` odkazuje na internú kartu `products.id`. Natívna
identita e-shopu má tieto jednoznačné polia:

| Pole | Samostatný produkt | Variant |
| --- | --- | --- |
| `external_product_id` | Natívne ID produktu | Natívne ID rodiča |
| `external_variant_id` | `NULL` | Natívne ID variantu |
| `external_code` | Kód predajnej položky | Kód predajného variantu |
| `parent_code` | `NULL` | Kód rodiča |
| `is_variant` | `false` | `true` |

Unikátna väzba `(shop_id, product_id)` zostáva. Ďalšie unikátne indexy chránia
vlastníctvo natívneho samostatného produktu a variantu v oddelených menných
priestoroch. Číselné ID produktu a variantu sa môžu zhodovať bez kolízie.
Pravidelné skladové projekcie, FIFO nákupné ceny a produktové publikovanie
používajú kód explicitnej väzby; nemusia sa zhodovať s interným SKU. Cieľ sa
pred zápisom stále overuje podľa natívnej identity. Existujúce SKU sa nemenia.
Samostatný produkt musí mať overené natívne ID produktu; variant musí mať
natívne ID rodiča aj variantu. Bez nich sa nepublikujú zásoby, nákupné ceny ani
produktové úpravy a neviaže sa automatický natívny účinok objednávky. Samotný
kód produktu tieto kontroly nenahrádza.

Chýbajúce ID na inak rovnakej väzbe doplní bežný postup **Náhľad → Prepojiť**.
Náhľad upozorní na overenie natívnej identity. Potvrdenie znovu načíta presný
vzdialený cieľ, skontroluje pôvodnú lokálnu väzbu a doplní jej ID pod spoločným
zámkom a unikátnymi indexmi. Zachová ID riadku, sklad aj ceny. Dve karty s tým
istým vzdialeným kódom zostávajú konfliktom; náhľad nesmie zvoliť jednu za
obsluhu. Doplnenie ID je zablokované, kým má daná karta alebo rodič odosielaný
či neistý skladový, cenový alebo produktový zápis; najprv sa vyrieši jeho
pôvodná operácia. Táto kontrola sa opakuje pri potvrdení prepojenia.

Migrácia prevedie staré variantové kódy na jednotný význam `external_code`.
Natívne ID rodiča doplní iba z presného uloženého rodiča s jediným zodpovedajúcim
variantom. Chýbajúci dôkaz zostáva `NULL`; neodvodzuje sa z číselnej podobnosti
alebo názvu. Neúplný variantový kód migráciu zastaví v transakcii. Ak staré riadky
prisudzujú rovnaké natívne ID viacerým kartám, migrácia ponechá všetky riadky aj
pôvodný `external_id` ako dôkaz, ale vyprázdni normalizované ID všetkých
konfliktných vlastníkov. Nevyberá víťaza. Unikátne indexy potom chránia nové
overované vlastníctvo. Počet takto označených riadkov je v protokole migrácie. Opakované spustenie
neprepíše neskôr overené údaje starými stĺpcami.

Staré fyzické stĺpce `external_id` a `variant_code` sa pri aditívnej migrácii
ponechajú, ale aplikácia ich už nečíta ani nezapisuje. Rovnomenné hodnoty v
existujúcich API odpovediach a auditných dokumentoch sú odvodené projekcie;
nejde o ďalší zdroj identity. Po nových zápisoch sa nevracajte k binárke, ktorá
používa staré stĺpce. Obnova má použiť kompatibilnú opravu vpred.

## Nahradenie už vymazaného pokladňového variantu

Pri ručnom výbere cieľového samostatného produktu alebo konkrétneho variantu
pod iným produktom možno výslovne označiť
`manual_matches[].replace_deleted_variant = true`. Toto nie je všeobecné
odpojenie ani presmerovanie existujúceho živého produktu. Povoľuje iba prechod
zo starého variantu na existujúcu predajnú položku pri zachovaní rovnakej Hub karty.
Pre variant sú povinné presné `code`, `parent_code` a `is_variant=true`.
Rodičovský produkt s variantmi sám nie je platným cieľom. Náhľad aj potvrdenie
vyžadujú neprázdnu, presne zhodnú množinu platných EAN cieľa a zachovanej karty;
neplatné čiarové kódy cieľa výmenu blokujú. SKU, EAN a ID karty sa nemenia.

Náhľad zobrazí ID skladovej karty, jej EAN, `previous_mapping` a nový cieľ
vrátane kódov a natívnych ID oboch produktov a variantov. EAN karty je lokálny
podklad; vymazanému vzdialenému variantu sa nevymýšľa nový vzdialený údaj. Server pri náhľade aj potvrdení
číta pôvodného rodiča podľa `external_product_id` cez dokumentovaný
[`GET /products/simple`](https://docs.upgates.com/api-reference/produkty-seznamy)
s `variants_yn=true`, osobitne pre `archived_yn=false` a `archived_yn=true`.
Kontroluje natívne ID pôvodného variantu vo všetkých vrátených variantoch;
premenovanie či deaktivácia nie sú vymazaním. Neúplná odpoveď, zmena identity,
opätovné objavenie variantu alebo prekročenie limitu čítania zápis zastavia.
Ak chýba pôvodné natívne ID, najprv treba overiť pôvodnú väzbu načítaním z
obchodu. Samotný chýbajúci kód nie je dôkazom vymazania.

Aktívne objednávky, aktuálne natívne skladové účinky závislé od starej väzby a
odosielané či nevyjasnené publikácie výmenu blokujú. Samotné staršie fyzické
pohyby a FIFO vrstvy výmene nebránia a zostávajú nezmenené. Predchádzajúce
kontrolné body synchronizácie sa neprepíšu: pre nový cieľ je potrebné nové
kontrolované zosúladenie. Uloží sa tá istá mapovacia položka, predchádzajúca
väzba zostáva v potvrdenom náhľade a výsledok je obnoviteľný rovnakým request ID.
Pozorované ceny, dostupnosť a stav posledného odoslania starého cieľa sa na
nový produkt neprenášajú.


Rozšírenie variantových cieľov (4. 11. 2026) zachováva rovnakú transakciu a
mapovací riadok. Kontrola rozpracovaných publikácií zahŕňa pôvodného aj nového
rodiča a cieľový kód. Náhľad označí existujúci starý variant alebo lokálnu
závislú operáciu ako konflikt konkrétneho riadka; ostatné riadky zostávajú
prehľadné a potvrdiť možno iba vybranú pripravenú podmnožinu. Neúplná vzdialená
odpoveď alebo zmena identity pri potvrdení naďalej zastaví celú vybranú transakciu.
Mazanie v Upgates je samostatný schvaľovaný krok, tento postup ho nevykonáva.

Výsledok pridáva `replaced_mappings` s `product_id`, zachovaným SKU a EAN,
`previous_mapping` a novou `mapping`; čas a operátor zostávajú v potvrdenom
výsledku. Rovnaké request ID vráti pôvodný výsledok bez opakovania zápisu.
Nový náhľad už správne prepojeného cieľa vráti `already_linked`, aj keď je
vo vstupnom riadku ešte označené `replace_deleted_variant=true`.

Rovnaký kód variantu a rodiča nestačí na stav `already_linked`: musia sa zhodovať
aj čerstvo načítané natívne ID oboch. Ak bol variant znovu vytvorený s rovnakými
kódmi a novými ID, explicitná výmena prejde všetkými vyššie uvedenými kontrolami
vymazania pôvodného variantu, EAN, vlastníctva a rozpracovaných operácií.
Bez voľby `replace_deleted_variant=true` sa takáto väzba automaticky neopraví.

Ak sa pri rovnakej hodnote kódu zmenilo aj natívne ID rodiča, stará kompletná
cache rodiny nesmie blokovať už overenú výmenu ani predstierať obsah nového
rodiča. Iba pri výslovne potvrdených náhradách všetkých vybraných položiek
tejto rodiny sa jej pôvodné ID porovná s každou pôvodnou väzbou. Nezhoda alebo
cache tretej identity naďalej zastavia celú transakciu. Pôvodný obsah vrátane
súrodencov a času úplného načítania sa uchová v internom poli
`product_link_previews.result._content_archives` spolu s výsledkom výmeny.
Aktívna cache potom obsahuje iba čerstvé navigačné metadáta vybraných cieľov;
nie je úplným podkladom na export a nepreberá staré ceny ani zásoby. Čas
úplného načítania sa neobnoví. Bežné prepojenie ani **Načítať odkazy** túto
výnimku nemajú. Archív sa nevracia v bežnej odpovedi API a následné úplné
načítanie katalógu ho neprepíše. Opakovanie dokončenej dávky vracia ten istý
výsledok bez ďalšieho archívu alebo zápisu väzieb.

Zmena nepotrebuje SQL migráciu a nezapína skladovú synchronizáciu. Návrat kódu
sa robí kompatibilným revert PR; nevracia už vykonané väzby na vymazané ciele.
Syntetické testy overujú zachovanie kariet, identifikátorov a pohybov, audit,
opakovanie, EAN/rodičovské konflikty, vlastníctvo cieľa a nevyjasnenú publikáciu.
Nasadenie ani tieto testy samy nepotvrdzujú dokončenie konkrétnej živej dávky.

## Nahradenie už vymazaného samostatného produktu

API podporuje aj starú samostatnú položku prostredníctvom samostatného
opt-in príznaku `manual_matches[].replace_deleted_product = true`.
`replace_deleted_variant` zostáva výhradne pre pôvodnú variantovú väzbu;
oba príznaky nesmú byť súčasne `true`. `is_variant` naďalej opisuje **nový
cieľ**, takže vymazaný samostatný produkt možno nahradiť iným samostatným
produktom aj konkrétnym variantom. Príklad syntetického vstupu náhľadu:

```json
{
  "request_id": "5f04537b-e8e9-4334-b6fd-72c5fe95bd3e",
  "shop_code": "xtrek",
  "product_ids": [42],
  "manual_matches": [{
    "product_id": 42,
    "code": "NEW-LEAF",
    "parent_code": "NEW-PARENT",
    "is_variant": true,
    "replace_deleted_product": true
  }]
}
```

Pôvodná väzba musí byť aktívne evidovaná (`is_listed=true`) a musí mať
overené `external_product_id`, presný `external_code`, `is_variant=false`,
`external_variant_id=null` a `parent_code=null`. Pri náhľade **aj potvrdení**
server číta `GET /products/simple` s filtrom pôvodných `product_ids`,
`variants_yn=false` a osobitne `archived_yn=false` i `archived_yn=true`.
Ak sa pôvodné ID objaví v ktoromkoľvek úplnom zozname, produkt stále existuje;
platí to aj po premenovaní, deaktivácii alebo zmene na rodiča variantov.
Chýbajúci kód, prázdna neúplná odpoveď ani viditeľnosť produktu nie sú dôkazom
vymazania. Chybné stránkovanie, filtrovanie, spojenie alebo prekročenie limitu
čítačov zastaví operáciu. Zmiešané výmeny produktov a variantov zdieľajú
pôvodný limit ôsmich vzdialených požiadaviek na jednu fázu.

Náhľad uvádza `replaces_deleted_product`, pôvodnú mapovaciu položku a presný
nový cieľ. Existujúci pôvodný produkt spôsobí konflikt riadka
`product_links_product_still_exists`; ostatné pripravené riadky možno vybrať
samostatne. Opätovné objavenie pôvodného produktu pri potvrdení zastaví celú
vybranú transakciu a pôvodný náhľad sa ohradí rovnakým postupom
`known_unapplied` ako pri variantových výmenách.

Platí rovnaká neprázdna, presne zhodná množina overených čiarových kódov,
vlastníctvo cieľa a kontrola protichodných osí ako pri výmene variantu.
Aktívne objednávky, natívne účinky, odosielané alebo nevyjasnené skladové,
produktové a cenové publikácie zostávajú blokujúce. Kontrola publikácií
zahŕňa pôvodný samostatný kód aj nový rodičovský a cieľový kód. V jednej
transakcii sa zmení iba tá istá väzba a jej pozorované metadáta; ID karty,
SKU, identifikátory, skladové zostatky, sledovanie, pohyby, FIFO a objednávková
história zostávajú zachované. Audit `replaced_mappings` obsahuje obe identity.

Pri opätovnom použití pôvodného kódu sa predchádzajúca kompletná cache
archivuje do `_content_archives` iba vtedy, keď jej natívne ID a kód presne
zodpovedajú overenej pôvodnej samostatnej väzbe. Cache tretej identity je
konflikt. Archív, zmena väzby a výsledok sa uložia atomicky; čas úplného
načítania sa neobnoví a aktívna cache obsahuje iba nové navigačné metadáta.

Nezadané aj `false` príznaky sa pred výpočtom request hash vynechávajú, preto
staršie variantové a bežné náhľady zostávajú obnoviteľné. Opakovanie presnej
dokončenej dávky vráti pôvodný výsledok bez ďalších vzdialených čítaní či
zápisov. Nový náhľad už správne prepojeného cieľa vráti `already_linked` aj
s pôvodným opt-in príznakom. Rozšírenie je dostupné cez API bez nového
ovládača v UI, SQL migrácie alebo automatického mazania v e-shope.

## Výslovne skontrolované migračné opravy

Nasledujúce dve voľby v `manual_matches` sú samostatné opt-in opravy bežných
väzieb. Nekombinujú sa navzájom ani s `replace_deleted_product` či
`replace_deleted_variant`. Bez nich platí pôvodná presná zhoda neprázdnych
EAN pri výmene a pôvodná zhoda rodiča pri dopĺňaní natívnych ID. Resolver
ostatných workflow zostáva nezmenený.

### Výmena s viacerými platnými GTIN alebo bez čiarového kódu

`reviewed_deleted_replacement` pripne ID pôvodného mapovacieho riadku,
presné natívne ID nového rodiča a variantu, vecný `identity_evidence`
(10–2000 znakov bez riadiacich znakov) a doslovné
`same_physical_item_confirmed=true`. Samostatný cieľ má
`native_variant_id=null`. Príklad jednej syntetickej ručnej voľby:

```json
{
  "product_id": 42,
  "code": "CURRENT-LEAF",
  "parent_code": "CURRENT-FAMILY",
  "is_variant": true,
  "reviewed_deleted_replacement": {
    "mapping_id": 7,
    "native_product_id": 800,
    "native_variant_id": 101,
    "mode": "verified_barcode_subset",
    "identity_evidence": "Overené rovnaké vyhotovenie a rozmery; výrobca potvrdzuje pokračujúcu položku s oboma uloženými GTIN.",
    "same_physical_item_confirmed": true
  }
}
```

Režim `verified_barcode_subset` vyžaduje neprázdnu množinu platných GTIN
nového cieľa, ktorá je podmnožinou overených identifikátorov tej istej karty
aj prípadnej ručnej EAN úpravy. Zápis neodstraňuje ostatné platné GTIN karty.
Nový GTIN mimo tejto množiny, neplatný čiarový kód, protichodná variantová os
alebo iný vlastník cieľového kódu, natívneho ID či GTIN zostávajú konfliktom.
Samotná príslušnosť ku GTIN množine nenahrádza fyzické preskúmanie výrobku:
odlišná generácia, veľkosť, farba alebo konštrukcia nesmie dostať toto potvrdenie.

Režim `exact_sku_without_barcode` vyžaduje prázdne množiny overených GTIN
na karte, v jej ručnej úprave aj na novom cieli. Cieľ nesmie obsahovať ani
neplatný čiarový kód. Starý `external_code`, nový `code` a kanonické SKU
musia byť presne rovnaké. Navyše je povinná štruktúrovaná referencia:

```json
"evidence_source": {"kind": "shop_mapping", "id": 8}
```

`shop_mapping` označuje existujúcu evidovanú väzbu tej istej karty v druhom
aktívnom Upgates e-shope: rovnaký presný kód, úplné natívne ID, bežná väzba
s faktorom 1. Ani zdieľaná väzba s faktorom 1 nie je týmto dôkazom. Referencia
na inú kartu či neúplnú väzbu sa odmietne. Alternatívou je
`{"kind":"supplier_product","id":123}`: aktívna dodávateľská položka už
jednoznačne priradená tejto karte aktívnym `product_supply_sources` a presným
dodávateľsky vymedzeným `supplier_sku` identifikátorom. Ak tento zdroj má
platný GTIN, bezčiarový režim sa nepoužije; najprv sa overí riadna oprava EAN.

Server zmrazí lokálny zdroj vrátane jeho vlastníctva, natívnych ID a hashov
uloženého obsahu; pri potvrdení ho pod identitným zámkom znovu porovná.
Referencia sama nespúšťa ďalší úplný pull druhého e-shopu alebo dodávateľa.
Obsluha musí v dôkaze uviesť, čo fyzicky overila. Verejný náhľad obsahuje
`reviewed_deleted_replacement.request` a zdrojový súhrn so `source_hash`.

Oba režimy stále dokazujú neprítomnosť pôvodného natívneho cieľa v aktívnej
aj archivovanej časti pri náhľade aj potvrdení. Zachovávajú všetky objednávkové,
natívne a publikačné blokácie, kartu, ID mapovania, identifikátory, sklad a
FIFO. Výsledok obsahuje pôvodnú aj novú väzbu a použitý dôkaz. Nejde o
prenos živého cieľa na inú kartu ani o zlúčenie kanonických kariet.

### Doplnenie staršej rodiny s náhradným kódom rodiča

Starší import používal pri prázdnom natívnom kóde rodiča prvý kód variantu
ako lokálny kľúč rodiny. Tento náhradný kód sa nesmie zapísať na rodiča,
pretože už patrí predajnému variantu. Najprv sa samostatne opraví natívny
kontajner na overený jedinečný kód. Hub cez tento postup natívne kódy nemení.

Každá voľba potom obsahuje `complete_unverified_parent`, napríklad:

```json
{
  "product_id": 42,
  "code": "PHYSICAL-A",
  "parent_code": "SOURCE-FAMILY",
  "is_variant": true,
  "complete_unverified_parent": {
    "mapping_id": 7,
    "expected_parent_code": "PHYSICAL-A",
    "native_product_id": 800,
    "native_variant_id": 101,
    "identity_evidence": "Overený pôvodný fyzický variant podľa presného GTIN a opravený zdrojový kód kontajnera.",
    "same_physical_item_confirmed": true
  }
}
```

V náhľade musia byť **všetky existujúce väzby daného e-shopu pod pôvodným
náhradným rodičom** a celá takáto rodina sa potvrdzuje naraz. Každá musí byť
bežný evidovaný variant s oboma natívnymi ID dosiaľ `NULL`, rovnakým
nezmeneným variantovým kódom a presne zhodnou neprázdnou množinou platných
GTIN. Známe natívne ID sa týmto postupom neprepisuje. Pôvodný rodič musí
zodpovedať jednému z kódov vybranej rodiny; nový rodič musí patriť rovnakému
čerstvo overenému natívnemu kontajneru pre všetky listy.

Čítanie navyše overí obsadenie nového rodičovského kódu v produktovom aj
variantovom priestore. Kód iného produktu alebo predajného variantu opravu
blokuje. Stále platí najviac osem GET na fázu a limity výberu. Vlastníctvo,
EAN, osi, aktívne objednávky, natívne účinky a odosielané či neisté publikácie
sa kontrolujú aj pri potvrdení. Osobitne sa chránia starý a nový rodič.

Náhľad zmrazí mapovania celej rodiny aj odtlačky starej a novej lokálnej
cache. Existujúci obsah nesmie tvrdiť inú známu natívnu identitu. Potvrdenie
zmení iba `parent_code` a doplní chýbajúce natívne ID existujúcich riadkov;
ponechá pozorované ceny, zásobu a všetky ostatné obchodné údaje. Pôvodná
cache sa atomicky archivuje do interného `_content_archives`; jej starý
náhradný kľúč už neposkytuje úplný exportovateľný produkt. Nový rodič dostane
presné navigačné metadáta, nikdy premenovaný starý úplný obsah. Výsledok
`completed_parent_mappings` zachová pred/po identity a výslovný dôkaz.

Obe rozšírenia používajú existujúce UUID, hash náhľadu, 30-minútovú platnosť,
atómový výsledok a `known_unapplied` protokol. Nezadané či `null` nové voľby
sa vynechávajú z request hash, takže staršie dávky ostávajú obnoviteľné.
Opakovanie rovnakého dokončeného UUID nemení mapovanie, archív ani históriu.
Bez SQL migrácie, vzdialených zápisov, automatickej opravy EAN alebo prevodu
na zdieľanú skladovú skupinu. Kompatibilný revert kódu nevracia už potvrdené
väzby na pôvodné neúplné alebo vymazané ciele; auditné výsledky sa ponechajú.
