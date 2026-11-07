# Predaj rôznych počtov kusov z jednej skladovej karty

Výslovne potvrdená skupina predajných položiek môže čerpať jednu fyzickú kartu
Hubu. Príkladom sú voľné záplaty predávané po 1, 8 alebo 100 kusoch. Varianty
e-shopu zostávajú samostatnými predajnými položkami; príjem, rezervácie, výdaj,
vratky a FIFO používajú jednu spoločnú zásobu v kusoch.

Táto možnosť nemení bežné párovanie 1:1. Je určená pre predaj rôzneho počtu
rovnakých voľných kusov. Veľkosť, farba alebo odlišný výrobok vyžadujú vlastnú
fyzickú kartu. Uzavreté výrobné balenie, pár alebo súprava sa automaticky
nepovažujú za voľné kusy.

Rovnaký postup možno výslovne použiť aj pre viac predajných záznamov presne
toho istého celého výrobku, ak má každý faktor **1**. Základnou skladovou
jednotkou potom zostáva jeden celý predávaný kus: napríklad jeden stan alebo
jedna kompletná súprava riadu. Súprava sa tým nerozkladá na jednotlivé diely.
Dôkaz musí pomenovať celý výrobok, zhodný variant a jednotku; samotný spoločný
názov či GTIN nestačí. `base_piece_unit_confirmed=true` v tomto prípade
potvrdzuje tento nezmenený celý skladový kus. Odlišná farba, veľkosť,
konštrukcia či obsah balenia zostávajú odlišnou fyzickou identitou.

Aj skupina s faktorom 1 používa samostatné predajné kľúče a obmedzenia
publikovania opísané nižšie. Bežná výmena jednej vymazanej položky za jednu
novú položku preto používa [obnovu bežnej väzby](product-links.md), pokiaľ
nie je skutočne potrebná skupina súčasne existujúcich predajných záznamov.

## Jednotky a história

`shop_products.shared_stock=true` označuje výslovne skontrolovanú predajnú
položku. `sale_to_stock_factor` je celé kladné číslo od 1 do 1 000 000:
počet fyzických kusov na jednu predajnú jednotku. Aj variant s faktorom 1 má
v skupine vlastnú identitu. `sale_barcodes` uchováva overené čiarové kódy
predajnej položky; tieto kódy sa nepridávajú k identifikátorom fyzického kusa.

| Údaj | Jednotka |
| --- | --- |
| Surové množstvo objednávky a `stock_snapshot.lines[].quantity` | Predajné jednotky konkrétneho variantu |
| `shop_order_items.sale_quantity` | Zmrazené predajné množstvo z objednávky |
| `shop_order_items.sale_to_stock_factor`, `sale_mapping_id` | Zmrazený faktor a identita predajnej väzby |
| `shop_order_items.quantity`, rezervácie, pohyby a FIFO alokácie | Fyzické kusy |
| Natívny odpočet Upgates a jeho kompenzácia | Predajné jednotky konkrétnej väzby |
| Fyzická vratka | Skutočne prijaté fyzické kusy |

Pri potvrdenom príjme 200 kusov a predaji jedného variantu „8 kusov“ vznikne
rezervácia 8 kusov a následne výdaj 8 kusov. Zostane 192 kusov. Ak sa fyzicky
vrátia 3 záplaty, vratka prijme 3 kusy, zachová pôvodný výdaj a využije jeho
FIFO náklad. Karanténa a uvoľnenie vratky používajú existujúci postup.

Prevod sa neodvodzuje z názvu, GTIN, `pack_size`, nákupnej faktúry ani z hodnoty
zásoby e-shopu. Snímka staršej objednávky bez predajných polí ostáva v pôvodnom
tvare s faktorom 1. Uložený faktor novej objednávky sa spätne neprepočítava podľa
aktuálneho mapovania. Zmenu alebo zmazanie použitej väzby blokuje aj databáza.

## Skontrolované prepojenie cez API

Všetky cesty vyžadujú operátorské prihlásenie. Cookie zápisy používajú bežnú
CSRF ochranu; odpovede majú `Cache-Control: no-store`.

| Cesta | Účinok |
| --- | --- |
| `POST /api/shared-stock-sales/preview` | Overí natívne položky a uloží celú navrhovanú skupinu. |
| `GET /api/shared-stock-sales/{id}` | Obnoví pôvodný náhľad alebo výsledok po prerušení spojenia. |
| `POST /api/shared-stock-sales/{id}/apply` | Znovu overí zdroje a atómovo uloží presnú skupinu s výsledkom. |

Náhľad obsahuje `request_id`, `shop_code`, `product_id`, 1–50 položiek `sales`,
vecný opis `identity_evidence` a dve doslovné potvrdenia `true`:
`same_physical_item_confirmed` a `base_piece_unit_confirmed`. Každá položka má
presný `code`, `parent_code`, `is_variant`, `native_product_id`, prípadné
`native_variant_id` a celočíselný `sale_to_stock_factor`. Kódy aj natívne ciele
musia byť v zozname jedinečné. Samostatný produkt používa svoj kód aj ako
`parent_code`, `is_variant=false` a bez `native_variant_id`.

