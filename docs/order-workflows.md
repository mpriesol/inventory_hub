# Objednávky a sklad — používateľská príručka

BIKETREK a xTrek používajú spoločné skladové karty podľa kódu predajnej položky. Hub eviduje vlastnú fyzickú zásobu, rezervácie a výdaje. Dodávateľská dostupnosť ani množstvo uvedené v e-shope nie sú príjmom do nášho skladu.

Táto príručka opisuje pracovný postup. Zapnutie automatického spracovania pre konkrétny e-shop je samostatné nastavenie; nasadenie novej verzie ho samo nezapína.

## Čo jednotlivé operácie robia

| Operácia | Výsledok |
| --- | --- |
| Načítanie objednávok | Zobrazí objednávky v Hube. Samotné načítanie nemení fyzické ani rezervované množstvo. |
| Náhľad skladovej operácie | Ukáže navrhované množstvá, rezervácie, nedostatky a prípadné chyby. Ešte ich nezaúčtuje. |
| Rezervácia | Pridelí objednávke dostupné vlastné kusy. Fyzická zásoba zostáva rovnaká; voľná zásoba klesne. |
| Storno pred výdajom | Uvoľní rezerváciu tejto objednávky. Nevracia tovar fyzicky na sklad. |
| Výdaj | Jednorazovo odpočíta všetky skladované položky objednávky a uzamkne vydaný obsah. |
| Návrh množstiev pre e-shop | Ukáže vlastnú voľnú zásobu. Odosielanie množstiev do e-shopov zostáva vypnuté. |

**Voľná zásoba = fyzická zásoba − rezervované kusy − karanténa.** Ak vydáme už rezervované kusy, fyzická aj rezervovaná zásoba klesnú rovnako; voľné množstvo sa tým druhýkrát neznižuje.

## Príprava skladu a e-shopu

1. Over spoločné SKU a prepojenia predajných položiek. Variant môže byť v BIKETREK pod rodičom „xTrek“ a v xTrek pod iným produktom; presne rovnaký kód označuje tú istú skladovú kartu.
2. Doplň skutočné zásoby cez príjem alebo **Sklad → Počiatočný stav** (`/stock/opening`). Počiatočný stav používa iba dosiaľ neevidované položky; už prijatý tovar tam nenahrávaj druhýkrát.
3. V **Objednávky → Skladové spracovanie** (`/orders/stock`) načítaj možnosti e-shopu, vyber sklad a potvrď význam jeho stavov. Prvé potvrdenie uloží začiatok skladovej evidencie. Staršie objednávky sa tým spätne nezaúčtujú.
4. Na `/orders/inbox` použi **Obnoviť zoznam v Hube**, potom **Stiahnuť zmeny z e-shopu** alebo potvrď **Zapnúť pravidelné načítavanie**. Tým sa založí konfigurácia zdroja potrebná pre automatický režim. Over výsledok behu a správny e-shop.
5. Najprv spracuj reprezentatívnu objednávku ručne a over množstvá vrátane predaja cez pokladňu. Potom na `/settings/stock` načítaj uložené nastavenia a výslovne povoľ požadovaný automatický režim. Pravidelné načítavanie musí zostať zapnuté, ak chceš priebežne objavovať nové objednávky bez ručného načítania.

Sklad priradený potvrdenej objednávkovej politike zostáva pevný. Zmena e-shopu, SKU ani priradenia skladu nie je spôsob opravy už vydanej objednávky.

Podporované sú celé **kusy**. Metre, vážený tovar, prepočty balení a zložené sety vyžadujú osobitné riešenie. Obstarávacie náklady skladu sa vedú v EUR bez DPH; predajná cena sa nepoužíva namiesto nákupnej.

## Ručné spracovanie jednej objednávky

Na `/orders/stock` vyber e-shop a zadaj číslo objednávky. Načítaj náhľad a skontroluj SKU, množstvá, dostupné kusy, rezervácie a vylúčené riadky. Potvrdenie výdaja vyžaduje aj potvrdenie skutočného odoslania alebo odovzdania.

Pred výdajom možno v e-shope pridať, odstrániť alebo vymeniť tovar a zmeniť množstvo. Potom vytvor nový náhľad; Hub upraví rezervácie podľa aktuálnej objednávky. Náhľad platí 30 minút a pri zmene podkladu alebo zásob môže vyžadovať nové načítanie.

Pri nedostatku tovaru sa rezervujú dostupné kusy a zvyšok zostane nekrytý. **Čiastočný výdaj sa nevykonáva.** Objednávku možno vydať až po pokrytí všetkých jej skladovaných riadkov pri zachovaní rezervácií ostatných objednávok.

