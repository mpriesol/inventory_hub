# Upgates: súbeh zásob a kapacita API

Overenie kontraktu: 14. 10. 2026. Tento dokument oddeľuje vlastnosti aktuálneho publishera, verejný kontrakt Upgates, meranie konkrétneho pripojenia a ešte nevykonaný živý pilot.

## Výsledok kontroly súbehu

Upgates automaticky mení hlavný sklad pri dokončení objednávky, zmene počtu kusov, storne a odstránení objednávky. Verejná dokumentácia nepotvrdzuje prepínač, ktorý by tieto zápisy vypol, ani podmienený zápis `stock` cez očakávanú revíziu/`If-Match`. Hlavný sklad nemožno deaktivovať; vedľajšie sklady nemajú rovnaké objednávkové správanie.

Aktuálny `stock_sync.process_item` vykonáva vzdialené čítanie, novú lokálnu kontrolu, absolútny zápis a kontrolné čítanie. Nasledujúci priebeh sa tým nedá vylúčiť:

| Krok | Hub | Upgates |
| --- | --- | --- |
| Počiatočný stav | Voľné 4 kusy; ešte nepozná novú objednávku | 4 kusy |
| Čítanie | Prijme 4; chce napríklad upraviť dostupnosť | 4 kusy |
| Nová objednávka | Ešte nie je načítaná | Natívny odpočet na 3 |
| Absolútny PUT | Pošle uložené `stock:4` | Prepíše množstvo na 4 |
| Kontrolný GET | Zhoda s požadovanou hodnotou | 4 kusy; zhoda nedokazuje správnosť |

Existujúce zámky chránia lokálne transakcie Hubu, nie vzdialené prijatie objednávky. Čerstvý kurzor ani ďalšie čítanie pred zápisom nie sú distribuovanou transakciou. Databázový hraničný test s napodobeným Upgates transportom overuje tento protipríklad priamo cez produkčný publisher; nie je to živý test e-shopu ani dôkaz bezpečného súbehu.

Preto potvrdenie skladovej autority samo osebe nestačí na ostré zapnutie aktuálneho absolútneho publishera. Jeho existujúci kontrakt vyžaduje neprítomnosť nezávislých zapisovateľov; tento predpoklad zatiaľ pre bežný Upgates hlavný sklad nie je preukázaný. Riadená údržbová inicializácia zostáva osobitným postupom pri skutočne pozastavených predajoch a zosúladených objednávkach.

## Kapacitný model aktuálneho kódu

Jeden bežný priechod používa `K + 2 × D` volaní: `K` je počet položiek, ktoré prejdú lokálnou kontrolou a vyžadujú vzdialené čítanie, `D` počet odlišných položiek. Každá odlišná položka pridá jeden PUT a jeden kontrolný GET. `batch_size` je pracovná dávka Hubu, nie počet produktov v jednej API požiadavke. Neoverené lokálne množstvo sa preskočí pred sieťovým volaním.

Prázdne načítanie objednávok spravidla potrebuje dve požiadavky (aktívne a zmazané objednávky). Každý úplný pokus spracovania objednávky používa detail a číselník stavov. Pri päťminútovom intervale ide ideálne o 576 čítaní denne už pri prázdnom zberači a ďalších 576 pre každú objednávku opakovanú každých päť minút. Čas spracovania tento počet znižuje; nové objednávky, stránkovanie, ručné operácie a ďalšie integrácie ho zvyšujú. Dokončené objednávky majú tiež periodickú úplnú kontrolu.

Interval skladového publishera začína po dokončení celého priechodu. Výpočet `počet volaní × 86400 / interval` je model ideálneho tempa, nie nameraná denná spotreba ani záruka čerstvosti.

## Meranie pripojenia

V **Nastavenia → Dostupnosti a prenos zásob** je pre vybraný e-shop samostatná čítacia diagnostika. Na výslovné spustenie načíta `GET /api/v2/status` uloženým serverovým pripojením a zobrazí povolené služby, čas odpovede a bezpečne spracované hlavičky kvóty. Pripojenie sa nesmie merať opakovaným zaťažovaním až po HTTP 429.

