# BIKETREK / xTrek — centrálny sklad

## Schválené obchodné pravidlá

Upresnenie vlastníka z 23. 9. 2026 je záväzné pre nasledujúce implementačné kroky:

- **Manuálne objednávkové položky bez skladovej identity** môžu byť servisná práca aj fyzický diel. Zostávajú mimo skladovej evidencie; neodpisujú zásobu, nevytvárajú produkt a neblokujú spracovanie mapovaných riadkov. Historické objednávky sa spätne neopravujú. Nový bežný predaj používa naskladnené produkty. Kódovaná položka s chybným alebo nejednoznačným párovaním je odlišná situácia a vyžaduje kontrolu; nesmie sa automaticky vyhlásiť za manuálnu výnimku.
- **Čiastočné odovzdanie objednávky sa nepoužíva.** Pred výdajom možno položku pridať, odstrániť, vymeniť alebo zmeniť množstvo; rezervácie sa upravia rozdielom. Stav **Odoslaná / Vyzdvihnutá** znamená jeden fyzický výdaj aktuálnych skladovaných riadkov a uzamknutie jeho obsahu. Opakovaný import nesmie vytvoriť ďalší výdaj. Platba webovej objednávky sama nestačí. Priamy dokončený pokladňový predaj sa eviduje raz podľa vlastnej identity objednávky.
- **Po výdaji** sa zmena rieši oficiálnou vratkou, reklamáciou alebo neprevzatím zásielky. Samotná zmena názvu stavu, zmazanie riadka ani storno nesmie spätne prepísať výdaj. Príjem vráteného tovaru nastane až pri potvrdenom fyzickom návrate, s odkazom na pôvodný výdaj.
- **„overíme“ je objednateľné.** Neznáma dostupnosť nesľubuje termín; sama nesmie skryť produkt ani zakázať košík. Ručné vypnutie produktu a povinná kontrola nového importu naďalej platia.
- **Dodacia lehota patrí dodávateľovi.** Každý dodávateľ má vlastnú konfiguráciu. Ak nemá vyplnený text pre dostupný tovar, použije sa `do 5 dní`; predvolený text pre neznámu dostupnosť je `overíme`. Dodávateľská zásoba nikdy nezvyšuje vlastný fyzický sklad.
- **Kód predajnej položky je spoločná identita BIKETREK a xTrek.** Vlastník potvrdil spoločné kódy variantov. Jednoznačný presne zhodný kód môže prepojiť položku aj bez EAN; rozdielne platné EAN alebo rozpor s existujúcim mapovaním vyžadujú kontrolu.
- **Pokladňový zberný produkt „xTrek“ v BIKETREK** môže obsahovať tisíce navzájom nesúvisiacich variantov, ktoré sú v xTrek e-shope pod rôznymi produktmi. Rodičovstvo je údaj konkrétneho e-shopu, nie podmienka spoločnej skladovej identity. Jeden tovar má jednu skladovú kartu a viac kanálových prepojení. Skladové spracovanie nesmie meniť jeho viditeľnosť v e-shope.

Štvrtý balík nižšie implementuje potvrdené spracovanie jednotlivých objednávok vrátane rezervácií a výdaja. Automatický zber objednávok, FIFO, vratky a odosielanie vlastných zásob do e-shopov ešte nebežia.

## Prvý implementačný balík

### Produktový import bez fyzickej zásoby

`POST /shops/{shop}/upgates/products/import` načíta produkty, varianty, mapovania a snímku údajov e-shopu. Nevytvára `stock_balances` ani `stock_movements` a nepotrebuje predvolený sklad. Snímka `shop_stock` nie je vlastná zásoba; predajná cena e-shopu nie je obstarávacia cena.

Parameter `include_stock` môže chýbať alebo byť JSON `false`. Iná hodnota vracia HTTP 400 ešte pred volaním e-shopu. Odpoveď zachováva kompatibilné `stock_initialized: 0`. UI odstránilo voľbu inicializácie zásoby. Existujúce historické pohyby, množstvá a náklady sa nemenia.

