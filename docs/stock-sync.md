# Pravidelný prenos zásob a dostupností

Táto funkcia prenáša vlastné voľné množstvo a dostupnosť konkrétnych SKU z Hubu do BIKETREK a xTrek. Je oddelená od [údržbového publikovania](stock-publication.md). Neuzatvára sklad a neblokuje bežné príjmy ani spracovanie objednávok počas sieťových požiadaviek. Nasadenie nepovoľuje žiadny nový živý prenos: plánovač, oprávnenie e-shopu aj serverový prepínač začínajú vypnuté.

**Implementovaný protokol `delta_v1`:** bežný prenos používa podpísaný rozdiel `stock_increment` a eviduje natívny skladový účinok objednávok každého e-shopu. Absolútne `stock` patrí iba do samostatnej potvrdenej údržby. Nový protokol ani jeho migrácie automaticky nepreberajú staré oprávnenie či nezapínajú zápisy. Živý test celého objednávkového cyklu ešte nie je potvrdený; [rozbor súbehu a plán pilotu](upgates-sync-readiness.md) odlišuje implementovaný kód od overenej prevádzky.

## Použitie

V nastaveniach dostupností a synchronizácie vyber e-shop. Interval, veľkosť pracovnej dávky a najvyšší prípustný vek načítania objednávok môžeš nastaviť pre sklad; prázdna hodnota v e-shope znamená zdedenie. Vlastná hodnota e-shopu má prednosť. Zmena intervalu sama neoprávňuje Hub odosielať zásoby.

| Nastavenie | Predvolene | Rozsah |
| --- | --- | --- |
| Interval medzi dokončenými priechodmi | 300 sekúnd | 60–86 400 sekúnd |
| Počet SKU v pracovnej dávke | 20 | 1–100 |
| Najvyšší vek objednávok | 900 sekúnd | 60–86 400 sekúnd |

Jeden priechod spracuje postupne všetky zalistované SKU daného e-shopu. Väčší katalóg sa rozdelí na dávky; nejde iba o prvých 20 produktov pri každom intervale. **Spustiť teraz** vytvorí ručný priechod bez zapnutia pravidelného plánovača. API môže ručný prenos zúžiť na vybrané SKU. Opakované stlačenie pri rovnakom rozpracovanom výbere zobrazí existujúci priechod. Iný výber počká, kým prebiehajúci priechod skončí.

