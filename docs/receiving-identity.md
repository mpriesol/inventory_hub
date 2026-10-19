# Príjem: identita produktu a opakovanie skenov

Táto časť príjmu pracuje iba s údajmi v Hube. Skenovanie, kontrola identity ani dokončenie príjmu nevolajú Upgates. Dokončenie príjmu zapíše pohyby a nákupné ceny do lokálneho skladu; samo nespúšťa publikovanie zásob do e-shopov.

## Pre obsluhu

- Zoznam položiek sa otvorí automaticky. Šípky pri prijatom množstve pridajú alebo odoberú jednu fakturovanú jednotku; ceruzka umožní zadať presné množstvo. Počas ukladania alebo čakajúcich skenov sú ručné zmeny blokované. Pri stratenej odpovedi sa najskôr načíta uložený stav, aby ďalší klik nevychádzal zo starého množstva.
- Stĺpec EAN uprednostní kód z faktúry, potom overené kódy jednoznačne priradeného produktu a napokon kód z katalógu rovnakého dodávateľa podľa presného dodávateľského SKU. Katalógový zdroj je označený; samotné zobrazenie kódu ešte nezakladá skladovú kartu ani identitu pre skener. Pri konflikte sa náhradný kód nedoplní.
- **Dokončiť aj bez všetkých položiek** umožní po výslovnom potvrdení uzavrieť historickú faktúru aj s neprijatými riadkami alebo nulovým príjmom. Do skladu vstúpi iba zadané prijaté množstvo. Už predané alebo neprítomné kusy nechaj neprijaté; tlačidlo nedoplní chýbajúce množstvá. Doklad sa presunie do dokončených a opakovanie nevytvorí ďalšie pohyby.
- Každý nový sken pripočíta zadané množstvo. Desať samostatných skenov toho istého EAN pridá desať kusov. Rýchle opakované skenovanie nie je blokované časovým oknom.
- Ak sa odpoveď stratí, opakuj pôvodnú operáciu. Rovnaká operácia nepripočíta množstvo druhýkrát. Nový fyzický sken musí zostať samostatnou operáciou.
- Ak rovnaký kód zodpovedá viacerým faktúrovým riadkom, systém svojvoľne nevyberie prvý. Riadky môžu mať rozdielne nákupné ceny. Použi výslovný výber riadka, ak ho klient podporuje, alebo existujúcu ručnú úpravu jeho prijatého množstva.
- Sken môže použiť EAN z faktúry, kód dodávateľa, celý produktový kód alebo ďalší evidovaný overený EAN už priradeného produktu.
- Rozpor kódu, EAN a uloženého produktu treba opraviť pred príjmom. Ani „dokončiť napriek neprijatým riadkom“ neobíde konflikt identity.
- Pri dokončení sa kontroluje aj produkt, ktorý bol priradený už pri otvorení faktúry. Zmena jeho identifikátorov medzi otvorením a dokončením nemôže potichu naskladniť iný produkt.
- Jeden fyzický produkt môže mať viac EAN aj viac dodávateľských kódov. Rovnaký krátky neoverený číselný kód sám osebe nespája produkty od rôznych dodávateľov.
- Dokončený alebo zrušený príjem sa pri opätovnom otvorení zobrazí iba na čítanie. Šípka späť aj **Ukončiť** vrátia zoznam bez pozastavovania, ďalšieho dokončenia či zmeny skladu. Pozastavený príjem treba najskôr obnoviť cez **Pokračovať** na zozname príjmov.
- Pred skenovaním sa vždy načíta aktuálny stav relácie. Samotných 100 % prijatých položiek neznamená, že príjem bol dokončený; rozhoduje uložený stav. Pri odchode z rozpracovaného príjmu sa stav znovu overí a až potom sa príjem pozastaví. Ak pozastavenie zlyhá a server stále eviduje rozpracovaný príjem, stránka zostane otvorená s chybou.
- Nepotvrdené skeny dokončeného alebo pozastaveného príjmu možno overiť pôvodným tlačidlom opakovania. Samotná neodosielajúca sa uložená fronta neblokuje návrat na zoznam a nezmaže sa pri odchode. Počas práve prebiehajúcej požiadavky treba počkať na odpoveď.

