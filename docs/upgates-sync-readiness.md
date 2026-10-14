# Upgates: súbeh zásob a kapacita API

Overenie kontraktu: 14. 10. 2026. Tento dokument oddeľuje implementovaný protokol `delta_v1`, verejný kontrakt Upgates a ešte nepotvrdený živý pilot celého objednávkového cyklu. Popis implementácie nie je potvrdením nasadenia ani zapnutia skladových zápisov.

## Výsledok kontroly súbehu

Upgates automaticky mení hlavný sklad pri dokončení objednávky, zmene počtu kusov, storne a odstránení objednávky. Verejná dokumentácia nepotvrdzuje prepínač, ktorý by tieto zápisy vypol, ani podmienený zápis `stock` cez očakávanú revíziu/`If-Match`. Hlavný sklad nemožno deaktivovať; vedľajšie sklady nemajú rovnaké objednávkové správanie.

Predchádzajúci absolútny publisher vykonával vzdialené čítanie, lokálnu kontrolu, zápis celého množstva a kontrolné čítanie. Nasledujúci protipríklad vysvetľuje, prečo ho nový pravidelný protokol nahrádza:

| Krok | Hub | Upgates |
| --- | --- | --- |
| Počiatočný stav | Voľné 4 kusy; ešte nepozná novú objednávku | 4 kusy |
| Čítanie | Prijme 4; chce napríklad upraviť dostupnosť | 4 kusy |
| Nová objednávka | Ešte nie je načítaná | Natívny odpočet na 3 |
| Absolútny PUT | Pošle uložené `stock:4` | Prepíše množstvo na 4 |
| Kontrolný GET | Zhoda s požadovanou hodnotou | 4 kusy; zhoda nedokazuje správnosť |

Lokálne zámky nechránia vzdialené prijatie objednávky. Čerstvý kurzor ani ďalšie čítanie pred zápisom nie sú distribuovanou transakciou. Tento protipríklad bol reprodukovaný izolovaným databázovým testom predchádzajúceho publishera s napodobeným transportom; nešlo o živú zmenu e-shopu.

Bežný publisher preto už neposiela absolútne `stock`. Posiela `stock_increment` vypočítaný z lokálnej zmeny a doloženého natívneho účinku vlastných Upgates objednávok. Riadená absolútna inicializácia zostáva osobitným údržbovým postupom pri skutočne pozastavených predajoch a zosúladených objednávkach. Zmena protokolu nepreukazuje podporu neznámych objednávkových prechodov a sama neoprávňuje zapnúť ostrú synchronizáciu.

## Kapacitný model aktuálneho kódu

Platný nezmenený kontrolný bod s nezmenenou dostupnosťou spotrebuje **0 vzdialených volaní** medzi pravidelnými šesťhodinovými kontrolami. Aj vlastná natívna objednávka a rovnaká lokálna rezervácia môžu vyrušiť prenášaný rozdiel: oba počítadlá sa posunú bez ďalšieho odpočtu či volania.

Zmeny a kontroly po termíne sa načítajú podľa pracovnej dávky `batch_size`, osobitne pre samostatné produkty a varianty. Požiadavka má najviac 100 presných kódov a obmedzenú dĺžku filtra. Kompletná odpoveď musí obsahovať práve požadované položky, jednoznačné identity a úplné stránkovanie; chyba nevyvolá pokusy po každom produkte zvlášť. Po čítaní sa opäť overí lokálny podklad. Úspešný zápis má jeden PUT bez následného GET.

Model čerstvého priechodu je `G + D`, kde `G` je počet potrebných čítacích skupín a `D` počet skutočných zápisov. Pri zmene všetkých `K` oprávnených položiek je to `G + K`. Samostatná kontrola nezmenených položiek po šiestich hodinách potrebuje iba `G`. Zastaraný dávkový výsledok alebo nová lokálna zmena môžu vyžiadať ďalší GET; chyby, ručné údržby a ostatné workflow tento model nezahŕňa. Neoverené lokálne množstvo alebo chýbajúci kontrolný bod sa neprevedú na nulu.

Historické modely `K + 2 × D` a neskorší `G + 3 × D` opisovali predchádzajúci absolútny publisher. Nie sú modelom `delta_v1` ani dôvodom na návrat k starému zapisovateľovi.

Prázdne načítanie objednávok spravidla potrebuje dve požiadavky (aktívne a zmazané objednávky). Každý úplný pokus spracovania objednávky používa detail a číselník stavov. Pri päťminútovom intervale ide ideálne o 576 čítaní denne už pri prázdnom zberači a ďalších 576 pre každú objednávku opakovanú každých päť minút. Čas spracovania tento počet znižuje; nové objednávky, stránkovanie, ručné operácie a ďalšie integrácie ho zvyšujú. Dokončené objednávky majú tiež periodickú úplnú kontrolu.