Počiatočný stav používa samostatný kontrolovaný import s fyzickým množstvom, identitou, pôvodom a obstarávacou cenou, opísaný v treťom balíku nižšie. Párovanie rozdielnych kódov pri bežnom pulle a oddelenie údajov e-shopu od spoločného produktu rozširuje druhý balík. Existujúce historické duplicity automaticky nezlučuje.

### Bezpečnejší príjem podľa faktúry

Mutácie príjmovej relácie sa serializujú databázovým zámkom. Finalizácia zamyká dotknuté bilancie v stabilnom poradí, vrátane súbehu dvoch prvých príjmov rovnakej položky. Zápis pohybov, bilancií, dokončenia relácie a výsledku pre opakovanie je jedna DB transakcia. Opakovaná finalizácia vráti uložený výsledok bez druhého príjmu.

Každý kladne prijatý riadok vyžaduje vyriešenú identitu a konečnú nezápornú nákupnú cenu. Chýbajúca alebo neplatná cena sa neodhadne a neprevezme z predaja či starého priemeru; celý príjem zostane nedokončený s chybou. Ani `force=true` túto kontrolu neobchádza. Rozpracovaná relácia obsahuje vlastnú kópiu riadkov; samotná výmena CSV ju neopraví. Kým nebude doplnený editor prijímanej ceny/identity, oprava takého riadka vyžaduje riadený administrátorský zásah do nedokončenej relácie, bez zásahu do histórie pohybov. Nulový prijatý riadok nevytvára pohyb.

Oceňovanie zostáva **váženým priemerom**. Príjem bez faktúry, neocenené FIFO vrstvy a dodatočné doplnenie nákladu patria nasledujúcej etape. Súborový index faktúry sa aktualizuje až po úspešnom DB commite; jeho prípadné zlyhanie nesmie spôsobiť opakované zaúčtovanie. Neúspech sa loguje. Opakované dokončenie bezpečne zopakuje opravu indexu; samostatná automatická obnova indexu zatiaľ neexistuje.

### Dostupnosť v konfigurácii dodávateľa

V **Dodávatelia → Obecné → Dodávateľská dostupnosť** sa nastavujú:

```json
{
  "adapter_settings": {
    "availability": {
      "orderable": "do 5 dní",
      "unknown": "overíme"
    }
  }
}
```

Chýbajúce a prázdne hodnoty dostanú predvolené texty; explicitný text, napríklad `do 7 dní`, zostane zachovaný. Maximálna dĺžka je 100 znakov. Toto nastavenie používa katalógový aj AI import a existujúca obmedzená aktualizácia dostupnosti. Staré AI nastavenia `orderable`, `unknown`, `hide_zero_stock` už dostupnosť neprepisujú. AI môže naďalej určiť `supplier_name`.

Nové produkty sa stále vytvárajú skryté s povinnou kontrolou `validation_required`. Nulová alebo neznáma zásoba dodávateľa sama nezakazuje košík. Aktualizácia dostupnosti existujúceho produktu stále nemení jeho aktivitu, košík ani skladové množstvo a zachováva doterajšie obmedzenia na samostatné produkty. Už zakázaný košík sa touto aktualizáciou automaticky nezapína.

Náhľad zachytí použitú politiku. Zmena konfigurácie vyžaduje nový náhľad pred novým externým zápisom; overovanie už neistého výsledku zostáva čítacie a používa pôvodný obsah. Podrobnosti sú v [katalógu](supplier-catalog.md) a [AI obsahu](ai-content.md).

## Druhý implementačný balík — identity a kontrola objednávok

### Párovanie pri sťahovaní produktov

Produktový pull najprv hľadá jednoznačné existujúce prepojenie pre konkrétny e-shop a jeho predajnú položku. Pri variante používa variantový kód; parent kód nesmie zastúpiť skladovanú veľkosť/farbu. Interné ID samostatného produktu a variantu majú oddelený význam. Existujúce dáta z pullu a katalógového importu majú odlišné historické vyplnenie `external_code`, preto resolver používa aj `is_variant`, `variant_code` a `parent_code`.

