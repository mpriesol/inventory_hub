# xTrek Atomer → Upgates: analytická etapa

Stav 2. novembra 2026: čítacia diagnostika a offline dry-run. Produkčná
migrácia katalógu ani vlastného skladu nie je vykonaná. Podrobné zdrojové
snapshoty, mapy identít a reporty patria do súkromného odovzdania; do Git sa
neukladajú prevádzkové dáta, tokenizované feed adresy ani prístupy.

## Overené vstupy a rozsah

Získané boli aktuálne buyers XML, oba produktové Heureka feedy, availability
XML a čítací export Hubu. Oba produktové Heureka súbory mali pri tomto behu
identický SHA-256; nezlučujú sa ako dva katalógy. Chyba verejnej exportnej
domény Upgates sa obchádza použitím existujúceho API napojenia podľa pokynu
vlastníka. Diagnostika DNS nepreukazuje expiráciu domény.

Základný API zoznam neobsahuje všetky rozšírené polia. Samostatné sekcie
`products/parameters`, `products/labels`, `products/files`, `products/related`,
`products/variants`, kategórie, parametrový číselník, presmerovania a konfigurácia
DPH boli získané cez [čítací export](migration-export.md). Jednotlivé sekcie
majú vlastné časy; nie sú spoločným freeze. Overenie číselníka dostupností
musí zohľadniť prekrývajúce sa výsledky filtrov, zachovať jednotlivé pozorovania
a zrátať jedinečné ID iba raz. Zmenený obsah toho istého ID zastaví export.

## Párovanie a dry-run

1. Uchovať pôvodné XML, API stránky, čas a SHA-256. Neprítomné pole odlíšiť
   od prázdneho poľa a potvrdenej nuly.
2. Použiť existujúci `IdentityIndex`, natívne e-shopové väzby, zachované SKU
   a overené GTIN. Rozporný EAN alebo značka zastaví automatické priradenie.
3. Medzi buyers a Heureka využiť exact kód, jedinečný validný GTIN alebo
   jednoznačnú skupinu s úplným pomenovaným vektorom parametrov. Interné
   Heureka ID ani `PRODUCTNO` nie sú automaticky výrobné kódy.
4. Varianty s rozdielnymi kódmi možno priradiť len v overenej rodine podľa
   úplnej jedinečnej kombinácie parametrov a nerozpornej identity. Samotná
   podobnosť názvu nestačí. Duplicita sa nezahodí načítaním do slovníka.
5. Každé pole má pred/po návrh, dôvod, scope, blokátory a odtlačok predpokladu.
   Rôzne hodnoty detí pre jedno pole parenta vyžadujú riešenie pred zápisom.
   Bohatší cieľový obsah a cieľové položky bez zdrojovej zhody sa zachovajú.
6. Nové pridelenie SKU ostáva viazané na overený výrobný kód a dvojpísmenový
   register značiek. Prevzatie existujúceho e-shopového produktu používa
   jeho overený pôvodný kód; nezavádza paralelnú politiku SKU.

Absencia bezpečnej zhody znamená kandidáta na doplnenie, nie dôkaz, že produkt
v cieli neexistuje. Spárovaný produkt s obsahovým rozdielom nie je automaticky
pripravený na zápis. Test opakovateľnosti offline analýzy nie je testom
produkčného importu, ktorý v tejto etape nebol vytvorený ani spustený.

## Sklad a otvorené podmienky

Availability je zdroj kladných množstiev pripravených na expedíciu. Chýbajúci
riadok nie je nula. Buyers `COUNT_ON_STOCK` je iba orientačné pole a nesmie
vytvárať fyzickú zásobu. Pred zápisom treba doložiť fyzický stav oproti
množstvu po rezerváciách, úplné nulové/nepriradené/archivované karty, skladové
priradenie a čas. Verejná dokumentácia Atomeru určuje skladový systém, ale
nepreukazuje konkrétny exportný ovládací prvok dostupný tomuto účtu.

Katalógový import nesmie vytvárať príjem, FIFO vrstvy ani skladové potvrdenie.
Iniciálny stav patrí existujúcemu `opening_stock`/FIFO postupu; chýbajúci náklad
je neznámy, nie nula. Spoločná karta sa nesčíta cez BIKETREK, xTrek a Hub.
Pred prechodom treba T0, zohľadnenie objednávok a pohybov, koordináciu konkrétnych
writerov a overenie autority. Samotné nasadenie čítačky nič z toho nezapína.

Ďalšie otvorené údaje: kompletný Atomer katalóg vrátane skrytých položiek,
MPN dôkazy, neznáme značky/prefixy, typy/jednotky/poradie parametrov, význam
`PRICE_OLD`, jednotka hmotnosti, ďalšie zaradenia do kategórií, ALT/titulky,
prílohy a funkčné zobrazenie formátovaných krátkych popisov. Pôvodné keywords
v archíve nie sú dokončenou funkčnou migráciou.

## Následný postup a návrat

Najprv vyriešiť konkrétne identity a povinné medzery. Potom rozšíriť existujúce
adaptéry/importné koncepty o Atomer provenienciu; nepísať paralelný importer.
Pilot má obsahovať samostatný produkt, variantovú rodinu, zmenený kód,
konflikt identity, HTML a neznáme množstvo. Zápisy po malých dávkach používajú
trvalé operation UUID a uložený výsledok; nejasný PUT sa automaticky neopakuje.

Každý úspech vyžaduje spätné čítanie natívneho produktu/variantu. Média treba
kopírovať s hashmi, zachovať poradie a otestovať bez Atomer hosta. Pôvodné URL
potrebujú jednoznačnú mapu, bezkolízne 301, canonical a kontrolu slučiek.
Druhý identický beh má byť no-op. Návrat obsahu sa týka len dotknutých polí
s nezmeneným odtlačkom; novšie zásahy sa neprepisujú. Skladový rollback používa
výslovne posúdené kompenzačné pohyby, nikdy obnovu starej celej bilancie.

## Validácia

Čítací API export má testy autorizácie, allowlistov, úplnosti strán, duplicitných
ID, presných desatinných hodnôt, nulových/prázdnych údajov, odmietnutia presmerovaní
a bezpečných chýb. PR #121 prešiel plným CI vrátane izolovaných DB testov a bol
nasadený. Offline analýza má samostatné kontroly GTIN s úvodnou nulou, interných
Heureka ID, konfliktného kódu/EAN, HTML, neznámej zásoby a opakovateľného merge.
Živé katalogizačné/skladové zápisy, prenos všetkých médií a produkčný no-op
import zostávajú nevykonané a nemožno ich označiť za úspešné testy migrácie.