Server kontroluje stav príjmu pod zámkom aj pri priamom API volaní. Nový sken, ručná zmena množstva, prijatie všetkých riadkov, reset a prvé dokončenie sú dovolené iba pre nový alebo rozpracovaný príjem. Pozastavený príjem treba obnoviť; zrušený sa nesmie zaúčtovať ani s `force=true`. Úspešný pôvodný sken možno naďalej overiť jeho UUID a dokončenie opakovať bez ďalšieho pohybu. Čiastočne aj vôbec neprijaté riadky vyžadujú výslovné `force=true`; potvrdenie neobchádza kontroly identity a ceny.

Nové HTTP skeny, ručné zmeny množstva a pohyby dokončeného príjmu zapisujú identitu prihlásenej obsluhy (`user:<id>`, prípadne spoločný `operator-token`). Klientsky údaj `scanned_by` nemôže nahradiť túto identitu. Historické záznamy sa nemenia a opakovanie úspešnej požiadavky zachová pôvodného autora. Použitie spoločného tokenu nerozlišuje konkrétnych ľudí; na to používaj samostatné účty.

Založenie príjmu prijíma faktúrový názov z indexu alebo jeho tvar `dodávateľ:názov`. Prefix musí patriť zvolenému dodávateľovi. Odkaz nemôže obsahovať cestu, zakódované oddeľovače ani odkazovať cez symlink; číta sa iba existujúci CSV dokument v priečinku faktúr daného dodávateľa. Neplatný odkaz sa odmietne pred prístupom do databázy.

Rovnaké obmedzenie názvov a súborových ciest platí aj pre starší JSON režim príjmu. Vyhľadanie relácie, história, súhrny, dokončenie a zápis indexu nesmú nasledovať symlinky ani meniť súbory mimo pevne určených priečinkov dodávateľa.

Kompletná administrácia opráv identifikátorov a priraďovanie neskôr prijatej faktúry k už vykonanému príjmu zostávajú ďalšími krokmi. Chýbajúca nákupná cena vo faktúrovom príjme sa naďalej musí vyriešiť pred jeho dokončením. Samostatný príjem bez známej ceny používa existujúci postup v [dokumentácii nákupných vrstiev](fifo.md).

## Založenie skladových kariet z príjemky

Príjemka používa ten istý zoznam produktov ako Hub. Existencia skladovej karty a počet fyzických kusov sú dve samostatné informácie. Pri položkách sa zobrazuje stav karty a **Náš sklad** v sklade danej príjemky; neoverený historický stav sa nezobrazuje ako potvrdená nula.

| Stav karty | Význam a ďalší krok |
| --- | --- |
| Spárovaný | Jednoznačne existujúca karta; možno ju otvoriť. |
| Základná karta | Karta založená predchádzajúcim príjmom bez dokončeného produktového obsahu; **Doplniť údaje karty** otvorí spoločnú prípravu. |
| Len vo feede | Dodávateľský produkt je v načítanom katalógu, ale karta v Hube chýba. |
| Nenájdený | V overenom načítanom katalógu ani v Hube sa nenašla jednoznačná zhoda; príprava vychádza z faktúry. |
| Nejasná zhoda | Identifikátory si odporujú alebo existuje viac kandidátov; treba opraviť identitu pred založením. |
| Feed sa nepodarilo overiť | Chýba použiteľný katalógový podklad. Nejde o potvrdenie, že produkt vo feede neexistuje. |

**Vybrať chýbajúce produkty** a **Založiť skladové karty (N)** otvoria existujúci editor `/product-import` s konkrétnymi vybranými položkami. Feed poskytne produktové údaje; pri položke bez feedu sa použije dostupný názov, kód a EAN faktúry. Chýbajúce údaje možno doplniť ručne alebo cez spoločnú voliteľnú AI prípravu s oficiálnymi zdrojmi. Predajná cena, DPH, EAN ani dostupnosť sa nevymýšľajú. Rovnaký produkt na viacerých riadkoch faktúry sa pripraví iba raz; faktúrové riadky, bonusy, množstvá a rozdielne nákupné ceny zostanú samostatné.

Rozpracovaná príprava je trvalo naviazaná na príjemku. Opakované otvorenie pokračuje v uloženom koncepte. **Späť na príjem faktúry …** vráti tú istú príjemku; pozastavený príjem možno obnoviť bez straty prijatých množstiev. Uložené karty sa opäť overia spoločným resolverom. Konflikt sa nesmie vyriešiť vytvorením duplicity.

