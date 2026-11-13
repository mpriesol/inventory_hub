# ARIGA

Dodávateľ `ariga` pokrýva najmä Fenix, ďalej Ruike a prípadné ďalšie značky.
Používa spoločnú prípravu pre **BIKETREK** aj **xTrek**; značka Fenix nie je
obmedzením sortimentu. B2B: <https://b2b.ariga.cz/index.php>.

## Registrácia a katalóg

Pred založením overiť existujúci záznam `ariga` v konfigurácii aj v PostgreSQL.
Existujúcu neaktívnu kartu doplniť, nevytvárať druhého dodávateľa. Aktivácia
dodávateľa sama nenastavuje zdroj produktov ani skladovú automatiku.

Nastavenia: EUR, `adapter_settings.vat_rate: 23` a `vat: 23`, koeficienty
Fenix/Ruike `1`. Predvolený e-shop nemusí byť vybraný; spoločná príprava umožňuje
oba ciele. Existujúce SKU sa nemenia. Dodávateľský kód z faktúry sám nepotvrdzuje
výrobný kód ani nové fyzické SKU; platí [politika identity](sku-policy.md).

Katalógový feed zatiaľ nie je dodaný. Pri kontrole prihláseného B2B účtu a
veľkoobchodných informácií nebol nájdený odkaz na XML/CSV export. To nevylučuje
feed poskytovaný dodávateľom individuálne. Implementovaný náhradný zdroj je
`ariga-b2b`, serverový scraper v `services/ariga_b2b.py` s natívnym parserom
`adapters/ariga_catalog.py`. Používa existujúci katalóg aj prípravu oboch e-shopov.

**Dodávatelia → Ariga → Upraviť → Feedy → Produkty → ARIGA B2B** umožňuje
uložiť e-mail a heslo; výber nastaví `feeds.current_key: products`. Heslo je
zapisovateľné, ale API ho späť nevydáva; prázdne pole ponechá uložené heslo.
Používa existujúcu chránenú konfiguráciu dodávateľa. Prihlásenie v prehliadači
neprihlási server. B2B URL sa nevkladá do poľa XML/CSV feedu. Základ konfigurácie:

```json
{"feeds":{"current_key":"products","sources":{"products":{
  "mode":"ariga-b2b","remote":{"auth":{"mode":"form","username":"","password":""}}
}}}}
```

V **Produktoch dodávateľa** sa katalóg obnoví existujúcim načítaním zdroja.
Scraper po prihlásení potvrdí EUR, prejde hlavné kategórie a všetky strany,
odstráni opakované odkazy a prečíta jednotlivé produkty vrátane všetkých
explicitných variantov. Neobmedzuje značky iba na Fenix/Ruike. Uchová názov,
technické parametre, zdrojový český popis, kategóriu a oficiálne obrázky;
predajný obsah sa pripravuje po slovensky existujúcim postupom.

Identitu určuje dodávateľský kód konkrétneho variantu. EAN ani výrobné číslo
neodvodzuje z názvu či kódu; chýbajúce údaje ostanú prázdne. Existujúce SKU a
pravidlá priraďovania identity sa nemenia. Cenový kontrakt:

| Zdroj | Pravidlo |
| --- | --- |
| Veľkoobchodná cena bez DPH | Zachovať ako nákupnú cenu. Pre odvodenú cenu s DPH použiť × 1,23 a centové zaokrúhlenie. Českú VO cenu s DPH ignorovať. |
| Odporúčaná MOC s DPH | Zachovať nominálnu sumu. Slovenský základ vypočítať delením 1,23; nepripočítať DPH k už hrubej MOC. |
| Historická cena faktúry | Zachovať presnú fakturovanú cenu bez DPH; aktuálna ponuka ju nesmie prepísať. |

Parser tieto pravidlá vykonáva pomocou `Decimal` a `adapter_settings.vat_rate`
(predvolene 23). Chýbajúca cena nie je nula. Testy preverujú aj spoločný
Upgates cenový payload pre oba režimy s/bez DPH. Pred prvým odoslaním overiť
náhľady oboch e-shopov a ich skutočné nastavenia. Prípadný budúci feed potrebuje
overenie a mapovanie svojich polí; tieto konvencie ho automaticky neparsujú.

### Dostupnosti a hranice čítania

