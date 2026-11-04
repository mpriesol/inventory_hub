# Import obsahu z Atomeru do xTrek

Prvá vykonateľná etapa používa existujúci denník odosielania vybraných polí.
Aktualizuje iba slovenský dlhý HTML popis, SEO titulok a SEO popis existujúcej
karty v xTrek Upgates. Rozšírenie nižšie dopĺňa parametre samostatných produktov.
Nevytvára produkty, varianty, väzby, skladové pohyby ani
FIFO vrstvy. Ceny, dostupnosti, objednateľnosť, aktivita, identifikátory,
kategórie a galérie nie sú vstupom tohto odosielania.

## Verný prenos parametrov a vzhľad textu

Voliteľné pole `parameters` je určené iba samostatným produktom. Nezavádza
varianty ani ich parametre. Názvy sa dodávajú výslovnou overenou JSON mapou
`PROPERTY_ID → presný názov`; hodnoty sa čítajú výhradne z pôvodného vybraného
buyers XML. Mapa je vstup importu, nie automaticky overený globálny číselník.
Pred použitím ju treba podložiť auditom alebo priamym zdrojovým číselníkom.

Čiarky, jednotky a celé pôvodné hodnoty sa zachovávajú. Napríklad
`180 x 60 x 0,8 cm` je jedna hodnota. Existujúce hodnoty rovnakého slovenského
parametra sa nahradia celou zdrojovou hodnotou; parametre mimo zdrojového
výberu sa zachovajú. Pri ďalších jazykoch vybraného parametra sa náhľad odmietne,
aby nezanikli preklady. Duplicity názvov/ID alebo chýbajúce mapovanie sú chyba.
Postup nemení globálne typy, jednotky, filtre ani poradie definícií parametrov.

Náhľad a každé ďalšie overenie načítajú úplné parametre cez samostatný
`products/parameters` endpoint a preveria natívne ID. Príznak v zmrazenej
identite odlišuje tento režim od starších textových operácií, ktorých pôvodné
dôkazy a UUID zostávajú použiteľné. API payload môže obsahovať iba `code`,
vybrané slovenské texty a overené parametre. Zásoby a ceny sa neposielajú.
Pred/po dôkaz, blokovanie nejasných zápisov, no-op a návrat používajú rovnaký
trvalý denník. Prvý parameterový pilot vyžaduje neprázdne obnoviteľné originály.

Voľba **Prispôsobiť formátovanie Upgates** odstráni zdrojové CSS triedy a
vlastné prezentačné štýly. Zachová slová, čísla, nadpisy, zoznamy, tabuľky aj
významové zvýraznenia. Neprípustné HTML sa odmietne pred úpravou. Ide o
deterministické formátovanie, bez AI prepisovania obsahu alebo SEO. Predvolený
režim `original` zachová pôvodné správanie; `upgates` je výslovná voľba.

JSON `selection` navyše podporuje `parameter_names` a `formatting`.
Oba vstupy sa uchovajú v dôkaze a účtujú do identity požiadavky tam, kde sa
použijú. Zmena mapy/režimu pri rovnakom UUID sa odmietne.

Galérie treba samostatne porovnať vrátane obsahu súborov, poradia a dostupnosti
na cieľovom CDN. Ak už zodpovedajú, ich opätovný upload nie je potrebný.
Zhodný počet alebo podobný názov súboru nie je sám dôkazom zhody. Zmenšenie
obrázka platformou treba vykázať oddelene od zámeny fotografie alebo orezu.
Toto rozšírenie nie je úplný katalógový writer: ďalšie väzby, prechod skladu,
adresy a nepodporované zdrojové polia zostávajú osobitné úlohy.

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

## Jednorazový priamy dávkový prenos

`scripts/atomer_direct_batch.py` je ručne spúšťaná orchestrácia existujúceho
parsera, parameter mappera a allowlistovaného obsahového payloadu. Je určená
iba pre výslovne povolenú migráciu existujúcich samostatných xTrek kariet.
Nie je napojená na scheduler, UI ani produkčný startup a nevyžaduje migráciu DB.
Nezakladá produkty, varianty alebo Hub väzby a neposiela obrázky, ceny,
sklad, dostupnosť či aktivitu. Zdrojové znenie nemení generatívnym modelom.

Vstupy sú pôvodné úplné XML, jeho očakávaný SHA-256 a auditný ZIP s
`identity_map.jsonl` a `parameter_dictionary.json`. Historická bezkonfliktná
identita je iba kandidát. `prepare` overí zdroj; `scan` kontroluje aktuálnu
natívnu identitu, značku/EAN, EUR cenu s DPH, obnoviteľný obsah a poradie galérie.
Presná bajtová/pixelová zhoda obrázkov je automaticky overiteľná; odlišné
obrazové dáta zostávajú na samostatné posúdenie. Chýbajúce či nejednoznačné
polia nie sú nahradené vymyslenými hodnotami.

Príklad spustenia (cesty a hash sú lokálne vstupy operátora):

```sh
python scripts/atomer_direct_batch.py prepare --source /private/buyers.xml \
  --audit /private/audit.zip --source-sha256 "$ATOMER_SHA256" \
  --run-dir /private/atomer-run
```

Nasledujúce akcie používajú tie isté argumenty: `scan`, `apply --limit 10`
pre prvú kontrolovanú dávku, `apply` pre ostatné pripravené položky a `verify`
pre druhý čítací no-op priechod. Login a kľúč sa zadávajú do skrytého promptu;
voliteľné `--credentials-stdin` prijíma jeden JSON riadok od bezpečného volajúceho.
Údaje sa neposielajú mimo pevnej adresy xTrek API ani neukladajú do žurnálu.

Čítanie a PUT používa dávky najviac 20 kariet. Pred každým PUT sa znova načíta
živý cieľ a atomicky uloží zámer, originály a presný payload. Každá karta
vyžaduje potvrdenie natívneho ID a úplný readback vybraných/chránených polí.
Neisté potvrdenie, sieťová chyba po PUT alebo zmena chránených polí zastaví
ďalšie zápisy; nie je implementované automatické opakovanie ani rollback.
Výsledok treba vyriešiť z trvalých dôkazov, nikdy vymazaním žurnálu.

Tento explicitný priamy režim nemá databázové parentové zámky Hubu, jeho
publisher žurnál, obnovu cache ani spoločné rezervácie API kvóty. Pred spustením
treba skontrolovať súbežné a neisté publikácie pre rovnaké karty a koordinovať
obsahové zápisy. Súborový zámok chráni iba ten istý pracovný adresár.
Kvóta sa sleduje zo živých hodinových a denných hlavičiek s rezervou na readback;
čerstvé porovnanie nezaručuje atómový CAS proti inému klientovi Upgates.
Súkromný adresár behu treba uchovať mimo Gitu, zálohovať a používať aj pri
pokračovaní. Historické počty kandidátov nie sú počty dokončených prenosov.
