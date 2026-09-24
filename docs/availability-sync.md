# Dodávateľské dostupnosti a pravidelný prenos zásob

Používateľský postup pre stránku **Nastavenia → Dostupnosti a prenos zásob** (`/settings/availability`). Nové automatické úlohy sú predvolene vypnuté. Uloženie nastavení dodávateľa ani načítanie tejto stránky nezapína odosielanie zásob do e-shopov.

## Načítanie dostupnosti dodávateľa

1. Odomknite stránku existujúcim operátorským tokenom a kliknite **Načítať / obnoviť nastavenia a stav**.
2. Vyberte nakonfigurovaný zdroj dostupnosti. Nastavte interval načítania v sekundách, platnosť údajov a minimálne pokrytie oproti predchádzajúcemu prijatému zdroju.
3. Uložte nastavenia. Pre jednorazový import stačí **Načítať dostupnosť teraz**. Pre ďalšie pravidelné importy zapnite **Pravidelne načítavať tento zdroj** a uložte zmenu.
4. Obnovte stav. Sledujte posledné úspešné načítanie, počet položiek a prípadnú chybu. Ručné spustenie iba zaradí prácu do fronty; neznamená dokončený import.

Interval je 300 až 604 800 sekúnd. Platnosť je 300 až 2 592 000 sekúnd a musí byť aspoň taká dlhá ako interval. Minimálne pokrytie 1 až 100 % chráni pred neúplným zdrojom; predvolené nastavenie je 100 %. Pri výrazne menšom zdroji najprv overte príčinu. Chybný zdroj neprepíše predchádzajúce údaje nulami. Po vypršaní ich platnosti sa použije neznáma dostupnosť.

Údaje dodávateľa nikdy nepridávajú fyzické kusy do nášho skladu. Text dostupnosti patrí do konfigurácie konkrétneho dodávateľa. Štandardný text pre dostupný tovar dodávateľa je **do 5 dní**, ak dodávateľ nemá vlastné nastavenie. Neznáma alebo zastaraná dostupnosť má text **overíme** a tovar zostáva objednateľný.

## Prenos do BIKETREK a xTrek

Vyberte cieľový e-shop a kliknite **Načítať / obnoviť stav prenosu**. Oba e-shopy majú samostatné povolenie a interval. Hodnoty sa predvolene preberajú zo spoločného skladu:

| Nastavenie | Predvolené | Význam |
|---|---:|---|
| Interval prenosu | 300 sekúnd | Ako často sa spúšťa automatický prenos |
| Počet položiek v jednej dávke | 20 | Veľkosť spracovávanej dávky |
| Najvyšší vek kontroly objednávok | 900 sekúnd | Ako čerstvá musí byť kontrola objednávok pred prenosom |

Spoločné hodnoty sú v rozbaľovacej časti **Spoločné nastavenia skladu**. Ich uloženie ovplyvní všetky e-shopy, ktoré ich preberajú. Potom obnovte stav prenosu. Ak chcete výnimku pre jeden e-shop, odškrtnite **Prevziať zo skladu** pri danom poli a zadajte hodnotu. Opätovné zaškrtnutie obnoví skutočné preberanie zo skladu.

Prenos odosiela dostupné množstvo a text dostupnosti. Vlastné voľné kusy majú **SKLADOM**; inak sa použije platný termín dodávateľa alebo objednateľné **overíme**. Názvy, ceny a obrázky majú samostatné odosielanie v produktovej tabuľke.

### Prvé povolenie

Pri **Povoliť odosielanie zásob do tohto e-shopu** treba potvrdiť skutočné prevádzkové nastavenie:

- Hub je jediný zdroj pravdy o zásobách pre oba e-shopy.
- Nezávislé odpočty a iné zápisy zásob sú vypnuté v oboch e-shopoch aj pokladniach.
- Objednávky a počiatočné stavy sú zosúladené so spoločným skladom.

Tieto potvrdenia nenastavujú Upgates za vás. Absolútne množstvo z Hubu môže prepísať súbežný odpočet v e-shope. Kontroly čerstvosti objednávok obmedzujú oneskorenie, ale pravidelné načítanie objednávok nie je okamžitá rezervácia pri dokončení nákupu.

Serverové povolenie zápisu je ďalšia podmienka. Ak je vypnuté, stránka to zobrazí a tlačidlo prenosu zostane nedostupné. Nasadenie kódu ani migrácia ho samy nezapnú. Pri zmene priradenia skladu alebo cieľového e-shopu môže systém vyžiadať nové potvrdenie.

Po uložení povolenia môžete použiť **Odoslať zásoby teraz**, aj keď pravidelné odosielanie zostáva vypnuté. Automatiku zapnete samostatným políčkom **Odosielať pravidelne v nastavenom intervale**. Na stránke sú odkazy na intervaly načítania a spracovania objednávok; tie zostávajú samostatné od intervalov prenosu zásob.

## Výsledok a neúspešná odpoveď

V posledných prenosoch otvorte **Detail** a obnovujte výsledok. Počet overených položiek znamená overený stav e-shopu. Chybné, preskočené a neisté položky majú vlastný stav a dôvod. Ďalšie položky môžu čakať na spracovanie.

Ak sa stratí odpoveď pri uložení nastavení alebo spustení práce, najprv obnovte nastavenia a stav. Stránka zápis automaticky neopakuje. Načítanie stavu po strate odpovede môže ukázať, že sa pôvodná operácia dokončila.

**Neistý zápis zásob** vyžaduje osobitný postup. Najprv treba overiť, že pôvodná vzdialená požiadavka už nemôže dobehnúť. Až potom zaškrtnite potvrdenie v detaile položky a použite **Načítať stav a uzavrieť neistotu**. Táto akcia načíta stav e-shopu a uzavrie výsledok podľa zhody; nezopakuje zápis. Samotná zhoda pri jednom načítaní bez ukončenia pôvodnej požiadavky nestačí.

Obnovenie nastavení nahrádza neuložené zmeny formulára. Zmenené nastavenia a čakajúce uloženie sú označené. Token sa neukladá do adresy ani úložiska prehliadača a pri zamknutí sa odstráni z pamäte.

## Overenie implementácie

Interakcie sú pokryté syntetickým testom `node frontend/tests/availability-sync-ui.cjs`: hranice intervalov, platnosť, revízie nastavení, skutočné preberanie hodnôt, potvrdenie autority, ručné spustenie pri vypnutej automatike, strata odpovede bez automatického opakovania, obnova neistého zápisu a ignorovanie odpovedí zo starého prístupu. Test neodosiela produkčné zásoby ani neoveruje skutočné nastavenie Upgates.
