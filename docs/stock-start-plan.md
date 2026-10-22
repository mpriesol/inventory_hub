# Spustenie skladu BIKETREK a prechod xTrek

Odporúčanie z 22. 10. 2026 podľa nového zadania vlastníka. Nejde o vykonanú
inventúru, prečíslovanie ani pridelenie skladovej autority. Detailné kontrakty
zostávajú v [politike SKU](sku-policy.md), [príjme](receiving-identity.md),
[potvrdení skladu](stock-tracking.md) a [prenose zásob](stock-sync.md).

## Kódy a karty

- Zachovať existujúce jednoznačné SKU. Starší dodávateľský prefix nebráni FIFO
  ani nákupu od ďalšieho dodávateľa. Rozhodujú interné Hub ID, jednoznačné väzby
  a doklady. Dve karty s históriou sa automaticky nezlučujú.
- Nová fyzická predajná jednotka používa jeden kód
  `<dvojpísmenová skratka značky>-<doložený kód výrobku/variantu>`. Nie je potrebný
  ďalší skladový kód ani číselný rad. Pred založením sa overí existujúca karta.
- Nájdená karta si ponechá pôvodné SKU aj pri doložení MPN alebo inom dodávateľovi;
  rozporná identita zostáva blokovaná. Návrh nového formátu nesmie vytvoriť
  paralelnú kartu pre ten istý výrobok.
- Pri migrácii xTrek použiť SKU existujúcej overenej karty Hubu, ak už existuje.
  Len nový výrobok dostane nové SKU. Pôvodné Atomer ID/kódy uchovať s pôvodom;
  nemusia sa kvôli tomu prečíslovať v samotnom Atomeri. Rodič nie je fyzická karta.

## Zakladanie v Hube a preberanie z e-shopu

Odporúčaný bežný postup je **Hub → príjem kusov → samostatné publikovanie**.
Základná karta nepotrebuje hotový e-shopový popis. SKU pridelí spoločná politika
po doložení konkrétnej predajnej jednotky, nie obsluha v novom číselnom rade.

Existuje aj **Upgates → Hub**: náhľad a explicitný výber rodín cez
`/shops/{shop}/upgates/products/preview` a `/import`. Pull používa kanálové
mapovanie, presné SKU a overené čiarové kódy; zachová existujúcu spoločnú kartu,
prípadne prevezme pôvodný kód novej upstream položky. Nepotvrdzuje sklad ani
nákupnú cenu. Nevyrába novú značkovú identitu z názvu e-shopového poľa.

Po pulle sa existujúci `reconcile_product_links` pokúsi doplniť iba jednoznačné
väzby z nakonfigurovaných dodávateľských identít: existujúci supplier alias,
legacy pointer alebo pôvodný prefix. Nie je to univerzálne vyhľadanie výrobku
u všetkých dodávateľov podľa EAN. Neznáma dodávateľská väzba sa rieši samostatným
[náhľadom a potvrdením](supplier-card-links.md). Konflikt sa nesmie obísť.

Neprázdne `codes` pri obnove existujúcich položiek obmedzia zápis na vybrané
rodiny. Rodina s variantmi zahŕňa aj súrodencov; na úzky pilot jedného variantu
použiť presné čítacie overenie, nie tvrdenie, že pull aktualizuje len tento list.
Pri potrebe prepojiť jediný existujúci variant použiť [produktové väzby](product-links.md).

Nové karty preto prednostne zakladať v Hube. Pri ručnom založení priamo v Upgates
sa nepovažuje samotné neskoršie stiahnutie za dôkaz dodržania politiky SKU ani
za automatické pripojenie dodávateľa. Existujúce odlišné shopové kódy možno
evidovať v mapovaní, ale tým sa nedokazuje pripravenosť konkrétneho skladového
publikátora; tú treba overiť pre každé cieľové SKU.

## Poradie prevádzkového prechodu

