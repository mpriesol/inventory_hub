# Príprava prevodu existujúcich SKU

Táto etapa pripravuje náhľad konkrétnych kariet. Nevytvára druhú kartu,
neprečísluje e-shop ani Hub, nezaúčtuje zásoby a nezapína skladovú autoritu.
Pravidlá novej identity sú v [sku-policy.md](sku-policy.md).

## Náhľad v Hube

V detaile existujúcej skladovej karty otvor **Náhľad zmeny SKU**. Doplň značku,
presný kód predajnej jednotky, jeho druh a podklad. Náhľad oddeľuje súčasné SKU
od navrhovaného SKU a ukazuje konflikty, aliasy, mapovania a dotknuté záznamy.
**Overiť v e-shopoch** vykoná samostatné čítanie aktuálnych identít cez existujúci
Upgates transport a jeho rozpočet požiadaviek. Výsledok možno stiahnuť ako JSON.
V tomto nástroji nie je tlačidlo na premenovanie.

`POST /api/sku-policy/migration-preview` prijíma najviac 20 rôznych položiek
`{product_id, sku_identity}` a `verify_remote` (predvolene `false`).
`sku_identity` má rovnaký kontrakt ako pri zakladaní karty. Odpoveď má čas,
hash snímky, jednotlivé výsledky a povinné následné kontroly. `can_apply` je
vždy `false`: úspešný návrh identity nie je potvrdenie pripravenosti produkčného
prečíslovania. Náhľad neukladá dôkaz identity ani schválenie migrácie.

Kontrola existujúcej identity používa spoločný resolver. Doložený MPN inej karty,
nejednoznačný EAN, kolízia kódu alebo aliasu a rozpor značky zostávajú konfliktom.
Kontroluje sa aj kolízia cieľových SKU medzi riadkami jednej dávky. Rovnaká
navrhovaná hodnota nikdy nie je pokyn zlúčiť dve histórie.

Pri variante zostáva rodič nezmenený. Rodičovský kód sa nesmie nahradiť kódom
jedného variantu. Pokladňová skupina xTrek má samostatné preverenie, pretože jej
variant môže súčasne existovať ako samostatný výrobok. Chýbajúce natívne ID sa
nesmie odvodiť z URL, názvu, EAN alebo nového kódu. Uložené ID v Hube a čerstvo
overené ID z Upgates sú v náhľade odlíšené.

Počty závislostí sú kontrola dopadu. Historické pohyby a FIFO nie sú dôvod na
ich prepisovanie. Aktívne rezervácie, rozpracované objednávky, importné prípravy
a neisté publikácie vyžadujú konkrétny postup prechodu. Neoverená oblasť sa
uvádza ako neoverená; nula v jednej tabuľke nedokazuje neprítomnosť externých
objednávok, košíkov alebo rozpracovaných súborových importov.

## Podporovaný spôsob premenovania v Upgates

Oficiálny kontrakt `PUT /api/v2/products` páruje produkty aj varianty podľa
`code`. V skontrolovanom kontrakte nie je overená operácia old-code/new-code
ani premenovanie adresované natívnym ID. Bežný CSV/XML import nového kódu sa
preto nesmie použiť ako premenovanie existujúcej karty.

Pred zápisom treba potvrdiť administratívny alebo podporou Upgates vykonaný
postup, ktorý zachová `product_id`, `variant_id` a rodiča. Pilot musí porovnať
tieto ID pred aj po operácii. Zmazanie a opätovné založenie nie je prijateľné.

Existujúci nástroj TOPEAK → ZOO zostáva úzkou výnimkou. Najprv overuje už
vykonané administratívne premenovanie a potom zosúlaďuje Hub. Automaticky ho
nerozširujeme na značkové SKU ani ho nespúšťame na zostávajúci historický zoznam.

## Snímka pred pilotom a okamih zmeny

1. Uchovať čerstvý náhľad a tabuľku old/new SKU, Hub ID, všetkých shopových
   mapovaní, natívnych ID, rodičov, EAN a variantových parametrov. Uložiť presný
   podklad MPN a dodávateľské identity s rozsahom dodávateľa.
2. Pred skutočným zápisom zabezpečiť overenú zálohu PostgreSQL a relevantných
   konfiguračných/cache/importných súborov. Čítací JSON alebo XLSX je podklad
   na porovnanie, nie plnohodnotná záloha databázy. Zaznamenať čas a overiť
   obnoviteľnosť. Nevyžadovať ani nezverejňovať tajné hodnoty.
