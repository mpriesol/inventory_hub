# Spoločné SKU a zakladanie kariet

## Pravidlo pre nové karty

Jedna konkrétna predajná jednotka má jednu kartu Hubu. Nové SKU má formát
`<dvojpísmenová skratka značky>-<overený kód konkrétneho výrobku alebo variantu>`.
Napríklad `TP-TBP-ML6B` je prípustné až po overení kódu konkrétneho výrobku,
nie samotného názvu Topeak. Dodávateľský kód, interné ID feedu a rodičovský kód
sú samostatné údaje. Žiadny z nich sa automaticky nevyhlási za MPN.

Pred vytvorením karty sa kontroluje existujúca identita: spoločné SKU, uložené
staré aliasy, overené čiarové kódy, dodávateľsky obmedzené identifikátory a
nové doložené identifikátory značky. Jednoznačná zhoda použije tú istú kartu.
Rozporné dôkazy alebo viacerí vlastníci sú konflikt, nie pokyn zlúčiť históriu.
Podobnosť názvov nie je dôkaz. Rodič skupiny sa nezakladá ako fyzická položka.

Pri už existujúcej karte zostáva jej SKU zachované, aj keď je ešte v starom
formáte. Register ani pridanie dodávateľa nespúšťajú prečíslovanie.
Zmena názvu značky, aliasu alebo dodávateľa nemení pridelený kód.

## Register značiek

Register v PostgreSQL uchováva jedinečnú skratku z dvoch veľkých písmen A–Z,
názov, overené aliasy, druh priestoru a podklad registrácie. Prvé skratky:

| Značka | Skratka |
| --- | --- |
| Topeak | TP |
| Continental | CT |
| Shimano | SH |
| Force | FO |
| Northfinder | NF |
| Schwalbe | SW |
| BBB Cycling (aj BBB) | BB |

Ďalšie návrhy zostávajú v dodanom audite a XLSX. Ich hromadné schválenie nie je
súčasťou tejto migrácie. Novú značku možno zaregistrovať priamo vo formulári
identity. Obsadená skratka alebo alias inej značky sa odmietne. Rozdiel veľkosti
písmen a medzier názvu sa zjednotí; rôzne obchodné názvy či interpunkcia vyžadujú
výslovný alias s podkladom. Sporné dvojice z auditu sa automaticky nezlučujú.

## Kód a výnimky

| Situácia | Postup |
| --- | --- |
| Overený MPN konkrétneho variantu | Použiť presnú zdrojovú hodnotu a uviesť podklad overenia. |
| Kód označuje rodiča alebo viac variantov | Novú kartu zatiaľ nevytvoriť; doložiť jednoznačný variantový identifikátor. Nepridávať odhadnutú farbu či veľkosť ako príponu. |
| Použiteľný MPN chýba | Výslovná výnimka: overený GTIN predajnej jednotky alebo doložený stabilný dodávateľský kód v rozsahu konkrétneho dodávateľa. Podklad musí vysvetliť použitie výnimky. |
| Rovnaký dodávateľský kód u dvoch dodávateľov | Nie je spoločná identita. Vyžaduje nezávislý dôkaz zhody; kolízia návrhu SKU sa odmietne bez svojvoľnej prípony. |
| Služba | Navrhnúť výslovný priestor druhu `service` a existujúci doložený kód služby. Nová karta je zatiaľ blokovaná, kým nebude zavedený typ neskladovej služby; bežná karta nesmie predstierať fyzický tovar. |
| Neznačkový tovar | Výslovne registrovaný priestor druhu `unbranded` a doložený kód konkrétnej predajnej jednotky. Chýbajúci kód zostáva blokovaný. |
| Neznámy alebo rozporný podklad | Uložiť rozpracovanú prípravu a doplniť dôkaz. Nevytvárať poradové číslo ani náhradnú príponu. |

Kód musí byť text. Úvodné nuly, veľkosť písmen a pôvodná hodnota zostávajú
zachované; okrajové medzery, riadiace znaky, nepodporovaný oddeľovač a prekročenie
limitu 100 znakov celého SKU sú viditeľná chyba. Kolízia líšiaca sa len veľkosťou
písmen nie je dôvod na zlúčenie. Platné UPC/EAN možno porovnať cez spoločnú
GTIN reprezentáciu, pričom pôvodné čiarové kódy sa neprepisujú.

Potvrdenie konkrétnej predajnej jednotky zahŕňa aj balenie, rozmery a variant,
ak rozhodujú o identite. Text podkladu je záznam overenia obsluhou; aplikácia
samotným vyplnením poľa neoverila stránku výrobcu. Neoverený `manufacturer_code`
z feedu zostáva kandidátom, nie schváleným MPN. Iný MPN už doloženej karty
vyžaduje samostatné vyriešenie rozporu.

## Každodenný postup

1. **Z katalógu:** vyber konkrétne položky a otvor existujúcu prípravu produktov.
   Pri novom výrobku doplň alebo over identitu a podklad. Náhľad ukáže spoločné
   SKU alebo konkrétny konflikt. Zhodná existujúca karta sa použije znova.
