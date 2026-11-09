# Príprava objednávok a webhookov na spustenie skladu

Toto je konkrétny návrh nastavenia BIKETREK a xTrek podľa čítania ich slovníkov
stavov 9. novembra 2026. Nie je to potvrdenie, že nastavenia už boli uložené alebo
že bol skladový prenos spustený. Dokument neobsahuje objednávky, zákaznícke údaje,
prihlasovacie údaje ani skladové množstvá. Predajné faktúry naďalej vystavuje a
spravuje Upgates; ich import do Hubu nie je podmienkou tohto spustenia.

## Význam stavov

Prijatá objednávka a úspešná platba rezervujú dostupné kusy. Aktívne vlastné stavy,
v ktorých sa čaká na vybavenie, doplnenie tovaru, zákazníka, dopravcu alebo osobný
odber, tiež rezervujú. Samotná platba alebo pripravenie na odber nepovoľuje výdaj.
Výdaj patrí odoslaniu alebo preukázanému odovzdaniu. Storno pred výdajom uvoľní
rezerváciu; po výdaji sa prípad rieši ručne a pôvodný výdaj zostáva zachovaný.

Systémové `PaymentInProcess` a `Unresolved` povoľujú v súčasnom kóde len `review`.
Prijatá objednávka už mohla mať rezerváciu; prechod na kontrolu ju automaticky
neruší. `PaymentFailed` a `PaymentCanceled` tiež zostávajú na kontrole, pokiaľ
objednávka skutočne neprejde do `Canceled`. Návraty, reklamácie a neprevzaté
zásielky sa automaticky nevydávajú ani nevracajú na sklad.

Význam **Predajňa / Kasa** v BIKETREK už vlastník potvrdil ako zaplatený a fyzicky
prevzatý tovar. Pri vlastnom stave xTrek **Vybavená objednávka** zatiaľ nie je
preukázané, že vždy znamená fyzické odovzdanie; návrh preto ponecháva `review`.
Toto nebráni uloženiu celej politiky ani príprave webhookov. Obsluha musí taký
prípad vyriešiť pred tým, než sa môže považovať za skladovo zaúčtovaný.

### BIKETREK

| Akcia | Presné ID a význam |
| --- | --- |
| `reserve` | 1 Prijatá; 3 Platba úspešná; 11 Pripravená na vybavenie; 13 Čaká na vyzdvihnutie dopravcom; 14 Čakáme na doplnenie tovaru; 15 Pripravená na osobný odber; 16 Dostupnosť overená, tovar objednaný; 17 SERVIS – Bicykel prijatý; 20 Tovar objednaný u dodávateľa; 22 SERVIS – pripravené k vyzdvihnutiu; 23 SERVIS – Čakáme na dodanie komponentov; 24 SERVIS – Čakáme na bicykel; 26 Čaká na vyjadrenie zákazníka; 27 Objednávka sa doriešuje |
| `issue` | 8 Odoslaná; 9 Predajňa / Kasa; 10 Doručená; 18 SERVIS – Bicykel odovzdaný; 19 Vyzdvihnutá na predajni |
| `cancel` | 2 Storno |
| `review` | 4 Platba zrušená; 5 Platba zlyhala; 6 Nedoriešená; 7 Platba prebieha; 12 Tovar bude vrátený; 25 Reklamácia |

Presný navrhovaný objekt `status_actions`:

```json
{
  "1": "reserve", "2": "cancel", "3": "reserve", "4": "review",
  "5": "review", "6": "review", "7": "review", "8": "issue",
  "9": "issue", "10": "issue", "11": "reserve", "12": "review",
  "13": "reserve", "14": "reserve", "15": "reserve", "16": "reserve",
  "17": "reserve", "18": "issue", "19": "issue", "20": "reserve",
  "22": "reserve", "23": "reserve", "24": "reserve", "25": "review",
  "26": "reserve", "27": "reserve"
}
```

### xTrek