3. Zachytiť množstvá, potvrdenie fyzického skladu, rezervácie, karanténu,
   FIFO vrstvy a alokácie, pohyby a odkazy na doklady. Neoverené množstvo
   zostáva neoverené; pri prečíslovaní sa nepotvrdzuje nula ani nový príjem.
4. Zmapovať otvorené objednávky, pokladňové riadky a staré SKU v rozpracovaných
   príjemkách/prípravách. Starý alias pri vyhľadávaní karty sám nedokazuje,
   že objednávkový resolver prijme starý kód proti novému mapovaniu. Implementované
   podmienky a stále otvorené hranice prechodu sú nižšie. Pred realizáciou
   preveriť konkrétne objednávky, nie iba počet lokálnych riadkov.
5. Na dohodnutý okamih zastaviť dotknuté súbežné produktové importy/publikácie.
   Každý sending/uncertain záznam najprv vyriešiť jeho pôvodným postupom.
   Zachovať denníky a UUID. Neposielať starú pripravenú dávku s už zmeneným SKU.
6. Skontrolovať reklamné feedy a konverzné identifikátory. V Upgates je pre
   vybrané feedy **Doplnky → konkrétny doplnok → Export produktov → ITEM_ID formát**.
   Režim Kód môže zmenou SKU odpárovať ponuky. Nastavenie neprepínať automaticky;
   uložiť aktuálne ITEM_ID/G:ID a pripraviť postup pre každú aktívnu integráciu.
7. Vykonať až samostatne odsúhlasený malý pilot overenou operáciou. Zachovať
   Hub ID a všetky pôvodné SKU ako aliasy s pôvodom. Nové SKU musí byť jedinečné
   medzi produktmi aj variantmi každého e-shopu. Dve karty s históriou nezlučovať.

## Obnova údajov a indexov

Po premenovaní nestačí opraviť len `Product.sku`. Existujú tri odlišné uložené
pohľady, ktoré treba obnoviť a znovu porovnať s natívnymi ID:

| Úložisko | Úloha a obnova |
| --- | --- |
| SQL `ShopProductContent` a `ShopProduct` | Obnoviť produktové údaje/mapovanie z čerstvého Upgates čítania. Overiť presného rodiča a variant, nevytvoriť ďalšiu kartu podľa nového kódu. |
| `shops/<shop>/cache/upgates_products.json` | Vynútená obnova produktového pullu, nie čakanie na starú cache. Pull bez zásob nie je skladový pohyb. |
| `shops/<shop>/catalog-cache/identities.json` | Samostatná kontrola aktuálnych produktových identít cez existujúci check s `refresh=True`; overiť úplnosť a úspech nahradenia indexu. |

Starší index zhôd nemá byť dôkazom identity. Katalóg zobrazuje overené aktuálne
SKU Hubu; dodávateľský kód zostáva samostatný údaj. Nepridelené alebo konfliktné
položky sa riešia spoločnou prípravou, nie starým návrhom dodávateľskej predpony.

Pripravené importy a AI úlohy s pôvodnou identitou musia prejsť novým náhľadom.
Historické úspešné a neisté denníky sa neprepisujú. Skladové checkpointy kľúčované
SKU vyžadujú explicitný prevod alebo nový overený základ; nepoužiť ich bez
kontroly na iný kód. Obnova indexu sama neoprávňuje zapnúť prenos zásob.

## Návrat a ukončenie pilotu

Pri nesúlade ID alebo nepriehľadnom výsledku zastaviť ďalšie položky. Najprv
zistiť skutočný stav z oboch systémov. Návrat používa rovnaký overený spôsob
premenovania existujúcej karty späť a kontrolu, že starý kód medzitým neobsadila
iná karta. Alias a audit sa zachovajú. Import starého CSV nie je postup návratu.

Pilot je dokončený až po porovnaní ID, rodičov, EAN, parametrov, množstiev,
rezervácií, FIFO, dokladov a histórie; po obnove všetkých troch pohľadov a kontrole
starého aj nového SKU vo vyhľadávaní, príjme a otvorených objednávkach. Reklamné
ID sa musia správať podľa potvrdeného plánu. Publikačné nastavenia zostávajú
v pôvodnom stave.

Tento náhľad vykonanie migrácie nenahrádza. Inicializácia fyzického skladu a
zapnutie autority sú ďalšie samostatné kroky. xTrek na Atomeri sa nepovažuje za
pokrytý iba tým, že existuje prípravné pripojenie Upgates.

