# Identita produktu a dodávateľské dostupnosti

## Pre obsluhu

Každý fyzický produkt alebo variant má vlastné nemenné interné ID. Spoločné SKU sa používa v Hube, BIKETREK aj xTrek. Pri novom produkte vzniká spojením prefixu dodávateľa a jeho kódu konkrétneho variantu: napríklad `PL-` + `001234` = `PL-001234`. Úvodné nuly sa zachovávajú. Pokladňový produkt xTrek v BIKETREK nemení identitu svojich variantov.

Dodávateľský kód sa uchováva samostatne. Ak sa ten istý preukázateľne zhodný produkt neskôr nakúpi od iného dodávateľa, zostáva jeden produkt so svojím pôvodným SKU a ďalšou dodávateľskou väzbou. Príjmy si zachovávajú vlastné nákupné ceny. Samotný prefix neznamená výhradného dodávateľa navždy.

### Nastavenie a uzamknutie prefixu

- Prefixy musia byť medzi dodávateľmi jedinečné aj bez ohľadu na veľkosť písmen. Jeden nesmie byť začiatkom druhého, napríklad `PL-` a `PL-A-`, pretože by mohli vytvoriť rovnaké výsledné SKU.
- Nový dodávateľ môže upraviť prefix pred jeho prvým použitím na trvalú identitu. Pri prvom použití sa prefix uzamkne.
- Existujúce konfigurácie s neprázdnym prefixom sa pri zavedení ochrany uzamknú. Nevieme spoľahlivo dokázať, že ich kódy ešte neboli použité, preto sa neprečíslujú.
- Uzamknutý prefix nemení bežné uloženie ani obnova starej konfigurácie. Ostatné nastavenia, napríklad feed či dodacia lehota, zostávajú upraviteľné.
- Výslovná rotácia prefixu je samostatná operátorská akcia pre budúce identity. Existujúce kanonické SKU a produkty nemení; pôvodný prefix zostáva trvalo rezervovaný tomuto dodávateľovi.
- Dva rozdielne prefixy uložené v starých a nových poliach konfigurácie sú konflikt. Systém nevyberie jednu hodnotu potichu.

Existujúce SKU, e-shopové prepojenia, zásoby, pohyby a nákupné ceny sa zavedením tejto ochrany nemenia.

### Čerstvý feed a neprepojený produkt

Úspešne stiahnutý feed potvrdzuje dodávateľské údaje. Aby ich skladová tabuľka vedela použiť, potrebuje aj jednoznačnú väzbu konkrétneho produktu na ponuku dodávateľa.

Pri bežnom načítaní údajov sa väzby dopĺňajú podľa presného SKU vytvoreného z prefixu a pôvodného kódu, prípadne podľa už overenej dodávateľskej identity. Podobnosť názvu ani samostatná náhodná zhoda EAN nezakladajú nové automatické prepojenie dostupnosti.

Na už stiahnuté údaje použite na stránke **Dostupnosti a prenos zásob** tlačidlo **Prepojiť dostupnosti** pri dodávateľovi:

1. Odomknite pracovisko operátorským tokenom.
2. Kliknite na **Načítať / obnoviť nastavenia a stav** a pri Paul Lange alebo inom konkrétnom dodávateľovi spustite prepojenie.
3. Počkajte na spracovanie jednotlivých dávok a skontrolujte výsledok vrátane konfliktov a preskočených väzieb.
4. Obnovte skladovú tabuľku a overte príslušné produkty.

Táto akcia pracuje s lokálnymi údajmi. Nesťahuje feed, nevolá Upgates, nezakladá fyzickú zásobu a nemení nákupné ani predajné ceny. Opakovanie neduplikuje existujúce väzby. Pri prerušení možno spustiť prechod znovu; už spracované väzby sa zachovajú.

Ak sa prenos odpovede preruší, najprv použite **Načítať / obnoviť nastavenia a stav**, potom prepojenie spustite znovu. Rozhranie nepokračuje naslepo po chybe alebo strate operátorského prístupu.

