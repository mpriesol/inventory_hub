# Dávkové API pre zdieľané predajné väzby

Dávka zdieľa čerstvé čítanie natívnych produktov medzi viacerými kartami.
Každá karta má vlastné UUID, náhľad, hash, potvrdenie a uložený výsledok.
Potvrdenie mení iba lokálne predajné väzby; nemení vzdialené produkty ani sklad.

| Cesta | Účinok |
| --- | --- |
| `POST /api/shared-stock-sales/batch/preview` | Pripraví individuálne náhľady zo spoločného čerstvého čítania. |
| `POST /api/shared-stock-sales/batch/apply` | Použije nové čerstvé čítanie a potvrdzuje karty v poradí. |
| `GET /api/shared-stock-sales/{id}` | Vráti uložený náhľad alebo výsledok konkrétnej karty. |

## Vstupy a limity

Batch preview prijíma `items`, ktorých položky majú tvar individuálneho
preview requestu. Príklad používa výlučne syntetické identifikátory:

```json
{
  "items": [{
    "request_id": "68ada97c-e453-4915-80d2-a77ab210f3a3",
    "shop_code": "example",
    "product_id": 42,
    "identity_evidence": "Overené dva predajné záznamy toho istého celého výrobku.",
    "same_physical_item_confirmed": true,
    "base_piece_unit_confirmed": true,
    "sales": [
      {"code":"ITEM-A","parent_code":"FAMILY","is_variant":true,"native_product_id":800,"native_variant_id":101,"sale_to_stock_factor":1},
      {"code":"ITEM-B","parent_code":"FAMILY","is_variant":true,"native_product_id":800,"native_variant_id":102,"sale_to_stock_factor":1}
    ]
  }]
}
```

Jedna dávka obsahuje 1–50 rôznych kariet jedného e-shopu a najviac 100
predajných listov spolu. UUID, karty, kódy a natívne ciele sa nesmú prekrývať.
Každá karta zachováva všetky vlastné kontroly identity a použiteľnosti väzieb.
Existujúce individuálne requesty a ich hashy zostávajú kompatibilné.

Batch apply posiela presné individuálne hashy vrátené náhľadom:

```json
{
  "shop_code": "example",
  "items": [{
    "id": "68ada97c-e453-4915-80d2-a77ab210f3a3",
    "preview_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "confirmed": true
  }]
}
```

## Čerstvosť a spracovanie

Jedna fáza používa najviac osem natívnych GET vrátane prípadného overovania
zmazaných cieľov. Platí pôvodné stránkovanie, limity odpovedí a obmedzenie
frekvencie požiadaviek. Server automaticky neopakuje neúspešné čítanie.

Spoločné pozorovanie platí iba v aktuálnom requeste, najviac 60 sekúnd od
začiatku čítania. Čerstvosť sa znovu kontroluje pred potvrdením každej karty,
aj po čakaní na jej lokálne kontroly. Apply vždy číta natívne údaje znova.
Nový request ani pokračovanie po chybe nepoužíva staré pozorovanie.

Každá skupina samostatne overuje presné natívne identity vo svojom rozsahu
kód/rodič/typ. Apply porovná aj čiarové kódy a osi s náhľadom. Lokálni
vlastníci a všetky podmienky sa načítajú nanovo pre každú kartu. Úspech jednej
karty sa uloží samostatne; prvá chyba zastaví ďalšie spracovanie.

## Výsledky a obnova

Platná obálka vracia HTTP 200 s `status=completed|stopped` a usporiadaným
`items`. Klient musí vyhodnotiť telo odpovede, nielen HTTP status.

| `outcome` | Význam |
| --- | --- |
| `prepared` | Náhľad je uložený; `data` obsahuje individuálny hash. |
| `completed` | Potvrdenie má uložený individuálny výsledok. |
| `replayed` | Vrátený je uložený náhľad/výsledok; rozhoduje jeho `data.status`. |
| `failed` | `error` obsahuje stabilný kód a prípadné `known_unapplied=true`. |
| `outcome_unknown` | Výsledok je neistý; pokračuje sa individuálnym GET podľa UUID. |
| `not_attempted` | Položka sa po zastavení nespustila a nemá `known_unapplied`. |

Pri zastavení sa uvádza `failure_stage` a `stopped_at`. Chybu spoločného
čítania vysvetľuje aj `batch_error`. Pri 429 sa vráti `retry_after` aj hlavička
`Retry-After`. Skoršie dokončené karty zostanú dokončené; zvyšok sa nespustí.

`known_unapplied=true` sa uvádza iba pre individuálne definitívne zastavený
pokus po vysporiadaní jeho prípadného súbežného potvrdenia. Spoločná chyba
čítania takto zastaví iba prvú čakajúcu apply položku. Zvyšné náhľady
neexspirujú. `not_attempted` nehovorí nič o inom súbežnom requeste.

Neistý výsledok sa najprv overí individuálnym GET. Presné opakovanie už
dokončeného UUID vráti pôvodný výsledok. Exspirovaný náhľad sa neobnovuje
automaticky; nový náhľad potrebuje nové UUID.

`source_read` obsahuje ID čítania, hash rozsahu, časy a spoločný počet
`shared_native_requests`. Tento počet sa nesčítava raz za každú kartu.
Auditné údaje o apply sa ukladajú vo výsledku bez zmeny preview hashu.
