# Nákupné ceny v e-shopoch

Stránka **Nastavenia → Nákupné ceny v e-shopoch** (`/settings/purchase-costs`) spravuje prenos obstarávacích cien z Hubu do BIKETREK a xTrek. Prenos cien má vlastné nastavenie a nemení množstvá, predajné ceny ani stavy objednávok. Nasadenie a migrácia samy nezapínajú pravidelné odosielanie.

## Akú cenu prenášame

**Produkt alebo variant:** cenu najstaršej zostávajúcej predajnej FIFO vrstvy vo vybranom sklade. Nie priemer celého skladu ani poslednú cenu z dodávateľského feedu. Rezervácia drží množstvo, ale neposúva FIFO vrstvy; cena ďalšieho kusa sa mení až pri skutočnom výdaji. Karanténa sa nepoužije.

Ak bol produkt vypredaný, nový potvrdený príjem poskytne novú cenu. Ak ešte zostávajú staršie kusy, neskorší príjem drahšieho alebo lacnejšieho tovaru ich nepredbehne. Pri nulovej zásobe zostane v e-shope posledná cena; po ďalšom príjme sa znovu aktualizuje. Chýbajúca alebo predbežná cena sa neposiela ako nula. Skutočne doložená nulová obstarávacia cena je prípustná.

**Vydaná objednávka:** pre každý jej skladový riadok sa spočítajú náklady konkrétnych vydaných FIFO kusov a vydelia pôvodným vydaným množstvom. Napríklad 1 kus za 10 €, 1 za 20 € a 1 za 30 € znamenajú 60 € spolu a 20 €/ks. Používa sa množstvom vážený priemer vydaných kusov, nie priemer zostávajúcej zásoby. Bezkódové servisné a iné neskladové riadky sa týmto spôsobom neoceňujú.

| Stav príkladu: 1 kus za každú cenu 10, 20, 30, 40, 50 € | Cena produktu podľa FIFO | Náklad vydanej objednávky |
| --- | ---: | ---: |
| Pred objednávkou | 10 € | — |
| Po rezervácii 3 kusov | 10 € | ešte nevznikol konečný výdaj |
| Po výdaji 3 kusov | 40 € | 3 × 20 € = 60 € |
| Zostatok 2 kusy za 40 a 50 € | 40 € | pôvodných 60 € sa nemení výdajom inej objednávky |

V príklade aj v Hube sú obstarávacie ceny v EUR bez DPH. Zostávajúci sklad má hodnotu 90 € a priemer 45 €; tento priemer sa neposiela ako cena ďalšieho FIFO kusa.

## Nastavenie a ručné spustenie

1. Odomkni stránku existujúcim operátorským tokenom, vyber e-shop a jeho sklad.
2. Samostatne povoľ prenos cien produktov a cien vydaných objednávok. Interval a veľkosť dávky možno prevziať zo skladu alebo nastaviť pre daný e-shop.
3. Ulož nastavenia. Ručné spustenie zaradí priechod do fronty; výsledok over v histórii. Pravidelné spúšťanie sa zapína samostatne.
4. Sleduj výsledky jednotlivých položiek. Zaradenie do fronty ani úspešné uloženie nastavení neznamenajú overené doručenie ceny.

Predvolené hodnoty sú 300 sekúnd a 20 položiek v dávke. Priechod pokračuje cez ďalšie dávky, takže veľký katalóg nezostane obmedzený na prvých 20 produktov. Prenos po príjme alebo výdaji prebehne pri spracovaní príslušnej položky; nejde o okamžitú operáciu v transakcii príjmu. Novšie údaje sa pred zápisom znovu overujú.

Automatický priechod preskočí už overenú nezmenenú FIFO cenu pre rovnaké prepojenie a cieľ. Ručné spustenie overí aj tieto ceny v e-shope, napríklad po nezávislej úprave v administrácii. Zhodná cena nevyvolá ďalší zápis. Veľký katalóg sa spracováva postupne; interval nie je zárukou dokončenia celého prenosu v danom čase.

Čas posledného dokončeného priechodu označuje kontrolu lokálnych podkladov a zaradenie zmien. Jednotlivé zápisy ešte môžu čakať vo fronte; doručenie potvrdzuje stav **Overené** pri konkrétnom zázname. Pri limite Upgates API sa uloží prestávka podľa `Retry-After`, spoločná aj s existujúcim načítavaním objednávok. Neistý zápis sa po prestávke automaticky neopakuje.

Zápis vyžaduje aj serverové `FIFO_COST_SYNC_WRITE_ENABLED=true`. Predvolene je vypnuté a UI tento stav zobrazuje. Toto povolenie sa týka cien; nenahrádza ani nezapína oprávnenie na pravidelný prenos skladových množstiev.

Automatika pracuje s objednávkami vydanými od zobrazenej aktivácie. Samotné zapnutie nespúšťa hromadný prepočet všetkých historických objednávok. Staršie objednávky možno riešiť jednotlivo podľa nasledujúceho postupu.

## Staršia uzavretá objednávka a oprava nákupnej ceny

Zadaj číslo objednávky v časti pre staršiu objednávku a vytvor náhľad. Hub použije jej **pôvodné FIFO alokácie** a ich aktuálne doložené ceny. Náhľad ukáže pôvodnú vzdialenú cenu a navrhovanú cenu; odoslanie vyžaduje samostatné potvrdenie.

Takto možno opraviť nákupnú cenu aj po uzavretí objednávky, napríklad keď sa dodatočne opraví cena príjmu. Objednávka sa kvôli tomu znovu nevydáva ani neotvára, nemení sa množstvo, predajná cena ani stav. Pôvodný náklad v okamihu výdaja a audit opravy zostávajú zachované v Hube.