Príklad so syntetickými identifikátormi:

```json
{
  "request_id": "68ada97c-e453-4915-80d2-a77ab210f3a3",
  "shop_code": "biketrek",
  "product_id": 42,
  "identity_evidence": "Overené rovnaké voľné záplaty. Jedna fyzická jednotka je jedna záplata.",
  "same_physical_item_confirmed": true,
  "base_piece_unit_confirmed": true,
  "sales": [
    {"code":"PATCH-1","parent_code":"PATCHES","is_variant":true,"native_product_id":800,"native_variant_id":101,"sale_to_stock_factor":1},
    {"code":"PATCH-8","parent_code":"PATCHES","is_variant":true,"native_product_id":800,"native_variant_id":108,"sale_to_stock_factor":8},
    {"code":"PATCH-100","parent_code":"PATCHES","is_variant":true,"native_product_id":800,"native_variant_id":200,"sale_to_stock_factor":100}
  ]
}
```

Potvrdenie posiela iba `{"preview_hash":"<presný hash náhľadu>","confirmed":true}`.
Hash nie je nové miesto na zmenu faktorov alebo výberu. Náhľad platí 30 minút;
dokončený presný opakovaný pokus vráti uložený výsledok bez nového čítania či
zápisu. Zmenený obsah pod rovnakým UUID sa odmietne. Jednoznačne neúspešný pokus
trvalo ukončí platnosť náhľadu pred označením `known_unapplied=true`.

Všetky existujúce väzby tejto karty v danom e-shope musia zostať v návrhu,
okrem jednej výslovne označenej náhrady už zmazanej bežnej väzby. Workflow
implicitne nemaže, nepresmeruje ani nepriraďuje cudzí natívny cieľ. Prevod
bežnej väzby na skupinu sa odmietne pri predchádzajúcej spravovanej objednávke,
aktuálnom natívnom záväzku alebo prebiehajúcom či neistom zápise do e-shopu.
Pridanie novej položky do nezmenenej existujúcej skupiny nemení význam starších
objednávok.

### Náhrada pôvodnej zmazanej položky

Jedna nová predajná položka môže obsahovať `replace_deleted_mapping` s presným
`mapping_id`, pôvodným `native_product_id` a pôvodným `native_variant_id`
(`null` pre samostatný produkt). Napríklad položka `PATCH-100` z vyššie uvedeného
syntetického príkladu môže navyše obsahovať:

```json
{"replace_deleted_mapping":{"mapping_id":70,"native_product_id":700,"native_variant_id":null}}
```

Musí ísť o existujúcu bežnú väzbu tej istej karty a e-shopu. Pôvodná natívna
identita sa nesmie nachádzať medzi novými živými cieľmi. Náhľad aj potvrdenie
znovu prečítajú aktívnu a archivovanú časť natívneho katalógu a overia absenciu
pôvodného produktu alebo variantu podľa jeho ID. Neúplný export, opätovné
objavenie položky, zmenená lokálna väzba alebo vyčerpanie spoločného limitu
čítaní zastavia postup. Čerstvé čítanie novej skupiny a absencie spolu používajú
najviac osem API požiadaviek.

Zvolená nová položka zachová ID pôvodného mapovania. Zvyšok skupiny, náhrada
identity a výsledok sa uložia v jednej databázovej transakcii. Náhľad a uložený
výsledok obsahujú celý pôvodný lokálny snapshot vrátane pozorovaného skladu,
ceny a časov synchronizácie. Súkromná diagnostická chyba ostáva len v uloženom
audite; API ju nevracia. Nová väzba začne bez starých pozorovaní a bez čakajúceho
odoslania. Fyzické tabuľky, sledovanie skladu a FIFO sa nemenia. Zlyhanie neskoršej
kontroly obsahu vráti späť celú skupinu vrátane už pripravených riadkov.

Táto cesta používa výslovné potvrdenie rovnakých základných skladových kusov
a faktorov; pri aliasoch celého výrobku zostávajú všetky faktory 1.
Nevyžaduje vymyslený EAN variantu ani neprenáša EAN rodiča na variant.
Bežný [postup náhrady zmazaného produktu](product-links.md) zachováva svoje
pravidlá zhody EAN. Pri znovupoužití rovnakého `parent_code` s odlišným natívnym
rodičom môže existujúca ochrana archívu obsahu celú viacpoložkovú skupinu
odmietnuť; náhradová výnimka sa nevzťahuje automaticky na všetkých súrodencov.

