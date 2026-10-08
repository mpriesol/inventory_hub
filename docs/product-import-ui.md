# Jednotná príprava produktov

V **Dodávatelia → Produkty dodávateľa** vyberte produkty, cieľový e-shop a kliknite na **Pripraviť import**. Rovnaká pracovná tabuľka slúži pre pôvodné údaje aj vylepšenie pomocou AI. Rozpracované dávky nájdete cez **Rozpracované importy**; priamy odkaz má tvar `/product-import?draft=<id>`.

## Pracovná tabuľka

- Jeden fyzický produkt alebo variant má vlastný riadok. Skutočné rodiny z feedu majú oddelenú hlavičku. Podobný názov nevytvára novú rodinu.
- Prehľad zobrazuje vybrané stĺpce. Pohľady **Ceny**, **Popisy a SEO**, **Kategórie**, **Parametre** a **Všetky polia** menia iba viditeľné stĺpce tej istej dávky. Viditeľnosť a šírky stĺpcov Prehľadu sa pamätajú v prehliadači; neukladajú sa tam produktové údaje ani prihlásenie.
- Kliknutie, F2 alebo Enter otvorí bunku; Tab potvrdí krátku bunku a presunie sa ďalej. Escape zahodí práve otvorenú krátku úpravu. Dlhý text, HTML, obrázky, parametre a kategórie majú väčší editor. HTML sa v tabuľke zobrazuje ako text, nespúšťa sa.
- Z Excelu možno vložiť obdĺžnik do aktívnej bunky. Rešpektuje aktuálne poradie stĺpcov a riadky viditeľnej stránky. Neplatný blok, presah stránky alebo protichodné spoločné hodnoty jednej rodiny odmietnu celý blok. Nevkladajú sa údaje do skrytých riadkov ani na ďalšiu stránku.
- Hromadné nastavenie zmení označené riadky dávky. Označenie pretrvá prepnutie stránky alebo filtra; počet v nástrojovej lište ukazuje celý označený výber. Spoločné rodinné polia (označené ◇) sa menia pre všetky varianty tejto rodiny zahrnuté v dávke, aj mimo aktuálnej stránky. Názov samostatného riadka, ceny, EAN a variantné atribúty zostávajú individuálne; rodičovský názov rodiny má vlastné pole.
- Kód produktu vzniká z dodávateľského prefixu a kódu dodávateľa. Kód dodávateľa sa upravuje samostatným stĺpcom; server naďalej overí identitu a kolízie. Kód výrobcu je zdrojový údaj pre Hub a AI; natívne zapisovateľné pole Upgates to nie je.
- Kategórie ukazujú zdroj dodávateľa a cieľovú cestu. Volí sa aktívna koncová kategória, jej produktoví predkovia sa doplnia automaticky. Pôvod hodnoty je označený ako Feed, Mapovanie, Odvodené, Ručne alebo AI.

## Uloženie, AI a e-shop

1. **Uložiť koncept** uloží rozpracované hodnoty dávky. Pri konflikte revízie zostanú neuložené hodnoty otvorené; server ich automaticky neprepíše. Obnova uloženej verzie vyžaduje pri neuložených úpravách výslovné zahodenie. Odchod odkazom, navigáciou Späť aj obnovenie stránky chránia neuložené úpravy vrátane textu, ktorý je zatiaľ iba v otvorenom veľkom editore.
2. **Pripraviť odhad** pripraví iba produkty so zapnutým AI. Rodina má spoločný prepínač. Následné tlačidlo uvedie súčet odhadov a výslovne spustí platené AI. Ručné hodnoty sa odovzdajú na server pred prípravou aj prijatím výsledkov.
3. **Prijať výsledky AI do tabuľky** prevezme overené výstupy do rovnakej dávky. Ručne upravené hodnoty majú prednosť. Chyby AI sú pri príslušnom riadku. Odhad spustenia a prijatie zahŕňajú iba rodiny s aktuálne zapnutým AI; historické náklady zostávajú viditeľné aj po vypnutí. Obnovovanie priebehu počas otvorenej bunky alebo veľkého editora stojí; pri neuložených úpravách čaká na ich uloženie, aby nezmenilo podklad rozpracovaných buniek.
4. **Uložiť produkty do Hubu** vytvorí alebo aktualizuje lokálnu evidenciu. Neodosiela do e-shopu a nevytvára fyzické skladové pohyby.
5. **Náhľad odoslania do e-shopu** sa odomkne po uložení produktov do Hubu. Zobrazuje výsledky kontrol, presný odosielaný payload a samostatné tlačidlo **Odoslať…**. Potvrdenie používa zmrazený overený náhľad; neskoršia zmena dávky vyžaduje nový. Nejasné alebo neúspešné výsledky používajú existujúcu kontrolu importu a bezpečnú obnovu neodoslaných položiek.

Frontend používa existujúce spoločné prihlásenie a `/api/product-imports`. Backendové pravidlá, mapovanie feedu a publikovanie opisuje príslušný serverový kontrakt. Existujúci starší náhľad `CatalogImport` zostáva dostupný pre rozpracované staršie AI úlohy a importy; nové výbery z katalógu už smerujú do jednotnej tabuľky.

## Overenie implementácie

`node frontend/tests/product-import-ui.cjs` používa skutočné React komponenty so syntetickými odpoveďami. Overuje zachovanie úprav po konflikte, rodinné polia, TSV, hromadnú úpravu, odhady/AI/prijatie, oddelenie uloženia do Hubu a odoslania, aj obnovu dávky priamym odkazom. `catalog-ui.cjs` overuje nový prechod z katalógu a zachováva kontrolu staršieho náhľadu. Ide o behaviorálne jsdom testy, nie dôkaz rozloženia v živom prehliadači ani produkčného importu.
