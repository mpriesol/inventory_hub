# FIFO pre BIKETREK a xTrek

Tento dokument opisuje lokálne skladové vrstvy a oceňovanie implementované v kóde. Nasadenie samo osebe nevytvorí príjem, nezmení historické pohyby a nezapne odosielanie zásob do e-shopov. Databázové a súbehové scenáre overuje izolované PostgreSQL CI; testy nepoužívajú produkčné e-shopy.

## Čo znamenajú vrstvy

Každý nový fyzický príjem v režime FIFO vytvorí samostatnú vrstvu. Výdaj spotrebúva dostupné vrstvy podľa času fyzického príjmu, pri rovnakom čase podľa ID vrstvy. Rezervácia drží iba množstvo; vrstvy spotrebuje až skutočný výdaj objednávky. Vrstva v karanténe sa do výdaja nezaradí.

Príklad: prijmeme 1 kus za 80 € a neskôr 1 kus za 90 €, bez DPH. Pred výdajom má zásoba hodnotu 170 € a priemer 85 €. Výdaj prvého kusa spotrebuje vrstvu za 80 €. Zostáva 1 kus za 90 €; zostávajúca hodnota aj priemer sú 90 €. Predajná cena sa pri výpočte nepoužíva.

Pre každé spojenie produktu a skladu je režim samostatný:

- `fifo`: existuje aktívny záznam FIFO a vrstvy zodpovedajú fyzickému množstvu.
- `legacy`: existujúci stav pokračuje v pôvodnom váženom priemere, kým obsluha nepotvrdí doložený prechod.
- `missing`: neexistuje fyzický skladový stav. Neznamená to overenú nulovú zásobu.

Prvý skutočný príjem alebo potvrdený počiatočný stav automaticky aktivuje FIFO iba na prázdnom spojení produktu a skladu bez starších fyzických pohybov. Historická nulová zásoba s pohybmi vyžaduje explicitný prechod. Existujúce objednávky a staré výdaje sa spätne neprepočítavajú.

## Prechod existujúcej zásoby

V detaile produktu vyber sklad a priprav prechod s presným SKU, časom fyzického overenia, menom obsluhy a odkazom na podklad. Zadaj doložené zostatkové vrstvy: množstvo, čas ich fyzického prijatia, nákupnú cenu bez DPH alebo výslovne neznámu/predbežnú cenu a zdrojový doklad.

Súčet vrstiev musí presne zodpovedať aktuálnemu fyzickému množstvu. Návrh neslúži na inventúrne opravy množstva. Nulový historický stav môže mať prázdny zoznam vrstiev. Obsluha osobitne potvrdí overené množstvá a doloženie stavu cien; neznáma cena zostáva výslovne neznáma.

Návrh platí 30 minút. Potvrdenie kontroluje zmrazený stav množstva, rezervácií, hodnoty, režimu a pohybov. Príjem, výdaj alebo zmena rezervácie medzi návrhom a potvrdením vyžaduje nový návrh. Opakovanie už dokončeného potvrdenia vracia pôvodný výsledok. Vzniká audit prechodu a nové zostatkové vrstvy, bez predstieraných historických príjmov alebo prepisovania starých pohybov.

## Príjem bez konečnej faktúry

Samostatný doložený príjem umožňuje prijať fyzický tovar podľa dodacieho listu aj bez konečnej ceny. Vyžaduje presný produkt, sklad, skutočné množstvo, čas fyzického prijatia, zdrojový doklad a obsluhu. Návrh a potvrdenie používajú rovnakú kontrolu zmeny skladu ako prechod.

Tento postup je dostupný pre aktívne FIFO alebo nový prázdny stav bez histórie. Pre existujúci starší stav najprv potvrď prechod. Bežný príjem z dodávateľskej faktúry si ponecháva kontrolu chýbajúcej ceny; neznáme ceny sa zadávajú cez samostatný doložený príjem.

Ceny sú v EUR bez DPH:

| Stav ceny | Význam | Zobrazenie |
| --- | --- | --- |
| `known` | Doložená nákupná cena | Známa hodnota |
| `provisional` | Doložený predbežný údaj | Číselný odhad, ocenenie nie je úplné |
| `unknown` | Cena ešte nie je známa | Cena je `NULL`, nie nula |

Ak zostáva vrstva s neznámou cenou, celková hodnota a priemer zásoby sú `NULL`. Prehľad samostatne ukáže známu hodnotu, predbežnú hodnotu a neocenené množstvo. Pri predbežných cenách môže ukázať odhad hodnoty a priemeru, s príznakom neúplného ocenenia. Skutočná doložená nulová cena je od neznámej ceny odlíšená.

Neznáma obstarávacia cena neblokuje fyzický výdaj aktívneho FIFO. Ak výdaj zasiahne neocenenú vrstvu, jeho úplný náklad zostáva `NULL`. Výdaj zo staršej známej vrstvy má známy náklad aj vtedy, keď neskoršia neocenená vrstva zostáva na sklade. Posledný skutočný príjem s neznámou cenou nepreberá cenu zo staršej faktúry.

## Vratka, karanténa a neskoršie doplnenie ceny

Oficiálna vratka sa viaže na konkrétny FIFO výdaj. Vyžaduje skutočné prijatie tovaru, číslo prípadu a jeho stav. Čiastočné opakované vratky sú povolené do výšky vydaného množstva. Vracajú sa posledné spotrebované alokácie ako prvé.

Vrátený tovar vytvorí novú vrstvu s časom fyzickej vratky. Zachová odkaz na pôvod ceny a vstúpi do karantény: zvýši fyzický stav, ale nezvýši predajné množstvo. Po overení dobrého stavu sa môže samostatným potvrdením uvoľniť v tom istom sklade alebo previesť do iného skladu. Čas vrstvy zostáva časom vratky; nezíska prednosť pôvodného historického príjmu. Cieľový sklad musí mať aktívne FIFO alebo byť nový a prázdny bez histórie.