Po výpadku spojenia najprv over uložený výsledok podľa ID náhľadu alebo v zozname nedávnych náhľadov. Stratená odpoveď sama neznamená, že operácia zlyhala. Opakované potvrdenie tej istej dokončenej operácie nevytvorí ďalší výdaj.

## Režimy automatického spracovania

| Režim | Správanie |
| --- | --- |
| Ručne (`manual`) | Skladové operácie potvrdzuje obsluha. Načítavanie objednávok môže fungovať samostatne. |
| Rezervácie (`reserve`) | Automatika spracúva povolené rezervácie a ich uvoľnenie. Kandidát na fyzický výdaj zostáva na ručné spracovanie. |
| Rezervácie a výdaj (`fulfill`) | Okrem rezervácií môže automatika jednorazovo vydať oprávnenú objednávku podľa potvrdených stavových pravidiel. |

Režim výdaja zapni iba vtedy, keď stav **Odoslaná / Vyzdvihnutá** v danom e-shope znamená skutočný odchod tovaru. Samotná platba webovej objednávky nestačí. Dokončený platený pokladňový predaj sa posudzuje podľa samostatných pravidiel pre pôvod pokladňa a dokončenie objednávky.

Zapnutie alebo rozšírenie automatického oprávnenia má vlastnú časovú hranicu. Rozhoduje vznik objednávky; neskoršia úprava starej odoslanej objednávky jej nedáva oprávnenie na nový automatický výdaj. Objednávky mimo časovej hranice zostávajú na ručné spracovanie. Konkrétny účinný režim a jeho hranice skontroluj v nastavení e-shopu.

Prechod z ručného režimu do automatiky nastaví nový začiatok automatického spracovania. Prechod na režim s výdajom nastaví nový začiatok automatických výdajov. Zmena samotných intervalov v rovnakom režime tieto časy neposúva. Návrat do ručného režimu a neskoršie nové zapnutie preto nie sú náhradou dočasnej pauzy.

## Nastavenia skladu a výnimky e-shopu

Prevádzkové nastavenia sú na `/settings/stock`. Vyber e-shop, načítaj aktuálne nastavenie a podľa potreby uprav predvoľby skladu alebo výnimky e-shopu. Výber skladu v tomto formulári slúži na úpravu jeho predvolieb; nepresúva existujúcu objednávkovú politiku do iného skladu.

Sklad poskytuje predvolené prevádzkové hodnoty všetkým pripojeným e-shopom. E-shop môže niektoré hodnoty prepísať; pri ostatných používa nastavenie svojho skladu. Pri každej hodnote skontroluj, či je zdedená alebo nastavená osobitne. Zmena predvolenej hodnoty ovplyvní e-shopy, ktoré ju neprepisujú.

**Režim ručne / rezervácie / výdaj je vždy výslovné nastavenie konkrétneho e-shopu.** Zmena predvolených intervalov skladu sama nepovolí výdaje ďalšiemu e-shopu.

| Nastavenie | Predvolené | Povolený rozsah |
| --- | --- | --- |
| Odstup načítavania objednávok | 300 sekúnd | 60–86 400 sekúnd |
| Odstup kontrolného prechodu histórie od začiatku evidencie | 24 hodín | 1–168 hodín |
| Presah pri načítavaní posledných zmien | 10 minút | 5–1 440 minút |
| Veľkosť jedného kontrolného intervalu vzniku objednávok | 7 dní | 1–30 dní |
| Najviac strán jedného dopytu | 100 | 1–100 |
| Najdlhšie trvanie zberného behu alebo jedného skladového pokusu | 180 sekúnd | 30–180 sekúnd |
| Základný odstup po chybe načítania | 300 sekúnd | 60–3 600 sekúnd |
| Najvyšší bežný odstup po chybe načítania | 3 600 sekúnd | 300–86 400 sekúnd |
| Najviac objednávok v jednej spracovateľskej dávke | 20 | 1–100 |
| Odstup opakovanej kontroly čakajúceho spracovania | 5 minút | 1–1 440 minút |
| Odstup opakovanej kontroly úplného podkladu objednávky | 24 hodín | 1–168 hodín |
| Najviac SKU v jednom porovnaní zásob | 20 | 1–100 |
| Platnosť porovnania zásob pred odoslaním | 15 minút | 5–60 minút |

Maximálny odstup po chybe nesmie byť kratší než základný. Dlhšia prestávka požadovaná samotným Upgates sa rešpektuje aj nad týmto bežným nastavením. Kratšie intervaly znamenajú viac API volaní; menšie limity môžu spôsobiť, že väčší zber skončí ako nedokončený. Nedokončený beh nepreskočí zvyšné objednávky.

