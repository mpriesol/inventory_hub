# Dodávateľské kódy existujúcich skladových kariet

Implementované rozhranie umožňuje prepojiť existujúcu kartu Hubu s konkrétnou položkou uloženého dodávateľského katalógu, aj keď ich kódy nie sú rovnaké. Napríklad karta môže používať pôvodné SKU e-shopov a dodávateľ môže mať nový kód variantu. Prefix dodávateľa sa nestáva novým SKU už existujúcej karty. Prepojenie nepotrebuje prefix ani register značiek; pracuje s konkrétnymi databázovými ID a dodávateľským kódom.

## Postup obsluhy

V **Sklade** otvorte detail karty a dodávateľské prepojenie. Vyberte dodávateľa, vyhľadajte jeho položku podľa kódu, EAN alebo názvu a pripravte náhľad. Porovnajte obe strany: kód, názov, EAN a parametre konkrétneho variantu. Potvrďte iba položku, ktorá patrí tej istej fyzickej skladovej karte.

Uloženie pridá dodávateľský identifikátor v rozsahu zvoleného dodávateľa a väzbu na presnú katalógovú položku. Zachová interné ID, SKU, názov, skupinu, predajné ceny, sklad, FIFO a existujúce väzby na e-shopy. Používa už uložený katalóg; neobnovuje feed ani neposiela požiadavky do e-shopov. Dostupnosť dodávateľa sa naďalej vyhodnocuje oddelene podľa čerstvosti jej pozorovania.

Ak karte chýba EAN, možno v tom istom náhľade výslovne zvoliť **Doplniť EAN od dodávateľa**. Náhľad ukáže presné overené kódy vybranej dodávateľskej položky. Potvrdenie ich doplní iba do prázdnej karty; existujúci EAN ani neoverený uložený čiarový kód neprepíše. Neplatné kódy sa nekopírujú a kód patriaci inej karte zápis zablokuje. Bez tejto voľby zostáva EAN zachovaný.

To isté funguje aj pri už uloženom dodávateľskom prepojení: v detaile prázdneho EAN použite **Doplniť od dodávateľa**, alebo pri existujúcej väzbe **Skontrolovať / doplniť EAN**. Netreba znovu vyhľadávať rovnakú dodávateľskú položku. Doplnenie sa spolu s prepojením zapíše v jednej transakcii, zvýši revíziu editora a zostane v histórii zmien karty. Ostatné ručné úpravy sa zachovajú. Výslovné potvrdenie môže doplniť aj predtým ručne vyprázdnený EAN.

Chýbajúci EAN alebo parameter je viditeľné upozornenie. Protichodný platný EAN, známa odlišná veľkosť/farba, neaktívny zdroj alebo vlastníctvo inej karty blokuje zápis. Existujúca iná väzba na toho istého dodávateľa sa týmto postupom neprepisuje. Podobný názov a podobné SKU pomáhajú nájsť kandidáta; samy nevytvárajú prepojenie.

Pri stratenej odpovedi obnovte výsledok uloženého náhľadu. Opakovanie tej istej potvrdenej operácie nepridá ďalší alias ani druhú väzbu. Ak sa identita medzi náhľadom a potvrdením zmení, pripravte nový náhľad po kontrole pôvodného výsledku.

## Upozornenia pri zakladaní kariet

[Príprava importu](product-imports.md) kontroluje existujúce karty pred uložením. Zobrazuje dôvod návrhu: zhodný platný EAN, presné SKU, dodávateľský identifikátor alebo podobný názov/SKU. Obsluha môže vybrať existujúcu kartu. Pri samotnej podobnosti môže výslovne potvrdiť, že ide o nový produkt; presný identifikačný konflikt tým nemožno obísť.

Voľba existujúcej karty sa uloží do prípravy. Samotné dodávateľské prepojenie vznikne až pri uložení do Hubu. Takto vybraný riadok slúži na prepojenie: neprepisuje existujúcu kartu a nevytvára produkt v e-shope. Ostatné nové riadky prípravy pokračujú pôvodným postupom. Kontrola platí aj pri automatickom uložení po AI; nevyhodnotený návrh potrebuje rozhodnutie obsluhy.

## Kontrakt a migrácia

Chránené API `/api/supplier-card-links` poskytuje vyhľadanie kandidátov, náhľad, načítanie výsledku a výslovné potvrdenie. Náhľad uchováva presnú dvojicu interných ID a kontrolný súčet identity. Zápis znovu overuje dodávateľský zdroj, aktuálnu kartu a vlastníctvo pod spoločným `IDENTITY_WRITE_LOCK`. Kontroly používajú existujúci resolver produktových identifikátorov. Aktuálne vlastníctvo vyplýva iba z dodávateľskej väzby a scoped aliasu; pôvod založenia ani historický návrh SKU neurčujú dnešného vlastníka. Migrácia 032 navyše chráni jednu dodávateľskú ponuku pred dvomi kartami databázovým unikátnym indexom.

Požiadavka náhľadu prijíma voliteľné `copy_supplier_eans` (predvolene `false`). Voľba a overené kódy sú súčasťou uloženého náhľadu; potvrdenie ich nemôže nahradiť iným zoznamom. Odpoveď `ean_adoption` vysvetľuje dostupnosť a plánované kódy, výsledok `copied_eans` potvrdzuje skutočne doplnené kódy. Opakovanie dokončeného potvrdenia vráti pôvodný výsledok bez ďalšieho identifikátora alebo auditu. Táto voľba nepotrebuje novú SQL migráciu a neaktivuje skladovú publikáciu.

Príprava importu má samostatné operácie `POST /api/product-imports/{id}/identity-review` a `POST /api/product-imports/{id}/identity-decisions`. Obidve používajú aktuálnu revíziu prípravy; rozhodnutie sa viaže na konkrétny skontrolovaný obsah. Uloženie kontrolu zopakuje v transakcii s produktovými zápismi.

Migrácia `028_supplier_card_links.sql` pridáva iba tabuľku náhľadov a výsledkov. Je zapojená do spoločného migračného spúšťača a API obrazu. Nezakladá obchodné väzby a nezapína skladové ani cenové zápisy. Pri návrate na kompatibilný starší kód ponechajte uložené výsledky; už potvrdené dodávateľské väzby sa revertom kódu nevrátia späť.

Testy používajú syntetické údaje a izolovaný localhost PostgreSQL `*_catalog_test`. Dokončenie testov alebo prítomnosť tlačidla nepotvrdzuje prepojenie konkrétnej produkčnej karty; to sa overuje samostatne v uloženom výsledku a detaile karty.