| Akcia | Presné ID a význam |
| --- | --- |
| `reserve` | 1 Prijatá objednávka; 3 Objednávka zaplatená; 18 Pripravená na expedíciu; 20 Preverujeme dostupnosť tovaru; 21 Čakanie na doplnenie tovaru u dodávateľa |
| `issue` | 16 Odoslaná; 22 Doručené |
| `cancel` | 2 Stornovaná objednávka |
| `review` | 4 Platba zrušená; 5 Platba zlyhala; 6 Nedoriešená; 7 Platba prebieha; 19 Reklamácia; 23 Vybavená objednávka; 24 Nevyzdvihnutá / Neprebratá zásielka; 25 Tovar bude vrátený |

```json
{
  "1": "reserve", "2": "cancel", "3": "reserve", "4": "review",
  "5": "review", "6": "review", "7": "review", "16": "issue",
  "18": "reserve", "19": "review", "20": "reserve", "21": "reserve",
  "22": "issue", "23": "review", "24": "review", "25": "review"
}
```

Aktuálny `GET /api/order-stock/options?shop_code=...` musí potvrdiť, že sa ID,
ich význam a `allowed_actions` nezmenili. Na uloženie treba jeho čerstvý
`status_hash` a mapu obsahujúcu **všetky** aktuálne ID. Starý hash sa neobchádza.

## Príprava zberu bez automatických skladových pohybov

Pre každý e-shop vykonať kroky v tomto poradí. Predpokladom je správne HTTPS
Upgates pripojenie a existujúci spoločný sklad `hlavny-sklad`.

1. Načítať `/api/order-stock/options?shop_code=...`. Ak väzba na sklad chýba,
   uložiť `POST /api/order-stock/warehouse-assignment` s `shop_code`,
   `warehouse_code: "hlavny-sklad"` a čerstvým `expected_assignment_hash`.
2. Uložiť `POST /api/order-stock/configure` s `shop_code`, `warehouse_code`,
   čerstvým `status_hash`, príslušnou úplnou `status_actions` a `confirmed: true`.
   Prvé uloženie vytvorí `policy.starts_at` podľa času servera; mapovanie samo
   nezapína automatické rezervácie ani výdaje.
3. Uložiť `POST /api/order-collection/configure` s `shop_code`, `enabled: true`,
   `confirmed: true` a `expected_revision: null`, iba ak nastavenie ešte
   neexistuje. Pri existujúcom nastavení použiť jeho aktuálnu revíziu zo statusu.
   Zber začne načítavať hlavičky objednávok a zapisovať ich do inboxu.
4. Ako administrátor uložiť `POST /api/order-webhooks/configure` s `shop_code`,
   aktuálnym `expected_revision` (pri prvom uložení `null`), `enabled: true`,
   `confirmed: true`, `public_base_url: "https://hub.biketrek.sk/api"` a
   `catchup_interval_seconds: 3600`.
5. Overiť stav cez GET endpointy nižšie. `configure` už zaradí registráciu;
   bez príčiny netreba pridávať ďalší `POST /order-webhooks/register`.

Počas prípravy ostáva `mode: manual`, teda `processing_ready: false`, a pravidelný
skladový prenos ostáva vypnutý. Zber a webhooky môžu napriek tomu napĺňať inbox.
Registrácia webhookov je výslovný externý konfiguračný zápis: worker načíta
existujúce registrácie a podľa potreby vytvorí/opraví tri vlastné callbacky
`Orders.create`, `Orders.update`, `Orders.delete`. Nemení objednávky ani zásoby.

### Význam prepínačov a serverové predpoklady

`external_write_enabled: false` v odpovedi `/order-webhooks/status` je pevná
diagnostická hodnota. **Nie je to serverový zákaz registrácie webhookov.**
Registrácia nemá osobitný env prepínač a nekontroluje
`STOCK_SYNC_WRITE_ENABLED` ani `STOCK_PUBLICATION_WRITE_ENABLED`. Tieto dva
prepínače riadia samostatné skladové publikovanie.

