# Dodávateľské dostupnosti a pravidelný prenos zásob

Používateľský postup pre stránku **Nastavenia → Dostupnosti a prenos zásob** (`/settings/availability`). Nové automatické úlohy sú predvolene vypnuté. Uloženie nastavení dodávateľa ani načítanie tejto stránky nezapína odosielanie zásob do e-shopov.

## Načítanie dostupnosti dodávateľa

1. Odomknite stránku existujúcim operátorským tokenom a kliknite **Načítať / obnoviť nastavenia a stav**.
2. Vyberte nakonfigurovaný zdroj dostupnosti. Automatické načítanie nastavte raz denne alebo menej často; v rozšírených nastaveniach upravte platnosť údajov a prah upozornenia na menší feed.
3. Uložte nastavenia. Pre jednorazový import stačí **Načítať dostupnosť teraz**. Pre ďalšie pravidelné importy zapnite **Pravidelne načítavať tento zdroj** a uložte zmenu.
4. Počas čakania a načítania sa stav obnovuje automaticky. Súhrn ukáže počet položiek vo feede, prijatých, chybných a bez údaja o množstve. **Zobraziť preskočené položky** otvorí kódy, hodnoty a konkrétne dôvody. Ručné spustenie iba zaradí prácu do fronty; neznamená dokončený import.

Predvolený automatický interval je 24 hodín a platnosť údajov 48 hodín. Automatický interval môže byť 24 hodín až 7 dní; existujúce týždenné zdroje zostávajú týždenné. Staršie zapnuté kratšie intervaly sa upravia na denné. Vypnuté zdroje ani ručné B2B čítanie Sloger sa nezapnú. Ručný refresh možno vyžiadať aj skôr. Platnosť je 300 až 2 592 000 sekúnd a musí pokrývať interval; formulár pri automatike ponechá aspoň hodinu rezervy. Prah 1 až 100 % upozorní na menší zdroj; predvolené nastavenie je 100 %. Platné položky sa prijmú aj pri upozornení. Pri výrazne menšom zdroji najprv overte príčinu. Chybné alebo chýbajúce položky neprepíšu predchádzajúce údaje nulami a nepredĺžia ich platnosť. Po vypršaní ich platnosti sa použije neznáma dostupnosť.

Údaje dodávateľa nikdy nepridávajú fyzické kusy do nášho skladu. Text dostupnosti patrí do konfigurácie konkrétneho dodávateľa. Štandardný text pre dostupný tovar dodávateľa je **do 5 dní**, ak dodávateľ nemá vlastné nastavenie. Neznáma alebo zastaraná dostupnosť má text **overíme** a tovar zostáva objednateľný.

Pri zapnutom prenose dodávateľských dostupností si Hub pamätá posledný potvrdený
stav osobitne pre každú kartu a cieľový e-shop. Nový feed s rovnakou dostupnosťou
nespôsobí nové API volanie pre daný produkt. Odosielajú sa len zmenené texty;
nový počet kusov u dodávateľa bez zmeny dostupnosti nestačí na odoslanie. Aj
vypršanie platnosti sa spracuje raz ako zmena na **overíme**. Existujúce
**SKLADOM** sa v tomto režime zachová do prevzatia zásob po inventúre. Vlastné
množstvá sa týmto prenosom nemenia.

Neúspešný prenos nezmaže posledné potvrdenie ani čakajúcu zmenu. Opravená identita
alebo politika vyvolá nové overenie; dočasná chyba pred zápisom sa opakuje
s odstupom. Neistý zápis zostáva na osobitné vyriešenie. Podrobnosti a ohraničené
ručné preoverenie sú v [dodávateľských dostupnostiach](supplier-availability.md#daily-refresh-and-delivery-of-changed-labels-28-october-2026).

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
- Natívne odpočty objednávok Upgates zostávajú zapnuté a ich účinky sú overené. Iné nezávislé integrácie zapisujúce zásoby sú vypnuté.
- Objednávky a počiatočné stavy sú zosúladené so spoločným skladom.

Tieto potvrdenia nenastavujú Upgates za vás. Pravidelný protokol `delta_v1` posiela rozdiel množstva a odpočíta už vykonaný natívny účinok objednávky konkrétneho e-shopu, takže rovnaký predaj neodpočíta druhýkrát. Každé SKU potrebuje overený počiatočný kontrolný bod. Absolútne množstvo patrí iba do osobitnej potvrdenej údržby. Webhooky zrýchľujú prenos oznámených zmien; kontrolný zber zachytáva stratené oznámenia. Asynchrónny prenos nie je okamžitá spoločná rezervácia posledného kusu medzi e-shopmi. Podmienky sú v [pravidelnom prenose](stock-sync.md).

Serverové povolenie zápisu je ďalšia podmienka. Ak je vypnuté, stránka to zobrazí a tlačidlo prenosu zostane nedostupné. Nasadenie kódu ani migrácia ho samy nezapnú. Pri zmene priradenia skladu alebo cieľového e-shopu môže systém vyžiadať nové potvrdenie.

Po uložení povolenia môžete použiť **Odoslať zásoby teraz**, aj keď pravidelné odosielanie zostáva vypnuté. Automatiku zapnete samostatným políčkom **Odosielať pravidelne v nastavenom intervale**. Na stránke sú odkazy na intervaly načítania a spracovania objednávok; tie zostávajú samostatné od intervalov prenosu zásob.

## Výsledok a neúspešná odpoveď

V posledných prenosoch otvorte **Detail** a obnovujte výsledok. Počet overených položiek znamená overený stav e-shopu. Chybné, preskočené a neisté položky majú vlastný stav a dôvod. Ďalšie položky môžu čakať na spracovanie.

Ak sa stratí odpoveď pri uložení nastavení alebo spustení práce, najprv obnovte nastavenia a stav. Stránka zápis automaticky neopakuje. Načítanie stavu po strate odpovede môže ukázať, že sa pôvodná operácia dokončila.

**Neistý zápis zásob** vyžaduje osobitný postup. Najprv treba overiť, že pôvodná vzdialená požiadavka už nemôže dobehnúť, a doložiť, či sa rozdiel aplikoval. Vyhodnotenie `delta_v1` vyžaduje výslovný výsledok a dôkaz; samotná zhoda aktuálneho počtu pri jednom načítaní to nedokazuje. Pôvodný zápis sa automaticky neopakuje a blokácia zostáva do vyriešenia. Podrobný postup je v [pravidelnom prenose](stock-sync.md).

Obnovenie nastavení nahrádza neuložené zmeny formulára. Zmenené nastavenia a čakajúce uloženie sú označené. Token sa neukladá do adresy ani úložiska prehliadača a pri zamknutí sa odstráni z pamäte.

## Overenie implementácie

Interakcie sú pokryté syntetickým testom `node frontend/tests/availability-sync-ui.cjs`: hranice intervalov, platnosť, revízie nastavení, skutočné preberanie hodnôt, potvrdenie autority, ručné spustenie pri vypnutej automatike, strata odpovede bez automatického opakovania, obnova neistého zápisu a ignorovanie odpovedí zo starého prístupu. Test neodosiela produkčné zásoby ani neoveruje skutočné nastavenie Upgates.
