# Pravidelný prenos zásob a dostupností

Táto funkcia prenáša vlastné voľné množstvo a dostupnosť konkrétnych SKU z Hubu do BIKETREK a xTrek. Je oddelená od [údržbového publikovania](stock-publication.md). Neuzatvára sklad a neblokuje bežné príjmy ani spracovanie objednávok počas sieťových požiadaviek. Nasadenie nepovoľuje žiadny nový živý prenos: plánovač, oprávnenie e-shopu aj serverový prepínač začínajú vypnuté.

## Použitie

V nastaveniach dostupností a synchronizácie vyber e-shop. Interval, veľkosť pracovnej dávky a najvyšší prípustný vek načítania objednávok môžeš nastaviť pre sklad; prázdna hodnota v e-shope znamená zdedenie. Vlastná hodnota e-shopu má prednosť. Zmena intervalu sama neoprávňuje Hub odosielať zásoby.

| Nastavenie | Predvolene | Rozsah |
| --- | --- | --- |
| Interval medzi dokončenými priechodmi | 300 sekúnd | 60–86 400 sekúnd |
| Počet SKU v pracovnej dávke | 20 | 1–100 |
| Najvyšší vek objednávok | 900 sekúnd | 60–86 400 sekúnd |

Jeden priechod spracuje postupne všetky zalistované SKU daného e-shopu. Väčší katalóg sa rozdelí na dávky; nejde iba o prvých 20 produktov pri každom intervale. **Spustiť teraz** vytvorí ručný priechod bez zapnutia pravidelného plánovača. API môže ručný prenos zúžiť na vybrané SKU. Opakované stlačenie pri rovnakom rozpracovanom výbere zobrazí existujúci priechod. Iný výber počká, kým prebiehajúci priechod skončí.

Pravidelný prenos vyžaduje výslovné nastavenie Hubu ako autority zásob. Pred jeho povolením musí obsluha overiť všetky predajné kanály spoločného skladu, vrátane pokladne BIKETREK:

- Samostatné znižovanie či zvyšovanie zásob v Upgates a ostatných integráciách je vypnuté. Hub je jediný zapisovateľ týchto skladových hodnôt. Toto nastavenie sa v Upgates musí overiť osobitne; zaškrtávacie pole ho samo nemení.
- Historické a rozpracované objednávky sú zosúladené s Hubom.
- Na všetkých kanáloch spoločného skladu je zapnuté pravidelné načítanie objednávok a automatické lokálne rezervácie alebo výdaje. Objednávky vyžadujúce kontrolu treba vyriešiť.
- Správca povolil serverový prepínač `STOCK_SYNC_WRITE_ENABLED=true`. Tento prepínač je nezávislý od `STOCK_PUBLICATION_WRITE_ENABLED` pre údržbu.

Ak sa nedá výlučné zapisovanie zásob v konkrétnom nastavení Upgates zabezpečiť, pravidelný prenos nezapínaj; ostáva dostupné riadené údržbové odosielanie. Zmena pripojenia, politiky skladu alebo zoznamu jeho predajných kanálov vyžaduje nové potvrdenie autority.

Pravidelná synchronizácia pracuje s oneskorením načítania objednávok. Nejde o okamžitú spoločnú pokladňovú rezerváciu medzi oboma e-shopmi. Kontrola aktuálnosti znižuje riziko starej projekcie, ale nedokazuje, že tesne pred odoslaním nevznikla nová objednávka. Kratší interval načítania objednávok a prenosu znižuje túto medzeru. Hub kontroluje čerstvé dokončené načítanie, kurzor objednávok a chýbajúce alebo nedokončené spracovanie na všetkých zúčastnených kanáloch.

## Prenášané údaje

Vlastné voľné množstvo je fyzické množstvo mínus rezervácie mínus karanténa. Zásoby dodávateľa sa doň nepripočítavajú. Dostupnosť sa vyhodnotí pri každom priechode:

1. Známa vlastná voľná zásoba väčšia ako nula: `SKLADOM`.
2. Inak čerstvá kladná ponuka dodávateľa: text v jeho konfigurácii, predvolene `do 5 dní`.
3. Inak `overíme`.

Aj tovar s dostupnosťou `overíme` zostáva objednateľný. Odosielané sú iba `stock`, `availability` a `can_add_to_basket_yn` pre presný skladový list. Variant pod rodičom „xTrek“ v BIKETREK sa aktualizuje samostatne; nemenia sa ostatné varianty ani názov rodiča. Ceny, názvy, obrázky a aktivita produktu nie sú súčasťou tohto prenosu.

Chýbajúci alebo neoverený lokálny stav nie je nula. Taká položka sa preskočí s dôvodom a najprv vyžaduje riadne zaevidovanie počiatočného stavu alebo príjmu. To platí aj pre produkt iba načítaný z katalógu dodávateľa. Platnosť ponúk dodávateľov sa kontroluje aj bez nového feedu, takže po uplynutí platnosti ďalší priechod prenesie `overíme`.