1. **Pripraviť karty oboch obchodov v Hube.** Použiť dostupné podklady xTrek,
   oddeliť vyriešené a blokované identity. Neúplný export ani neprítomnosť
   v Heureke neznamenajú zrušený produkt. Pokladňovú skupinu xTrek a duplicity
   preveriť osobitne. Prípravné xTrek Upgates nie je živý predajný kanál Atomeru.
2. **Spočítať fyzický sklad ku konkrétnemu času.** Priradiť kartu, množstvo,
   doloženú nákupnú cenu alebo explicitnú neznámu cenu podľa podporovanej
   operácie. Prijaté, rezervované a fyzicky ešte neodovzdané kusy zachytiť
   v rovnakom časovom reze. Dodávateľské/e-shopové množstvá neslúžia ako počet.
3. **Otestovať a spustiť BIKETREK po malom pilote.** Zachytiť príjem, rezerváciu,
   výdaj, opakovanie, storno, fyzickú vratku a delta prenos bez druhého odpočtu.
   Pred ostrou autoritou musia byť pokryté všetky predaje spoločnej zásoby.
   Aktuálny kód nemá kompletný Atomer objednávkový konektor ani overený samostatný
   formulár externých Atomer predajov. Samotné zaškrtnutie kanála to nenahrádza.
   Prechod potrebuje doložený dočasný postup zápisu Atomer predajov s pôvodným
   ID a ochranou proti dvojitému výdaju, alebo prvý pilot obmedziť na zásobu,
   ktorú Atomer nepredáva. Nevystavovať druhý predajný doklad iba kvôli evidencii.
4. **Migrovať xTrek na Upgates s Hub SKU.** Overiť nové natívne ID/mapovania,
   obsah, varianty, reklamné identifikátory a začiatok zberu objednávok. Určiť
   poslednú objednávku z Atomeru a prvú z Upgates; rovnaký predaj nezaúčtovať
   dvakrát. Historické objednávky neimportovať ako nové fyzické výdaje.
5. **Kontrolná inventúra a pripojenie xTrek.** Už potvrdenú zásobu znovu
   neotvárať ako počiatočný príjem. Rozdiel opraviť auditovanou inventúrnou
   korekciou cez FIFO. Prvý počiatočný stav patrí len dosiaľ nepotvrdeným
   kartám/skladom. Zosúladiť rezervácie a nové kontrolné body pred prenosom.

Otvorené staré objednávky možno pri malom počte vyriešiť ručne. Každá musí mať
určené, či jej rezervácia/výdaj už patrí do spočítaného stavu. Súhlas začať novú
produktovú históriu GA4 neznamená meniť ID Google/Meta katalógov ani automaticky
meniť nastavenia merania.

## Hranice pilotu

Pre identitu sú vybrané existujúce karty Hub 3837 (`ZOO-36379`, samostatný
Upgates produkt 7752) a Hub 3493 (`ZOO-34336`, variant 5727 pod rodičom 7407).
Živé párovacie náhľady 22. 10. 2026 pri pôvodnom SKU aj doloženom MPN/EAN
vrátili pôvodné Hub ID a SKU. Je to dôkaz opätovného použitia identity, nie
vykonaného prečíslovania, fyzického príjmu alebo celého predajného cyklu.

Pred zápisovým testom prečíslovania platí [migračný postup](sku-migration.md):
overená obnoviteľná serverová záloha a implementované následné zosúladenie
Hubu. Všeobecné značkové zosúladenie zatiaľ nie je implementované; nepremenovať
najprv e-shop a až potom riešiť chýbajúcu lokálnu operáciu. Súhlas vlastníka
z 22. 10. pokrýva malý pilot; nejde o chýbajúcu opakovanú požiadavku na súhlas.
Skutočné počty a náklady sa pri skúške nevymýšľajú. Serverové skladové prepínače
sa nasadením opravy pullu nemenia. Výsledky izolovaných testov sa uvádzajú
oddelene od vykonaných živých operácií.
