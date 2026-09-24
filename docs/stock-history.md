# História pohybov a prehľad skladu

## Pre obsluhu

**Sklad → História pohybov** (`/stock/movements`) číta existujúcu evidenciu Hubu. Odomyká sa rovnakým operátorským tokenom ako skladové spracovanie objednávok. Stránka nič nezaúčtuje, neopravuje ani neposiela do Upgates.

- Bez filtra SKU vidíte celý sklad; cez editor produktu otvoríte históriu presného kódu variantu.
- Vyhľadávanie hľadá v kóde, aktuálnom názve, doklade/objednávke a poznámke. Ďalšie filtre: sklad, typ a dátum pohybu.
- Množstvo so znamienkom je zmena. Pred/po sú fyzické množstvá uložené pri operácii, nie dnešná dostupnosť.
- Doklad príjmu otvára príslušnú reláciu. Ostatné operácie zobrazujú uloženú referenciu; chýbajúcu historickú poznámku ani doklad si systém nevymýšľa.
- Jednotkový náklad je pôvodná nákupná hodnota bez DPH v EUR. Neznámy náklad sa zobrazuje ako neznámy; nula je platná zaznamenaná cena. Rozhranie nepreceňuje staré pohyby podľa dnešného feedu.
- Rezervácia je zmena dostupnosti, nie fyzický pohyb. Samostatné opravy nákladov sa kontrolujú v detaile **Nákupné ceny**; nie sú novým fyzickým príjmom.

Dátumový filter používa UTC a zahŕňa celý koncový deň. Zobrazené časy používajú časové pásmo prehliadača. Pri stránkovaní sa zachová horná hranica ID, aby nové pohyby s vyšším ID neposúvali aktuálne stránky. **Načítať / obnoviť** vytvorí nový výber vrátane nových pohybov. Toto nie je dlhodobo uzamknutý databázový snapshot.

Úvodný prehľad číta rovnaké lokálne dáta: fyzické, rezervované, voľné a karanténne množstvo, nákupnú hodnotu a počet rozpracovaných objednávok spravovaných Hubom. Tento počet nie je počet všetkých otvorených objednávok v e-shopoch. Nízky stav znamená voľné množstvo na minime produktu alebo pod ním. Posledné udalosti sú skutočné pohyby; bez tokenu je namiesto nich odkaz na odomknutie. Neúspešné načítanie nie je prázdny sklad.

## Pre vývojárov

- `GET /api/stock-history/options`: sklady a enum typov, UTC filter.
- `GET /api/stock-history/movements`: `q`, presné `sku`, `warehouse_code`, `movement_type`, ISO `date_from/date_to`, `page`, `page_size` (25/50/100), voliteľný `snapshot_id`.
- Obe cesty používajú `operator_access`; úspech aj chyby vstupu/autorizácie majú `Cache-Control: no-store`.
- Služba iba číta `StockMovement` a súvisiace referencie. Množstvá a peniaze posiela ako desatinné reťazce, neznáme ceny ako `null`. `total_cost` je uložená hodnota pohybu; nepoužíva sa odhad množstvo × priemerná cena.
- `balance_before = balance_after - quantity`; názov je aktuálny efektívny názov z editoru, ostatné údaje sú pôvodný ledger. Pôvodná celá referencia samostatného príjmu je v `FifoReceipt.preview_data.source_reference`.
- `source=hub`, `upgates_calls=0` vyjadrujú skutočnú čítaciu cestu. Vyhľadávanie escapuje SQL wildcard znaky. Poradie je čas zostupne, potom ID zostupne.
- `snapshot_id` je iba horná hranica ID. Transakcia, ktorá pridelila menšie ID pred prvým čítaním a commitne neskôr, môže výber doplniť. Nejde o MVCC snapshot naprieč HTTP požiadavkami ani export pre účtovnú uzávierku.
- `GET /api/stock/summary` zachováva existujúce kľúče a pridáva `on_hand_total`, `available_total`, `open_managed_orders` (stav `pending/reserved`).

História nemá mutačné API. Oprava fyzickej histórie patrí do nového kompenzačného pohybu cez príslušný schválený proces. Tento balík nevytvára nové workflow vratiek ani inventúr.

Overenie: unit/API a izolované PostgreSQL testy v `test_stock_history*.py`, interakcie histórie a prehľadu v `frontend/tests/stock-history-ui.cjs`. jsdom neoveruje vizuálny layout v reálnom prehliadači. Konkrétny výsledok aktuálneho nasadenia patrí do `mvp-progress.md`.
