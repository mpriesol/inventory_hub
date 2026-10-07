# Príjem tovaru a priame odoslanie do Upgates

Stav overený čítaním kódu 7. 10. 2026 na obsahu `main` s tree
`758b3d80f6f4e74a0d06615ef36bdc3c279c6c6a`. Nasledujúce prepojenie je návrh,
nie implementovaná funkcia príjmu. Produkčné oprávnenia synchronizácie neboli
overené. Správa AI bola načítaná cez autorizované rozhranie: publikovaná je
verzia #9; koncepty #10/#11 nie sú aktivované a nesú nevyriešené integračné
podmienky. Samotná existencia novšieho konceptu neznamená jeho účinnosť.

## Čo dnes existuje

- `receiving_db.finalize_session` potvrdí prijaté množstvá, vytvorí nemenné
  pohyby, FIFO a zostatky. Uložený výsledok a kľúče riadkov chránia opakovanie.
  Chýbajúci lokálny produkt vytvorí z identity a názvu faktúry; nie je to úplná
  produktová karta z feedu.
- `ReceivingResultsModal` samostatne volá `/runs/prepare`. Pre Paul Lange ide
  o `paul_lange_web.prepare_from_invoice`: porovnanie s CSV exportom e-shopu,
  doplnenie nových produktov z feedu a vytvorenie súborov. Kľúč faktúrového
  metapoľa je `stock_updated_by_invoices`. Táto vetva ho zapisuje do CSV,
  nie priamo do Upgates.
- Dodávateľský katalóg používa `catalog_import.create_preview`,
  `queue_import` a `execute_import`. Existuje priame vytvorenie cez Upgates
  API, overenie výsledku a `register_created` pre lokálne mapovanie.
- AI príprava používa rovnaký importér s obsahovým doplnením. Má review,
  zmrazenú verziu pravidiel, samostatné potvrdenie a obnovu neistého výsledku.
- `POST /stock-sync/run` prijíma `shop_code`, `skus` a `confirmed`.
  Podporuje 1–100 presných SKU. Frontendový `runStockSync` zatiaľ výber
  neodovzdáva. Príjem túto cestu nevolá.
- Pravidelný worker môže zmenu zachytiť v ďalšom priechode, iba ak je e-shop
  autorizovaný, rozvrh zapnutý a všetky existujúce podmienky splnené.

## Navrhovaný výsledok príjmu

Zdrojom je dokončená DB príjemka a jej skutočne prijaté riadky, nie browserová
cache ani výsledné CSV. Rozlišujeme lokálny produkt, dodávateľskú ponuku a
existenciu v konkrétnom cieľovom e-shope.

| Stav položky | Akcia |
| --- | --- |
| Produkt v Hube a overené mapovanie v cieli | Synchronizovať aktuálny dostupný stav vybraných SKU. |
| Produkt v Hube, ale chýba v cieli; zdroj vo feede | Pripraviť import cez existujúci katalóg, voliteľne existujúcu AI prípravu. |
| Produkt ešte nie je v Hube, zdroj vo feede | Príjem založí lokálnu identitu; katalógový import ju následne použije a doplní mapovanie. |
| Chýba použiteľný feedový aj produktový zdroj | Označiť položku a ponúknuť novú rešeršnú prípravu podľa EAN/názvu; nevytvárať fiktívnu ponuku dodávateľa. |
| Nejednoznačná identita alebo viac kandidátov | Vyriešiť konkrétnu väzbu; nezaložiť ďalší produkt iba kvôli inému EAN. |

V detaile dokončenej príjemky majú byť akcie **Synchronizovať zásoby**,
**Pripraviť nové produkty**, **Pripraviť obsah / AI** a stav každej položky.
Výber e-shopu je výslovný. Akcia príjem opätovne nezaúčtuje.

## Prenos existujúcich zásob

1. Z príjemky získať a deduplikovať lokálne produktové ID a presné SKU.
2. Pre cieľ overiť mapovania a použiť existujúci `stock_sync.enqueue`.
   Väčší výber rozdeliť na nadväzujúce dávky podľa limitu služby.
3. Tesne pred odoslaním odvodiť **aktuálne fyzické mínus rezervované mínus
   karanténa** z potvrdenej evidencie. Nikdy neposielať CSV výpočet
   „starý exportovaný sklad + príjem“ ani znovu pripočítať prijaté kusy.
4. Uchovať väzbu príjemka → priechody a výsledky po produktoch. Opakované
   otvorenie alebo kliknutie musí ukázať existujúci stav.
5. Zachovať autoritu skladu, kontrolu objednávok a neistých zápisov.
   Manuálne tlačidlo nesmie tieto podmienky obísť. `uncertain` sa rieši
   existujúcim postupom, nie slepým opakovaním PUT.

