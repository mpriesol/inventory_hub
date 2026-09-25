# Potvrdený začiatok skladovej evidencie

Po migrácii `019_stock_tracking.sql` sú všetky existujúce stavy neoverené. Samotný historický pohyb, import z e-shopu ani stará testovacia príjemka nepotvrdzuje dnešnú fyzickú zásobu. Migrácia vytvorí prázdny register; nemení žiadnu bilanciu, nákupnú vrstvu ani pohyb.

## Postup pre obsluhu

- **Sklad → Potvrdené položky** je predvolený pohľad. Zahŕňa aj potvrdenú nulu. Filter **Fyzicky skladom** zobrazí len potvrdené položky s kladným fyzickým množstvom. Ďalšie filtre sprístupňujú celý katalóg a neoverené položky.
- Pri jednom dostupnom sklade sa sklad vyberie automaticky. Pri viacerých vyberte správny sklad pred kontrolou alebo potvrdením množstva.
- Existujúci tovar fyzicky spočítajte a potvrďte cez **Počiatočný stav** alebo **Opravu množstva** v detaile. Nulu možno výslovne potvrdiť cez Opravu množstva. Samotný náhľad nič nepotvrdzuje.
- Nový, skutočne prijatý tovar potvrďte bežným príjmom alebo dokumentovaným príjmom v Nákupných cenách. Ak položka nebola overená, začne sa nová evidencia prijatým množstvom. Starých 100 ks a nový príjem 3 ks znamená **3 potvrdené ks**, nie 103.
- Ďalšie príjmy a výdaje už pokračujú od potvrdeného stavu. Už potvrdené SKU nemožno znova nahrať počiatočnou dávkou; použite riadenú opravu množstva.
- **História pohybov → Historická evidencia** sprístupňuje staré importy, testy a ich uzavretie. Predvolene sa zobrazí nová evidencia. Prehľad skladu počíta len potvrdené zásoby a uvádza počet potvrdených položiek; nulový súčet neznamená dokončenú inventúru celého skladu.

## Dátový kontrakt

`stock_tracking` má jeden záznam pre `(product_id, warehouse_id)`: čas, operátora, zdroj operácie, posledné historické ID pohybu a FIFO vrstvy a snímku pôvodnej bilancie. Potvrdenie jedného skladu neoveruje rovnaké SKU v inom sklade.

Prvý potvrdený príjem alebo počet drží existujúci skladový a produktový zámok. Nenulový historický zostatok sa uzavrie novým kompenzačným pohybom `historical_stock_closure`, ktorý patrí do historickej evidencie. Nasledujúce nové pohyby a FIFO vrstvy patria do novej evidencie. Pôvodné pohyby, vrstvy aj výdajové alokácie zostávajú nezmenené. Celá zmena sa uloží v jednej transakcii s príjmom/inventúrou; neúspech sa vráti späť.

Opakovanie už dokončenej starej príjemky iba vráti jej uložený výsledok. Nepotvrdí staré kusy, nevytvorí nový príjem a neobnoví historickú zásobu.

Staré rezervácie, backordery alebo karanténa blokujú nový začiatok príslušného SKU/skladu. Musia sa najprv konkrétne zosúladiť; systém ich sám neruší. Nové rezervácie a výdaje vyžadujú potvrdený začiatok evidencie. Neoverená zásoba má neznáme množstvo, nie nulu: bežný aj údržbový prenos zásob, legacy push a prenos nákupnej ceny ju neodošlú. Vratka zo starého výdaja ani oprava starej vrstvy nesmie meniť novú evidenciu. Cieľový sklad vratky/presunu musí mať potvrdenú evidenciu položky.

API filtra skladu: `stock_scope=all|confirmed|unconfirmed|in_stock`. API filtra histórie: `tracking_scope=all|current|historical`. Staré API predvolené hodnoty zostávajú `all`; UI aktívneho skladu a posledné pohyby používajú nové potvrdené/current filtre.

## Nasadenie a obnova

Migrácia 019 je aditívna, opakovateľná, zabalená v API obraze a vykonaná po 018 pred reštartom API. Nezapína skladovú autoritu ani žiadny externý zápis. Obnova sa rieši kompatibilnou doprednou opravou; návrat k starému kódu bez registra by znovu ukazoval/importoval neoverené zásoby. Nemažte register, historické pohyby ani pôvodné FIFO vrstvy.

Izolované regresné testy pokrývajú prázdny register po migrácii, 100 + nový príjem 3 → 3, potvrdenú nulu, replay starého aj nového príjmu, zachovanie pôvodných vrstiev, blokáciu objednávok/cien/prenosu, filtre a návrat transakcie pri chybe. Výsledok ich behu a nasadenie sa evidujú v PR; samotný dokument nepotvrdzuje stav produkčnej DB.
