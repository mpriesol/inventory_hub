# Jednotná príprava produktov

V **Dodávatelia → Produkty dodávateľa** vyberte produkty a kliknite na **Pripraviť import**. Cieľový e-shop sa predvolí podľa nastavenia dodávateľa; ručný výber v katalógu má prednosť. Rovnaká pracovná tabuľka slúži pre pôvodné údaje aj vylepšenie pomocou AI. Rozpracované dávky nájdete cez **Rozpracované importy**; priamy odkaz má tvar `/product-import?draft=<id>`.

## Pracovná tabuľka

- Jeden fyzický produkt alebo variant má vlastný riadok. Skutočné rodiny z feedu majú oddelenú hlavičku. Podobný názov nevytvára novú rodinu.
- Prehľad zobrazuje vybrané stĺpce. Pohľady **Ceny**, **Popisy a SEO**, **Kategórie**, **Parametre** a **Všetky polia** menia iba viditeľné stĺpce tej istej dávky. Viditeľnosť a šírky stĺpcov Prehľadu sa pamätajú v prehliadači; neukladajú sa tam produktové údaje ani prihlásenie.
- Kliknutie, F2 alebo Enter otvorí bunku; Tab potvrdí krátku bunku a presunie sa ďalej. Escape zahodí práve otvorenú krátku úpravu. Dlhý text, HTML, obrázky, parametre a kategórie majú väčší editor. HTML sa v tabuľke zobrazuje ako text, nespúšťa sa.
- Z Excelu možno vložiť obdĺžnik do aktívnej bunky. Rešpektuje aktuálne poradie stĺpcov a riadky viditeľnej stránky. Neplatný blok, presah stránky alebo protichodné spoločné hodnoty jednej rodiny odmietnu celý blok. Nevkladajú sa údaje do skrytých riadkov ani na ďalšiu stránku.
- Hromadné nastavenie zmení označené riadky dávky. Označenie pretrvá prepnutie stránky alebo filtra; počet v nástrojovej lište ukazuje celý označený výber. Spoločné rodinné polia (označené ◇) sa menia pre všetky varianty tejto rodiny zahrnuté v dávke, aj mimo aktuálnej stránky. Názov samostatného riadka, ceny, EAN a variantné atribúty zostávajú individuálne; rodičovský názov rodiny má vlastné pole.
- Existujúci produkt si ponecháva svoj kód, aj keď pribudne ďalší dodávateľ. Kód nového produktu vzniká z dodávateľského prefixu a kódu dodávateľa. Kód dodávateľa sa upravuje samostatným stĺpcom; server naďalej overí identitu a kolízie. Kód výrobcu je zdrojový údaj pre Hub a AI; natívne zapisovateľné pole Upgates to nie je.
- Kategórie ukazujú zdroj dodávateľa a cieľovú cestu. Volí sa aktívna koncová kategória, jej produktoví predkovia sa doplnia automaticky. Pôvod hodnoty je označený ako Feed, Mapovanie, Odvodené, Ručne alebo AI.

## Uloženie, AI a e-shop