Poškodenú vratku tento postup neuvoľní do predaja. Samostatný proces odpisu/likvidácie ešte nie je implementovaný. Historický výdaj bez FIFO alokácií nemožno týmto postupom automaticky vrátiť, pretože jeho pôvodné vrstvy nie sú doložené. Vratka nemení uzamknutý obsah vydanej objednávky.

Príklad bez faktúry: prijmeme 2 kusy s neznámou cenou, oba vydáme, 1 kus sa fyzicky vráti do karantény, overíme ho a uvoľníme alebo prevedieme. Keď príde faktúra, oprava ceny pôvodnej vrstvy sa premietne do aktuálneho ocenenia zostávajúcej zásoby, všetkých jej odvodených vrstiev a aktuálnych nákladov výdajov. Pôvodné nákladové snímky výdajov a vratiek zostanú zachované.

Prehľad aktuálneho čistého nákladu odpočítava vrátené množstvo. Rovnaký kus sa po vratke a opätovnom predaji nemá počítať do nákladu dvakrát. Nejde o výpočet čistého zisku: predajné ceny, poplatky, dane a ostatné náklady tento prehľad nedopĺňa.

## Presnosť a prevádzkové obmedzenia

Množstvá vrstiev majú najviac tri desatinné miesta; jednotkové náklady a hodnoty štyri. Používa sa desatinná aritmetika a `ROUND_HALF_UP`. Čiastočný výdaj vrstvy preberá rozdiel jej hodnoty pred a po výdaji, čím zachová zostávajúci zaokrúhlený zostatok. Hodnoty mimo rozsahu databázového typu sa odmietnu.

Doložený príjem a vrstvy prechodu podporujú zlomkové množstvá. Existujúce spracovanie objednávok a jeho vratky naďalej vyžadujú celé kusy; všeobecný model merných jednotiek a predaj na metre tým nie je dokončený. Ak celý vydaný kus vznikol spotrebou zlomkov z viacerých vrstiev, jeho automatická vratka sa odmietne, keď by vyžadovala zlomkový vrátený fragment. Zlomkové jednotky pri vratke a ich samostatné uvoľnenie patria ďalšej etape; systém namiesto tichej zmeny hodnoty vyžaduje riešenie mimo tohto potvrdeného toku.

Všetky fyzické zmeny, prechody a opravy cien rešpektujú trvalú blokáciu skladu pri údržbe publikovania. FIFO samo nevolá Upgates, nezapisuje do fronty synchronizácie a nezapína živé odosielanie zásob.

## Technical contract

The application runs migration `011_fifo.sql` before starting code that uses FIFO or quarantine. The migration adds FIFO state, layers, immutable issue-cost snapshots, documented cutover/receipt drafts, and return/release/cost-revision audits. It makes balance valuation and movement post-valuation nullable; old stored values and immutable movement rows are preserved.

A `fifo_states` row selects FIFO for exactly one product and warehouse. Without it, existing weighted-average accounting remains explicitly legacy. Receipt layers sort by `physical_received_at, id`. `root_cost_layer_id` links returns and transfers to their price origin. `FifoState.revision` fences planned operations against physical or cost changes. Reservations never consume layers.

`fifo_allocations` links every consumed layer slice to its immutable sale movement. `quantity_before` records the rounding basis. `*_at_issue` values are historical snapshots; `*_current` values may follow documented root cost revisions. `returned_quantity` is audited by return records. Reports calculate net consumption after returns; gross allocation totals must not be added to subsequent resale costs without reversing returned quantities.

Internal `services.fifo` functions require the caller's warehouse admission gate and locked balance; they never commit. Public receipt and cutover functions own commit/replay. Stock movement values are final before the first INSERT because the database rejects UPDATE even within the same transaction. No FIFO operation holds database locks across network calls; it makes no remote calls.

Read APIs under `/api/fifo`:

- `GET /options`: active warehouses.
- `GET /stock?product_id=...&warehouse_code=...&limit=50&offset=0`: balance, valuation status, global next available layer, paginated layers and a snapshot hash.
- `GET /history` with the same filters: immutable movements including sale IDs.
- `GET /movements/{id}/allocations`: original/current issue allocations.
- `GET /issues/{id}/return-options` and `GET /costs/{root_layer_id}`: current return and root-cost audit information.

Controlled writes:

- `POST /cutovers/preview`, `POST /cutovers/{id}/apply`, `GET /cutovers/{id}`.
- `POST /receipts/preview`, `POST /receipts/{id}/apply`, `GET /receipts/{id}`.
- `POST /returns`, `POST /quarantine/release`, `POST /costs/revise`.

Amounts are exact decimal strings or `null`. Requests require operator access; actions use literal boolean assertions and durable request IDs. Reusing an ID with different input is rejected. Authentication occurs before database access, and responses use `Cache-Control: no-store`. Validation errors do not echo source documents.

Migration 011 also changes the generated available quantity to `on_hand - reserved - quarantined`. PostgreSQL 16 requires recreating the derived column, its known inventory/alert views and its low-stock index. There is no `CASCADE`: unknown external dependencies stop migration for review instead of being silently removed. Historical source quantities remain untouched.

Rollback is not dropping FIFO tables or restoring old valuation code. Once unknown costs, FIFO issues or quarantined stock exist, an older application can misread NULL values or publish unavailable quantities. Preserve the ledger and audits, stop affected operations, and prepare a forward correction or restore a verified complete backup under separate operational authorization. Migration alone does not activate existing stock, create initial inventory, or change publication permissions.