Pri ukladaní sa overuje verzia nastavenia. Ak ho medzitým zmenil iný používateľ, najprv načítaj aktuálne hodnoty. Po neistej odpovedi tiež najprv over uložený stav. Zapnutie výdajového režimu vyžaduje osobitné potvrdenie významu odoslania a odovzdania; potvrdzuje sa aj pri ďalšom ukladaní nastavení v tomto režime.

## Jednorazové načítanie a čakajúce objednávky

Na `/orders/inbox` tlačidlo **Obnoviť zoznam v Hube** číta uložené výsledky. **Stiahnuť zmeny z e-shopu** vyžiada jeden ohraničený pokus o načítanie hlavičiek aj pri vypnutom pravidelnom zbere. Môže ísť o zmeny alebo jeden interval kontroly úplnosti; jeden pokus nemusí prejsť celú históriu. Nezapne pravidelný zber, nezmení režim skladového spracovania a neodošle skladové množstvá do e-shopu. Ak je automatické skladové spracovanie už povolené, samostatný spracovateľ môže následne spracovať nové oprávnené záznamy.

Výsledok sleduj v prehľade behov a obnov zobrazený zoznam. Pri chybe sa jednorazový pokus považuje za skončený. Ak je pravidelný zber vypnutý, po uplynutí uvedenej prestávky vyžiadaj nový pokus. Čakajúca požiadavka sa nezaradí opakovane; limity Upgates sa nedajú obísť opakovaným stlačením tlačidla.

Úplný podklad objednávky sa pred skladovým spracovaním načítava znovu. Zoznam hlavičiek s rovnakým časom aktualizácie nie je potvrdením, že jej riadky zostali rovnaké. Pri nedostatku zásoby je potrebný skutočný príjem; automatika následne kontroluje čakajúce objednávky znova. Zásoba dodávateľa tento príjem nenahrádza.

V časti **Automatické skladové spracovanie** sleduj poslednú kontrolu, ďalší pokus, uložený výsledok a chybu. **Otvoriť ručnú kontrolu** otvorí stránku konkrétnej objednávky. Až **Načítať objednávku a pripraviť kontrolu** stiahne aktuálny podklad z e-shopu a uloží návrh v Hube. Stav „Spracovanie dokončené“ označuje výsledok poslednej kontroly; ďalšie zmeny objednávky sa stále preverujú. Samotný zobrazený automatický režim nestačí: skontroluj aj prípadné blokovanie zmenenými pravidlami, pripojením, pauzou alebo čakaním na povolený ďalší pokus.

Pozastavenie automatického spracovania skladu sa týka všetkých jeho e-shopov. Pravidelný zber hlavičiek, ručné príjmy, počiatočný stav a ručne potvrdené skladové objednávky zostávajú dostupné. Nejde o úplné uzamknutie skladu pri inventúre. Pred obnovením skontroluj čakajúce položky; pauza automaticky neruší ich doterajšie rezervácie.

## Manuálne riadky a spoločné varianty

Servis alebo ručne predaný diel **bez SKU, EAN a natívnej produktovej identity** zostáva mimo skladu. Je viditeľný medzi vylúčenými riadkami, ale nevytvára kartu, rezerváciu ani pohyb. Zľavy sú tiež neskladové riadky.

Položka s kódom, ktorý Hub nevie jednoznačne priradiť, nie je manuálna výnimka. Najprv vyrieš identitu. Konflikt EAN, kódy líšiace sa iba veľkosťou písmen alebo nejednoznačné mapovanie môžu operáciu zablokovať.

Pokladňový rodič „xTrek“ v BIKETREK môže obsahovať tisíce nesúvisiacich variantov. Hub spracúva konkrétny predaný variant; neodpisuje ostatných súrodencov a nemení jeho viditeľnosť v e-shope.

## Po výdaji

Vydaný obsah je uzamknutý. Zmena stavu, odstránenie riadka, zmazanie objednávky ani opätovné načítanie nevytvárajú automatickú vratku. Vrátený tovar sa musí riešiť s väzbou na pôvodný výdaj a potvrdeným fyzickým návratom. Vratky, reklamácie a neprevzaté zásielky sú zatiaľ na samostatnú kontrolu.

## Kontrola problémov