Po potvrdení spoločných kódov vlastníkom sa bez existujúceho prepojenia použije jednoznačné presne zhodné kanonické SKU (`matched_by=shared_sku`), aj keď EAN chýba. Platný EAN/UPC zostáva doplnkovou kontrolou a cestou na rozpoznanie existujúcich aliasov s rozdielnym kódom. Rozpor medzi kódom, mapovaním a čiarovým kódom je konflikt; prednosť existujúceho mapovania nesmie taký rozpor skryť. Kódy líšiace sa len veľkosťou písmen alebo viac kandidátov sa automaticky nezlúčia. Nevytvára sa náhradný vymyslený identifikátor.

Identifikátory sa načítajú aj pre kandidátov nájdených výlučne podľa SKU, aby sa odhalil rozdielny nový EAN, ktorý ešte nemá iného vlastníka. Úspešný produktový pull môže doplniť chýbajúci overený EAN/UPC k prepojenej položke; existujúce identifikátory neodstraňuje ani nenahrádza. Náhľad a audit objednávok identifikátory iba čítajú.

Prepojenie aj uloženie rodiny prebieha ako jedna operácia; konflikt rodiny nesmie nechať polovicu variantov uloženú. To platí aj pre celý pokladňový zberný produkt: chyba jednej jeho identity zatiaľ blokuje túto rodinu a zobrazí konflikt. Zápis prepojení zdieľa DB zámok s registráciou katalógového importu. Nové produkty a nadväzujúce záznamy sa ukladajú dávkovo. Bežný pull naďalej nemení skladové bilancie ani pohyby.

Prvý import nového produktu inicializuje jeho spoločné údaje. Ďalšie sťahovanie už existujúceho produktu obnovuje `ShopProduct` a `ShopProductContent` konkrétneho e-shopu a môže doplniť chýbajúce overené identifikátory. Názov, značka, skupina a variantné atribúty spoločného produktu sa neprepisujú obsahom druhého webu. Voľba `update_existing` v API zostáva kompatibilná, ale v UI je výslovne označená ako obnova údajov e-shopu.

Upgates pull už nevytvára ani neodvodzuje kanonické `ProductGroup`: nové skladové položky majú `group_id=None`, existujúce zaradenie sa nemení. Nadradený produkt každého kanála zostáva v `ShopProduct.parent_code`; rodina môže prepájať položky z rôznych kanonických skupín aj položky bez skupiny. Toto pravidlo funguje aj pri importe pokladňového produktu ako prvého. Skupiny vytvorené katalógovým importom alebo staršími importmi zostávajú zachované. Náhľad obrázka používa obsah rovnakého e-shopu ako príslušné prepojenie, takže rovnaký parent kód medzi e-shopmi nezamení zdrojový obsah.

Náhľad považuje rodinu za už prepojenú až vtedy, keď sú pre tento e-shop prepojené všetky jej predajné položky. Čiastočne prepojené varianty sa dajú doplniť. Výsledok uvádza nové produkty, nové prepojenia, uložené snímky a samostatný zoznam konfliktov; nevydáva konflikt za úspešný import.

Tento krok automaticky neprepisuje staré mapovania, nezlučuje už existujúce skladové produkty a nezavádza ručný editor konfliktov. Katalógový import si zachováva vlastný overený create-only pracovný tok; jeho staršie interné párovanie nie je touto zmenou celé nahradené.

Starší prenos produktov medzi e-shopmi odmietne rodinu, ktorej kódy sa nezhodujú s kanonickými SKU (`identity_alias_push_blocked`). Vyžaduje tiež výslovný výber všetkých položiek prenášanej rodiny (`selection_expands_family`); výber jedného variantu nesmie odoslať celý pokladňový zberný produkt. Kontrola existujúceho cieľa zohľadňuje parent kód aj prepojené produkty. Pri podporovanej rodine berie množstvo všetkých variantov z lokálnych bilancií a odstráni skladové údaje zdrojového e-shopu. Táto ochrana neaktivuje automatickú synchronizáciu zásob ani nemení viditeľnosť existujúcich produktov v e-shope.

### Chránená kontrola objednávok

