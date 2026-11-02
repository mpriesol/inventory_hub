# Import obsahu z Atomeru do xTrek

Prvá vykonateľná etapa používa existujúci denník odosielania vybraných polí.
Aktualizuje iba slovenský dlhý HTML popis, SEO titulok a SEO popis existujúcej
karty v xTrek Upgates. Nevytvára produkty, varianty, väzby, skladové pohyby ani
FIFO vrstvy. Ceny, dostupnosti, objednateľnosť, aktivita, identifikátory,
parametre, kategórie a galérie nie sú vstupom tohto odosielania.

## Obsluha

V **Obchody → xTrek → Import obsahu z Atomeru** (`/migration/atomer`) vyberte
buyers XML. Na riadku uveďte zdrojový kód samostatného produktu alebo `PARENT_ID`
celej rodiny a SKU existujúcej prepojenej karty v Hube. Pri rodine je SKU iba
východiskom; systém preverí všetky jej predajné jednotky. Najviac desať kariet
v jednej príprave, najviac sto variantov na rodinu.

1. Najprv pripojte existujúce karty cez [bežný náhľad väzieb](product-links.md).
   Chýbajúce karty možno prevziať cez samostatný overený Upgates pull bez skladu.
   Import obsahu nerobí implicitné párovanie ani nové prideľovanie SKU.
2. Vyberte iba potrebné polia a pripravte náhľad. HTML sa v porovnaní zobrazuje
   ako text, nevykonáva sa. Skontrolujte presné pôvodné a navrhnuté hodnoty.
3. Odosielajte po jednotlivých kartách. Úspech vyžaduje spätné čítanie obsahu,
   celej natívnej rodiny, ostatných jazykov a nedotknutých polí. Zmena
   prevádzkového pozorovania zostáva na preverenie; nikdy nespustí skladovú opravu.
4. Pri strate odpovede načítajte uložený výsledok. Rovnaký identifikátor
   požiadavky obnoví rovnakú operáciu. Nejasný PUT sa automaticky neopakuje.
5. Po úspechu možno pripraviť **náhľad návratu pôvodných hodnôt**. Je to nová
   operácia s vlastným odoslaním. Odmietne novší zásah do dotknutých polí;
   ostatné novšie hodnoty zostávajú zachované.

## Zdroj a identita

Server kompletne spracuje UTF-8 XML do 100 MiB, vypočíta SHA-256 presných
pôvodných bajtov a uchová vybrané riadky vrátane pôvodných hodnôt a pozície.
`raw_xml` je XML serializácia vybraného elementu; hash identifikuje pôvodný
úplný súbor. DTD, entity, neúplné XML, duplicitné vybrané identity a kolízia
samostatnej karty s rodičovským kódom sa odmietnu. Chyby nesúvisiacich kódov
neblokujú overenú vybranú dávku. Originálny súbor treba zachovať s auditom.

Zdrojová rodina musí pokryť celú cieľovú rodinu. Párovanie využíva presný kód
alebo jednoznačný platný GTIN; rozpor EAN, značky alebo natívnych ID blokuje
operáciu. Alias značky nad rámec veľkosti písmen a medzier a rodiny s odlišným
rodičovským kódom zatiaľ vyžadujú osobitné riešenie. Každý cieľový variant musí
mať výslovnú overenú väzbu v Hube. Existujúce SKU zostáva bez zmeny.

Všetky vybrané zdrojové deti musia mať rovnakú hodnotu spoločného poľa.
Prázdny zdroj nikdy nemaže cieľ. HTML sa potichu nečistí: nepovolené značky,
aktívny obsah, vložené médiá, nebezpečné atribúty a štýly blokujú konkrétny
pilot. Povolené tabuľky, zoznamy a prezentačné formátovanie sa zachovajú.
Natívny krátky popis a vložené médiá zostávajú mimo tejto etapy.

Prvý pilot vyžaduje aj obnoviteľnú neprázdnu pôvodnú hodnotu každého vybraného
poľa. Neznáme, nulové alebo prázdne originály vyžadujú samostatnú politiku
obnovy/vymazania; toto obmedzenie sa nesmie obísť vymyslenou hodnotou.

## Trvalé odosielanie a obnova

Príprava sa ukladá do existujúceho `ProductEditorPublication` pod UUID
požiadavky, s `workflow=atomer_content`. Obsahuje zdrojový hash, vybrané
originály, dôkazy identity, všetky natívne ID rodiny, pred/po hodnoty,
allowlistovaný payload a predpoklady. Rovnaké UUID s iným vstupom je konflikt.
Ak sa hodnoty už zhodujú vrátane HTML štruktúry, uloží sa dokončený `no_op`
bez PUT. Toto nemení ručné editorové overrides.

Spoločný publisher revaliduje lokálne väzby a živý cieľ, uplatní existujúci
parentový zámok proti AI/editor/FIFO writerom a pred PUT trvalo uloží zámer.
Po reštarte zostáva nejasný zápis ohradený. Explicitná obnova vyžaduje dôkaz,
že pôvodná požiadavka už nemôže dobehnúť; samotná zhoda pri GET nestačí.
HTML readback porovnáva štruktúru aj text, nie iba viditeľné slová.

Pozorovania zásob/dostupností sa uchovávajú oddelene. Rozdiel môže pochádzať
zo súbežného predaja; report neoznačí automaticky obsahový PUT za jeho príčinu.
Pri rozdiele sa úspech nepotvrdí a nič sa automaticky nekompenzuje.
Overený readback obnoví lokálnu pozorovanú cache pri zachovaní platných
`_hub_link_metadata` a nepozorovaných rozšírených sekcií rovnakej identity.

## API a rozsah nasadenia

- `POST /api/migration/atomer-content/preview`: multipart `source_file` a
  `selection` (`items`: `request_id`, `hub_sku`, `source_code`, `fields`).
- `GET /api/migration/atomer-content/publications`: posledných 100 operácií.
- `GET /api/migration/atomer-content/publications/{id}/audit`: úplný privátny dôkaz.
- `POST /api/migration/atomer-content/publications/{id}/rollback-preview`:
  nový `request_id`; iba dokončená pôvodná operácia, bez novšieho zásahu.
- Odoslanie, čítanie a explicitné vyriešenie používajú existujúce
  `/api/product-editor/publications/{id}` (`/send`, `/resolve`).

Všetky cesty vyžadujú bežný prístup operátora; cookie zápisy zachovávajú CSRF
kontrolu a odpovede sú `no-store`. Nie je potrebná SQL migrácia ani nový
prepínač autority. Nasadenie samo nevykoná žiadny produktový alebo skladový PUT.

Testy pokrývajú parser, autentizáciu, presné rodinné identity, HTML, neprípustné
polia, UUID replay, druhý no-op beh, zmenený cieľ, nejasný výsledok, spoločné
zámky, nezmenený lokálny sklad/FIFO a návrat iba dotknutých polí. Databázové
prípady používajú izolovaný localhost `*_catalog_test`, nikdy produkciu.
Výsledok živého pilotu a súkromné snapshoty patria do odovzdania; prítomnosť
kódu ani úspešné CI samy osebe nedokazujú dokončenú migráciu.
