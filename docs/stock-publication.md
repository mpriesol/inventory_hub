# Riadené odosielanie vlastnej zásoby do Upgates

BIKETREK a xTrek môžu pre vybrané skladové položky pripraviť vlastné voľné množstvo na odoslanie do Upgates. Ide o osobitnú operáciu po kontrole, nie o pravidelnú synchronizáciu počas predaja. Nasadenie tejto funkcie samo nepovoľuje živé zápisy.

Bežný plánovaný prenos s vlastným oprávnením a bez údržbového uzáveru opisuje [pravidelná synchronizácia](stock-sync.md). Jeho aktivácia vyžaduje výlučnú autoritu Hubu a vypnuté samostatné skladové zápisy v predajných kanáloch.

Tento dokument opisuje kontrakt tejto implementácie. Výsledky CI, nasadenie a prípadné neskoršie živé overenie treba uvádzať samostatne. Bez živého overenia nemožno tvrdiť, že zásoby konkrétneho e-shopu boli zmenené.

## Používateľský postup

Otvoriť ho možno cez **Sklad → Odoslať sklad do e-shopu** (`/stock/publication`). Uložené nastavenie a výsledky sa načítavajú výslovne; zadanie tokenu samo neodosiela požiadavku na zmenu zásob.

Odosiela sa **vlastná voľná zásoba = fyzická zásoba − rezervácie − karanténa** v sklade priradenom potvrdenej politike e-shopu. Dodávateľské množstvá sa nepripočítavajú. Zápis nemení ceny, texty, viditeľnosť ani lokálne skladové pohyby.

Vyberajú sa konkrétne SKU. Variant pod pokladňovým rodičom „xTrek“ v BIKETREK sa odosiela samostatne podľa svojho kódu; ostatné tisíce variantov tohto rodiča sa nepridávajú. Neznáma bilancia alebo nejednoznačné mapovanie operáciu blokujú. Neznámy stav sa nesmie poslať ako nula.

Pred odosielaním je potrebné údržbové okno:

1. Zastav všetky externé cesty, ktoré čerpajú zo spoločného uzatváraného skladu: webové objednávky, pokladňu, ručné zásahy v administrácii a ďalšie integrácie. Pri spoločnom sklade sa to týka BIKETREK aj xTrek, aj keď odosielaš iba do jedného e-shopu. Hub tieto cesty formulárom automaticky nezastaví.
2. Skontroluj a spracuj už existujúce objednávky vrátane tých, ktoré vznikli pred zapnutím automatickej evidencie. Prázdny zoznam čakajúcich úloh nedokazuje úplnosť objednávok.
3. Výslovne potvrď splnenie týchto podmienok a aktivuj lokálny uzáver skladu. Ten bráni aj ručným príjmom, počiatočnému stavu a ručným skladovým operáciám počas odosielania.
4. Vytvor náhľad iba pre požadované SKU. Porovnaj množstvo v Hube, množstvo v e-shope a presnú identitu cieľového produktu alebo variantu.
5. Potvrď aktuálny náhľad. Výsledok sleduj podľa uloženého ID; obnovenie stránky ani stratená odpoveď nie sú dôvodom na opätovné odoslanie.
6. Po kontrole všetkých výsledkov výslovne uvoľni lokálny uzáver. Až potom obnov pozastavené externé cesty predaja a zápisu.

Bežná pauza automatických objednávok v nastavení skladu na tento účel nestačí. Tá necháva ručne potvrdené operácie dostupné. Publikačný uzáver má širší účinok a zostáva aktívny aj po zatvorení stránky alebo reštarte aplikácie.

Oprávnenie odosielať sa nastavuje osobitne pre konkrétny e-shop. Okrem neho musí byť povolený aj serverový prepínač `STOCK_PUBLICATION_WRITE_ENABLED`; predvolene je vypnutý. Zapnutie automatických rezervácií alebo výdajov samo nepovoľuje odosielanie zásob.

Limity sa nastavujú na `/settings/stock`, rovnako ako ostatné skladové predvoľby a výnimky e-shopu:

| Nastavenie | Predvolené | Rozsah |
| --- | --- | --- |
| `publication_batch_size` — počet vybraných SKU v dávke | 20 | 1–100 |
| `publication_preview_minutes` — platnosť náhľadu | 15 minút | 5–60 minút |

Zmena zdedenej hodnoty sa týka e-shopov bez vlastnej výnimky. Platnosť náhľadu nie je platnosť uzáveru: vypršaný náhľad sklad automaticky neodomkne.

Jedna zmenená položka potrebuje viac API volaní: načítanie náhľadu, nové overenie pred zápisom, jeden zápis a následné čítanie. Veľkosť dávky nie je počet API volaní. Ak sa vzdialené množstvo pri kontrole už zhoduje s požadovaným, zápis sa vynechá a uloží sa overená zhoda.

## Neistý výsledok a obnova

Každá vybraná položka má samostatný pokus o odoslanie. Výpadok spojenia môže nastať po tom, čo Upgates zápis prijal. Hub preto po neistej odpovedi zápis automaticky neopakuje.

Čítacie overenie ukáže aktuálne vzdialené množstvo. Samotné overenie nezruší neistotu o pôvodnej požiadavke a neuvoľní sklad. Na vyriešenie neistého pokusu musí obsluha výslovne potvrdiť, že pôvodné externé požiadavky už skončili. Nasleduje nové čítanie: zhoda potvrdí overené množstvo, nezhoda označí pokus ako neúspešný. Tento postup neposiela ďalší zápis.

Uzáver nemožno uvoľniť, kým zostávajú zaradené, prebiehajúce alebo neisté pokusy. Neexistuje automatické odomknutie po uplynutí času. Pri probléme ponechaj údržbové okno, skontroluj uložený stav a vyrieš pôvodné pokusy; nevytváraj náhradné odoslanie na obídenie blokovania.

Vypnutie odosielania ani návrat staršej verzie aplikácie nevrátia vzdialené množstvá na pôvodnú hodnotu. Pôvodná zásoba môže byť po obnovení predaja už neaktuálna; automatická opačná korekcia sa nevykonáva.

## Technické hranice Upgates