Interval skladového publishera začína po dokončení celého priechodu. Výpočet `počet volaní × 86400 / interval` je model ideálneho tempa, nie nameraná denná spotreba ani záruka čerstvosti.

## Meranie pripojenia

V **Nastavenia → Dostupnosti a prenos zásob** je pre vybraný e-shop samostatná čítacia diagnostika. Na výslovné spustenie načíta `GET /api/v2/status` uloženým serverovým pripojením a zobrazí povolené služby, čas odpovede a bezpečne spracované hlavičky kvóty. Pripojenie sa nesmie merať opakovaným zaťažovaním až po HTTP 429.

Voliteľné overenie vzorky pridá najviac dve produktové požiadavky: do štyroch presne spárovaných samostatných produktov a štyroch variantov. Nepotrebuje aktivovať skladovú politiku ani fyzické množstvá. Používa rovnaký prísny dávkový parser ako publisher a zobrazí iba počty a výsledok overenia. Pred sieťovými volaniami končí lokálna čítacia transakcia. Pri zlyhaní skupín nepredstiera presne nameraný počet odoslaných požiadaviek a nič automaticky neopakuje. Predvolená kontrola bez vzorky naďalej vykoná iba jeden `/status`.

- Hodinový limit a dodatočný denný limit sa sčítavajú podľa pravidiel Upgates; teoretický denný strop je `24 × hodinový limit + denný limit`.
- `X-Rate-Limit-Total-Remaining` vyjadruje aktuálne dostupnú kapacitu, nie celý tento teoretický denný strop.
- Zobrazená kvóta pochádza z úvodného `/status`; pri kontrole vzorky teda predchádza jej produktovým GET.
- Chýbajúca alebo neplatná hlavička znamená neznámu hodnotu, nie nulu.
- Zdokumentované tri súbežné požiadavky platia pre skupinu API prístupov. Hub nevie z jedného diagnostického čítania zmerať súbeh ostatných integrácií.
- Diagnostika sama nenastavuje interval ani nekupuje kvótu. Jej požiadavky prechádzajú rovnakým spoločným limiterom ako ostatné pripojenia Hubu; lokálne odloženie sa počíta ako nula odoslaných požiadaviek.

### Spoločné riadenie rozpočtu

`UpgatesClient.from_shop` vytvára riadenú reláciu pre všetky aktívne API transporty Hubu: objednávky, katalóg, pravidelný aj údržbový sklad, vybrané produktové polia, FIFO ceny a diagnostiku. Kľúčom je normalizovaný HTTPS origin bez prihlasovacích údajov. Samostatné PostgreSQL spojenie v autocommit režime povoľuje jednu súbežnú požiadavku naprieč procesmi a drží iba vlastný session advisory lock, nie obchodnú transakciu. Streamovaná odpoveď drží povolenie do uzavretia; ukončenie spojenia uvoľní zámok.

Zvyšok hodinového limitu sa spotrebuje pred dennou rezervou. `Total-Remaining` sa nepoužíva ako obnoviteľný lokálny rozpočet. Neplatné alebo chýbajúce hlavičky nevytvoria novú kapacitu: bez známeho rozpočtu je povolený jeden overovací pokus a potom dočasné odloženie. HTTP 429 uloží spoločné `Retry-After`. Lokálne zamietnutie vznikne pred odoslaním a nesmie sa zameniť s neistým vzdialeným PUT. Transport nič automaticky neopakuje. Pri nedostupnosti PostgreSQL sa požiadavka neodošle; výslovný legacy režim `USE_POSTGRES=false` má iba procesnú ochranu.

Iné integrácie zostávajú mimo tohto riadenia. Ich spotreba sa prejaví v hlavičkách, ale Hub nemôže zaručiť ich súbeh. Potrebné navýšenie kvóty sa určuje podľa skutočných zmien, objednávok a kontrol, nie iba podľa celkového katalógu; táto implementácia nič neobjednáva.

Konkrétne kvóty, počty prepojených SKU a prevádzkové merania sa kontrolujú v diagnostike Hubu; nie sú súčasťou repozitárovej dokumentácie. Úspešný `/status` nepreukazuje bezpečnosť skladových zápisov ani celý objednávkový cyklus.

## Implementovaný rozdielový protokol a jeho hranice

Verejné API má `stock_increment`, ktorý neprepisuje celý práve platný stav. Implementácia `stock_delta.py` ho spája s kontrolnými bodmi a `stock_native.py` s auditovaným pôvodom objednávkových účinkov:

1. Kontrolný bod vzniká po potvrdenej údržbe, dokončenom zbere a spracovaní objednávok a čítacej zhode vzdialeného a lokálneho množstva. Ukladá lokálne množstvo, natívny účinok, presnú vzdialenú identitu, pripojenie a revíziu.
2. Prenáša sa `(nové lokálne − uložené lokálne) − (nové natívne − uložené natívne)`. Vlastná Upgates objednávka sa neodpočíta druhýkrát; príjem, inventúrna oprava alebo zmena druhého kanála zostáva súčasťou lokálneho rozdielu. Výdaj rezervovaného kusa sa neposudzuje ako ďalšia nezávislá rezervácia.
3. Natívny účinok používa presné identity a systémové typy stavov. Neznámy typ, nepodporovaná položka, zmazanie, nepreukázané storno alebo opätovné otvorenie sa nezamenia za nulový účinok. Podklady musia pokrývať aktuálne spracované objednávky; nejasné prípady blokujú bežný prenos.
4. Každý PUT má trvalý zámer a zmrazenú revíziu kontrolného bodu. Po jednoznačnom potvrdení sa kontrolný bod posunie. Nejasný výsledok sa neopakuje; vyžaduje doložené `applied`/`not_applied` a vysvetlenie po skončení pôvodnej požiadavky. Obyčajné kontrolné čítanie nemôže rozhodnúť výsledok súbežného rozdielu.
5. Absolútne `stock` zostáva iba na samostatnú riadenú údržbu pri zastavených vzdialených zmenách. Periodický audit rozdiel oznámi, ale neopravuje ho absolútnym zápisom.
6. Inkrementálny zber zahŕňa vytvorenie objednávok od roku 1900 a ďalej filtruje podľa poslednej zmeny. Zachytí tak novú úpravu starej objednávky bez automatického prehrania jej fyzickej histórie. Zmena pred začiatkom lokálnej evidencie vyžaduje kontrolu; nesmie sa potichu ignorovať.

Migrácie `024` (rozpočet), `025` (natívna evidencia) a `026` (rozdielový protokol a kontrolné body) sú aditívne a súčasťou explicitného deployment spúšťača. Nezakladajú historické pohyby a neaktivujú živé zápisy. Staré oprávnenie absolútneho protokolu nestačí; nové zapnutie vyžaduje potvrdené podmienky a platné kontrolné body. Návrat k starému absolútnemu pravidelnému zapisovateľovi nie je prípustný spôsob obnovy po použití rozdielov; treba ponechať evidenciu a použiť kompatibilnú doprednú opravu.

Živý cyklus vytvorenia, úpravy a storna objednávky s novým protokolom ešte nie je potvrdený. Súčasný predaj posledného kusu v oboch e-shopoch je osobitný problém. Polling môže zmenšiť časové okno; úplná garancia vyžaduje centrálnu rezerváciu pri nákupe alebo rozdelenie kusov medzi kanály. Pravidlo objednateľnosti `overíme` sa nemení. Webhooky môžu neskôr zrýchliť zistenie zmien, ale nie sú zámkom hlavného skladu a tento krok ich neimplementuje.

## Ohraničený živý pilot

Pilot potrebuje overený testovací Upgates e-shop alebo izolované testovacie produkty a objednávky bez napojenia na reálne platby, fakturáciu, expedíciu a zákaznícke notifikácie. Nesmie zdieľať produkčnú skladovú identitu. Pri absencii tohto overenia je možné vykonať len čítacie meranie a izolované automatické testy.

Pre jeden samostatný produkt a jeden variant zaznamenať presné ID, východiskový stav a limit volaní. Otestovať postupne vytvorenie objednávky, zmenu počtu, storno, príjem, udalosť druhého kanála, opakované oznámenie a nejednoznačný výsledok zápisu. Vyhodnotiť pôvod odpočtov, konečný stav a spotrebované volania. Nevykonávať „nezmenený“ PUT na reálnom produkte: aj ten môže prepísať objednávku vytvorenú medzi čítaním a zápisom. Nevytvárať fiktívny fyzický príjem v produkčnom Hube.

## Primárne zdroje

- [Sklad a objednávkové zmeny](https://www.upgates.cz/a/skladove-zasoby)
- [Vývoj doplnku, upozornenie na opakovaný odpočet](https://www.upgates.cz/a/api-dokumentace-doplnku)
- [Produkty: stock a stock_increment](https://docs.upgates.com/api-reference/produkty)
- [Produktové zoznamy a dávkové čítanie](https://docs.upgates.com/api-reference/produkty-seznamy)
- [Kvóty a súbeh požiadaviek](https://docs.upgates.com/api/rate-limiting)
- [Stav API a oprávnenia](https://docs.upgates.com/api-reference/~endpoints)
- [Doručovanie webhookov](https://docs.upgates.com/webhooks/intro)
