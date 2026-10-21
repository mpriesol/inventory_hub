# Príprava prevodu existujúcich SKU

Táto etapa pripravuje náhľad konkrétnych kariet. Nevytvára druhú kartu,
neprečísluje e-shop ani Hub, nezaúčtuje zásoby a nezapína skladovú autoritu.
Pravidlá novej identity sú v [sku-policy.md](sku-policy.md).

## Náhľad v Hube

V detaile existujúcej skladovej karty otvor **Náhľad zmeny SKU**. Doplň značku,
presný kód predajnej jednotky, jeho druh a podklad. Náhľad oddeľuje súčasné SKU
od navrhovaného SKU a ukazuje konflikty, aliasy, mapovania a dotknuté záznamy.
**Overiť v e-shopoch** vykoná samostatné čítanie aktuálnych identít cez existujúci
Upgates transport a jeho rozpočet požiadaviek. Výsledok možno stiahnuť ako JSON.
V tomto nástroji nie je tlačidlo na premenovanie.

`POST /api/sku-policy/migration-preview` prijíma najviac 20 rôznych položiek
`{product_id, sku_identity}` a `verify_remote` (predvolene `false`).
`sku_identity` má rovnaký kontrakt ako pri zakladaní karty. Odpoveď má čas,
hash snímky, jednotlivé výsledky a povinné následné kontroly. `can_apply` je
vždy `false`: úspešný návrh identity nie je potvrdenie pripravenosti produkčného
prečíslovania. Náhľad neukladá dôkaz identity ani schválenie migrácie.

Kontrola existujúcej identity používa spoločný resolver. Doložený MPN inej karty,
nejednoznačný EAN, kolízia kódu alebo aliasu a rozpor značky zostávajú konfliktom.
Kontroluje sa aj kolízia cieľových SKU medzi riadkami jednej dávky. Rovnaká
navrhovaná hodnota nikdy nie je pokyn zlúčiť dve histórie.

Pri variante zostáva rodič nezmenený. Rodičovský kód sa nesmie nahradiť kódom
jedného variantu. Pokladňová skupina xTrek má samostatné preverenie, pretože jej
variant môže súčasne existovať ako samostatný výrobok. Chýbajúce natívne ID sa
nesmie odvodiť z URL, názvu, EAN alebo nového kódu. Uložené ID v Hube a čerstvo
overené ID z Upgates sú v náhľade odlíšené.

Počty závislostí sú kontrola dopadu. Historické pohyby a FIFO nie sú dôvod na
ich prepisovanie. Aktívne rezervácie, rozpracované objednávky, importné prípravy
a neisté publikácie vyžadujú konkrétny postup prechodu. Neoverená oblasť sa
uvádza ako neoverená; nula v jednej tabuľke nedokazuje neprítomnosť externých
objednávok, košíkov alebo rozpracovaných súborových importov.

## Podporovaný spôsob premenovania v Upgates

Oficiálny kontrakt `PUT /api/v2/products` páruje produkty aj varianty podľa
`code`. V skontrolovanom kontrakte nie je overená operácia old-code/new-code
ani premenovanie adresované natívnym ID. Bežný CSV/XML import nového kódu sa
preto nesmie použiť ako premenovanie existujúcej karty.

Pred zápisom treba potvrdiť administratívny alebo podporou Upgates vykonaný
postup, ktorý zachová `product_id`, `variant_id` a rodiča. Pilot musí porovnať
tieto ID pred aj po operácii. Zmazanie a opätovné založenie nie je prijateľné.

Existujúci nástroj TOPEAK → ZOO zostáva úzkou výnimkou. Najprv overuje už
vykonané administratívne premenovanie a potom zosúlaďuje Hub. Automaticky ho
nerozširujeme na značkové SKU ani ho nespúšťame na zostávajúci historický zoznam.

## Snímka pred pilotom a okamih zmeny

1. Uchovať čerstvý náhľad a tabuľku old/new SKU, Hub ID, všetkých shopových
   mapovaní, natívnych ID, rodičov, EAN a variantových parametrov. Uložiť presný
   podklad MPN a dodávateľské identity s rozsahom dodávateľa.
2. Pred skutočným zápisom zabezpečiť overenú zálohu PostgreSQL a relevantných
   konfiguračných/cache/importných súborov. Čítací JSON alebo XLSX je podklad
   na porovnanie, nie plnohodnotná záloha databázy. Zaznamenať čas a overiť
   obnoviteľnosť. Nevyžadovať ani nezverejňovať tajné hodnoty.