## Objednávky so starým SKU (implementované v kóde)

Objednávkový resolver má osobitný čítací prechod. Spoločný resolver produktov,
pullov, katalógu a príjmu zostáva prísny: všeobecný alias nie je povolenie zmeniť
existujúce shopové mapovanie.

Prechod prijme starý objednávkový kód iba pri splnení všetkých podmienok:

- presný registrovaný `internal_sku` patrí jednej aktívnej karte bez kolízie
  aktuálneho SKU, aliasov, EAN/UPC alebo iného shopového mapovania;
- aktuálne zalistované mapovanie daného e-shopu vedie na rovnaké Hub ID a jeho
  kód sa rovná aktuálnemu kanonickému SKU;
- `ProductEditorAudit` obsahuje úplný `verified_code_reconciliation` prechod
  od starého po aktuálny kód, pri viacerých premenovaniach v jednoznačnom
  rastúcom poradí revízií. Každý krok má rovnaké shop ID, mapping ID, natívnu
  identitu a fingerprint aktuálneho API pripojenia;
- natívne produktové ID v objednávke súhlasí s overeným produktom/rodičom.
  Samostatná karta nemá `option_set_id`; variant ho musí mať. **`option_set_id`
  sa neporovnáva s `variant_id` a nevyhlasuje sa za ten istý identifikátor**.
  Konkrétny variant identifikuje presný auditovaný alias a nezmenené natívne
  variantové ID v mapovaní, nie odvodená farba/veľkosť;
- rodič variantu zostáva nezmenený. Pokladňová skupina xTrek sa týmto prechodom
  automaticky nepokrýva.

Výsledok má `matched_by=verified_sku_alias` a odvodený `sku_alias_proof` s ID
auditov, mapovaním a natívnymi ID. `/orders/stock` ukáže starý kód → aktuálne SKU
alebo konkrétnu chýbajúcu identitu/dôkaz. Pri každom náhľade a potvrdení sa dôkaz
znovu načíta; uložená projekcia ho nesmie podsunúť. Chýbajúca história alebo
natívne údaje zostávajú blokované. Objednávka sa neprepisuje v Upgates.

Natívne skladové podklady dokážu po rovnakom overení použiť pôvodné nemenné
pozorovanie s pôvodným textom kódu. Nezmenené musia byť mapping ID, druh karty,
natívne ID a rodič. Staré udalosti sa neprepisujú; nové pozorovanie je nová
udalosť. Opakovanie výdaja zostáva idempotentné a už vydaná objednávka sa
nevydá druhýkrát. Zmena množstva alebo identity vydanej objednávky zostáva
blokovaná pôvodným obsahovým zámkom.

Pôvodný `OrderStockPolicy.starts_at`, hranice automatizácie a požiadavka
fyzického potvrdenia sa nemenia. Objednávky spred začiatku evidencie sa týmto
mechanizmom spätne nezaúčtujú. Čítací audit objednávok zostáva konzervatívny;
prijateľnosť pre sklad sa kontroluje čerstvým náhľadom `/orders/stock`.

Existujúci TOPEAK → ZOO nástroj vytvára tento overený audit. Všeobecný zápis
nového značkového SKU a jeho následné lokálne zosúladenie **zatiaľ nie je
implementované**. Budúca implementácia musí vytvoriť tento istý kontrolovaný
audit až po čerstvom overení všetkých natívnych ID a atomickom uložení aliasu
a mapovaní. Ručne dopísaná poznámka k aliasu nie je náhradou. Táto etapa ho
nevyrába pre plánované premenovanie ani nesprístupňuje migračné `apply`.

## Overiteľná serverová záloha pred pilotom

`scripts/backup_inventory.py` je pripravený nástroj pre administrátora na
produkčnom hostiteľovi. Nie je automatický deploy krok ani vzdialený endpoint.
Nezastavuje služby a neobnovuje produkčnú DB. Pred spustením treba overiť
aktuálne Compose súbory, umiestnenia dát, dostatok miesta pre archív aj dočasnú
DB a údržbové okno. GitHub prístup nie je serverový prístup.

1. Pozastaviť dotknuté externé importy/publikácie, všetkých Hub writerov a
   naplánované deploye. Zaznamenať predchádzajúce nastavenia. Administrátor
   zastaví API a prípadné samostatné workery; PostgreSQL zostane dostupný.
   Samotné nastavenie `manual` alebo skladová publikačná blokácia nezastaví
   všetky produktové/príjmové/filesystem zápisy. E-shopové objednávky vzniknuté
   počas okna treba následne zozbierať a zosúladiť; nemení sa začiatok evidencie.