Webhook worker sa spúšťa s PostgreSQL API. Potrebuje aktívny e-shop, zhodné API
pripojenie, zapnutý collector a pre povolenie callbackov zapisovateľný súkromný
`SUPPLIER_CREDENTIALS_ROOT` mimo verejných dát. Samostatné tajomstvo webhooku
vytvorí server; nepatrí do dokumentácie, shell výpisov ani Git. Upgates účet musí
mať práva pre webhook API a server musí byť dostupný na uvedenom verejnom základe.
Chyba práv, pripojenia, tajomstva alebo nejednoznačný výsledok registrácie zostane
vo statuse; neistá registrácia sa nesmie naslepo opakovať.

## Overenie pripravenosti a neskorší začiatok spracovania

### Časové filtre zberu a diagnostika odmietnutia

[Dokumentácia Upgates pre zoznam objednávok](https://docs.upgates.com/api-reference/objednavky)
uvádza `creation_time_from`, `creation_time_to` a `last_update_time_from` ako
`DateTime`; horná hranica vytvorenia je vrátane uvedeného času. [Schéma DateTime](https://docs.upgates.com/api-reference/~schemas)
odkazuje na ISO 8601, ale výslovne neurčuje podporu zlomkov sekundy ani osobitné
pravidlo pre interpretáciu časového pásma filtrov. Z príkladov odpovede sa
neodvodzuje zmena časového pásma požiadavky.

Zber preto posiela konzervatívny zápis v UTC s celými sekundami. Dolné hranice
zaokrúhli nadol; hornú hranicu so zlomkom sekundy zaokrúhli nahor na nasledujúcu
celú sekundu. Presné sekundové hranice nemení. Takto sa na hrane okna nestratí
objednávka a každý okraj sa môže prekryť o menej než sekundu. Overí celú odpoveď
proti skutočne odoslanému oknu a ponechá aj toto prekrytie, aby počty položiek,
stránkovanie, kontrola duplicít a poradie zmien zostali overiteľné. Záznam mimo
odoslaného okna alebo neplatný identifikátor, čas či metadáta stále odmietnu celú
stranu.

Čítací audit objednávok používa ten istý prevod dolnej hranice na celé sekundy
v UTC. Pri výbere posledných 7, 30 alebo 90 dní preto neodošle zlomok sekundy
a nestratí objednávku na dolnom okraji výberu. Zaokrúhľuje iba filter požiadavky;
presné časy zo zdrojovej odpovede zachováva. Audit naďalej číta jednu vybranú
stranu bez importu objednávok alebo skladového zápisu.

Zaokrúhlenie patrí iba GET požiadavke. Pôvodný `policy.starts_at`, ostatné
aktivačné prahy, uložené hranice priechodu a kurzory zostávajú presné. Ani časy
objednávok v odpovedi sa nezaokrúhľujú. Objednávka z prekrytia vytvorená tesne pred
pôvodným začiatkom politiky preto naďalej dostane historickú kontrolu
`stock_native_historical_change`; nestane sa automaticky novou objednávkou
oprávnenou na skladový výdaj.

HTTP 400 pri načítaní zoznamu má samostatný kód
`order_collection_upgates_http_400`, viditeľný cez existujúci stav zberu a históriu
priechodov. Do diagnostiky sa neprenáša telo odpovede, URL, prihlasovacie údaje ani
zákaznícke údaje. Ostatné chyby prístupu, obmedzenia a spojenia si ponechávajú
doterajšie kódy; nepridáva sa automatické opakovanie jedného HTTP volania.

Odmietnutá odpoveď má v tých istých poliach stavu a histórie pevný kód
`order_collection_invalid_response_` s jednou z nasledujúcich prípon:

| Prípona | Odmietnutá podmienka |
| --- | --- |
| `metadata` | Neplatný zoznam, metadáta strán alebo počty položiek |
| `identity` | Neplatný záznam, číslo, UUID, ID stavu alebo pôvod objednávky |
| `timestamp` | Neplatný čas vytvorenia alebo poslednej zmeny |
| `created_window` | Vytvorenie mimo odoslaného uzavretého intervalu |
| `update_before_creation` | Posledná zmena pred vytvorením objednávky |
| `updated_window` | Posledná zmena pred odoslanou dolnou hranicou zmien |
| `deleted_scope` | Príznak zmazania odporuje požadovanému výberu |
| `duplicate_identity` | Opakované číslo objednávky alebo UUID na jednej strane |
| `ordering` | Časy zmien na strane nie sú zoradené vzostupne |

Kód uvádza prvú zistenú kategóriu, neobsahuje hodnoty zo zdroja ani telo odpovede.
Staršie priechody si ponechávajú všeobecný `order_collection_invalid_response`.
Rozlíšenie nemení filtre, prijímané údaje, časové hranice ani opakovanie zberu;
chybná strana sa stále neuloží a neúspešný priechod neposunie kurzor úplnosti.

Predchádzajúce rýchle zlyhanie priechodu s nulou strán a všeobecným
`order_collection_source_unavailable` samo nepreukazuje odmietnutie zlomkov
sekundy. Táto úprava formátu a bezpečná diagnostika sú overené syntetickými
testami. Úspech na konkrétnom e-shope musí potvrdiť nový dokončený zber po nasadení;
nenahrádza ho úspešné načítanie jednej objednávky cez webhook.

### Kontrolné endpointy

| GET endpoint | Očakávaný dôkaz |
| --- | --- |
| `/api/order-collection/status?shop_code=...` | Collector zapnutý, pripojenie sa zhoduje, úspešný dokončený zber, bez aktuálnej chyby |
| `/api/order-webhooks/status?shop_code=...` | `enabled: true`, `registration_status: registered`, tri registrované udalosti, aktuálna kontrola registrácie, bez nevyriešeného registračného zámeru |
| `/api/order-collection/runs?shop_code=...` | Úspešný úplný `delta` priechod po registrácii; jednotlivý webhook neposúva kurzor úplnosti |
| `/api/order-collection/inbox?shop_code=...` | Nové hlavičky a zmeny sa ukladajú; nulový počet pri absencii nových objednávok sám nepotvrdzuje doručovanie |
| `/api/stock-settings/options?shop_code=...` | Počas prípravy `manual`; po samostatnej aktivácii správny sklad, revízia politiky, režim a `processing_ready: true` |
| `/api/order-processing/jobs?shop_code=...` | Po aktivácii známy výsledok každej spracovanej objednávky; výnimky a nekryté položky sú viditeľné |

Pri začatí spracovania načítať aktuálne revízie cez `stock-settings/options` a
uložiť `POST /api/stock-settings/shop` s `shop_code`, `expected_revision`,
`expected_warehouse_revision`, `overrides: {}`, `mode: "fulfill"`,
`fulfillment_confirmed: true`, `confirmed: true`. Ak sa majú výdaje riešiť ručne,
zvoliť `reserve`; samotná zmena režimu nie je potvrdením fyzických zásob.

**Aktivačné medzery sa musia vysporiadať.** Prvé nastavenie politiky vytvorí
`starts_at`; neskorší prechod `manual → reserve/fulfill` vytvorí nový
`automation_starts_at`. Vstup do `fulfill` navyše vytvorí `issue_starts_at`.
Všetky prahy sa porovnávajú s časom **vytvorenia** objednávky. Objednávky vzniknuté
medzi prípravou politiky a aktiváciou sa automaticky neprevezmú. Ani objednávky
vytvorené počas režimu `reserve` sa po neskoršom prepnutí na `fulfill` nezačnú
automaticky fyzicky vydávať. Pred skladovým baseline treba konkrétne otvorené a
historické prípady vyhodnotiť a zosúladiť; časy sa neposúvajú dozadu a staré
výdaje sa nevyrábajú.

Pravidelný prenos zásob je ďalší samostatný krok. Potrebuje pripravené oba kanály,
platné zaúčtovanie objednávok, explicitnú autoritu a správne baseline. Podrobnosti
sú v [objednávkovej automatike](order-automation.md) a [skladovom prenose](stock-sync.md).