Uloženie kariet **nemení fyzický sklad ani faktúrovú nákupnú cenu**. Pohyby a FIFO vzniknú až bežným dokončením príjmu podľa pôvodných prijatých množstiev a jednotkových cien faktúry. Samostatné voľby BIKETREK, xTrek a AI používajú [spoločný importný postup](product-imports.md). Uloženie iba do Hubu je možné aj bez kompletných obchodných údajov; publikovanie do e-shopu naďalej vyžaduje platné ceny, DPH, kategóriu a ostatné kontroly.

### API prípravy kariet

Pod cestou `/suppliers/{supplier_code}/receiving/sessions/{session_id}`:

| Metóda a koncovka | Úloha |
| --- | --- |
| `GET /products` | Čítací prehľad identity, katalógového zdroja, potvrdeného vlastného skladu a prepojených konceptov. |
| `POST /products/prepare` | Založenie alebo opätovné otvorenie prípravy pre `line_ids`; načíta zdroje na serveri. |
| `POST /products/reconcile` | Opätovné overenie a priradenie uložených kariet do nedokončeného príjmu; bez zmeny množstiev. |

Rozšírenie používa existujúce JSON dokumenty príjemky a importných konceptov, bez novej SQL migrácie. Bežné čítanie nevolá dodávateľské portály ani e-shopy; príprava nadväzuje na aktuálne uložený katalóg a existujúci importér.

Tieto tri nové endpointy vyžadujú operátorské prihlásenie a vracajú `Cache-Control: no-store`. Testy `test_receiving_products_db.py` a importné regresie pokrývajú výpadok feedu, overenú zhodu viacerých EAN, odlišný kód vo feede a na faktúre, opakované uloženie, zachovanie ručných údajov a pôvodného SKU, návrat do tej istej príjemky a oddelenie založenia karty od skladového pohybu. Databázové prípady používajú iba izolovanú testovaciu schému; UI scenáre používajú syntetické odpovede.

## Spoločná identita

`services/product_identity.py` je spoločný resolver pre načítanie z e-shopov, objednávky, lokálny katalógový import a faktúrový príjem. Presný spoločný SKU má prednosť ako identifikátor. Overené EAN/UPC a kód dodávateľa v rámci konkrétneho dodávateľa sú ďalšie kontrolné dôkazy. Rôzni vlastníci týchto dôkazov znamenajú konflikt, nie automatické zlúčenie. Zmena iba veľkosti písmen SKU je tiež konflikt.

Pri lokálnom príjme/importoch resolver nevyužíva rodiča ani mapovanie konkrétneho e-shopu. Pokladňový produkt xTrek preto nemení skladovú identitu variantu. Existujúca podpora jednoznačného overeného EAN umožňuje nájsť fyzický produkt aj cez ďalší dodávateľský kód; jeho kanonický SKU sa pritom nemení.

Uložený `ReceivingLine.product_id` sa odovzdáva ako `expected_product_id` a porovnáva s aktuálnymi dôkazmi. Neexistujúci alebo neaktívny produkt sa neprijme. Aj jednoznačný dodávateľský kód musí súhlasiť so známymi overenými EAN produktu.

Zápisy identity pri dokončení príjmu používajú ten istý transakčný advisory lock `IDENTITY_WRITE_LOCK` ako registrácia katalógového importu a načítanie produktov. Až po vyriešení všetkých prijatých riadkov sa zamykajú zostatky a zapisujú skladové pohyby. Konflikt vracia celú nedokončenú transakciu späť. Pri súbežnom príjme rovnakého overeného EAN od dvoch dodávateľov vznikne jedna fyzická identita s dvomi dodávateľskými identifikátormi.

Zložené EAN z faktúry sa zachovajú v `receiving_lines.ean` a pri zápise sa rozdelia na jednotlivé identifikátory. Úvodné nuly sa nestrácajú. Udalosť ručnej zmeny množstva odkazuje na faktúrový riadok a do poľa kódu ukladá jeden overený EAN, prípadne reálny dodávateľský alebo kanonický SKU do 100 znakov. Dlhý pôvodný zdroj EAN zostáva celý na riadku; kód sa neskracuje na vymyslený identifikátor. Nový neznámy produkt vyžaduje dodávateľský kód a používa uzamknutý prefix + kód. Existujúci produkt možno naďalej identifikovať overeným EAN; staré SKU s `EAN-` sa nemenia. Samotný EAN už nevytvára nové náhradné SKU neznámeho produktu.