Objednávka musí mať pôvodný doložený FIFO výdaj. Staršiu objednávku bez alokácií nemožno správne dopočítať z dnešného skladu. Zmenené kódy alebo množstvá vo vzdialenej objednávke sa nesmú potichu priradiť k iným kusom. Viac riadkov s rovnakým kódom sa blokuje, keďže aktualizácia Upgates podľa kódu nedokáže bezpečne určiť jeden konkrétny riadok.

Vratka nemení pôvodné vydané množstvo ani náklad pôvodného predaja v tomto prenose. Má samostatnú evidenciu. Cenu pôvodnej objednávky preto nedelíme zostávajúcim množstvom po vratke.

## DPH, mena a zaokrúhľovanie

Prenos podporuje EUR a zisťuje cenový režim cieľového e-shopu alebo konkrétnej objednávky. Režim staršej objednávky sa nemusí zhodovať so súčasným nastavením e-shopu. Pri cenách s DPH sa obstarávacia cena prepočíta podľa doloženej sadzby daného riadka. Neznáma mena, DPH alebo režim zápis zablokujú.

Odosielaná jednotková cena sa zaokrúhľuje na štyri desatinné miesta pomocou `ROUND_HALF_UP`. Presný celkový náklad zostáva uložený v Hube. Pri delení, napríklad 10 €/3 kusy, sa hodnota 3,3333 €/ks po vynásobení môže o zlomok centu líšiť od celku. Odlišné zaokrúhlenie v odpovedi Upgates sa nepovažuje automaticky za zhodu; výsledok zostane na kontrolu.

## Neistý zápis

Po strate odpovede Hub neposiela tú istú zmenu naslepo znovu. Záznam `Neistý` blokuje ďalšie odoslanie rovnakého produktu alebo objednávky aj po reštarte. Až keď je overené, že pôvodná požiadavka už nemôže dobehnúť, možno potvrdiť jej ukončenie, doplniť poznámku a načítať vzdialený výsledok. Samotná zhoda ceny bez ukončenia pôvodnej požiadavky nestačí. Obnova iba overí stav; neposiela nový zápis.

## Vývojársky kontrakt

- `fifo_cost_projection.py` číta lokálne FIFO dôkazy, nemení sklad a nezapisuje nové alokácie. Produkt používa najbližšiu známu vrstvu, objednávka svoje pôvodné alokácie s aktuálnymi nákladmi. Každá projekcia nesie podpis zdrojových údajov.
- `fifo_cost_source.py` vykonáva ohraničené čítanie, minimálny zápis nákupnej ceny a následnú kontrolu. Produkt/variant používa `price_purchase`, položka objednávky `buy_price`. Objednávkový zápis explicitne vypína emaily, SMS a mazanie vynechaných produktov. Zachová cenový režim konkrétnej objednávky.
- Produktové pozorovanie používa tri GET: produkt, cenový režim a jazyky/mena. Úplný zmenový cyklus má desať API volaní (tri pozorovania a jeden PUT); objednávkový cyklus štyri. Nezmenená overená automatická projekcia nevolá Upgates. Súbežná zmena skladového množstva produktu neporuší kontrolu nákupnej ceny; identity a cenové polia zostávajú kontrolované.
- `fifo_cost_sync.py` a `fifo_cost_worker.py` spravujú zapínateľné nastavenia, ohraničené priechody, audit a obnovu. Pred možným vzdialeným zápisom sa záväzne uloží stav `sending`; nejasný výsledok nie je automaticky opakovateľný.
- Produktový zápis rešpektuje spoločnú ochranu cieľa s AI a manuálnymi produktovými publikáciami, vrátane variantov pod spoločným pokladňovým rodičom xTrek. Neistá zmena jedného variantu nemá byť prekrytá zápisom iného publish workflow.
- Chránené API má prefix `/fifo-cost-sync` a odpovede `Cache-Control: no-store`. Všetky zmeny nastavení používajú kontrolu revízie.

| Cesta | Účel |
| --- | --- |
| `GET /options?shop_code=…` | Nastavenia, predvoľby skladu, dostupnosť serverového zápisu a stav |
| `POST /warehouse` | Spoločný interval a veľkosť dávky |
| `POST /configure` | Nastavenia konkrétneho e-shopu a skladu |
| `POST /run` | Ručný priechod |
| `GET /history?shop_code=…` | Stránkovaná história |
| `POST /orders/preview` | Náhľad ceny konkrétnej vydanej objednávky vrátane staršej |
| `GET /publications/{id}` | Obnova konkrétneho výsledku |
| `POST /publications/{id}/send` | Potvrdenie pripraveného náhľadu |
| `POST /publications/{id}/resolve` | Overenie neistého zápisu po potvrdenom skončení pôvodnej požiadavky |

Migrácia `018_fifo_cost_sync.sql` pridáva nastavenia a trvalé záznamy prenosov. Je zapojená do API obrazu a deploymentu po `017` pred reštartom. Nemení historické pohyby, nákupné ceny ani nastavenia aktivácie.

Oficiálny kontrakt: [produkty](https://docs.upgates.com/api-reference/produkty), [objednávky](https://docs.upgates.com/api-reference/objednavky). Verejné API neposkytuje transakciu spoločnú s databázou Hubu ani dokumentovaný podmienený zápis. Kontrola tesne pred zápisom a následné čítanie nenahrádzajú vzdialený compare-and-swap. Súbežné nezávislé úpravy tých istých nákupných cien musia byť prevádzkovo koordinované.

Pri návrate aplikácie sa nemažú záznamy odoslaní ani neisté pokusy. Najprv vyrieš rozpracované zápisy; nepouži kód, ktorý ich ochranu nepozná. Uprednostni doprednú opravu. Návrat kódu sám nevracia už zapísané ceny v e-shope.