Kanónická karta musí byť aktívna, `item_type=stock` a `stock_unit=pcs`.
Založenie skupiny nevyžaduje potvrdený počet kusov; nepredstiera však jeho
znalosť. Výsledok obsahuje `stock_changed=false` a `remote_writes=0`.
Nezapína evidenciu, nevytvára bilanciu, príjem, korekciu, počiatočnú FIFO vrstvu,
údržbový kontrolný bod ani publikovanie. Historická hodnota napríklad „2“
nemôže týmto prepojením nadobudnúť význam „2 kusy“ ani „200 kusov“.

## Publikovanie a nákupná cena

Fyzická dostupnosť je spoločná: `qty_on_hand - qty_reserved - qty_quarantined`.
Predajná dostupnosť každého variantu je jej celá časť po vydelení faktorom.
Pri 192 voľných kusoch sú výsledky 192, 24 a 1 predajných jednotiek.
Projektor zachováva pôvodné `qty_*` polia v kusoch a pre skupinu pridáva
`qty_sellable`, faktor a `sale_key`. Oba skladové publikátory používajú
predajné množstvo. Jeden výber SKU môže preto vytvoriť viac predajných položiek.

Každá väzba má samostatný kontrolný bod, natívny záväzok, pokus a blokáciu
neistého výsledku. `sale_key` je ID mapovania prevedené na text; staršie bežné
väzby používajú prázdny text. Rozdielové odosielanie kompenzuje natívny odpočet
iba na zodpovedajúcom variante. Generácia zmenenej fyzickej karty sa označí za
vybavenú až po úspešnom spracovaní všetkých jej predajných položiek.
Nová skupina nepreberá staršie kontrolné body s prázdnym `sale_key`; pred
pravidelným odosielaním sa osobitne overí nový východiskový bod každého listu.

Predajná nákupná cena produktu je aktuálna FIFO cena za kus krát faktor.
Nákupná cena položky vydanej objednávky je skutočný FIFO náklad fyzického výdaja
delený zmrazeným predajným množstvom. Samotná vratka nemení pôvodný náklad
výdaja. Neznáme ocenenie, neistý vzdialený zápis a neúplné natívne pokrytie
zostávajú blokujúce podľa existujúceho protokolu.

Produktové FIFO publikácie používajú pre bežnú väzbu kľúč
`product:<product_id>` a pre skupinu `product:<product_id>:<mapping_id>`;
objednávkové kľúče `order:<order_id>` sa nemenia.

## Rozsah editora a ostatných workflow

Editor zobrazuje všetky predajné väzby a ich faktory; nevyberá posledný variant
ako reprezentanta celej karty. Lokálne názvy a poznámky možno upravovať, ale
bežné publikovanie jednej ceny, EAN alebo variantovej osi pre kartu/e-shop
skupinu odmietne. Hromadný Upgates pull, bežný import a staršie migračné
párovanie tiež nesmú prepísať skupinu ani preniesť GTIN balenia do fyzickej
karty. Úprava predajných opisov a cien jednotlivých variantov zostáva v Upgates.

Ochrana proti automatickému pripojeniu sa vzťahuje aj na druhý e-shop.
Existencia skupiny kdekoľvek na karte blokuje nové bežné pripojenie iba podľa
kanónického SKU alebo GTIN, ktoré by bez dôkazu dosadilo faktor 1. Nová väzba
sa potvrdí rovnakým skupinovým workflow aj pri predaji jedného kusa. Existujúce
bežné väzby v druhom e-shope ostávajú zachované; lokálne vyhľadanie pri príjme
nepotrebuje predajnú väzbu. Export `shop_links` zachováva `shared_stock`,
`sale_to_stock_factor` a `sale_barcodes`, aby kontrola a záloha nestratili význam
jednotlivých predajných položiek.

## Migrácia a nasadenie

`046_shared_stock_sales.sql` pridáva predajné polia, snímky objednávok,
`shared_stock_sale_previews`, kontrolné obmedzenia a kľúče predajných položiek
publikátorov. Staré riadky dostanú faktor 1 a prázdny `sale_key`; pohyby,
množstvá, FIFO ani natívne udalosti sa neprepisujú. Bežné párovanie si ponecháva
čiastočný jedinečný index; odložená kontrola nedovolí zmiešať bežné a zdieľané
väzby tej istej karty/e-shopu. Karta so skupinou nemôže zmeniť fyzickú jednotku.

SQL je opakovateľné, pribalené do API obrazu a pridané do explicitného runnera
po doterajších migráciách 005–038. Inštalátory 039–045 zostávajú bez zmeny.
Nasadenie kódu nevytvára živú skupinu. Najprv musia prejsť testy, kontrola
schémy a nový náhľad konkrétnych identít. Po uložení sa overí výsledok a každá
natívna väzba; fyzické množstvo a povolenie publikovania sa riešia samostatne.

Po použití zdieľanej skupiny nie je bezpečný návrat na starší kód, ktorý
predpokladá jednu väzbu na kartu/e-shop. Zachovajte schému a zmrazené výsledky
a použite kompatibilnú opravu. Nesnažte sa obnoviť starý jedinečný kľúč
zmazaním predajných väzieb alebo prepočítaním historických objednávok.