| Hlásenie alebo situácia | Postup |
| --- | --- |
| Nedostatok zásoby | Over fyzický tovar a dokonči správny príjem. Nevytváraj náhradný počiatočný stav pre už evidovanú kartu. |
| Neznáme alebo konfliktné SKU | Oprav produktové prepojenie a potom načítaj nový podklad. |
| Zmenené stavy e-shopu | Over ich význam a znovu potvrď nastavenie; automatika ich sama neodhadne. |
| Zmenený API cieľ alebo neplatný prístup | Oprav pripojenie správneho e-shopu a over jeho stav pred obnovením. |
| Výsledok požiadavky nie je známy | Použi čítacie overenie aktuálneho stavu pred opakovaním zápisu. |
| Objednávka už bola vydaná a obsah sa zmenil | Rieš oficiálnu opravu/vratku; pôvodný výdaj neprepisuj. |
| Návrh zásoby ukazuje neznámy stav | Chýba preukázaná bilancia alebo platné prepojenie. Neznámy stav nie je nula. |

## Zastavenie prevádzky

Pri probléme prepni automatické spracovanie dotknutého e-shopu na ručný režim alebo použi príslušnú pauzu automatického spracovania. Pred obnovením skontroluj posledné výsledky a čakajúce objednávky. Pozastavenie zberu hlavičiek a pozastavenie skladových operácií sú rozdielne nastavenia.

Vypnutie automatiky ani návrat staršej verzie aplikácie nevrátia rezervácie a vydaný tovar späť. Už zaúčtované operácie ostávajú v evidencii. Návrh vlastných zásob neposiela údaje do Upgates; odosielanie zásob je samostatná nedokončená etapa.

Technický kontrakt a testovacie hranice sú v [order-automation.md](order-automation.md); podrobné doménové pravidlá v [central-stock.md](central-stock.md).

## Odoslanie zásob do e-shopov

Na obrazovke **Publikovanie zásob** (`/stock/publication`) je samostatný postup na porovnanie a kontrolované odoslanie vybraných SKU počas údržby. Predvolene je odosielanie vypnuté. Táto obrazovka nie je potrebná na každodenné načítavanie objednávok. Postup a význam blokácie skladu nájdeš v [návode na publikovanie](stock-publication.md). Limit jednej dávky a platnosť porovnania sa nastavujú v prevádzkových nastaveniach skladu alebo odchýlkach e-shopu.


## Ako rozlíšiť účinok tlačidla

Pod dátovými akciami objednávok, nastavení, publikovania zásob, príjmu a nákupných cien je text so zdrojom a účinkom. Farba je iba doplnok; rozhodujú tieto označenia:

| Označenie | Význam |
| --- | --- |
| Hub · čítanie | Načíta údaje už uložené v Hube. |
| Hub · zmena | Mení lokálnu evidenciu, konfiguráciu alebo uloží návrh operácie. |
| Upgates ↓ čítanie | Backend načíta údaje z uvedeného e-shopu. |
| Upgates ↑ zápis | Backend môže poslať zmenu do uvedeného e-shopu. |
| Hub → Upgates · úloha na pozadí | Po uložení požiadavky alebo povolení automatiky môže nasledovať označená vzdialená operácia. Odpoveď tlačidla ešte nepotvrdzuje jej dokončenie. |

Na `/orders/inbox` **Obnoviť zoznam v Hube** nevolá Upgates. **Stiahnuť zmeny z e-shopu** uloží požiadavku a až worker vykoná čítanie Upgates. Ak je povolené automatické skladové spracovanie, môže potom samostatne zmeniť miestne rezervácie či výdaj; v hlavičke je viditeľný jeho režim.

Na `/orders` kontrolný zoznam načíta jednu stranu objednávok a číselník stavov: plánuje dve volania Upgates. Na `/orders/stock` načítanie možností číta stavy z Upgates; uloženie pravidiel ich znova overuje. Príprava aj potvrdenie skladovej operácie čítajú aktuálnu objednávku. Obnovenie už uloženého výsledku podľa ID číta iba Hub.

Na `/stock/publication` aj príprava porovnania volá Upgates a uloží náhľad v Hube. Odoslanie zaradí prácu na pozadí: kontrolné čítanie, prípadný zápis a následné overenie. **Načítať skutočný stav z e-shopu** opäť číta Upgates a uloží pozorovanie; **Obnoviť výsledok uložený v Hube** číta iba miestny záznam. Overenie neistého výsledku sa riadi osobitnými pravidlami v návode na publikovanie.

Počet volaní je označený ako plán, keď je známy z konkrétnej operácie. Pri dávkach a stránkovaní závisí od dát a kontrol. Text **Skutočný počet zatiaľ nie je k dispozícii** znamená, že API tejto operácie ešte neposkytuje meranú spotrebu. Nejde o nulu. Počet požiadaviek prehliadača do Hubu ani počet objednávok sa nepovažuje za počet volaní Upgates. Zostatok kvóty sa bez údajov z Upgates nezobrazuje.