Nová stránka **Kontrola objednávok** načíta zvolený e-shop a obdobie po výslovnom pokyne používateľa. API `GET /shops/{shop}/upgates/orders/audit?days=30&page=1` podporuje 7, 30 alebo 90 dní a jednu stránku najviac 100 objednávok na požiadavku. Automaticky neprechádza históriu a neobnovuje ju na pozadí. Pri každom načítaní použije najviac jednu objednávkovú stránku a číselník stavov.

Objednávkové riadky sa rozlišujú na prepojené, identifikované bez kanálového prepojenia, manuálne mimo skladu, nevyriešené a konfliktné. Manuálna výnimka sa týka položky bez skladovej identity; riadok s kódom a chýbajúcim párovaním sa nesmie potichu vynechať. Čiarový kód bez kódu môže vyžadovať kontrolu identity. Zľavové riadky sú osobitné neskladové položky. Sety, nejednoznačné jednotky a neplatné množstvá sa označia na kontrolu; zložený tovar sa automaticky neodpisuje dvakrát.

Upgates objednávkové `product_id` a `option_set_id` sú orientačné; audit ich nepoužíva ako náhradu overeného kódu či čiarového kódu. Produktový model zatiaľ nemá autoritatívnu mernú jednotku, preto skladované riadky s inou jednotkou než `ks` dostanú upozornenie. Resolver načítava mapovania daného e-shopu naraz a ďalšie údaje po dávkach; pri veľkých katalógoch zostáva priestor na zúženie tohto čítania.

Z aktuálneho stavu sa zobrazuje iba **kandidát** na rezerváciu, výdaj, storno alebo kontrolu. Odoslaná/Vyzdvihnutá a dokončený platený pokladňový predaj majú význam podľa schválených pravidiel. Samotná platba webovej objednávky nestačí. Neznámy stav a vratka/reklamácia vyžadujú kontrolu. Táto čítacia obrazovka nepotvrdzuje minulý výdaj, trvanie rezervácie ani skutočný návrat tovaru.

Audit nevytvára ani nemení `shop_orders`, rezervácie, produkty, skladové pohyby alebo účtovné údaje. Prenáša iba potrebné údaje objednávky a produktových riadkov; raw odpoveď sa neukladá do súborov ani neposiela do prehliadača. Zákaznícke adresy, kontakty, ceny a poznámky sa do výsledku nezaraďujú. Odpoveď má `Cache-Control: no-store`.

Prístup vyžaduje rovnaký existujúci operátorský token ako AI obsah (`AI_CONTENT_ACCESS_TOKEN`). Spoločná kontrola je v `access.py`; chýbajúci alebo krátky serverový token prístup uzavrie. Token zostáva iba v pamäti prehliadača a neukladá sa do URL ani localStorage. Ide o prechodné zdieľané oprávnenie, nie dokončené používateľské účty a roly. Ostatné existujúce nechránené cesty týmto balíkom nezískavajú všeobecné prihlásenie.

## Tretí implementačný balík — kontrolovaný počiatočný stav

Stránka **Sklad → Počiatočný stav** (`/stock/opening`) používa existujúci operátorský token. Všetky nové cesty pod `/stock/opening` vyžadujú túto kontrolu pred prístupom k DB a vracajú `Cache-Control: no-store`. Token zostáva iba v pamäti prehliadača. Vyplnené meno obsluhy je deklarovaný údaj o pôvode; zdieľaný token nepotvrdzuje totožnosť konkrétneho človeka.

### Vstup a náhľad

Obsluha vyberie existujúci aktívny sklad, vyplní označenie zdrojového súpisu, meno a čas fyzického spočítania s časovým pásmom. Budúci čas sa odmietne. CSV má presné hlavičky `sku;quantity;unit_cost;unit`, najviac 5 000 riadkov a 1 MB. Podporované oddeľovače sú bodkočiarka, čiarka a tabulátor; desatinnú čiarku treba oddeliť alebo uzavrieť podľa pravidiel CSV. Neznáme či duplicitné hlavičky sa nepovažujú za platný vstup.

Každý riadok potrebuje presné existujúce kanonické SKU, kladný celý počet a explicitnú obstarávaciu cenu **EUR bez DPH**. Jediná podporovaná jednotka je **`ks`**. Produktový model zatiaľ nemá autoritatívne merné jednotky; táto verzia preto nepokrýva metrový ani vážený tovar a nevykonáva prevody balení. EAN ani parent kód nenahrádzajú SKU a import nezakladá produkty.