Úspešná položka znamená následne overené vzdialené hodnoty. **Dokončený priechod** môže obsahovať preskočené položky; skontroluj počty a dôvody. Úspešný prenos nikdy nevytvára lokálny skladový pohyb.

## Neistý výsledok

Pri výpadku po začatí zápisu môže Upgates požiadavku ešte spracovať. Hub taký pokus automaticky neopakuje a blokuje ďalšie zápisy rovnakého SKU do rovnakého e-shopu. Ostatné SKU môžu pokračovať. Neistý pravidelný zápis bráni aj začatiu údržbového uzáveru dotknutého skladu.

Po overení, že pôvodná požiadavka už definitívne skončila, použi **Vyriešiť neistý prenos**. Obnova iba načíta aktuálny vzdialený stav: zhoda potvrdí pôvodný výsledok, nezhoda ho uzavrie ako neúspešný. Obnova sama neposiela nový PUT. Až ďalší bežný priechod môže preniesť aktuálny lokálny stav.

## Vývojársky kontrakt

Všetky cesty vyžadujú operátorský token a `Cache-Control: no-store`. Prefix API je `/stock-sync` (v prehliadači `/api/stock-sync`).

| Cesta | Vstup / výsledok |
| --- | --- |
| `GET /options?shop_code=biketrek` | Nastavenia skladu/e-shopu, efektívne hodnoty, blokujúce dôvody, posledných 20 priechodov. |
| `POST /warehouse` | `warehouse_code`, `expected_revision`, tri číselné nastavenia, `confirmed:true`. |
| `POST /configure` | `shop_code`, `expected_revision`, `enabled`, `authorized`, tri nullable výnimky; pri novej autorite aj `hub_is_stock_authority`, `external_stock_writers_disabled`, `orders_reconciled`, všetky `true`; vždy `confirmed:true`. |
| `POST /run` | `shop_code`, prípadne `skus` (1–100 presných jedinečných kódov), `confirmed:true`; vracia trvalé ID priechodu. |
| `GET /runs/{id}` | Súhrn a položky s požadovanými a overenými hodnotami; `offset`/`limit` (najviac 1 000), neisté položky sa zobrazia ako prvé. |
| `POST /items/{id}/resolve` | `confirmed:true`, `external_requests_finished:true`; iba čítacie zosúladenie neistého pokusu. |

Migrácia `015_stock_sync.sql` pridáva samostatné predvoľby skladu, politiky e-shopov, priechody a položky. Existujúce bilancie ani pohyby nemení. Uplatní sa cez `python -m inventory_hub.stock_sync_migrate` pred štartom novej aplikácie. Spoločný pracovník používa session advisory lock; sieťové volania prebiehajú po ukončení DB transakcie. Každý PUT má najprv uložený stav `sending`. Čiastočný unikátny index drží konflikt pre `(shop_id,sku)` v stavoch `sending` a `uncertain`. Po páde sa `sending` zmení na `uncertain`, nikdy na stav oprávňujúci opakovanie PUT.

Identita aj vlastná bilancia sa znovu vyhodnotia po vzdialenom čítaní pred uložením zámeru. Sklad sa počas sieťovej práce nemení na údržbový režim; následné lokálne zmeny sa premietnu ďalším priechodom. Otvorenie údržby a príprava pravidelného `sending` zámeru krátko zamykajú rovnaký riadok skladu, takže jeden postup nemôže prekryť druhý. HTTP 429 zohľadňuje uložený `Retry-After` aj spoločné limity objednávkových integrácií.

Transport zdieľa overovanie HTTPS, presnej identity, veľkosti odpovede a jednoznačného potvrdenia s údržbovým transportom. Nepoužíva presmerovania ani automatické opakovanie zápisu. Oficiálny kontrakt podporuje sklad a dostupnosť aj na variantoch cez [PUT products](https://docs.upgates.com/api-reference/produkty) a samostatné načítanie variantov cez [produktové zoznamy](https://docs.upgates.com/api-reference/produkty-seznamy). Verejný kontrakt nedokumentuje podmienený zápis či idempotentný kľúč; preto výlučná autorita a explicitné riešenie neistoty zostávajú nevyhnutnou súčasťou aktivácie.

Vypnutie plánovača necháva ručný prenos dostupný pri zachovanom oprávnení. Odobratie oprávnenia zastaví nové pokusy rozpracovaných priechodov, ale nezruší už odoslanú sieťovú požiadavku. Návrat staršej verzie aplikácie ani vypnutie serverového prepínača nevracajú vzdialené zásoby späť; na opravu sa použije dopredná zmena po zosúladení neistých pokusov.