V **Dostupnosti a synchronizácia** možno použiť rovnaký zdroj `products`.
Pravidelné načítanie je predvolene vypnuté, nastavenie ponúka denný interval.
Obnovuje pozorovania dostupnosti; katalógové ceny a obsah sa naďalej obnovujú
cez katalóg. `Skladem` znamená áno bez počtu kusov, `Není skladem` nie bez
počtu; `Na cestě`, `Na dotaz` a iné nepotvrdené hodnoty sú neznáme. Nevytvárajú
potvrdenú nulu ani nepredĺžia staršie pozorovanie. Import a načítanie dostupností
nevytvárajú fyzický príjem, vlastné množstvo, FIFO pohyb ani zápis do e-shopov.

Čítanie je obmedzené na HTTPS doménu B2B, známe kategórie/produkty, login a
prepnutie EUR. POST je povolený iba na prihlásenie a stránkovanie; košík ani
objednávky sa nepoužívajú. Medzi požiadavkami je najmenej 0,5 sekundy,
limit celého behu 15 minút, 2 000 produktových stránok, 40 kategórií, 250
strán katalógu, 4 MiB na odpoveď a 100 MiB spolu. Chyba prihlásenia, ochranná
výzva, zmena meny/variantov alebo neúplné stránkovanie zastavia beh bez
automatického opakovania. Posledný úspešný katalóg zostane zachovaný.

Úplný XML snapshot obsahuje iba produktové fakty; heslo, cookies a HTML účtu
sa neukladajú do snapshotov ani chýb. Dostupnosti používajú existujúci dočasný
adresár a po spracovaní ho odstránia. Nie je potrebná migrácia ani nová
závislosť. Testy používajú syntetické HTML; úspešné CI nenahrádza prvé úplné
živé načítanie po uložení serverových prihlasovacích údajov.

## PDF faktúry bez feedu

`adapters/ariga_invoice.py` podporuje overený jednostranový textový EUR formát
s tabuľkou **Zboží / Kód / Cena/Ks bez DPH / Kusů / Celkem bez DPH / DPH /
Celkem s DPH**. Overuje dodávateľa, číslo, dátumy, každý riadok, množstvo ×
cenu, DPH, poštovné aj celkový súčet pomocou `Decimal`. Nečitateľný sken,
viacstranový alebo zmenený formát sa odmietne ako zdroj príjmu; neakceptuje
sa iba čitateľná časť dokladu. Podpora iného formátu potrebuje nový vzor.

**Dodávatelia → Ariga → Faktúry → Nahrať** uloží originál, overené položky a
medzisúbor pre existujúci príjem. **Faktúry → Pridať faktúru** využíva rovnaký
parser na archívne údaje, ale samotné archivovanie nevytvára zdroj príjmu.
Pri nepodporovanom formáte možno originál uchovať vo Faktúrach s označením
chyby parsovania, bez vymyslených finančných údajov.

Cena/Ks bez DPH sa prenesie presne z dokladu. Pôvodná nulová DPH pri
cezhraničnej faktúre ostáva nulová; katalógových 23 % sa na doklad neuplatní.
EAN na tomto formáte chýba, preto ostáva prázdny. Nepomenované výrobné kódy
sa neodvodzujú z názvu ani z dodávateľského kódu.

Nekódované **Poštovné** sa overí a zahrnie do celkových súm a otlačku dokladu.
Nevyrába sa preň SKU, EAN ani počet kusov. Zoznam parsovaných tovarových
riadkov a CSV pre príjem ho nezahŕňajú; jeho samostatný rozpis ostáva v
originálnom PDF. Nemení jednotkové nákupné ceny tovaru ani FIFO.

Spoločný dispatch `adapters/invoice_pdf.py` používa upload, archív aj overenie
príjmu. Opakované nahratie zachová index, poznámky a stav; zmenený doklad s
rovnakým číslom sa odmietne. Pred založením príjmu sa znovu overí originál aj
zhoda CSV. Samostatné faktúry s rôznymi doloženými číslami môžu obsahovať
rovnaké položky. Upload nezaloží skladovú kartu, príjemku ani skladový pohyb.

Nie je potrebná migrácia ani nová závislosť. Testy používajú syntetické
dokumenty; súkromný vzor sa neukladá do repozitára. CI, nasadenie, uloženie
produkčnej konfigurácie a prvé živé spracovanie dokladu sa overujú samostatne.
