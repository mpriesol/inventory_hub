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

Katalógový feed zatiaľ nie je dodaný. ARIGA nemá v tejto implementácii hotový
serverový B2B konektor ani natívny katalógový parser. URL B2B stránky nie je
feed a nesmie sa vložiť ako `products` alebo `stock`. Prihlásenie v prehliadači
nie je trvalé prihlásenie servera. Prístupové údaje patria iba do existujúcej
chránenej konfigurácie, nikdy do Git-u.

Pri dodaní feedu použiť existujúce mapovanie/normalizáciu a overiť konkrétne
zdrojové polia, EUR, identity variantov, obrázky a dostupnosť. Cenový kontrakt:

| Zdroj | Pravidlo |
| --- | --- |
| Veľkoobchodná cena bez DPH | Zachovať ako nákupnú cenu. Pre odvodenú cenu s DPH použiť × 1,23 a centové zaokrúhlenie. Českú VO cenu s DPH ignorovať. |
| Odporúčaná MOC s DPH | Zachovať nominálnu sumu. Slovenský základ vypočítať delením 1,23; nepripočítať DPH k už hrubej MOC. |
| Historická cena faktúry | Zachovať presnú fakturovanú cenu bez DPH; aktuálna ponuka ju nesmie prepísať. |

Konvencie `catalog.purchase_price_basis: net`, `retail_price_basis: gross` a
`retail_vat_policy: preserve_gross` zaznamenávajú dohodnutý budúci katalógový
kontrakt. Samotné uloženie týchto hodnôt bez mapovania/parsera nespúšťa import
ani nepreukazuje správnu normalizáciu neznámeho feedu. Overiť náhľady oboch
e-shopov a ich skutočný režim ceny s/bez DPH pred prvým odoslaním.

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