Príjem dopĺňa aj jednoznačnú väzbu na dodávateľskú ponuku, aby ďalší skladový feed našiel ten istý produkt. Pravidlá prefixu, konfliktov a lokálnej opravy starých väzieb sú v [supplier-identity.md](supplier-identity.md).

## API skenera

`POST /suppliers/{supplier_code}/receiving/sessions/{session_id}/scan`

```json
{
  "code": "4006381333931",
  "qty": 1,
  "scanned_by": "scanner",
  "request_id": "8ad719ce-894c-4a9f-97b3-5e70c62ac61f",
  "line_id": 123
}
```

`line_id` je voliteľný výber konkrétneho faktúrového riadka. Musí patriť danej relácii a zodpovedať naskenovanému kódu. `request_id` je voliteľné kvôli starším klientom, ale nový klient ho posiela vždy. Starší klient bez UUID má pôvodnú sémantiku: každá požiadavka je nový sken, bez záruky bezpečného opakovania po výpadku.

Odpoveď naďalej obsahuje `status`, `line`, `summary`; dopĺňa `request_id` a `replayed`. `line.id` umožňuje výslovný výber pri nejednoznačnosti. `line.product_code` pri známom produkte zobrazuje jeho kanonický SKU; pri ešte nepriradenom riadku zostáva odvodený návrh dodávateľského kódu s prefixom.

Odpovede s položkami obsahujú aj `display_ean` a `ean_source` (`invoice`, `product`, `supplier_catalog` alebo `null`). Služba `receiving_barcodes.py` číta zdroje dávkovo bez zmeny pôvodného `line.ean`, fingerprintu, produktových väzieb či skladu. Historické odpovede opakovaných skenov môžu tieto doplnkové polia postrádať; klient po opakovaní používa aktuálny súhrn.

- Nové UUID: nová operácia a jeden `ScanEvent`.
- Rovnaké UUID a rovnaká relácia, orezaný kód, množstvo, pracovník a výber riadka: pôvodná uložená odpoveď s `replayed: true`, bez ďalšieho pripočítania a udalosti.
- Rovnaké UUID s iným obsahom alebo v inej relácii: HTTP 409, `detail.code = scan_request_conflict`.
- Viac zhodných riadkov bez výberu: HTTP 409, `scan_line_ambiguous`, zoznamy `line_ids` a `line_numbers`.
- Výber nesúvisiaceho riadka: HTTP 409, `scan_line_mismatch`.
- Rozpor identity: HTTP 409, `receiving_identity_conflict`, podľa dostupnosti aj riadok, kandidáti a dôvody.
- Pripočítanie nad rozsah databázového množstva: HTTP 409, `scan_quantity_out_of_range`.

Neúspešná transakcia neukladá úspešné potvrdenie UUID. Úspešný sken možno zopakovať aj po pozastavení či dokončení relácie. Jeho uložená odpoveď je historická: napríklad po ďalších skenoch alebo ručnom vynulovaní zobrazuje pôvodné množstvo. Klient po obnove načíta aktuálny súhrn, neprenesie starú odpoveď ako nový stav skladu.

Klient vyžaduje platný `status` z `GET .../summary` (`new`, `in_progress`, `paused`, `completed`, `cancelled`). Množstvá odovzdané navigáciou neautorizujú nové skeny pred týmto čítaním. Iba `new` a `in_progress` povoľujú úpravy. Načítanie aj replay sú viazané na aktuálnu reláciu, takže oneskorená odpoveď predchádzajúcej relácie nemôže znovu odomknúť dokončený príjem. Odchod z aktívnej relácie používa nové čítanie stavu; po chybe pozastavenia čítaním rozlíši skutočné zlyhanie od stratenej odpovede alebo súbežného dokončenia. Neistá odpoveď bez potvrdeného neaktívneho stavu neznamená úspešné pozastavenie.

Zámok relácie chráni súčet množstiev. Transakčný zámok UUID chráni aj súbežné použitie rovnakého ID v rôznych reláciách. `receiving_scan_requests` ukladá otlačok požiadavky, odpoveď a väzbu na `scan_events` v rovnakej transakcii ako množstvo. Klient pri neistej odpovedi zachová UUID aj payload a nesmie vygenerovať nové UUID iba kvôli retry. Načítanie stránky či reštart API nemaže uložené potvrdenia.

## SK Profi Bike PDF invoices