3. Zachytiť množstvá, potvrdenie fyzického skladu, rezervácie, karanténu,
   FIFO vrstvy a alokácie, pohyby a odkazy na doklady. Neoverené množstvo
   zostáva neoverené; pri prečíslovaní sa nepotvrdzuje nula ani nový príjem.
4. Zmapovať otvorené objednávky, pokladňové riadky a staré SKU v rozpracovaných
   príjemkách/prípravách. Starý alias pri vyhľadávaní karty sám nedokazuje,
   že objednávkový resolver prijme starý kód proti novému mapovaniu. Pred
   realizáciou doplniť a otestovať tento prechod vrátane idempotencie výdaja.
5. Na dohodnutý okamih zastaviť dotknuté súbežné produktové importy/publikácie.
   Každý sending/uncertain záznam najprv vyriešiť jeho pôvodným postupom.
   Zachovať denníky a UUID. Neposielať starú pripravenú dávku s už zmeneným SKU.
6. Skontrolovať reklamné feedy a konverzné identifikátory. V Upgates je pre
   vybrané feedy **Doplnky → konkrétny doplnok → Export produktov → ITEM_ID formát**.
   Režim Kód môže zmenou SKU odpárovať ponuky. Nastavenie neprepínať automaticky;
   uložiť aktuálne ITEM_ID/G:ID a pripraviť postup pre každú aktívnu integráciu.
7. Vykonať až samostatne odsúhlasený malý pilot overenou operáciou. Zachovať
   Hub ID a všetky pôvodné SKU ako aliasy s pôvodom. Nové SKU musí byť jedinečné
   medzi produktmi aj variantmi každého e-shopu. Dve karty s históriou nezlučovať.

## Obnova údajov a indexov

Po premenovaní nestačí opraviť len `Product.sku`. Existujú tri odlišné uložené
pohľady, ktoré treba obnoviť a znovu porovnať s natívnymi ID:

| Úložisko | Úloha a obnova |
| --- | --- |
| SQL `ShopProductContent` a `ShopProduct` | Obnoviť produktové údaje/mapovanie z čerstvého Upgates čítania. Overiť presného rodiča a variant, nevytvoriť ďalšiu kartu podľa nového kódu. |
| `shops/<shop>/cache/upgates_products.json` | Vynútená obnova produktového pullu, nie čakanie na starú cache. Pull bez zásob nie je skladový pohyb. |
| `shops/<shop>/catalog-cache/identities.json` | Samostatná kontrola aktuálnych produktových identít cez existujúci check s `refresh=True`; overiť úplnosť a úspech nahradenia indexu. |

Starší index zhôd nemá byť dôkazom identity. Katalóg zobrazuje overené aktuálne
SKU Hubu; dodávateľský kód zostáva samostatný údaj. Nepridelené alebo konfliktné
položky sa riešia spoločnou prípravou, nie starým návrhom dodávateľskej predpony.

Pripravené importy a AI úlohy s pôvodnou identitou musia prejsť novým náhľadom.
Historické úspešné a neisté denníky sa neprepisujú. Skladové checkpointy kľúčované
SKU vyžadujú explicitný prevod alebo nový overený základ; nepoužiť ich bez
kontroly na iný kód. Obnova indexu sama neoprávňuje zapnúť prenos zásob.

## Návrat a ukončenie pilotu

Pri nesúlade ID alebo nepriehľadnom výsledku zastaviť ďalšie položky. Najprv
zistiť skutočný stav z oboch systémov. Návrat používa rovnaký overený spôsob
premenovania existujúcej karty späť a kontrolu, že starý kód medzitým neobsadila
iná karta. Alias a audit sa zachovajú. Import starého CSV nie je postup návratu.

Pilot je dokončený až po porovnaní ID, rodičov, EAN, parametrov, množstiev,
rezervácií, FIFO, dokladov a histórie; po obnove všetkých troch pohľadov a kontrole
starého aj nového SKU vo vyhľadávaní, príjme a otvorených objednávkach. Reklamné
ID sa musia správať podľa potvrdeného plánu. Publikačné nastavenia zostávajú
v pôvodnom stave.

Tento náhľad vykonanie migrácie nenahrádza. Inicializácia fyzického skladu a
zapnutie autority sú ďalšie samostatné kroky. xTrek na Atomeri sa nepovažuje za
pokrytý iba tým, že existuje prípravné pripojenie Upgates.

Zdroje kontraktu overené 21. 10. 2026:
- https://docs.upgates.com/api-reference/produkty
- https://docs.upgates.com/api-reference/produkty-seznamy
- https://www.upgates.cz/a/varianty
- https://www.upgates.cz/a/propojeni-na-srovnavace-zbozi
