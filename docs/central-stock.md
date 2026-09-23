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

Objednávkové pravidlá sú tu špecifikácia ďalšej etapy. Doterajšie balíky ešte nezapínajú rezervácie, výdaje objednávok, FIFO ani automatické odosielanie vlastných zásob do e-shopov.

## Prvý implementačný balík

### Produktový import bez fyzickej zásoby

`POST /shops/{shop}/upgates/products/import` načíta produkty, varianty, mapovania a snímku údajov e-shopu. Nevytvára `stock_balances` ani `stock_movements` a nepotrebuje predvolený sklad. Snímka `shop_stock` nie je vlastná zásoba; predajná cena e-shopu nie je obstarávacia cena.

Parameter `include_stock` môže chýbať alebo byť JSON `false`. Iná hodnota vracia HTTP 400 ešte pred volaním e-shopu. Odpoveď zachováva kompatibilné `stock_initialized: 0`. UI odstránilo voľbu inicializácie zásoby. Existujúce historické pohyby, množstvá a náklady sa nemenia.

Počiatočný stav potrebuje samostatný kontrolovaný import s fyzickým množstvom, identitou, pôvodom a obstarávacou cenou. Táto cesta zatiaľ nie je implementovaná. Párovanie rozdielnych kódov pri bežnom pulle a oddelenie údajov e-shopu od spoločného produktu rozširuje druhý balík opísaný nižšie. Existujúce historické duplicity automaticky nezlučuje.

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

## Nasadenie a ďalší postup

Balík neobsahuje databázovú migráciu ani opravu historických dát. Nové config polia sú spätne kompatibilné a majú predvolené hodnoty. Nasadenie prebieha existujúcim workflow po merge PR. Pri návrate na staršiu verziu kódu zostávajú dáta zachované, ale vrátia sa pôvodné riziká príjmu a inicializácie skladu; dovtedy tieto operácie nepoužívať.

Úprava pre spoločné SKU a pokladňový produkt tiež nevyžaduje migráciu alebo nový konfiguračný parameter. Po nasadení treba v Hube znovu načítať náhľad a spustiť vybrané produktové prepojenia; nasadenie samo nespúšťa import ani externý zápis. Návrat na predchádzajúcu verziu obnoví obmedzenie skupín a staré pravidlo párovania bez SKU; existujúce dáta ostanú zachované, ale produktový pull a legacy push treba do nápravy pozastaviť.

Ďalej treba dokončiť jednotnú identitu a kontrolovaný otvárací stav, ochranu prístupu, objednávkový inbox a spracovanie uzamknutého výdaja, rezervácie, FIFO, vratky, frontu synchronizácie a pracovný editor. Autoritu nad skladom Hub prevezme až po overení celého toku vrátane pokladne a výpadkov.