Množstvo je najviac 999 999 999 kusov; jednotková cena najviac 99 999 999,9999 EUR so štyrmi desatinnými miestami. Hodnota riadka musí vojsť do presnosti skladového modelu. Chýbajúca, záporná, nekonečná či neplatná cena je chyba. Explicitná nulová cena zostáva možná s viditeľným upozornením. Nulové množstvo sa nepreskočí, ale označí ako chyba. Náklady sa nepreberajú z e-shopu, predajnej ceny ani posledného priemeru. Výpočty a uložené ceny používajú `Decimal`; odpoveď ich prenáša ako desatinné reťazce.

`POST /stock/opening/preview` overí celý vstup. Neplatný náhľad zobrazí chyby a nemá zaúčtovateľnú dávku. Platný náhľad uloží dávku, riadky, pôvod, čas platnosti a hash, ale nevytvorí bilanciu ani pohyb. UUID požiadavky umožňuje obnovu: rovnaké UUID s rovnakým obsahom vráti tú istú dávku, iný obsah pod rovnakým UUID je konflikt. Náhľad platí 30 minút. Zmena vstupu alebo prístupového tokenu v UI zneplatní náhľad a potvrdenia.

### Zaúčtovanie a obnova

Obsluha musí osobitne potvrdiť skontrolovaný obsah aj vysporiadanie rozpracovaných príjmov. Tovar započítaný v počiatočnom stave sa nesmie neskôr znovu zaúčtovať z nedokončeného príjmu. Databázové zámky riešia súbeh zápisov, ale nedokážu určiť, ktoré fyzické kusy obsluha spočítala. Pred otvorením preto treba určiť hranicu počítania a vyriešiť prekrývajúce sa príjmy.

`POST /stock/opening/{batch_id}/finalize` vyžaduje pôvodný hash a obe výslovné potvrdenia. Znovu overí sklad, identitu SKU, platnosť náhľadu a prázdny stav každého dotknutého produktu v tomto sklade. **Aj existujúca nulová bilancia alebo jediný historický pohyb blokujú otvorenie.** Ide o prvé zavedenie tovaru do skladu, nie korekciu starých nesprávnych údajov; história sa nemení.

Dávka sa zamkne a bilancie sa získajú v rovnakom stabilnom poradí ako pri príjme. Súbežný príjem dokončený ako prvý zablokuje otvorenie. Ak sa prvé dokončí otvorenie, následný príjem zásobu normálne zvýši a použije existujúci vážený priemer. Dve otváracie dávky pre tú istú položku v jednom sklade nemôžu obe uspieť.

Jediná transakcia vytvorí pre každý riadok nemenný pohyb `INITIAL`, množstvo, priemernú obstarávaciu cenu a hodnotu bilancie, označí dávku ako dokončenú a uloží výsledok. Chyba ktoréhokoľvek riadka vráti späť celú transakciu. Čas spočítania je údaj o pôvode; čas pohybu je skutočný čas zápisu. Počiatočný stav nevymýšľa dátum posledného nákupu ani nákupné FIFO vrstvy.

Opakované potvrdenie rovnakej dokončenej dávky vracia uložený výsledok bez ďalšieho pohybu, aj po neskoršom príjme. Pri strate odpovede UI ponechá identifikátor dávky a ponúkne **čítacie overenie výsledku** cez `GET /stock/opening/{batch_id}`. Zápis automaticky neopakuje. Nedokončenú platnú dávku možno potvrdiť výslovne znovu po overení. Prehľad nedávnych dávok cez `GET /stock/opening/batches` umožňuje obnovu aj po obnovení stránky.

Tento balík nezapisuje do e-shopov, fronty synchronizácie ani objednávok. Nezapína rezervácie, výdaje či FIFO. Samotné nasadenie nevytvorí počiatočnú zásobu.

## Štvrtý implementačný balík — rezervácie a uzamknutý výdaj