2. **Z faktúry alebo skenovania:** pri chýbajúcej karte použi **Založiť skladové
   karty**. Rovnaká príprava uchová väzbu na pôvodné riadky. Po uložení sa vráť
   do tej istej príjemky; fakturované ceny a prijaté množstvá zostávajú jej údajmi.
   Zaúčtovanie už nevytvára novú kartu iba zo starej dodávateľskej predpony.
3. **Bez feedu:** v **Sklade** otvor **Nová skladová karta**. Najprv vyhľadaj
   existujúce SKU alebo EAN; pre nový výrobok stačí názov a overená identita.
   Dodávateľské väzby doplníš cez existujúci detail karty alebo pri príjme;
   API ručného založenia prijíma aj samostatný dodávateľský kontext.
4. **Uloženie do Hubu** vytvorí alebo použije kartu a doloženú dodávateľskú väzbu.
   Hotový e-shopový popis, predajná cena a DPH nie sú podmienkou základnej karty.
5. **Potvrdenie prijatých kusov** je samostatné zaúčtovanie príjemky. **Publikovanie
   do vybraných e-shopov** má vlastný náhľad a kontrolu úplnosti údajov.

Pri konflikte skontroluj zobrazené karty a konkrétne kódy. Ak chýba značka,
zaregistruj ju alebo dolož alias. Ak sa nezhoduje variant či balenie, oprav
podklad vo formulári. Dve existujúce karty s históriou rieši následná migrácia;
nevyberaj jednu iba podľa podobného názvu. Po strate odpovede pri ručnom založení
zopakuj pôvodnú požiadavku cez existujúce obnovenie, aby zostalo rovnaké UUID.

## Implementácia a hranice

`services/sku_policy.py` poskytuje spoločný náhľad a uloženie doloženej identity;
`services/product_identity.py` zostáva spoločným resolverom. Nová identita je
aditívny záznam viazaný na `Product.id`, nie náhrada skladového modelu.
`ProductSupplySource` a dodávateľsky obmedzené identifikátory naďalej predstavujú
ponuky viacerých dodávateľov. Výslovné prepojenie nevyžaduje dodávateľskú predponu.

| API (s prefixom `/api`) | Úloha |
| --- | --- |
| `GET /sku-policy/brands` | Register a aliasy |
| `POST /sku-policy/brands` | Registrácia značky alebo výslovného priestoru výnimky |
| `POST /sku-policy/brands/{id}/aliases` | Doložený alias existujúcej značky |
| `POST /sku-policy/preview` | Čítací náhľad: existujúca karta, nové SKU, konflikt alebo chýbajúci podklad |
| Existujúce `/product-imports` a `/product-editor/products` | Uloženie cez spoločnú politiku a pôvodné ochrany opakovania |

Operátorské overenie a ochrana cookie zápisov zostávajú spoločné. Zápis identity
používa existujúci transakčný `IDENTITY_WRITE_LOCK`; náhľad sa pri uložení znova
overí. Podklad je súčasťou revízie prípravy, takže staré rozhodnutie nemožno použiť
po zmene identity. Rozpracované alebo neisté odoslania zachovávajú pôvodný denník
a pravidlá obnovy. Priamy nový katalógový import vyžaduje najprv uloženú kartu.

V PostgreSQL režime sú staré `/runs/prepare` a `/runs/prepare_legacy` zablokované
s odkazom na spoločnú prípravu. Historický refresh XML naďalej stiahne zdroj, ale
nevytvorí importný CSV s novými dodávateľskými SKU. Existujúce historické súbory
sa nemažú. Čítanie už existujúcich produktov z e-shopu zostáva samostatným importom
ich pôvodnej identity, nie hromadným prečíslovaním.

## Nasadenie a návrat

Migrácia `029_sku_policy.sql` pridáva register, aliasy a doložené produktové
identity. Explicitný migračný runner ju vykoná po 028 pred reštartom API; SQL
je zabalené v API obraze. Existujúce produkty, zásoby, rezervácie, FIFO, doklady,
publikačné prepínače ani historické identity sa nemigrujú automaticky.

Pred nasadením platí bežná databázová záloha. Pri probléme zachovať nové tabuľky
a už uložené dôkazy; preferovať kompatibilnú opravu vpred. Starý kód by opäť
povoľoval dodávateľské prideľovanie, preto sa pri núdzovom návrate nesmú zakladať
nové karty cez staré cesty. Tabuľky ani potvrdenia požiadaviek nevymazávať.

## Nasledujúca etapa

Čítací náhľad konkrétnych kariet a podrobný postup pilotu sú implementované
v [príprave prevodu SKU](sku-migration.md). Náhľad nie je vykonanie prečíslovania.

Na existujúcich auditných podkladoch pripraviť malý pilot prečíslovania s čerstvou
kontrolou natívnych ID Upgates, aliasov a indexov. Topeak duplicity a pokladňová
skupina xTrek majú vlastné konflikty; história sa automaticky nezlučuje. Až potom
nasleduje náhľad fyzického počítania s doloženými nákupnými cenami a samostatný
prevádzkový prechod. BIKETREK je produkčný Upgates, xTrek zostáva na Atomeri;
prípravné pripojenie xTrek Upgates nepreukazuje zachytenie jeho živých predajov.
Nasadenie tejto etapy neaktivuje skladovú autoritu ani prenos množstiev.