Príjem podporuje pôvodné textové PDF SK Profi Bike cez `POST /suppliers/sk-profi-bike/upload-invoice`, teda nahratie v dodávateľskom postupe príjmu. Samostatné všeobecné nahratie do archívu `/invoices/upload` nie je tento parser. Adaptér `sk_profi_bike_invoice.py` používa `pypdf`, overí dodávateľa, číslo, dátumy, poradie riadkov, množstvá, jednotkové ceny bez DPH a súhrn DPH. Podporuje viacstranové doklady s opakovanou jedno- alebo dvojriadkovou hlavičkou, zalomené názvy v stĺpci názvu a jednotky `Ks`, `Pár`, `Sada`, `Bal`. Množstvá zostávajú vo fakturovanej jednotke; balenia a páry sa nerozpočítavajú na jednotlivé kusy. Číslovanie strán, ak je prítomné, musí zodpovedať všetkým fyzickým stranám PDF v správnom poradí. Nečitateľná alebo chýbajúca strana sa nesmie potichu vynechať. Nepodporované, šifrované alebo naskenované PDF sa odmietne; limit je 20 MB a 50 strán.

Voliteľný stĺpec `Zľava %` sa overí proti pôvodnej jednotkovej cene a čistému súčtu riadka. Do príjmu ide cena po zľave: fakturovaný čistý súčet delený množstvom, na štyri desatinné miesta. Rozpočítaná cena musí po vynásobení množstvom zachovať centový súčet riadka aj dokladu; nepodporovaná presnosť sa odmietne namiesto zmeny hodnoty príjmu. Výslovná 100 % zľava je známy nulový náklad, nie chýbajúca cena. Opakované SKU zostávajú samostatnými riadkami, pretože platená položka a bonus môžu mať rozdielnu cenu. DPH musí presne sedieť buď ako súčet zaokrúhlených daní riadkov, alebo ako zaokrúhlená daň zo základu za každú sadzbu; ľubovoľná centová tolerancia sa nepoužíva. Riadkové súčty, DPH aj celková fakturovaná suma sa vždy kontrolujú.

Ukladá pôvodné PDF, kanonické CSV `SCM;TITLE;QTY;PRICE;EAN` a index pre existujúci príjem. `PRICE` je nákupná cena bez DPH; konfigurácia používa `unit_price_source: "ex"`. Faktúra bez EAN má prázdny EAN, nie vymyslený kód. Existujúci produkt sa páruje dodávateľským aliasom alebo overeným EAN; nový používa prefix `SKP-`. Skenovanie EAN vyžaduje už známu jednoznačnú identitu v Hube. Samotná prítomnosť produktu v katalógu ju nenahrádza.

Opakované nahratie rovnakého dokladu zachová existujúci stav a nevytvorí duplikát. Zmenený obsah pod rovnakým číslom skončí konfliktom. Nahratie nevytvára skladový pohyb ani nepotvrdzuje fyzické prevzatie. Historický doklad použitý na overenie musí zostať neprijatý, kým obsluha nepotvrdí skutočný príjem.

Syntetické testy overujú parser, viacstranové doklady vrátane poslednej strany iba so súhrnom, zľavy a bonusy, dve konvencie DPH, zachovanie zalomených názvov, odmietnutie poškodeného číslovania strán, ceny, súčty a opakovanie nahratia. Otlačok pôvodného formátu bez zliav sa nemení. Dňa 13. 10. 2026 bol lokálne overený pôvodný jednostranový aj nový štvorstranový dodávateľský doklad; ich obsah nie je súčasťou repozitára. `test_skp_stock_workflow_db.py` používa izolovanú PostgreSQL databázu pre spoločné SKU, SKP alias, opakovanie skenera/finalizácie a inventúrnu korekciu cez nemenné pohyby a FIFO. Nezapisuje do produkčného skladu.

## Migrácia a overenie

Pred spustením nového API treba zabaliť a vykonať `013_receiving_scan_requests.sql` cez `python -m inventory_hub.receiving_scan_migrate`.

Migrácia vytvorí tabuľku potvrdení skenov a rozšíri existujúci `receiving_lines.ean` z 20 na 255 znakov. PostgreSQL vyžaduje obnovu odvodeného stĺpca `line_fingerprint` aj známeho pohľadu `v_invoice_lines_detail` z migrácie 002. Oba sa obnovia v jednej transakcii; fingerprint zachová pôvodný výraz a pohľad skutočnú uloženú definíciu, vlastníka, oprávnenia tabuľky aj stĺpcov vrátane možnosti udeľovať práva, komentáre a voľby pohľadu. Nové predvolené granty nesmú rozšíriť pôvodné práva obnoveného pohľadu.