**Kontrola objednávok → Skladové spracovanie** (`/orders/stock`) je samostatný chránený postup pre jednu vybranú objednávku. Pôvodný audit na `/orders` zostáva čítací. Nové API pod `/order-stock` používa existujúci operátorský token a `Cache-Control: no-store`; token sa neukladá do URL ani úložiska prehliadača. Nie je to automatický importer ani všeobecný systém používateľských rolí.

### Začiatok evidencie a stavy

Obsluha výslovne načíta možnosti e-shopu, vyberie existujúci aktívny sklad a potvrdí význam konkrétnych stavových ID. Pri prvom potvrdení nastavenia server uloží **aktuálny čas začiatku evidencie**. Nie je možné ho spätne posunúť. Vybraný sklad zostáva pevný; ďalšia potvrdená zmena mapovania stavov mení revíziu politiky a zachováva začiatok evidencie. Konfigurácia sama nespracuje žiadnu objednávku.

Spracovať možno iba objednávky vytvorené od tohto začiatku. Staršie objednávky vrátane stále otvorených sa týmto balíkom automaticky nepreberajú. Existujúce lokálne `shop_orders` bez príznaku spravovania tiež zostávajú mimo nového postupu. Tak sa starý výdaj nezapočíta do novej fyzickej zásoby druhýkrát. Pred aktiváciou treba dokončiť počiatočný stav a určiť prevádzkovú hranicu; import zásob sa nesmie miešať s nezohľadneným predajom.

Predvolená voľba každého nového stavu je kontrola. Server povoľuje rezervovanie pri type `Received` a výslovne zvolených bežných stavoch typu `Custom`; výdaj pri `Sent` alebo dohodnutých názvoch Odoslaná/Vyzdvihnutá; uvoľnenie pri `Canceled`. Neznáme stavy, rozpoznané vratky/reklamácie a zlyhané platby zostávajú na kontrolu. Bežný stav prijatej objednávky nemožno nastaviť ako okamžitý výdaj. Identifikátor, názov, typ a správanie stavov sú súčasťou kontrolného hashu; zmena číselníka vyžaduje nové potvrdenie nastavenia.

Zaplatená webová objednávka zostáva kandidátom na rezerváciu. Pri objednávke s pôvodom `cash-register`, stavom typu `Received` povoleným na rezervovanie, platným dátumom platby a `resolved_yn=true` sa navrhne výdaj dokončeného pokladňového predaja. Pred každým prvým výdajom obsluha osobitne potvrdzuje aj fyzické odovzdanie.

### Čerstvý zdroj a náhľad

Prehliadač posiela iba e-shop a číslo objednávky, nikdy vlastné skladové riadky alebo množstvá. Server načíta jednu aktívnu objednávku cez presný filter `order_numbers` a číselník stavov. Prázdny výsledok, zmazaná/neznáma objednávka, viac výsledkov, neplatné UUID alebo chýbajúci jednoznačný čas znamenajú kontrolu; nezmenia sa na storno. Načítanie má časový a veľkostný limit a nič nezapisuje do Upgates. Neprechádza automaticky ďalšie stránky.

Ukladajú sa iba potrebné fakty objednávky a riadkov, nie zákaznícke adresy, kontakty, poznámky ani raw odpoveď. Zdroj vyžaduje stabilné UUID objednávky a riadkov, čas vytvorenia/aktualizácie s pásmom, presné stavové ID a podporované typy položiek. Rovnaké číslo alebo UUID nemožno v jednom e-shope použiť na druhú skladovú objednávku. Pokladňa a web nemajú oddelený identifikačný priestor v rámci toho istého e-shopu.

Identita sa rieši existujúcim spoločným resolverom. Skladovo použiteľné je jednoznačné kanálové prepojenie alebo presné spoločné SKU (`identified/shared_sku`). Samotný čiarový kód bez kódu, konflikt EAN, nejednoznačné mapovanie či nevyriešená kódovaná položka blokujú rezerváciu/výdaj. Manuálna výnimka nemá kód, čiarový kód ani natívnu produktovú/variantovú identitu; platí aj pre služby a ručne predaný diel. Zľavové riadky sa osobitne vylúčia. Takéto riadky sa zobrazia, ale nevytvoria skladový produkt, rezerváciu ani pohyb.