## Rýchle skenovanie príjmu

Každý nový sken pridá zadané množstvo, aj keď opakovane skenuješ rovnaký EAN. Desať skenov po jednom kuse znamená desať kusov. Počas odosielania predchádzajúceho skenu môžeš pokračovať; ďalšie sa zoradia. Tlačidlo **Pridať sken do príjmu** spotrebuje zadaný kód iba raz aj pri dvojitom kliknutí.

Ak odpoveď príde nejasne alebo spojenie vypadne, použi **Zopakovať nepotvrdený sken**. Zachová sa identifikátor aj pôvodné množstvo operácie. Ak už Hub sken prijal, ďalší kus nepripočíta. Ďalšie skeny počkajú, kým sa táto požiadavka vyrieši. Prázdna, poškodená alebo nesúvisiaca odpoveď HTTP 200 sa nepovažuje za potvrdenie skenu. Kód aj identifikátor ostanú pripravené na rovnaké opakovanie. Čakajúca fronta sa uchová počas relácie daného prehliadača; po návrate na príjem sa obnoví s výslovným opakovaním. Pred ukončením príjmu alebo hromadnou úpravou počkaj na potvrdenie fronty.

Pri rovnakom kóde na viacerých faktúrových riadkoch Hub nevyberie prvý riadok. Otvor položky a uprav množstvo konkrétneho riadka. Pri naskladnení sa stále kontroluje identita produktu a doklad.

Sken prijíma iba kladné množstvo; nula, záporná alebo prázdna hodnota sa neprevedie na jeden kus. Skenovanie mení rozpracovaný príjem. Fyzická zásoba sa zmení až potvrdením celého príjmu. Na obrazovkách nákupných cien sa používajú názvy **Sklad a nákupné ceny**, **Príjmy a nákupné ceny** a **Rozpis nákladu výdaja**. Ocenenie postupne spotrebúva náklady najstarších príjmov; technický názov algoritmu nie je potrebný na každodennú obsluhu.

## Vývojársky kontrakt označovania akcií

Zdieľaný `frontend/src/components/ui/ActionScope.tsx` prijíma explicitné `effects`, voliteľný `shop`, plánované `calls` a výlučne backendom namerané `actualCalls`. Komponent sám nespúšťa sieťové požiadavky ani neodhaduje spotrebu podľa počtu frontend fetchov. Plán a skutočnosť sú oddelené. `known` je konkrétny plán; `variable` uvádza závislosť od dát; bez podkladu zostáva `unknown`. Kombinovaná operácia musí uviesť všetky účinky vrátane práce workera.

Označenia aktuálne pokrývajú pracovné stránky objednávok, skladových nastavení, kontrolovaného publikovania, skener príjmu a panel nákupných cien; tabuľka a história používajú rovnaký komponent. Nasledujúce časti ešte vyžadujú samostatný audit efektov a doplnenie označení:

- dodávateľský katalóg: lokálne filtrovanie oproti obnove feedu, náhľad a skutočný import;
- existujúce importné obrazovky Upgates a staršie upload tlačidlá;
- AI pracovisko: lokálne čítanie, AI príprava, webový prieskum, čítanie e-shopu a kontrolované publikovanie;
- stiahnutie dodávateľských faktúr oproti lokálnemu nahratiu a ich ostatné pomocné operácie;
- jednotná backendová telemetria skutočných požiadaviek, chybových pokusov a času/zdroja zostatku API kvóty.

Tieto zostávajúce časti sa týmto dokumentom neoznačujú za hotové. UI testy kontrolujú klasifikáciu akcií spolu s volanými endpointmi a potlačenie dvojitého odoslania. `frontend/tests/receiving-scan-ui.cjs` overuje desať rýchlych skenov rovnakého kódu, rôzne UUID, stratenú odpoveď po možnom uložení, opakovanie presného payloadu aj obnovu po opätovnom otvorení stránky. Doplnené regresie overujú poškodené HTTP 200 odpovede, nesprávne UUID, neplatný súhrn, oneskorené počiatočné načítanie, nekladné množstvo a prepnutie toho istého komponentu medzi dvoma príjmami počas požiadavky. Každá návšteva príjmu má vlastný kontext fronty; stará odpoveď nesmie blokovať ani prepísať novú. Testy používajú syntetické odpovede; rozloženie v reálnom prehliadači tým nie je overené.