2. Na hostiteľovi uložiť presnú skontrolovanú verziu skriptu z nasadeného
   commitu. Nasledujúce cesty sú referenčné; overiť ich oproti serveru:

   ```bash
   python3 /protected-tools/backup_inventory.py \
     --compose-dir /opt/inventory-hub \
     --compose-file docker-compose.yml \
     --compose-file docker-compose.ai-content.yml \
     --data-dir /opt/inventory-data \
     --output-root /opt/inventory-backups \
     --writers-paused
   ```

3. Nástroj odmietne bežiace API a výstup vnorený do zálohovaných dát. Vytvorí
   adresár `0700` a súbory `0600`: konzistentný custom-format `pg_dump`, archív
   aplikačných dát vrátane cache/indexov/importných denníkov, archív nasadzovacej
   konfigurácie a samostatný archív pripojeného supplier secrets volume.
4. Overí SHA-256/veľkosť archívov a celý dump obnoví s `--exit-on-error` a
   `--single-transaction` do nového náhodne pomenovaného PostgreSQL kontajnera
   z rovnakého lokálneho image ID. Kontajner má `--network none`, žiadne
   produkčné volumes ani publikované porty a po kontrole sa odstráni. Overia
   sa aj počty základných skladových/FIFO/objednávkových tabuliek.
5. Iba úspešný `manifest.json` s `status=verified` a `restore_verified=true`
   dokazuje túto kontrolnú obnovu. Zlyhanie ponechá stav `incomplete` a pilot
   sa nezačne. Archívy obsahujú prístupy a zákaznícke dáta: neposielať ich do
   chatu ani GitHubu; uchovať aj druhú chránenú kópiu. Na kontrolu postupu stačí
   sanitizovaný manifest, nikdy hodnoty tajomstiev.
6. Po zachovaní zálohy administrátor obnoví služby overeným prevádzkovým
   postupom a pôvodné nastavenia. Zálohovací skript ich sám nespúšťa ani
   nemení skladovú autoritu. Prerušená záloha potrebuje kontrolu a novú snímku,
   nie pokračovanie pilotu s čiastočnými súbormi.

Testovaná kontrolná obnova DB nie je kompletný nácvik obnovy celého servera:
permissions, deploy konfigurácia, externé uložené kópie a filesystem/secret
archívy vyžadujú administrátorskú kontrolu pred pilotom. Celkový bod obnovy
je konzistentný len pri skutočne pozastavených všetkých writeroch; príznak
`--writers-paused` je výslovné potvrdenie obsluhy, nie automatická detekcia.

### Opätovné overenie existujúcej zálohy bez odstávky

Na serveri s Dockerom a už uloženou úplnou zálohou možno spustiť:

```bash
python3 /protected-tools/backup_inventory.py \
  --verify-existing /opt/inventory-backups/NAZOV_KONKRETNEJ_ZALOHY
```

Použiť presný existujúci adresár, nie domyslený názov ani automatický výber
„najnovšieho“. Tento režim nepotrebuje Compose, prístup do produkčnej DB,
`--writers-paused` ani zastavenie API. Nič neobnovuje do produkcie. Vyžaduje
lokálne dostupný pôvodný PostgreSQL image podľa ID uloženého v manifeste;
image nesťahuje a nenahrádza inou verziou.

Pred spustením na živom hostiteľovi overiť voľný disk pre celú rozbalenú
testovaciu DB, indexy a dočasné súbory, rezervu pamäte/CPU a prijateľnú záťaž
disku. Izolácia siete neizoluje výkon hostiteľa. Obnovu naplánovať mimo špičky
alebo použiť samostatný dôveryhodný Docker host s chránenou kópiou zálohy
a rovnakým lokálnym image. Tento režim automaticky neobmedzuje prostriedky
kontajnera a dostupnú kapacitu nepotvrdzuje.

Kontrola odmietne neúplný manifest, chýbajúcu časť rozsahu alebo niektorý zo
štyroch súborov, symlink adresára/manifestu/archívu, neplatné metadáta,
nesúhlas SHA-256/veľkosti, nečitateľný obsah tar či cesty a odkazy unikajúce
z archívu. Konzervatívne odmieta aj každý tar odkaz s komponentom `..`,
vrátane niektorých inak interných odkazov: samotná lexikálna normalizácia
nevylúči únik cez reťazec symlinkov. Taký archív vyžaduje osobitnú kontrolu,
nie vypnutie poistky. Potom celý dump obnoví rovnakým izolovaným postupom, porovná
počty základných tabuliek s pôvodným výsledkom a opäť overí nemennosť zdroja.
Zdrojový manifest ani archívy neupravuje a neúspešnú pôvodnú snímku dodatočne
neoznačí za overenú.