Prvá verzia pracuje len s kladnými celými `ks`. Sety, nepodporované jednotky, dĺžkové údaje a zlomkové existujúce zásoby/rezervácie vyžadujú kontrolu. Jednotky sa neodhadujú. Starý čítací audit môže zobraziť viac kandidátov, než je povolené fyzicky zaúčtovať.

`POST /order-stock/preview` uloží nemenný náhľad s revíziou politiky a lokálnej objednávky, zdrojovým hashom, skladovými účinkami a platnosťou 30 minút. UUID požiadavky s rovnakým vstupom vráti tú istú snímku; s iným vstupom je konflikt. Náhľad môže založiť lokálnu evidenciu objednávky, ale nemení fyzické ani rezervované množstvo. Aj blokovaný náhľad sa zachová na diagnostiku a zobrazí nedostatky či chybné riadky; nemožno ho zaúčtovať.

### Rezervácia, zmena a storno

`Reservation.quantity` je celý dopyt riadka, `shortage_qty` jeho nekrytá časť. Do `StockBalance.qty_reserved` prispieva iba rozdiel týchto hodnôt aktívnej rezervácie. Viac riadkov s rovnakým produktom sa pred overením dostupnosti spočíta; pridelenie medzi riadky je deterministické podľa UUID.

Pri potvrdení sa zamkne objednávka a dotknuté produkty/bilancie v stabilnom poradí. Prepočíta sa vlastná predchádzajúca alokácia a voľné fyzické kusy, pričom rezervácie iných objednávok zostanú zachované. Zmena množstva, pridanie, odstránenie alebo výmena produktu upraví iba rozdiel rezervácie. Odstránené evidované riadky zostanú označené, nemažú sa. Storno pred výdajom uvoľní známe uložené rezervácie a nevytvorí fyzický pohyb.

Pri nedostatku sa rezervujú dostupné celé kusy a zvyšok zostane `backorder`. Dodávateľská dostupnosť nepridáva zásobu a neumožňuje záporný výdaj. Dopyt na produkt bez bilancie ponechá bilanciu neprítomnú; tým nezablokuje jeho neskorší počiatočný stav. Príjem ani tento balík automaticky neprerozdelí voľné kusy medzi čakajúce objednávky: obsluha načíta a potvrdí nový náhľad príslušnej objednávky. Pridelenie určuje poradie potvrdených operácií, nie nový automatický prioritizačný algoritmus.

### Výdaj, ocenenie a opakovanie

`POST /order-stock/previews/{id}/apply` vyžaduje hash náhľadu a potvrdenie; pri výdaji navyše fyzické odovzdanie. Pred prvým zápisom znovu načíta aktuálnu objednávku. Zmena zdroja, politiky, lokálnej revízie, identity alebo skladového výpočtu vyžaduje nový náhľad. Čítanie e-shopu sa dokončí pred uzamknutím skladových riadkov. Nejde o distribuovanú transakciu s Upgates: prípadná ďalšia zmena na webe sa zistí pri nasledujúcej výslovnej kontrole.

Výdaj vyžaduje dostatok zásoby pre všetky aktuálne skladové riadky pri zachovaní cudzích rezervácií. **Čiastočný výdaj sa nevykoná.** Jediná DB transakcia uloží `SALE_OUT` pre každý skladovaný riadok, zníži fyzické množstvo, spotrebuje vlastnú rezerváciu, označí objednávku ako vydanú a uloží výsledok. Chyba vráti späť celú operáciu. Súbeh s príjmom, počiatočným stavom a ostatnými objednávkami používa spoločné poradie zámkov. Samotné rezervovanie/storno nevytvára `SALE_OUT` ani iný fyzický pohyb.

