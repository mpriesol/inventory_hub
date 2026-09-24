# Príjem: identita produktu a opakovanie skenov

Táto časť príjmu pracuje iba s údajmi v Hube. Skenovanie, kontrola identity ani dokončenie príjmu nevolajú Upgates. Dokončenie príjmu zapíše pohyby a nákupné ceny do lokálneho skladu; samo nespúšťa publikovanie zásob do e-shopov.

## Pre obsluhu

- Každý nový sken pripočíta zadané množstvo. Desať samostatných skenov toho istého EAN pridá desať kusov. Rýchle opakované skenovanie nie je blokované časovým oknom.
- Ak sa odpoveď stratí, opakuj pôvodnú operáciu. Rovnaká operácia nepripočíta množstvo druhýkrát. Nový fyzický sken musí zostať samostatnou operáciou.
- Ak rovnaký kód zodpovedá viacerým faktúrovým riadkom, systém svojvoľne nevyberie prvý. Riadky môžu mať rozdielne nákupné ceny. Použi výslovný výber riadka, ak ho klient podporuje, alebo existujúcu ručnú úpravu jeho prijatého množstva.
- Sken môže použiť EAN z faktúry, kód dodávateľa, celý produktový kód alebo ďalší evidovaný overený EAN už priradeného produktu.
- Rozpor kódu, EAN a uloženého produktu treba opraviť pred príjmom. Ani „dokončiť napriek neprijatým riadkom“ neobíde konflikt identity.
- Pri dokončení sa kontroluje aj produkt, ktorý bol priradený už pri otvorení faktúry. Zmena jeho identifikátorov medzi otvorením a dokončením nemôže potichu naskladniť iný produkt.
- Jeden fyzický produkt môže mať viac EAN aj viac dodávateľských kódov. Rovnaký krátky neoverený číselný kód sám osebe nespája produkty od rôznych dodávateľov.

Kompletná administrácia opráv identifikátorov a priraďovanie neskôr prijatej faktúry k už vykonanému príjmu zostávajú ďalšími krokmi. Chýbajúca nákupná cena vo faktúrovom príjme sa naďalej musí vyriešiť pred jeho dokončením. Samostatný príjem bez známej ceny používa existujúci postup v [dokumentácii nákupných vrstiev](fifo.md).

## Spoločná identita

`services/product_identity.py` je spoločný resolver pre načítanie z e-shopov, objednávky, lokálny katalógový import a faktúrový príjem. Presný spoločný SKU má prednosť ako identifikátor. Overené EAN/UPC a kód dodávateľa v rámci konkrétneho dodávateľa sú ďalšie kontrolné dôkazy. Rôzni vlastníci týchto dôkazov znamenajú konflikt, nie automatické zlúčenie. Zmena iba veľkosti písmen SKU je tiež konflikt.

Pri lokálnom príjme/importoch resolver nevyužíva rodiča ani mapovanie konkrétneho e-shopu. Pokladňový produkt xTrek preto nemení skladovú identitu variantu. Existujúca podpora jednoznačného overeného EAN umožňuje nájsť fyzický produkt aj cez ďalší dodávateľský kód; jeho kanonický SKU sa pritom nemení.

Uložený `ReceivingLine.product_id` sa odovzdáva ako `expected_product_id` a porovnáva s aktuálnymi dôkazmi. Neexistujúci alebo neaktívny produkt sa neprijme. Aj jednoznačný dodávateľský kód musí súhlasiť so známymi overenými EAN produktu.

Zápisy identity pri dokončení príjmu používajú ten istý transakčný advisory lock `IDENTITY_WRITE_LOCK` ako registrácia katalógového importu a načítanie produktov. Až po vyriešení všetkých prijatých riadkov sa zamykajú zostatky a zapisujú skladové pohyby. Konflikt vracia celú nedokončenú transakciu späť. Pri súbežnom príjme rovnakého overeného EAN od dvoch dodávateľov vznikne jedna fyzická identita s dvomi dodávateľskými identifikátormi.

Zložené EAN z faktúry sa zachovajú v `receiving_lines.ean` a pri zápise sa rozdelia na jednotlivé identifikátory. Úvodné nuly sa nestrácajú. Udalosť ručnej zmeny množstva odkazuje na faktúrový riadok a do poľa kódu ukladá jeden overený EAN, prípadne reálny dodávateľský alebo kanonický SKU do 100 znakov. Dlhý pôvodný zdroj EAN zostáva celý na riadku; kód sa neskracuje na vymyslený identifikátor. Pôvodný spôsob automatického založenia neznámeho faktúrového produktu zostáva: dodávateľský prefix + kód, prípadne prefix + `EAN-` + prvý overený EAN, vždy s povinnou kontrolou nového produktu.

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