Samostatný produkt sa číta cez `GET /api/v2/products/{code}/simple`. Variant sa číta cez `GET /api/v2/products/variants` s filtrom `variant_codes`; odpoveď obsahuje ploché `variants[]` s identitou variantu aj rodiča. Filter `codes` označuje rodičovské produkty. Úplnosť odpovede, presná identita a číselné množstvo sa overujú; chýbajúce údaje neznamenajú nulu. [Zoznamy produktov a variantov](https://docs.upgates.com/api-reference/produkty-seznamy)

Úzky zápis používa `PUT /api/v2/products`:

```json
{"products":[{"code":"SAMOSTATNE-SKU","stock":7}]}
```

```json
{"products":[{"code":"RODIC","variants":[{"code":"SKU-VARIANTU","stock":7}]}]}
```

`stock` je absolútne množstvo. Relatívny `stock_increment` nie je povolený. API odpovedá príznakom `updated_yn` a správami samostatne na produkte aj variante; úspech rodiča ani samotné HTTP 200 nepotvrdzujú úspech vybraného variantu. Zdokumentovaný limit je 100 položiek a najviac 100 variantov na produkt. Táto implementácia odosiela každý vybraný skladový list samostatne. [Produktové API](https://docs.upgates.com/api-reference/produkty)

Upgates odporúča pri aktualizácii odosielať iba zmenené atribúty. Žiadne ceny, popisy, nastavenia dostupnosti, aktivita, prázdne rodiny alebo súrodenecké varianty sa do skladového payloadu nepridávajú. [Odporúčania pre API](https://docs.upgates.com/api/best-practices)

V preskúmanej verejnej dokumentácii nie je uvedené podmienené porovnanie verzie pri zápise, `If-Match`/ETag, idempotentný kľúč ani atómový skladový batch. Čítanie tesne pred zápisom preto nebráni predaju medzi týmito dvoma požiadavkami. Údržbové potvrdenie je prevádzkový predpoklad, nie vzdialený zámok overený API. Z tohto dôvodu táto etapa nepovoľuje pravidelné absolútne prepisovanie počas aktívneho predaja. [Produktové API](https://docs.upgates.com/api-reference/produkty), [základný kontrakt API](https://docs.upgates.com/api/intro)

## Zámer, uzáver a transport

Publikačný zámer uchováva presný cieľ, vybrané položky, lokálny podklad, vzdialené pozorovanie, náhľad a potvrdenie. Fingerprint identifikuje skutočne použitú HTTPS adresu a API používateľa; kľúče sa do auditu ani verejných odpovedí neukladajú.

Lokálny uzáver je trvalý a kontrolujú ho všetky podporované zapisovače skladového ledgeru. Zablokovanie musí byť overené pred zmenou rezervácií, bilancií a pohybov, vrátane prvého príjmu bez existujúcej bilancie. Sieťové volania neprebiehajú pod databázovými zámkami. Samotný optimistický prepočet tesne pred sieťou by neposkytoval rovnakú ochranu.

Stav `sending` sa uloží pred vstupom do transportu. Od tohto bodu pád procesu, časový limit, zrušenie úlohy alebo nečitateľná odpoveď nesmú zámer vrátiť do stavu umožňujúceho nový PUT. Zrušenie korutiny nezaručuje zastavenie požiadavky bežiacej vo vlákne; neistý zámer preto zachováva uzáver.

Transport používa overené HTTPS, zakázané presmerovania, časové limity spojenia a čítania, obmedzenú veľkosť odpovede, presný zoznam povolených polí a žiadne automatické opakovanie zápisu. Nespracúva vzdialené chybové texty ako dôveryhodný pokyn a nevystavuje surové odpovede operátorovi. Vybraný produkt/variant sa overuje podľa kódu aj natívnych ID.

HTTP 429 zachováva bezpečnú prestávku. `Retry-After` môže byť dátum v GMT; súbežnosť Upgates je štandardne obmedzená na tri požiadavky a môže byť zdieľaná viacerými prístupmi. Obnova nesmie opakovaným stlačením tlačidla prestávku obísť. [Limity API](https://docs.upgates.com/api/rate-limiting)

## API a trvalá evidencia

Backendové cesty majú prefix `/stock-publication`; frontend používa `/api/stock-publication`. Všetky používateľské cesty vyžadujú operátorský prístup a odpovede sa necachujú. Potvrdzovacie príznaky prijímajú iba skutočné booleovské `true`, nie reťazce alebo čísla.

| Cesta | Účel a podstatný vstup |
| --- | --- |
| `GET /options?shop_code=...` | Sklad, politika publikovania, aktívny uzáver, limity a stav serverového povolenia. |
| `POST /configure` | `{shop_code, expected_revision, enabled, confirmed:true}`; samostatné oprávnenie e-shopu s ochranou revízie. |
| `POST /holds` | `{shop_code, external_writers_paused:true, orders_reconciled:true, confirmed:true}`; uloženie údržbových tvrdení a aktivácia uzáveru. |
| `POST /holds/{id}/release` | `{maintenance_completed:true, confirmed:true}`; kontrolované ukončenie uzáveru po vyriešení pokusov. |
| `POST /preview` | `{request_id, shop_code, skus}`; UUID požiadavky a explicitný zoznam SKU, bez vzdialeného zápisu. |
| `GET /batches?shop_code=...` a `GET /batches/{id}` | Uložené dávky a detail položiek pre čítaciu obnovu po neistej odpovedi. |
| `POST /batches/{id}/submit` | `{preview_hash, confirmed:true}`; potvrdenie presného aktuálneho náhľadu. |
| `POST /batches/{id}/cancel` | `{confirmed:true}`; zrušenie iba tam, kde stav dávky ešte bezpečne umožňuje zastavenie. |
| `POST /batches/{id}/verify` | `{confirmed:true}`; čítacie pozorovanie vzdialenej zásoby bez druhého PUT. |
| `POST /batches/{id}/resolve` | `{external_requests_finished:true, confirmed:true}`; výslovné vyriešenie neistoty s novým čítaním. |

`stock_publication_policies` drží oprávnenie, revíziu a cieľ e-shopu. `stock_publication_holds` drží aktívny uzáver skladu a tvrdenia obsluhy; čiastkový unikátny index povoľuje iba jeden aktívny uzáver na sklad. `stock_publication_batches` uchováva nemenný náhľad, jeho hash, časy a výsledok. `stock_publication_items` uchováva SKU, vzdialenú identitu, požadované a pozorované množstvá, identitu pokusu, potvrdenie API a prípadné výslovné vyriešenie. Nullable JSONB výsledky používajú SQL NULL, nie JSON `null`.

| Stav dávky | Význam |
| --- | --- |
| `prepared` | Náhľad čaká na potvrdenie; nevykonal vzdialený zápis. |
| `queued` | Potvrdená dávka čaká na spracovateľa. |
| `running` | Spracovateľ kontroluje a postupne vybavuje vybrané položky. |
| `completed` | Všetky položky majú overené požadované množstvo; uzáver zostáva aktívny. |
| `blocked` | Spracovanie sa zastavilo; predchádzajúce úspešné položky sa nevracajú späť. |
| `cancelled` | Dávka bola bezpečne zastavená bez opakovania alebo obrátenia zápisu. |
| `uncertain` | Aspoň jeden pokus vyžaduje výslovné vyriešenie; sklad zostáva uzavretý. |

Položky rozlišujú `prepared`, `sending`, `verified`, `failed`, `uncertain`, `cancelled`. Stav `verified` môže znamenať aj zhodu bez PUT; existencia `attempt_id` rozlišuje založený pokus. Po chybe sa zvyšné ešte neposlané položky zrušia. Nový pokus nie je opätovným spustením starej dávky: vyžaduje nový náhľad a nové potvrdenie po vyriešení pôvodného stavu.

Worker spracúva iba explicitne zaradené dávky. Používa samostatný session advisory lock `691432114` na spojení bez otvorenej dátovej transakcie počas siete. Po prerušení zmení rozpracovaný `sending` na `uncertain`; dávku bez takéhoto zámeru zablokuje. Nevytvára periodické publikácie a nikdy nemení neistý pokus späť na odosielateľný.

Založenie a potvrdenie náhľadu zamyká v poradí globálna identita → Shop → objednávková politika → Warehouse → nastavenia/uzáver → dávka/položka. Uvoľnenie a čítacia obnova nepotrebujú platnú aktuálnu politiku na zistenie pôvodného skladu: zamykajú Warehouse → uzáver → dávku. Po sieťovom čítaní sa znovu overuje platnosť podkladu pred uložením výsledku; žiadna neistá odpoveď sa nesmie presmerovať na nový e-shop.

Migrácia `010_stock_publication.sql` je aditívna. Neaktivuje oprávnenie e-shopov, nemení fyzické zásoby a musí sa vykonať po `009` pred spustením kódu, ktorý kontroluje uzávery. Staršie skladové testovacie schémy potrebujú novú tabuľku uzáverov tiež, pretože ju číta spoločný vstup do skladového zápisu.

## Overenie a prevádzkové obmedzenia

Pred merge treba v izolovanom prostredí overiť najmä vypnutý serverový zápis, presný rozsah SKU, variant pod veľkým rodičom, zmenu identity alebo zásoby po náhľade, zablokovanie všetkých lokálnych zapisovačov, trvalý zámer pred sieťou, čiastočné/nejednoznačné odpovede, pád pred a po odoslaní, čítaciu obnovu bez druhého PUT a nemožnosť uvoľniť nevyriešený uzáver.

Publikačné tabuľky a audit sa pri návrate verzie nemažú. Verzia, ktorá publikačný uzáver nepozná, nesmie počas aktívneho uzáveru obnoviť zapisovanie do skladu. Najprv treba vyriešiť externé pokusy, skontrolovať stav a riadne ukončiť údržbu.

Súvisiace pravidlá: [objednávkové postupy](order-workflows.md), [automatické rezervácie a výdaje](order-automation.md), [centrálny sklad](central-stock.md).