Ocenenie zostáva váženým priemerom. `unit_cost` výdaja zachytí uzamknutú priemernú obstarávaciu cenu. Nové nullable pole `stock_movements.total_cost` uloží presný odobratý náklad: pomernú časť aktuálnej celkovej hodnoty zaokrúhlenú na štyri desatinné miesta (`ROUND_HALF_UP`). Posledný riadok preberie zvyšok zaokrúhlenia a úplné vyprázdnenie ponechá nulovú hodnotu zásoby. Priemer sa výdajom nemení. Tento postup nevytvára FIFO vrstvy ani nedopočítava historické náklady. Predajné ceny sa nepoužívajú na ocenenie; keďže ich tento proces neimportuje, nové evidované riadky majú predajné ceny `NULL`, nie vymyslenú nulu. Neimportovaná mena objednávky zostáva tiež `NULL`; skladové náklady sú naďalej v EUR.

Opakované potvrdenie dokončeného náhľadu vráti pôvodný výsledok bez ďalšieho čítania e-shopu alebo zápisu. Nový náhľad už vydanej nezmenenej objednávky zobrazí dokončený výsledok; sklad sa druhýkrát nezníži. Zmenené položky/identita alebo neskoršie storno, vratka či reklamácia nemôžu prepísať vydaný obsah ani automaticky vrátiť zásobu. Vratkový proces s potvrdením fyzického návratu ešte nie je súčasťou tohto balíka.

Pri stratenej odpovedi UI uchová UUID a vyžaduje čítacie overenie `GET /order-stock/previews/{id}`. Zápis samo neopakuje. Prehľad posledných 20 náhľadov daného e-shopu umožňuje obnovu po načítaní stránky. Zmena vstupu, nastavenia alebo tokenu zneplatní zobrazený náhľad a potvrdenia. Stratená odpoveď nastavenia sa overuje novým načítaním možností.

## Nasadenie a ďalší postup

Prvý a druhý balík neobsahujú databázovú migráciu ani opravu historických dát. Nové config polia sú spätne kompatibilné a majú predvolené hodnoty. Nasadenie prebieha existujúcim workflow po merge PR. Pri návrate na verziu pred prvým balíkom zostávajú dáta zachované, ale vrátia sa pôvodné riziká príjmu a inicializácie skladu; dovtedy tieto operácie nepoužívať.

Úprava pre spoločné SKU a pokladňový produkt tiež nevyžaduje migráciu alebo nový konfiguračný parameter. Po nasadení treba v Hube znovu načítať náhľad a spustiť vybrané produktové prepojenia; nasadenie samo nespúšťa import ani externý zápis. Návrat na predchádzajúcu verziu obnoví obmedzenie skupín a staré pravidlo párovania bez SKU; existujúce dáta ostanú zachované, ale produktový pull a legacy push treba do nápravy pozastaviť.

Počiatočný stav pridáva migráciu `006_opening_stock.sql` s tabuľkami dávok a riadkov. Deployment ju explicitne spustí po `005` a pred reštartom API; oba súbory sú súčasťou API obrazu. Opakované vykonanie nemení zásoby ani pohyby. Pri chybe sa nasadenie preruší pred reštartom. Návrat kódu cez revert PR môže ponechať nové tabuľky aj už zaúčtované pohyby; nesmie ich mazať. Schéma je aditívna, ale revert sám neruší zaúčtovaný počiatočný stav. Prípadná dátová oprava potrebuje osobitný postup s kompenzačnými pohybmi.

Štvrtý balík pridáva migráciu `007_order_stock.sql`, explicitne spustenú po `006` pred reštartom API. Rozširuje existujúce objednávkové modely a pridáva politiky/náhľady. Dva cenové stĺpce objednávkového riadka a menu objednávky povoľuje ako `NULL`; existujúce hodnoty nemení. Žiadne staré objednávky, rezervácie ani výdaje sa migráciou nedopočítajú. Návrat kódu cez revert PR ponechá schému, už potvrdené rezervácie a nemenné pohyby; ich zrušenie vyžaduje osobitný riadený postup. Nasadenie samo nevytvorí politiku, neaktivuje e-shop a nezaúčtuje objednávky.

Ďalej treba dokončiť automatický zber/revízie objednávok a obnovu pri výpadkoch, všeobecné roly a merné jednotky, FIFO, oficiálne vratky, frontu synchronizácie a pracovný editor. Autoritu nad skladom Hub prevezme až po overení celého toku vrátane pokladne a výpadkov.