- Hodinový limit a dodatočný denný limit sa sčítavajú podľa pravidiel Upgates; teoretický denný strop je `24 × hodinový limit + denný limit`.
- `X-Rate-Limit-Total-Remaining` vyjadruje aktuálne dostupnú kapacitu, nie celý tento teoretický denný strop.
- Chýbajúca alebo neplatná hlavička znamená neznámu hodnotu, nie nulu.
- Zdokumentované tri súbežné požiadavky platia pre skupinu API prístupov. Hub nevie z jedného diagnostického čítania zmerať súbeh ostatných integrácií.
- Diagnostika nie je trvalé meranie všetkých volaní, globálny limiter ani automatické nastavenie vhodného intervalu. Existujúce reakcie na 429 zostávajú v jednotlivých workflow.

Výsledok živého čítania konkrétneho e-shopu sa uvádza až po nasadení a vykonaní. Úspešný `/status` nepreukazuje bezpečnosť skladových zápisov.

### Pozorovanie administrácie BIKETREK

Pri čítacej kontrole 14. 10. 2026 administrácia v Doplnky → API zobrazila limit **50 za hodinu + 600 v dennej rezerve**, bez dokúpených požiadaviek. To znamená teoreticky 1 800 požiadaviek za deň pri využití všetkých hodinových období, nie 1 800 okamžite dostupných požiadaviek. Aktuálny zostatok a pripojenie Hubu treba overiť diagnostikou.

Už model deviatich nezmenených SKU pri päťminútovom intervale predstavuje 2 592 čítaní denne; s prázdnym zberačom objednávok 3 168. Nie je to zmeraná prevádzková spotreba ani potvrdený počet aktuálne oprávnených SKU. Dokazuje však, že individuálna kontrola celého katalógu týmto tempom nezodpovedá zobrazenej kapacite.

V Sklad a dostupnosti boli aktívne hlavný sklad, Predajňa – vystavené a Sklad dodávateľa. Viditeľné nastavenia dostupnosti, súčtu variantov, akcie pri zmene a limitu zásob neobsahovali prepínač natívneho odpočtu objednávkou. Žiadne nastavenie ani množstvo sa počas kontroly nezmenilo. Táto kontrola nepotvrdzuje neprítomnosť iného, zdokumentovaného len podporou poskytovateľa, mechanizmu.

## Nadväzujúca zmena protokolu

Verejné API má aj `stock_increment`. Je vhodným kandidátom na prenos rozdielov, pretože neprepisuje celý práve platný stav. Samotná zámena názvu poľa nie je správna implementácia:

1. Evidovať pôvod každej udalosti. Natívny odpočet vlastnej Upgates objednávky sa nesmie do toho istého e-shopu poslať druhýkrát.
2. Príjmy, inventúrne opravy, rezervácie a udalosti druhého predajného kanála musia mať presnú, nemennú väzbu na zmenu disponibilného množstva. Výdaj už rezervovaného kusa nesmie vytvoriť ďalší odpočet voľných kusov.
3. Každý odosielaný rozdiel potrebuje trvalý zámer a audit. Upgates verejne nedokumentuje idempotentný identifikátor takého pohybu: nejasný výsledok sa nesmie automaticky zopakovať a obyčajná zhoda aktuálneho počtu ho pri súbežných predajoch nedokazuje.
4. Absolútny zápis ponechať iba na kontrolovanú inicializáciu/zosúladenie pri zastavených vzdialených zmenách, prípadne až po získaní inej doloženej záruky Upgates.
5. Dávkové čítanie, odosielanie iba skutočných zmien a spoločné riadenie API rozpočtu navrhnúť spolu s týmto protokolom. Samotná cache absolútneho publishera neopraví konflikt zapisovateľov.
6. Webhooky môžu zrýchliť zistenie zmien. Prijatú udalosť treba najprv trvalo uložiť a rýchlo potvrdiť; kontrolný inkrementálny zber zostáva potrebný. Nie sú zámkom hlavného skladu.

Ide o návrh ďalšej implementácie, nie vlastnosť tejto diagnostiky. Súčasný predaj posledného kusu v oboch e-shopoch je osobitný problém. Polling môže zmenšiť časové okno; úplná garancia vyžaduje centrálnu rezerváciu pri nákupe alebo rozdelenie kusov medzi kanály. Doterajšie pravidlo objednateľnosti `overíme` umožňuje aj objednávky nekryté vlastným skladom a bez nového obchodného rozhodnutia sa nemení.

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