- Nové UUID: nová operácia a jeden `ScanEvent`.
- Rovnaké UUID a rovnaká relácia, orezaný kód, množstvo, pracovník a výber riadka: pôvodná uložená odpoveď s `replayed: true`, bez ďalšieho pripočítania a udalosti.
- Rovnaké UUID s iným obsahom alebo v inej relácii: HTTP 409, `detail.code = scan_request_conflict`.
- Viac zhodných riadkov bez výberu: HTTP 409, `scan_line_ambiguous`, zoznamy `line_ids` a `line_numbers`.
- Výber nesúvisiaceho riadka: HTTP 409, `scan_line_mismatch`.
- Rozpor identity: HTTP 409, `receiving_identity_conflict`, podľa dostupnosti aj riadok, kandidáti a dôvody.
- Pripočítanie nad rozsah databázového množstva: HTTP 409, `scan_quantity_out_of_range`.

Neúspešná transakcia neukladá úspešné potvrdenie UUID. Úspešný sken možno zopakovať aj po pozastavení či dokončení relácie. Jeho uložená odpoveď je historická: napríklad po ďalších skenoch alebo ručnom vynulovaní zobrazuje pôvodné množstvo. Klient po obnove načíta aktuálny súhrn, neprenesie starú odpoveď ako nový stav skladu.

Zámok relácie chráni súčet množstiev. Transakčný zámok UUID chráni aj súbežné použitie rovnakého ID v rôznych reláciách. `receiving_scan_requests` ukladá otlačok požiadavky, odpoveď a väzbu na `scan_events` v rovnakej transakcii ako množstvo. Klient pri neistej odpovedi zachová UUID aj payload a nesmie vygenerovať nové UUID iba kvôli retry. Načítanie stránky či reštart API nemaže uložené potvrdenia.

## Migrácia a overenie

Pred spustením nového API treba zabaliť a vykonať `013_receiving_scan_requests.sql` cez `python -m inventory_hub.receiving_scan_migrate`.

Migrácia vytvorí tabuľku potvrdení skenov a rozšíri existujúci `receiving_lines.ean` z 20 na 255 znakov. PostgreSQL vyžaduje obnovu odvodeného stĺpca `line_fingerprint` aj známeho pohľadu `v_invoice_lines_detail` z migrácie 002. Oba sa obnovia v jednej transakcii; fingerprint zachová pôvodný výraz a pohľad skutočnú uloženú definíciu, vlastníka, oprávnenia tabuľky aj stĺpcov vrátane možnosti udeľovať práva, komentáre a voľby pohľadu. Nové predvolené granty nesmú rozšíriť pôvodné práva obnoveného pohľadu.

Nezmenia sa vstupné hodnoty riadkov, pohyby ani ceny. Žiadny `CASCADE` sa nepoužíva. Ďalší pohľad priamo nad EAN alebo nad `v_invoice_lines_detail` migráciu zastaví a celá transakcia sa vráti späť. Vlastné pravidlá, triggery, grantové reťazce od iných grantorov, predvolené hodnoty či bezpečnostné štítky na známom pohľade vyžadujú osobitnú kontrolu; migrácia ich nesmie zahodiť. Opakované spustenie je bezpečné. Pri oprave použiť postup vpred; tabuľku potvrdení nevymazávať, inak sa stratia dôkazy pre retry.

Regresie v `test_receiving_db.py` pokrývajú desať samostatných skenov, retry vrátane stavu po dokončení a resete, súbeh rovnakého UUID, konflikt payloadu, neočakávaný sken, zložené EAN, dve ceny rovnakého produktu a opätovnú kontrolu už priradených produktov. `test_product_identity.py` a `test_catalog_db.py` pokrývajú spoločný SKU, overené EAN, dodávateľský rozsah a konflikty importu. Databázové testy vyžadujú izolovanú lokálnu PostgreSQL databázu podľa `AGENTS.md`.


`test_receiving_migration_db.py` začína na skutočnej schéme 001 + 002 pred prvým spustením 013. Overuje zachovanie existujúceho riadka a fingerprintu, funkčný pohľad vrátane dlhého EAN, jeho vlastníka a oprávnení, bezpečné opakovanie migrácie a rollback pri oboch druhoch neznámej závislosti. Nevyhodnocuje prázdnu databázu ako dôkaz zachovania historických dát.