Výslovne vypnuté alebo neobjednateľné väzby sa automaticky nezapnú. Chýbajúce kódy, nejednoznační vlastníci a rozporné identifikátory vyžadujú vyriešenie. Staré SKU bez prefixu sa neprečíslujú; môžu sa pripojiť cez už existujúcu jednoznačnú dodávateľskú identitu.

### Význam výsledku v tabuľke

Rozhranie rozlišuje chýbajúce prepojenie, konflikt identity, chýbajúce pozorovanie a staré pozorovanie. Čerstvá dostupná ponuka poskytne dodávateľskú lehotu. Zastarané údaje sa opravou väzby neobnovia; potrebujú nový úspešný feed.

Naďalej platí: vlastná voľná zásoba má prednosť ako **SKLADOM**. Pri nulovom vlastnom voľnom stave a čerstvej dostupnej ponuke sa používa dodávateľská lehota, predvolene **do 5 dní**. Inak zostáva objednateľné **overíme**. Prenos týchto hodnôt do e-shopov je samostatný povolený skladový proces.

## Pre vývojárov

### Prefix

`supplier_prefix.py` je spoločný parser a normalizátor. Autoritatívna konfiguračná cesta je `adapter_settings.mapping.postprocess.product_code_prefix`. Staré cesty `adapter_settings.product_code_prefix` a `product_code_prefix` sa migrujú iba bez rozporu. Kód je text; neprevádza sa na číslo. Zložené SKU musí dodržať limit produktového identifikátora.

`config_io` chráni uloženie, obnovu a prvé použitie pomocou registra `suppliers/.product-prefixes.json` na rovnakom trvalom dátovom zväzku ako konfigurácie. Zápisy serializuje procesový zámok `suppliers/.product-prefixes.lock`; register sa zapisuje atómovo. Tento mechanizmus predpokladá spoločný filesystem podporujúci `flock` pre procesy používajúce spoločné konfigurácie.

Register nie je súčasťou editovateľného JSON ani jeho histórie. Klientské polia `product_prefix`, `product_prefix_locked` a `product_prefix_lock_reason` sú iba odpoveď API a nedokážu odomknúť register. Zmena stavu uzamknutia nemení hash konfiguračného obsahu pripravovaného feedu/importu.

Odstránenie konfigurácie dodávateľa neuvoľní jeho rezervovaný prefix. Register sa musí zachovať spolu s trvalými dodávateľskými dátami; jeho vymazanie nie je spôsob prečíslovania produktov.

`claim_supplier_prefix(supplier, expected_prefix)` sa volá pred trvalým použitím prefixu. Overí aktuálny config, jedinečnosť a zhodu s pripraveným prefixom. Zlyhanie nesmie pokračovať so záložným alebo prázdnym prefixom. Chybný register sa nesmie automaticky nahradiť prázdnym; zápis identity musí zostať blokovaný.

### Výslovná rotácia prefixu pre nové produkty

Chránený `POST /api/suppliers/{supplier}/prefix-rotation` prijíma
`{"expected_prefix":"SL-","new_prefix":"SLG-"}`. Vyžaduje bežný operátorský
prístup a zhodu očakávaného prefixu s konfiguráciou aj registrom. Názvy zostávajú
neprázdnym textom s najviac 20 písmenami, číslicami, spojovníkmi alebo podčiarkovníkmi.
Konflikt súčasného aj historicky rezervovaného priestoru iného dodávateľa sa
odmietne vrátane prekrytia prefixov a rozdielu iba vo veľkosti písmen.
Nový priestor sa nesmie prekrývať ani so súčasným či vyradeným prefixom toho
istého dodávateľa, aby odlišné dodávateľské kódy nevytvorili totožné SKU.

Rotácia mení iba konfiguračný prefix a jeho register. Nemení databázu, existujúce
produkty, dodávateľské aliasy, fyzický sklad ani produkty BIKETREK či xTrek.
Po obnove katalógu nové prípravy odvodia návrhy nových kódov z nového prefixu;
už preukázaná identita si zachová pôvodné kanonické SKU. Existujúci importovaný
produkt sa tým neprečísluje. Staré pripravené operácie naďalej podliehajú
kontrole aktuálneho prefixu.

