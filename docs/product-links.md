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

Jedna karta Hubu má najviac jednu predajnú reprezentáciu v konkrétnom e-shope.
Rovnaký výrobok ako samostatný produkt aj pokladňový variant v BIKETREK je
konflikt. Obsluha odstráni duplicitný pokladňový variant v e-shope a ponechá
samostatný produkt. Hub nikdy automaticky nemaže vzdialené produkty ani
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

Pri ručnom výbere samostatného cieľového produktu možno výslovne označiť
`manual_matches[].replace_deleted_variant = true`. Toto nie je všeobecné
odpojenie ani presmerovanie existujúceho živého produktu. Povoľuje iba prechod
zo starého variantu na samostatný produkt pri zachovaní rovnakej Hub karty.

Náhľad zobrazí `previous_mapping` a nový cieľ. Server pri náhľade aj potvrdení
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