1. **Uložiť koncept** uloží rozpracované hodnoty dávky. Pri konflikte revízie zostanú neuložené hodnoty otvorené; server ich automaticky neprepíše. Obnova uloženej verzie vyžaduje pri neuložených úpravách výslovné zahodenie. Odchod odkazom, navigáciou Späť aj obnovenie stránky chránia neuložené úpravy vrátane textu, ktorý je zatiaľ iba v otvorenom veľkom editore.
2. Pri každom riadku sú vedľa **AI** stĺpce **Import do BIKETREK** a **Import do xTrek**. Možno vybrať jeden e-shop, oba alebo žiadny (iba Hub). Nastavenie platí pre celú variantovú rodinu. Predvolený e-shop sa nastavuje v **Dodávatelia → Obecné → Predvolený e-shop pre import**. Pri druhom e-shope má rodina samostatný výber cieľovej kategórie; kódy kategórií sa medzi e-shopmi nekopírujú.
3. Jedno hlavné tlačidlo **Pripraviť import** spracuje aktuálnu fázu. Pri zapnutom AI pripraví jeho obsah podľa zdedených pravidiel. AI vždy používa feed a oficiálne zdroje. Bez AI zostávajú feedové a ručné údaje. Zaškrtnutie AI alebo e-shopu samotné nespustí platené spracovanie ani zápis.
4. Ak pravidlá vyžadujú kontrolu nákladov, hlavné tlačidlo sa zmení na **Spustiť AI · odhad … USD**. Schválenie obsahu zostáva dostupné cez odkaz konkrétnej AI úlohy. **Prijať výsledky AI a pokračovať** prevezme overené výstupy, zachová ručné hodnoty, uloží produkty do Hubu a pripraví náhľad vybraných e-shopov. Ak sú nákladová kontrola, obsahové schválenie alebo potvrdenie importu vypnuté, platí existujúca zdedená automatika; UI ich nezapína ani neobchádza.
5. **Uložiť produkty do Hubu** zostáva samostatnou možnosťou bez odoslania a fyzických skladových pohybov. Ak nie je vybraný žiadny e-shop, hlavná akcia sa volá **Pripraviť a uložiť do Hubu** a nevytvára náhľad ani zápis e-shopu.
6. Overený náhľad zobrazuje každý cieľový e-shop a jeho položky. Samostatné potvrdenie **Odoslať…** používa zmrazený náhľad. Neskoršia zmena dávky vyžaduje nový. Po začatom odoslaní sa dávka nemení; aj pri čiastočnom výsledku sa používa iba **Overiť výsledok / zopakovať neodoslané**, aby sa úspešný e-shop neodoslal druhýkrát.
7. Ak všetky položky už v e-shope existujú, **Dokončiť prípravu - produkty už existujú** uzavrie výsledok bez vytvárania produktov. Nepripravený e-shop nemožno novo zaškrtnúť; už vybraný možno odškrtnúť pre uloženie iba do Hubu. Nové produkty v dodatočne vybranom e-shope zostávajú skryté a označené na kontrolu.
8. Po overenom importe sú priamo pri riadku v príslušnom stĺpci e-shopu odkazy **Administrácia** a **Produkt v e-shope**. Odkazy pochádzajú z overeného prepojenia; chýbajúce adresy sa neodhadujú.

Obnovovanie priebehu AI sa počas otvorenej bunky alebo veľkého editora zastaví; pri neuložených úpravách čaká na ich uloženie. Náklady historických úloh zostávajú viditeľné aj po vypnutí AI.

Frontend používa existujúce spoločné prihlásenie a `/api/product-imports`. Backendové pravidlá, mapovanie feedu a publikovanie opisuje príslušný serverový kontrakt. Existujúci starší náhľad `CatalogImport` zostáva dostupný pre rozpracované staršie AI úlohy a importy; nové výbery z katalógu už smerujú do jednotnej tabuľky.

## Overenie implementácie

`node frontend/tests/product-import-ui.cjs` používa skutočné React komponenty so syntetickými odpoveďami. Overuje zachovanie úprav po konflikte, rodinné polia, TSV, hromadnú úpravu, odhady/AI/prijatie, dva cieľové e-shopy a ich kategórie, odkazy výsledku, uloženie iba do Hubu aj obnovu dávky priamym odkazom. `supplier-config-ui.cjs` overuje uloženie predvoleného e-shopu; `catalog-ui.cjs` overuje jeho predvoľbu aj následný ručný výber. `catalog-ui.cjs` overuje nový prechod z katalógu a zachováva kontrolu staršieho náhľadu. Ide o behaviorálne jsdom testy, nie dôkaz rozloženia v živom prehliadači ani produkčného importu.