Pôvodná hodnota zostáva v `retired_prefixes` registra aj po ďalšom bežnom
uložení, použití prefixu alebo odstránení konfigurácie. Iný dodávateľ si ju
nemôže privlastniť a rotácia ju nedokáže opätovne aktivovať. Opakovaný rovnaký
požiadavok je úspešný bez ďalšieho zápisu len vtedy, keď je aktuálny nový prefix
a očakávaný starý prefix už patrí medzi vyradené hodnoty; odpoveď má
`replayed: true`. Iný zastaraný predpoklad skončí konfliktom.

Oba atómové zápisy serializuje existujúci filesystemový zámok. Najprv sa
uloží uzamknutý nový prefix spolu s rezerváciou starého, potom konfigurácia.
Výpadok medzi zápismi ponechá obidva priestory rezervované a nezhoda registra
s konfiguráciou blokuje načítanie, uloženie, použitie aj ďalšiu rotáciu.
API tento stav neopravuje automaticky; pred ďalšou prácou treba prevádzkovo
overiť oba súbory a dokončiť pôvodnú zmenu. Vymazanie registra nie je oprava.

### Dodávateľské väzby

`services/supplier_links.py` používa existujúci `IDENTITY_WRITE_LOCK` a spoločný resolver. Priraďuje existujúci produkt k `SupplierProduct` cez `ProductSupplySource` a dodávateľsky obmedzený identifikátor. Ak existuje iba prijaté skladové pozorovanie, môže založiť minimálny záznam dodávateľskej položky; tým sa nevytvára fyzický produkt, množstvo ani cenový údaj. Existujúce katalógové údaje a pravidlá väzby sa zachovajú.

Čítanie dostupnosti zostáva bez zápisu produktových väzieb. Chýbajúce prepojenie sa opravuje explicitným lokálnym prechodom alebo pri príslušnom importe. Autoritatívnymi údajmi zostávajú interné produktové ID a uložené dodávateľské väzby, nie opakované premenovávanie SKU.

Chránený endpoint s `Cache-Control: no-store`:

```http
POST /api/supplier-availability/paul-lange/links/reconcile
Content-Type: application/json

{"after_product_id": 0, "limit": 500}
```

Limit je 1–500. Odpoveď obsahuje `supplier`, `scanned`, `linked`, `existing`, `skipped`, pole `conflicts`, `skipped_details` a `next_after_product_id`. Ak je kurzor neprázdny, nasledujúca požiadavka pokračuje za ním. Počty väzieb sa nemusia rovnať počtu prečítaných produktov: produkt môže mať viac dodávateľských identifikátorov alebo žiadny kandidátsky kód pre zvoleného dodávateľa.

Prefix, normalizované dodávateľské dáta a zápis väzby sa znova kontrolujú pri použití. Zápis nezasahuje do `stock_balances`, `stock_movements`, FIFO vrstiev ani vzdialených produktov. Pri zlyhaní databázovej transakcie sa väzby danej dávky vrátia späť; uzamknutý prefix zostáva konzervatívne uzamknutý.

### Nasadenie a overenie

Táto etapa nepotrebuje novú SQL migráciu. Potrebuje existujúce schémy identity, katalógu a dodávateľských pozorovaní. Nasadenie nezapína plánovanie ani zápisy zásob/cien do e-shopov.

Regresie musia pokryť existujúci skladový produkt načítaný z e-shopu, samotný minimálny skladový feed, nový príjem, viac dodávateľov, úvodné nuly, opakovanie dávky, konfliktné EAN/kódy/vlastníkov, zakázané väzby a zachovanie histórie. Zámok konfigurácie sa overuje aj pri obnove, starých aliasoch a súbežnom použití. Plné databázové scenáre sa spúšťajú iba v izolovanom localhost `*_catalog_test`; výsledok CI a nasadenia sa zaznamenáva oddelene.