Nezmenia sa vstupné hodnoty riadkov, pohyby ani ceny. Žiadny `CASCADE` sa nepoužíva. Ďalší pohľad priamo nad EAN alebo nad `v_invoice_lines_detail` migráciu zastaví a celá transakcia sa vráti späť. Vlastné pravidlá, triggery, grantové reťazce od iných grantorov, predvolené hodnoty či bezpečnostné štítky na známom pohľade vyžadujú osobitnú kontrolu; migrácia ich nesmie zahodiť. Opakované spustenie je bezpečné. Pri oprave použiť postup vpred; tabuľku potvrdení nevymazávať, inak sa stratia dôkazy pre retry.

Regresie v `test_receiving_db.py` pokrývajú desať samostatných skenov, retry vrátane stavu po dokončení a resete, súbeh rovnakého UUID, konflikt payloadu, neočakávaný sken, zložené EAN, dve ceny rovnakého produktu a opätovnú kontrolu už priradených produktov. `test_product_identity.py` a `test_catalog_db.py` pokrývajú spoločný SKU, overené EAN, dodávateľský rozsah a konflikty importu. Databázové testy vyžadujú izolovanú lokálnu PostgreSQL databázu podľa `AGENTS.md`.

`frontend/tests/receiving-scan-ui.cjs` overuje reálnu stránku nad syntetickým API: frontu a opakovanie UUID, oba odchody z dokončeného/zrušeného/pozastaveného príjmu bez zápisu, zlyhanie pozastavenia aktívneho príjmu, súbežné dokončenie, stratenú odpoveď pozastavenia, neznámy stav a obnovu čakajúceho skenu po dokončení. Test nepoužíva produkčné doklady ani e-shopy a neoveruje rozloženie v reálnom prehliadači.

Rozšírenie z 13. 10. 2026 dopĺňa testy predvoleného zoznamu, zobrazeného EAN, šípok množstva a potvrdeného čiastočného/nulového dokončenia. `test_receiving_barcodes_db.py` overuje izoláciu dodávateľských kódov, konflikty, nezmenený zdroj faktúry a dokončenie iba skutočne prijatých množstiev vrátane opakovania. Rozšírenie nevyžaduje databázovú migráciu.


`test_receiving_migration_db.py` začína na skutočnej schéme 001 + 002 pred prvým spustením 013. Overuje zachovanie existujúceho riadka a fingerprintu, funkčný pohľad vrátane dlhého EAN, jeho vlastníka a oprávnení, bezpečné opakovanie migrácie a rollback pri oboch druhoch neznámej závislosti. Nevyhodnocuje prázdnu databázu ako dôkaz zachovania historických dát.

## Výber riadka a kontrola pred zaúčtovaním

Pri `scan_line_ambiguous` klient ponechá sken vo fronte a ponúkne konkrétne faktúrové riadky s cenou a množstvom. Iba dokázané odmietnutie pred zápisom dovoľuje doplniť `line_id`; po neistom výsledku sa opakuje rovnaké UUID a celý payload. Kým výber nie je vyriešený, nemožno príjem zaúčtovať. Pred úplným aj čiastočným zaúčtovaním je súhrn skladu, množstva, hodnoty a dôsledkov; neznáma nákupná cena zostáva neznáma. Archív dokladov a nahratie faktúry na príjem majú odlišné názvy a prepojenie. [Súvislosti](ux-audit-followup.md).

## Find an invoice from the landing-page scanner

`GET /receiving/lookup?code=...` is authenticated and read-only. It searches normalized receiving CSVs and database receipts across suppliers, including completed/cancelled sessions. It uses exact supplier/canonical codes and verified invoice, product or supplier barcodes; it does not finalize, reopen, create stock or guess from a title. CSV parsing reuses the receiving grammar and caches only unchanged files. Unreadable invoices are reported.

A unique complete result opens the corresponding session; multiple results or an incomplete search require an explicit invoice choice. A new/active receipt receives one UUID-preserving initial scan through the existing persistent queue. Reload/remount cannot generate a second operation for that scan. A completed/cancelled receipt opens read-only with matching rows highlighted, without adding quantity. Paused receipts resume through the existing endpoint. All scan identity and line-ambiguity checks still run before changing received quantity.