`stock_updated_by_invoices` je samostatná auditná informácia. Dnešný prenos
zásob posiela iba množstvo, dostupnosť a možnosť nákupu. Ak sa má faktúrové
metapole udržiavať aj vzdialene, musí mať vlastné zachovanie existujúcich
hodnôt, jednoznačné identifikátory dokladov, zlúčenie bez duplicít a overenie
výsledku. Ochrana príjmu proti opakovaniu zostáva v DB, nie v tomto texte.

## Nové produkty a spoločný importér

Príjem má odovzdať existujúcej príprave `supplier`, `feed_key`, `run_id`,
`product_ids` a cieľ. Zachovať ceny/DPH, fotografie, varianty, identitu,
kategórie a kontroly existujúceho importéra. Nenahrádzať ho konverziou CSV.

`register_created` už vie použiť produkt založený príjmom; nevytvára však
skladovú synchronizačnú úlohu. Správne poradie je potvrdené vytvorenie v cieli
→ uloženie mapovania → odoslanie aktuálneho dostupného množstva. Nové stavy
objednávok medzi týmito krokmi sa musia premietnuť do posledného prepočtu.
Import produktu naďalej nesmie sám vytvoriť druhý skladový pohyb.

Výber variantov a skutočná produktová rodina potrebujú samostatné porovnanie:
feedová skupina nie je automaticky správny parent. Existujúci explicitný
výber sa nemá rozšíriť bez zobrazenia reálnych kandidátov používateľovi.

## Príprava bez feedu

Dnešná AI príprava vyžaduje katalógové ID; načítanie z Upgates je iba
aktualizačná cesta. Potrebný je nový vstupný pracovný záznam s EAN/názvom,
faktúrovým riadkom, overenou identitou, zdrojmi, obchodnými údajmi, galériou
a reálnymi variantmi. Po rešerši a kontrole sa normalizuje do spoločného
importného modelu. Cena, dodávateľský kód, vlastná zásoba a varianty sa
nedopočítavajú podľa podobnosti. Chýbajúci nepovinný údaj neblokuje celú dávku.

## Poradie realizácie a overenie

1. Prepojiť výsledok DB príjmu s presným výberom SKU a existujúcim prenosom;
   pridať trvalé výsledky, odstrániť automatické generovanie CSV ako hlavný krok.
2. Pripojiť existujúcu katalógovú a AI prípravu a následnú synchronizáciu po
   úspešnom mapovaní. Samostatne zachovať požadované faktúrové metapole.
3. Rozšíriť zdrojový pracovný záznam a rešerš pre položky bez feedu.

Zmysluplné regresie: retry finalizácie a odoslania; opakované SKU na viacerých
faktúrových riadkoch; čiastočne prijaté množstvo; produkt len v Hube;
vytvorenie v Upgates s oneskoreným mapovaním; objednávka medzi príjmom a sync;
neistý API výsledok; parent s variantmi; prvý potvrdený príjem po starom
neoverenom stave. Pilotné živé zápisy musia mať určený rozsah.

## Nové projektové pravidlá

Kompletné používateľské pravidlá sú súkromná prevádzková konfigurácia;
nepatria do verejného repozitára. Nástroj `ai_rule_package` pripravuje ich
kontrolovateľný prenos. Publikované verzie sú v `ai_rule_versions`, nie
v seed texte `initial_book`. Bez načítania aktuálnej knihy a uloženia novej
verzie nemožno tvrdiť, že sa pravidlá živého Hubu zmenili.

Zachovať celý obsah, pôvod a stav každého bloku: spoločný postup, e-shop,
dodávateľ, značka, kategória, znalosti a jednorazové výnimky. Návrhový
register sa publikovaním textu nestáva schváleným. História je proveniencia,
nie druhá sada účinných pokynov. Tajné feedové endpointy nahrádzajú odkazy na
bezpečnú konfiguráciu; ich význam a mapovanie zostávajú.

Samotný prenos inštrukcií neimplementuje tieto chýbajúce správania:

- úplný štruktúrovaný register kategórií s podmienenou povinnosťou a rozsahom;
- cielený výber znalostí materiálu/technológie podľa potvrdených vlastností;
- vyhľadanie a potvrdenie celej rodiny, doplnenie a vizuálna kontrola galérií;
- konkrétne dodávateľské výnimky dostupnosti a variantov v deterministickom
  importe, vrátane rozdielnych formátov feedu;
- pracovné režimy a vykonané kontroly A/B/C, bezpečný prístup k B2B zdrojom;
- XML export a skutočné spustenie jeho generátora a XSD validátora.

Priamy API import musí validovať skutočný JSON. XML pravidlo ceny sa nesmie
aplikovať druhýkrát na API cenu s už vyhodnoteným režimom DPH. Zmena pravidiel
nesmie prepísať sklad, identitu, ceny ani hotové importy. Aktuálne a budúce
prípravy majú dostať novú verziu; už zmrazené úlohy vyžadujú výslovnú novú
prípravu, nie tichú zmenu auditu.