Na štandardný výstup vypíše samostatný JSON výsledok s odtlačkom zdrojového
manifestu, časom a počtami tabuliek; úspech má `status=verified`,
`restore_verified=true` a návratový kód 0. Chyba má `status=failed`,
`restore_verified=false` a nenulový návratový kód. Výsledok možno uložiť do
nového chráneného súboru mimo pôvodnej zálohy; zachovať aj neúspešné výsledky.
Nevypisuje obsah DB, dokumentov ani tajomstvá. Úspech overuje obnoviteľnosť
tejto konkrétnej snímky, nie jej aktuálnosť, existenciu druhej kópie,
pravidelné plánovanie alebo úplnú obnovu aplikácie a súborov.

`api/tests/test_backup_inventory.py` používa syntetické súbory a napodobené
Docker príkazy na kontrolu bezpečnostných podmienok. Úspech týchto testov
nie je vykonaná kontrolná obnova produkčnej zálohy. Tú dokladá až výstup
skutočného serverového spustenia nad vybranou zálohou.

Pri návrate po neúspešnom premenovaní sa uprednostní kontrolovaná oprava
identity vpred alebo spätné premenovanie pri zachovaní ID. Úplná obnova DB
nie je bežný rollback SKU: mohla by stratiť novšie príjmy a objednávky. Vyžaduje
osobitné rozhodnutie, zastavenie všetkých writerov a zosúladenie všetkých zmien
po snímke. Tento skript takú obnovu produkcie vôbec neponúka.

## Poradie malého pilotu

Najprv samostatný produkt s overeným MPN a natívnym ID; až po úspechu jeden
variant s nezmeneným rodičom. Kolízne karty a POS xTrek ostávajú mimo dávky.
Pre každú kartu uchovať súkromný protokol pred/po a splniť tieto kroky:

1. Čerstvý migračný náhľad, voľný cieľový kód, rovnaké native ID, MPN, EAN a
   variantové parametre. Potvrdený serverový backup a osobitné povolenie zápisu.
2. Zoznam otvorených objednávok/košíkov pred aj po hranici evidencie, nemenných
   UUID a už zaúčtovaných rezervácií/výdajov. Skontrolovať staré kódy aj aliasy;
   historické objednávky nesmú byť odoslané do nového skladu automaticky.
3. Podporované administratívne premenovanie existujúcej karty a implementované
   následné lokálne zosúladenie s auditom. Ak druhý krok ešte nie je dostupný,
   nezačínať prvý. Nenahrádzať ho SQL editom či importom nového `CODE`.
4. Porovnať identitu, množstvá, rezervácie, karanténu, FIFO, pohyby a doklady.
   Po prečíslovaní zatiaľ očakávať presne nulovú zmenu skladových hodnôt.
5. Obnoviť všetky tri vyššie uvedené pohľady a zneplatniť staré pracovné
   náhľady; neisté/úspešné denníky sa nemenia. Overiť obe SKU v Hube a príjme
   a čerstvý náhľad starej objednávky. Reálny výdaj potvrdzovať až pri odovzdaní.
6. Porovnať presné `ITEM_ID`/`G:ID`, URL a rodičovské skupiny vo všetkých
   aktívnych reklamných feedech aj konverziách. Snímka administrácie BIKETREK
   dodaná 22. 10. 2026 ukazuje pri Heureke formát **ID**; je to dôkaz nastavenia
   jedného doplnku, nie automatické overenie aktuálnych hodnôt feedu či Google/Meta.
7. Až po uzavretí prvého protokolu pokračovať variantom. Nastavenia zásob
   ponechať pôvodné; počiatočné naskladnenie a skladová autorita sú samostatné.

Zdroje kontraktu opätovne overené 22. 10. 2026:
- https://docs.upgates.com/api-reference/produkty
- https://docs.upgates.com/api-reference/produkty-seznamy
- https://www.upgates.cz/a/varianty
- https://www.upgates.cz/a/propojeni-na-srovnavace-zbozi
- https://docs.upgates.com/api-reference/objednavky