Nezmenený kontrolný bod a nezmenená dostupnosť nepotrebujú vzdialené volanie medzi šesťhodinovými kontrolami. Zmenené položky a položky s kontrolou po termíne sa čítajú v skupinách presných kódov: samostatné produkty a varianty osobitne, s obmedzením počtu a dĺžky filtra. Pred zápisom sa opäť overí lokálny stav. Každá položka má najviac jeden PUT, bez okamžitého kontrolného GET a bez automatického opakovania. Chybná dávková odpoveď sa nerozšíri na pokusy po každom produkte. [Model spotreby a jeho hranice](upgates-sync-readiness.md#kapacitný-model-aktuálneho-kódu).

Pravidelný prenos vyžaduje výslovné nastavenie Hubu ako autority zásob. Pred jeho povolením musí obsluha overiť všetky predajné kanály spoločného skladu, vrátane pokladne BIKETREK:

- Natívne skladové účinky objednávok Upgates zostávajú zapnuté a sú výslovne potvrdené. Ostatné nezávislé integrácie zapisujúce zásoby musia byť vypnuté; ich účinky tento protokol nepozná.
- Historické a rozpracované objednávky sú zosúladené s Hubom.
- Na všetkých kanáloch spoločného skladu je zapnuté pravidelné načítanie objednávok a automatické lokálne rezervácie alebo výdaje. Objednávky vyžadujúce kontrolu treba vyriešiť.
- Každé prenášané SKU má v danom e-shope kontrolný bod vytvorený pri potvrdenej údržbe a platné podklady natívnych účinkov objednávok.
- Správca povolil serverový prepínač `STOCK_SYNC_WRITE_ENABLED=true`. Tento prepínač je nezávislý od `STOCK_PUBLICATION_WRITE_ENABLED` pre údržbu.

Chýbajúci kontrolný bod alebo nepodporovaný natívny účinok blokuje prenos; nie je dôvodom na návrat k absolútnemu zápisu. Zmena pripojenia, politiky skladu alebo zoznamu predajných kanálov vyžaduje nové potvrdenie autority. Zmena identity skladového listu zneplatní jeho kontrolný bod. Kým má e-shop skladovú autoritu alebo neistý skladový zápis, AI úpravy nesmú meniť jeho dostupnosť. Rozpracovaný alebo neistý AI zápis dostupnosti treba vyriešiť pred pridelením autority.

### Počiatočné zosúladenie a kontrolný bod

Pri prvom zapnutí alebo doloženom zosúladení musí obsluha reálne pozastaviť vzdialené objednávkové a ostatné skladové zmeny a otvoriť [údržbovú blokáciu](stock-publication.md). Lokálna blokácia sama predaj na e-shope nezastaví. Po dokončenom načítaní objednávok od začiatku údržby a ich skladovom spracovaní sa prípadný rozdiel množstva opraví samostatným údržbovým postupom. Vytvorenie kontrolného bodu iba načíta vzdialené údaje: všetky vybrané položky musia mať rovnaké vzdialené a lokálne voľné množstvo.

Kontrolný bod uloží presnú vzdialenú identitu, pripojenie, lokálne voľné množstvo, súčet natívnych účinkov daného e-shopu a dostupnosť. Má revíziu a nemenný audit naviazaný na údržbovú blokáciu. Nezakladá lokálny pohyb ani sám nemení vzdialenú zásobu. Bez úspešnej kontroly celej vybranej dávky nevznikne čiastočne platný výsledok.

Pre ďalší prenos platí `rozdiel = (lokálne množstvo − uložené lokálne množstvo) − (natívny účinok − uložený natívny účinok)`. Natívny odpočet má záporné znamienko. Rezervácia vlastnej Upgates objednávky sa preto do rovnakého e-shopu neposiela druhýkrát; príjem alebo zmena z druhého kanála sa prenesie ako skutočný rozdiel. Potvrdenie PUT posunie oba uložené počítadlá. Zmeny, ktoré vzniknú počas sieťového volania, zostanú pre ďalší priechod.

Natívna evidencia sa opiera o presné skladové identity a systémový typ stavu, nie o názov stavu či lokálnu obchodnú politiku. Neznáme identity, necelé množstvá, nepodporované zostavy, zmazané objednávky a nepreukázané storno či opätovné otvorenie sa nesmú odhadovať. Taký prípad vyžaduje kontrolu a podľa potreby údržbové zosúladenie. Inkrementálny zber zachytí aj nové zmeny objednávok vytvorených pred začiatkom lokálnej skladovej evidencie: dolná hranica dátumu vytvorenia je rok 1900, hranica poslednej zmeny zostáva inkrementálna. Historické zmeny sa tým automaticky nezaúčtujú do skladu.

Pravidelná synchronizácia pracuje s oneskorením načítania objednávok. Nejde o okamžitú spoločnú pokladňovú rezerváciu medzi oboma e-shopmi. Kontrola aktuálnosti znižuje riziko starej projekcie, ale nedokazuje, že tesne pred odoslaním nevznikla nová objednávka. Kratší interval načítania objednávok a prenosu znižuje túto medzeru. Hub kontroluje čerstvé dokončené načítanie, kurzor objednávok a chýbajúce alebo nedokončené spracovanie na všetkých zúčastnených kanáloch.

### Čakajúca objednávka a skladové spracovanie

Objednávka nemusí byť obchodne vybavená, aby sa mohli publikovať správne skladové zásoby. Už zaúčtovaná rezervácia s nekrytou časťou môže zostať v stave opakovanej kontroly a prenos pokračuje, ak Hub overí jej aktuálny skladový podklad. Nestačí samotný text chyby ani stav úlohy: musí súhlasiť aktuálna hlavička, identita objednávky, zdrojový hash a skladová revízia. Čítací prepočet rezervácie nesmie požadovať zmenu doterajších alokácií.

Po novom príjme alebo uvoľnení rezervácie inej objednávky môže pribudnúť tovar pre čakajúcu objednávku. Vtedy sa prenos pozastaví, kým spracovateľ doplní jej rezerváciu. Novoprijaté kusy sa tak nepublikujú ako voľné pred ich pridelením. Nedokončený výdaj pre nedostatok zásoby, chyba zdroja, zmazaná alebo konfliktná objednávka sa týmto pravidlom neobchádzajú.

Vratku alebo storno po pôvodnom výdaji možno výslovne uzavrieť cez skladovú výnimku v prehľade objednávok, ak sa vydaný obsah nezmenil a obsluha overila evidenciu. Uzavretie má vlastný nemenný audit a nevytvára príjem ani nový výdaj. Pri zmene podkladu, politiky alebo nastavení oprávnenia je potrebná nová kontrola. Postup je v [objednávkovej príručke](order-workflows.md#po-výdaji).

## Prenášané údaje

Vlastné voľné množstvo je fyzické množstvo mínus rezervácie mínus karanténa. Zásoby dodávateľa sa doň nepripočítavajú. Dostupnosť sa vyhodnotí pri každom priechode:

1. Známa vlastná voľná zásoba väčšia ako nula: `SKLADOM`.
2. Inak čerstvá kladná ponuka dodávateľa: text v jeho konfigurácii, predvolene `do 5 dní`.
3. Inak `overíme`.

Aj tovar s dostupnosťou `overíme` zostáva objednateľný. Bežný prenos posiela iba `stock_increment`, `availability` a `can_add_to_basket_yn` pre presný skladový list. Variant pod rodičom „xTrek“ v BIKETREK sa aktualizuje samostatne; nemenia sa ostatné varianty ani názov rodiča. Ceny, názvy, obrázky a aktivita produktu nie sú súčasťou tohto prenosu.

Chýbajúci alebo neoverený lokálny stav nie je nula. Taká položka sa preskočí s dôvodom a najprv vyžaduje riadne zaevidovanie počiatočného stavu alebo príjmu. Potvrdená počiatočná nulová bilancia z manuálnej inventúrnej korekcie je tiež platným podkladom: má vlastný audit, nevytvára fiktívny pohyb ani nákupnú vrstvu. Pri dodávateľskom produkte, ktorý fyzicky nemáme, možno v [oprave množstva](stock-adjustments.md) výslovne zdokumentovať prvé spočítanie **0 kusov**, bez fiktívneho pohybu. To platí aj pre produkt iba načítaný z katalógu dodávateľa. Platnosť ponúk dodávateľov sa kontroluje aj bez nového feedu, takže po uplynutí platnosti ďalší priechod prenesie `overíme`.

Úspešná zmenená položka znamená jednoznačné potvrdenie presného vzdialeného listu a posunutie kontrolného bodu, nie nemennosť vzdialeného počtu po objednávke. Nezmenená položka môže byť dokončená bez vzdialeného volania. Periodické čítanie zachytí rozdiel, ale samo ho neopraví absolútnym PUT. **Dokončený priechod** môže obsahovať preskočené položky; skontroluj počty a dôvody. Prenos nevytvára lokálny skladový pohyb.

## Neistý výsledok

Nákupné ceny majú samostatný [prenos z FIFO](fifo-purchase-costs.md) s vlastným povolením a intervalom. Bežný skladový prenos uvedený vyššie zostáva obmedzený na množstvo, dostupnosť a objednateľnosť.

Pri výpadku po začatí zápisu môže Upgates požiadavku ešte spracovať. Hub taký pokus automaticky neopakuje a blokuje ďalšie zápisy rovnakého SKU do rovnakého e-shopu. Ostatné SKU môžu pokračovať. Neistý pravidelný zápis bráni aj začatiu údržbového uzáveru dotknutého skladu.

Po overení, že pôvodná požiadavka definitívne skončila, treba doložiť, či bol konkrétny rozdiel vykonaný (`applied`) alebo nevykonaný (`not_applied`), a uložiť vysvetlenie dôkazu. Samotná zhoda aktuálneho množstva to pri súbežných objednávkach nedokazuje. Riešenie neposiela PUT ani nepoužíva kontrolné čítanie ako dôkaz vykonania. Pri doloženom vykonaní posunie kontrolný bod; pri nevykonaní ho ponechá pre ďalší riadny priechod. Ak výsledok nemožno doložiť, blokácia zostáva.

## Vývojársky kontrakt

Všetky cesty vyžadujú operátorský token a `Cache-Control: no-store`. Prefix API je `/stock-sync` (v prehliadači `/api/stock-sync`).

| Cesta | Vstup / výsledok |
| --- | --- |
| `GET /options?shop_code=biketrek` | Nastavenia skladu/e-shopu, efektívne hodnoty, blokujúce dôvody, posledných 20 priechodov. |
| `POST /warehouse` | `warehouse_code`, `expected_revision`, tri číselné nastavenia, `confirmed:true`. |
| `POST /configure` | `shop_code`, `expected_revision`, `enabled`, `authorized`, tri nullable výnimky; pri novej autorite aj `hub_is_stock_authority`, `external_stock_writers_disabled`, `native_order_stock_enabled`, `orders_reconciled`, všetky `true`; vždy `confirmed:true`. `external_stock_writers_disabled` znamená ostatné integrácie, nie natívne objednávky Upgates. |
| `POST /baseline` | `shop_code`, aktívne `hold_id`, 1–100 presných `skus`, `external_writers_paused:true`, `orders_reconciled:true`, `confirmed:true`; iba čítacie overenie a uloženie kontrolných bodov. |
| `POST /run` | `shop_code`, prípadne `skus` (1–100 presných jedinečných kódov), `confirmed:true`; vracia trvalé ID priechodu. |
| `GET /runs/{id}` | Súhrn a položky s požadovanými a overenými hodnotami; `offset`/`limit` (najviac 1 000), neisté položky sa zobrazia ako prvé. |
| `POST /items/{id}/resolve` | `confirmed:true`, `external_requests_finished:true`, pre `delta_v1` navyše `outcome:applied/not_applied` a `evidence` (8–1 000 znakov); bez nového vzdialeného zápisu. |

Migrácia `015_stock_sync.sql` založila predvoľby, politiky, priechody a položky. Aditívne `024_upgates_api_budget.sql`, `025_stock_native.sql` a `026_stock_delta.sql` dopĺňajú spoločný rozpočet, natívnu evidenciu a kontrolné body s protokolom `delta_v1`. Sú súčasťou explicitného spúšťača `python -m inventory_hub.migrate`; nemenia fyzické bilancie ani nezapínajú automatiku. Starý absolútny protokol sa nesmie použiť na nový pravidelný prenos.

Spoločný pracovník používa session advisory lock; sieťové volania prebiehajú po ukončení obchodnej DB transakcie. Každý PUT má najprv uložený stav `sending` a zmrazenú revíziu kontrolného bodu. Čiastočný unikátny index drží konflikt pre `(shop_id,sku)` v stavoch `sending` a `uncertain`. Po páde sa `sending` zmení na `uncertain`, nikdy na oprávnenie zopakovať PUT. Lokálne odmietnutie API rozpočtom s preukázaným `request_sent:false` nie je odoslaný ani nejasný zápis.

Identita aj vlastná bilancia sa znovu vyhodnotia po vzdialenom čítaní pred uložením zámeru. Obe lokálne kontroly a ich projekcie používajú poradie zámkov **identita → e-shop → sklad**, aby sa produktové prepojenie nezmenilo medzi overením objednávok a výpočtom odosielaného množstva. Zámky sa uvoľnia pred každým sieťovým volaním. Sklad sa počas sieťovej práce nemení na údržbový režim; následné lokálne zmeny sa premietnu ďalším priechodom. Otvorenie údržby a príprava pravidelného `sending` zámeru krátko zamykajú rovnaký riadok skladu, takže jeden postup nemôže prekryť druhý. HTTP 429 zohľadňuje uložený `Retry-After` aj spoločné limity objednávkových integrácií.

Transport zdieľa overovanie HTTPS, presnej identity, veľkosti odpovede a jednoznačného potvrdenia s údržbovým transportom. Nepoužíva presmerovania ani automatické opakovanie zápisu. Oficiálny kontrakt podporuje `stock_increment` a dostupnosť aj na variantoch cez [PUT products](https://docs.upgates.com/api-reference/produkty). Verejný kontrakt nedokumentuje idempotentný kľúč; explicitné riešenie neistoty preto zostáva nevyhnutné.

Všetky relácie z `UpgatesClient.from_shop` používajú spoločné riadenie API podľa normalizovaného HTTPS originu, vrátane objednávok, katalógu, cien a diagnostiky. PostgreSQL povoľuje jednu súbežnú požiadavku naprieč procesmi, uchováva zostatky hodinového limitu a dennej rezervy aj `Retry-After`. Samostatné spojenie je v autocommit režime a nezamyká obchodné riadky počas siete. Neznámy rozpočet dovolí jeden overovací pokus a potom dočasné odloženie; výpadok PostgreSQL neprepne na slabšiu ochranu. Výslovný legacy režim `USE_POSTGRES=false` má iba procesnú ochranu. Limiter nekupuje kvótu a neovláda cudzie integrácie.

Vypnutie plánovača necháva ručný prenos dostupný pri zachovanom oprávnení. Odobratie oprávnenia zastaví nové pokusy, ale nezruší už odoslanú požiadavku. Použitý protokol, natívne audity a kontrolné body sa pri oprave nemažú. Návrat k starému absolútnemu pravidelnému zapisovateľovi nie je bezpečný rollback; použi kompatibilnú doprednú opravu po vyriešení neistých pokusov.

Po pridelení skladovej autority dostupnosť nesmie súbežne prepisovať AI obsah. AI úprava môže pokračovať bez poľa dostupnosti; jeho riadenie patrí skladovej projekcii. Aktiváciu autority blokuje prebiehajúci/neistý AI zápis dostupnosti a nevyriešený skladový zápis blokuje takú AI zmenu aj po odvolaní autority.
