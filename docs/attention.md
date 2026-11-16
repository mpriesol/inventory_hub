# Spoločné upozornenia Hubu

Implementácia: blok **Vyžaduje pozornosť** na Prehľade, úplný zoznam a pravidlá
na `/settings/attention`. Prvým producentom sú výsledky dodávateľských dostupností.
Faktúry a ostatné služby sa pripájajú cez rovnaký kontrakt; automatické upozornenia
z faktúr zatiaľ nie sú zapojené.

## Ovládanie a pravidlá

- **Ignorovať tento problém** skryje aktuálnu epizódu. Ďalší neúspešný beh ju
  neodignoruje. Úspešné vyriešenie a následný nový výskyt vytvoria novú epizódu,
  ktorá sa znovu zobrazí. Ignorovanie možno aj ručne zrušiť.
- **Upozornenie vypnuté** je trvalé pravidlo pre konkrétny typ a zdroj alebo pre
  všetky zdroje daného typu. Globálne vypnutie má prednosť pred jednotlivými
  pravidlami. Nezastavuje spracovanie ani logovanie.
- **Zobraziť od výskytu** je počet samostatných problémových výsledkov v epizóde
  (1 až 100, predvolene 1). Opakované doručenie toho istého výsledku sa nepočíta.
  Konkrétne pravidlo prepisuje globálny počet. Zmeny platia pre celý Hub.
- Husky používa existujúce percento v rozšírených nastaveniach dodávateľa. Nie je
  tu druhá kópia percenta. Podlimitná dávka aktualizuje použiteľné položky a
  vyvolá upozornenie s aktuálnym počtom, porovnávacím počtom, percentom a minimom.
- Zoznam možno filtrovať podľa služby a stavu (aktívne, ignorované, potlačené
  pravidlom, vyriešené). Prehľad ukazuje najviac 5 položiek a celkový počet,
  úplný zoznam používa stránky po 20. Viditeľná stránka sa obnovuje každú minútu,
  mimo úpravy pravidla. Chyba čítania nezobrazí staré dáta ako nový výsledok.

Preklik **Otvoriť zdroj** smeruje na dotknutú službu. **Log** pri dodávateľovi
otvára celý konkrétny beh vrátane dostupných HTTP a validačných udalostí.
Filter `attention_id` v logu umožňuje samostatne nájsť životný cyklus problému.

## API a vnútorné služby

Všetky endpointy majú prefix `/api`, vyžadujú existujúci `operator_access` a
vracajú `Cache-Control: no-store`. Relácie používajú existujúcu ochranu CSRF;
samostatný autorizovaný klient môže použiť existujúci bearer prístup.

| Endpoint | Úloha |
| --- | --- |
| `GET /attention?state=active&source=supplier_availability&limit=20&offset=0` | Filtrované položky, presný počet a dostupné služby |
| `GET /attention/rules` | Predvolené a uložené pravidlá |
| `PUT /attention/rules/{kind}?scope=husky` | `expected_revision`, `enabled`, `min_occurrences`; `scope=*` je globálne pravidlo |
| `POST /attention/{id}/action` | `expected_revision`, `action=ignore` alebo `restore` |
| `POST /attention/events` | Doručenie výsledku producenta; kontrakt nižšie |

Príklad syntetickej udalosti (čísla nie sú produkčné údaje):

```json
{
  "kind": "supplier_availability.partial",
  "scope": "husky",
  "sequence": 100,
  "active": true,
  "details": {
    "run_id": 100,
    "total": 10,
    "accepted": 8,
    "rejected": 1,
    "unknown": 1,
    "warnings": ["supplier_availability_incomplete_feed"],
    "coverage": [{
      "target": "husky_products_feed", "rows": 10, "previous_rows": 100,
      "required_rows": 95, "min_coverage_percent": 95
    }]
  }
}
```

Identita problému je `(kind, scope)`. `sequence` musí monotónne rásť pre túto
identitu a označuje obchodný výsledok, nie pokus o jeho doručenie. Rovnaká
sekvencia a obsah vrátia `replayed=true`; odlišný obsah rovnakej sekvencie je
409 `attention_sequence_conflict`. Starší výsledok nemení novší stav.
Vyriešenie sa posiela s vyššou sekvenciou a `active=false`; samotné ignorovanie
nevyrieši príčinu. Pravidlá a obsluha používajú optimistický zámok revízie,
pri konflikte 409 `attention_changed` treba obnoviť stav.

