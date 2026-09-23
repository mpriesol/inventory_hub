# BIKETREK / xTrek — centrálny sklad

## Schválené obchodné pravidlá

Upresnenie vlastníka z 23. 9. 2026 je záväzné pre nasledujúce implementačné kroky:

- **Manuálne objednávkové položky bez skladovej identity** môžu byť servisná práca aj fyzický diel. Zostávajú mimo skladovej evidencie; neodpisujú zásobu, nevytvárajú produkt a neblokujú spracovanie mapovaných riadkov. Historické objednávky sa spätne neopravujú. Nový bežný predaj používa naskladnené produkty. Kódovaná položka s chybným alebo nejednoznačným párovaním je odlišná situácia a vyžaduje kontrolu; nesmie sa automaticky vyhlásiť za manuálnu výnimku.
- **Čiastočné odovzdanie objednávky sa nepoužíva.** Pred výdajom možno položku pridať, odstrániť, vymeniť alebo zmeniť množstvo; rezervácie sa upravia rozdielom. Stav **Odoslaná / Vyzdvihnutá** znamená jeden fyzický výdaj aktuálnych skladovaných riadkov a uzamknutie jeho obsahu. Opakovaný import nesmie vytvoriť ďalší výdaj. Platba webovej objednávky sama nestačí. Priamy dokončený pokladňový predaj sa eviduje raz podľa vlastnej identity objednávky.
- **Po výdaji** sa zmena rieši oficiálnou vratkou, reklamáciou alebo neprevzatím zásielky. Samotná zmena názvu stavu, zmazanie riadka ani storno nesmie spätne prepísať výdaj. Príjem vráteného tovaru nastane až pri potvrdenom fyzickom návrate, s odkazom na pôvodný výdaj.
- **„overíme“ je objednateľné.** Neznáma dostupnosť nesľubuje termín; sama nesmie skryť produkt ani zakázať košík. Ručné vypnutie produktu a povinná kontrola nového importu naďalej platia.
- **Dodacia lehota patrí dodávateľovi.** Každý dodávateľ má vlastnú konfiguráciu. Ak nemá vyplnený text pre dostupný tovar, použije sa `do 5 dní`; predvolený text pre neznámu dostupnosť je `overíme`. Dodávateľská zásoba nikdy nezvyšuje vlastný fyzický sklad.

Objednávkové pravidlá sú tu špecifikácia ďalšej etapy. Tento prvý balík ešte nezapína rezervácie, výdaje objednávok, FIFO ani automatické odosielanie vlastných zásob do e-shopov.

## Prvý implementačný balík

### Produktový import bez fyzickej zásoby

`POST /shops/{shop}/upgates/products/import` načíta produkty, varianty, mapovania a snímku údajov e-shopu. Nevytvára `stock_balances` ani `stock_movements` a nepotrebuje predvolený sklad. Snímka `shop_stock` nie je vlastná zásoba; predajná cena e-shopu nie je obstarávacia cena.

Parameter `include_stock` môže chýbať alebo byť JSON `false`. Iná hodnota vracia HTTP 400 ešte pred volaním e-shopu. Odpoveď zachováva kompatibilné `stock_initialized: 0`. UI odstránilo voľbu inicializácie zásoby. Existujúce historické pohyby, množstvá a náklady sa nemenia.

Počiatočný stav potrebuje samostatný kontrolovaný import s fyzickým množstvom, identitou, pôvodom a obstarávacou cenou. Táto cesta zatiaľ nie je implementovaná. Párovanie rozdielnych kódov oboch e-shopov a oddelenie ich obsahu od spoločného produktu sú ďalší krok; bežný pull zatiaľ používa existujúce SKU párovanie.

### Bezpečnejší príjem podľa faktúry

Mutácie príjmovej relácie sa serializujú databázovým zámkom. Finalizácia zamyká dotknuté bilancie v stabilnom poradí, vrátane súbehu dvoch prvých príjmov rovnakej položky. Zápis pohybov, bilancií, dokončenia relácie a výsledku pre opakovanie je jedna DB transakcia. Opakovaná finalizácia vráti uložený výsledok bez druhého príjmu.

Každý kladne prijatý riadok vyžaduje vyriešenú identitu a konečnú nezápornú nákupnú cenu. Chýbajúca alebo neplatná cena sa neodhadne a neprevezme z predaja či starého priemeru; celý príjem zostane nedokončený s chybou. Ani `force=true` túto kontrolu neobchádza. Rozpracovaná relácia obsahuje vlastnú kópiu riadkov; samotná výmena CSV ju neopraví. Kým nebude doplnený editor prijímanej ceny/identity, oprava takého riadka vyžaduje riadený administrátorský zásah do nedokončenej relácie, bez zásahu do histórie pohybov. Nulový prijatý riadok nevytvára pohyb.

Oceňovanie zostáva **váženým priemerom**. Príjem bez faktúry, neocenené FIFO vrstvy a dodatočné doplnenie nákladu patria nasledujúcej etape. Súborový index faktúry sa aktualizuje až po úspešnom DB commite; jeho prípadné zlyhanie nesmie spôsobiť opakované zaúčtovanie. Neúspech sa loguje. Opakované dokončenie bezpečne zopakuje opravu indexu; samostatná automatická obnova indexu zatiaľ neexistuje.

### Dostupnosť v konfigurácii dodávateľa

V **Dodávatelia → Obecné → Dodávateľská dostupnosť** sa nastavujú:

```json
{
  "adapter_settings": {
    "availability": {
      "orderable": "do 5 dní",
      "unknown": "overíme"
    }
  }
}
```

Chýbajúce a prázdne hodnoty dostanú predvolené texty; explicitný text, napríklad `do 7 dní`, zostane zachovaný. Maximálna dĺžka je 100 znakov. Toto nastavenie používa katalógový aj AI import a existujúca obmedzená aktualizácia dostupnosti. Staré AI nastavenia `orderable`, `unknown`, `hide_zero_stock` už dostupnosť neprepisujú. AI môže naďalej určiť `supplier_name`.

Nové produkty sa stále vytvárajú skryté s povinnou kontrolou `validation_required`. Nulová alebo neznáma zásoba dodávateľa sama nezakazuje košík. Aktualizácia dostupnosti existujúceho produktu stále nemení jeho aktivitu, košík ani skladové množstvo a zachováva doterajšie obmedzenia na samostatné produkty. Už zakázaný košík sa touto aktualizáciou automaticky nezapína.

Náhľad zachytí použitú politiku. Zmena konfigurácie vyžaduje nový náhľad pred novým externým zápisom; overovanie už neistého výsledku zostáva čítacie a používa pôvodný obsah. Podrobnosti sú v [katalógu](supplier-catalog.md) a [AI obsahu](ai-content.md).

## Nasadenie a ďalší postup

Balík neobsahuje databázovú migráciu ani opravu historických dát. Nové config polia sú spätne kompatibilné a majú predvolené hodnoty. Nasadenie prebieha existujúcim workflow po merge PR. Pri návrate na staršiu verziu kódu zostávajú dáta zachované, ale vrátia sa pôvodné riziká príjmu a inicializácie skladu; dovtedy tieto operácie nepoužívať.

Ďalej treba dokončiť jednotnú identitu a kontrolovaný otvárací stav, ochranu prístupu, objednávkový inbox a spracovanie uzamknutého výdaja, rezervácie, FIFO, vratky, frontu synchronizácie a pracovný editor. Autoritu nad skladom Hub prevezme až po overení celého toku vrátane pokladne a výpadkov.