Python služby volajú `services.attention.report(db, AttentionEvent(...))` v
transakcii s vlastným výsledkom alebo ho bezpečne projektujú z trvalého výsledku.
Vnútorné služby nemusia volať vlastné HTTP API. Log sa emituje až po commitnutí;
rollback nevytvorí falošný záznam o úspešnom upozornení.

Pre ďalšiu službu pridajte do `services/attention.py:DEFINITIONS` definíciu:

1. Stabilný `kind` (napr. `invoice.import_failed`), názov služby a závažnosť.
2. Striktný Pydantic model povolených metadát. Žiadne heslá, dokumenty,
   zákaznícke údaje, celé odpovede alebo voľný text výnimky.
3. Funkciu pre interný odkaz na konkrétnu službu/doklad; voliteľne nastavenia
   a filtrovaný diagnostický log. Odkaz nedodáva volajúci cez API.
4. Texty v `sk.json` a `en.json` (`attention.kinds`, `sources`, prípadne
   `messages`) a volanie pri probléme aj potvrdenom vyriešení.

Nová služba nepotrebuje novú tabuľku ani vlastný panel upozornení. Neznámy druh
udalosti, neplatné metadáta alebo externý odkaz sa odmietnu. Databázový test
overuje aj registráciu nezávislého syntetického fakturačného typu.

## Doručenie, úložisko a log

Migrácia `053_attention.sql` aditívne vytvára `hub_attention_rules` a
`hub_attention_items`. Jeden typ a zdroj majú jeden aktuálny záznam vrátane
počtu výskytov a poslednej epizódy; nejde o neobmedzený denník udalostí.
Vyriešený záznam ostáva dostupný. Rotácia diagnostického logu nestratí otvorený
problém. Existujúci súborový log zostáva obmedzený na 20 MB (4 × 5 MB).

Dodávateľský worker pred a po spracovaní projektuje posledný dokončený výsledok
aktuálne zvoleného feedu. ID behu je sekvencia. Po reštarte dobehne aj výsledok,
ktorý sa stihol uložiť pred pádom procesu. Pri prvom zapojení sa neprehráva celá
história: počty vychádzajú z projektovaných výsledkov. Nedokončený beh sa ešte
nevyhodnocuje; počas opakovaného načítania ostáva zdrojom upozornenia posledný
dokončený výsledok. Zlyhanie notifikácie nemení výsledok načítania; loguje bezpečný
typ/kód chyby a ďalší cyklus projekciu zopakuje. Neúspešné načítanie nevyrieši
predchádzajúcu neúplnosť, pretože nepotvrdilo kvalitu nových dát.

Logované udalosti: `attention_raised`, `attention_repeated`,
`attention_resolved`, `attention_ignored`, `attention_restored`,
`attention_rule_updated`. Majú ID, typ a zdroj problému; pri dodávateľovi aj beh
a počty. Kompletné metadáta udalosti sa do logu nekopírujú. Ide o spoločné
prevádzkové nastavenia pod existujúcim prístupom, nie osobné notifikácie/RBAC.

## Overenie a nasadenie

`test_attention_db.py` používa iba izolovaný localhost `*_catalog_test` a
overuje súbeh, opakovanie, ignorovanie, návrat problému, pravidlá, rollback,
filtre a opakovanú migráciu. `test_husky_db.py` overuje čiastočné prijatie,
zachovanie pôvodnej platnosti chýbajúcich položiek a projekciu výsledku.
`attention-ui.cjs` pokrýva pravidlá, ignorovanie/obnovenie, konflikt revízie,
chybu API a neskorú odpoveď po odhlásení. Vizuálny layout sa overuje osobitne.

Migrácia je zapojená do explicitného runnera a API obrazu. Neaktivuje skladovú
autoritu, externé zápisy ani nové dodávateľské načítanie. Pri návrate aplikácie
na predošlú verziu nové tabuľky ponechajte; starší kód ich ignoruje. Staršia
verzia Husky opäť používa blokovanie podlimitnej dávky, preto uprednostnite
opravu vpred. Nasadenie a živé overenie sa potvrdzujú samostatne cez workflow.
