# AI obsah produktov – A + B

Funkcia používa OpenAI Responses API z Hubu. Nepotrebuje projekt/GPT v ChatGPT ani prístup k tomuto chatovému účtu. Projekt na OpenAI Platform môže patriť inému firemnému účtu a má vlastnú API fakturáciu.

## Použitie

1. V katalógu dodávateľa vyber produkty a klikni **Pripraviť obsah / AI**. Jedna príprava prijíma najviac 500 vybraných variantov, päť e-shopov a 100 kombinácií rodina/e-shop.
2. Pre každú rodinu zvoľ **Vylepšiť obsah pomocou AI** alebo pôvodný feed a profil kategórie. Pracuje sa iba s označenými variantmi. Spoločný popis nemožno zapnúť iba pre časť variantov tej istej vybranej rodiny.
3. Vyber jeden alebo viac e-shopov. Kategória z profilu má prednosť pred náhradnou kategóriou nastavenou pre cieľ. Kódy kategórií overí existujúci náhľad importu proti Upgates.
4. Prepínače v príprave sú jednorazové výnimky. Trvalé nastavenia sú v **Nastavenia → AI obsah produktov → Pravidlá a kategórie**.
5. Spusti spracovanie podľa profilu. Výsledok obsahuje pôvodné podklady, upraviteľné texty, parametre, zdroje, chýbajúce fakty, upozornenia, verziu pravidiel a históriu.
6. Ceny sa naďalej upravujú v existujúcom náhľade importu vrátane hromadnej ceny variantov. Zmena ceny nepotrebuje nové platené AI spracovanie.

Chýbajúce povinné parametre sa v editore zobrazia automaticky podľa zmrazeného registra, aj pre jednotlivé varianty. Prázdne hodnoty sú zvýraznené; používateľ vyplní existujúce políčka alebo vyberie z povolených hodnôt. Žiadna hodnota vrátane Áno/Nie sa nepredpokladá. Nevyplnené riadky sa neposielajú ako neplatné prázdne parametre; povinný údaj naďalej kontroluje server. Po doplnení overeného údaja treba odstrániť iba jeho vyriešený záznam zo zoznamu chýbajúcich faktov, ostatné zostávajú.

## Pokračovanie v rozpracovanej úlohe

Úloha zostáva uložená po zatvorení stránky. Zoznam ponúka filtrovanie podľa potrebnej pozornosti, spracovania a dokončenia; detail obsahuje ďalší krok, zmrazené pravidlá, kontroly a audit.

| Operácia | Výsledok a obmedzenie |
|---|---|
| Archivovať / obnoviť | Presunie úlohu medzi pracovným zoznamom a archívom bez vymazania obsahu či histórie. Nemení produkt v e-shope. Prebiehajúcu operáciu nemožno archivovať. |
| Znovu otvoriť obsah | Pre stavy `import_blocked`, `exists`, `completed` a `cancelled` s uloženým výstupom vráti obsah na kontrolu. Zruší schválenie a starý náhľad; zachová podklady a verziu pravidiel. Nevolá AI a nevracia späť už vykonaný import. |
| Skopírovať obsah pre vybrané varianty | Vytvorí novú úlohu pre podmnožinu pôvodného výberu, načíta aktuálny feed a publikované pravidlá. Bez plateného volania skopíruje text, ponechá parametre a feedové dôkazy iba vybraných variantov a znova validuje. Spoločný text treba skontrolovať, pretože môže opisovať vynechané varianty. |
| Nová AI príprava / pôvodný feed pre výber | Vytvorí novú úlohu pre označené varianty. AI vetva čaká na spustenie odhadu; feedová vetva nepoužíva platené generovanie. |

Kopírovanie vyžaduje zostávajúci dôkaz, inak treba novú AI prípravu. Nová úloha odkazuje na pôvodnú; AI kópia vyžaduje ľudskú kontrolu a potvrdenie. Úlohu načítanú priamo z e-shopu nemožno rozdeliť: ďalšiu prípravu začni opätovným načítaním produktu.

Pri neznámom výsledku importu najprv použi kontrolu existujúceho importu. `retry_import` pracuje s pôvodným náhľadom a jeho kontrolami; neznamená bezpodmienečný nový zápis. Pri aktualizácii v stave `sending` alebo `uncertain` je zmena, opätovné otvorenie aj kopírovanie úlohy zablokované do overenia výsledku.

## Štyri nezávislé nastavenia

| Nastavenie | Zapnuté | Vypnuté | Predvolené |
|---|---|---|---|
| Schválenie obsahu | Čaká na človeka | Po úspešných kontrolách schváli zmrazená politika | Zapnuté |
| Aktívny po importe | Produkt bude aktívny | Produkt bude skrytý | Vypnuté |
| Odhad nákladov | Pred plateným volaním zobrazí odhad a čaká na spustenie | Preskočí túto zastávku | Zapnuté |
| Potvrdenie importu | Čaká na potvrdenie náhľadu | Import spustí automaticky | Zapnuté |

Technické kontroly nemožno vypnúť. Ani automatické schválenie neopravuje ceny, nevymýšľa fakty a neposiela skladové polia. `validation_required=0` sa odošle pri overenom AI obsahu prijatom človekom alebo explicitnou politikou. Pôvodný feed ponecháva `validation_required=1`. Aktivita produktu je samostatná voľba.

Nastavenia sa skladajú v poradí **spoločné → dodávateľ → značka → kategória → e-shop → produkt → jednorazová výnimka**. Pravidlo môže mať kombináciu podmienok; rozhoduje jeho najkonkrétnejšia dimenzia a počet podmienok. Konfliktné hodnoty pri rovnakej priorite sa odmietnu. Pri úlohe je viditeľný výsledný prepínač aj názov pravidla, ktoré ho nastavilo.

## Pravidlá a kategórie

Tlačidlo **Kategórie** otvára profily kategórií. Pokyny, povinné parametre aj mapovanie na e-shopy sa upravujú na jednom mieste; samostatné tlačidlo **Profily kategórií** ani pôvodná skupina kategóriových pravidiel už v navigácii nie sú.

| Pojem v UI | Čo obsahuje | Ako sa používa |
|---|---|---|
| **Kategórie** | `CategoryProfile`: základné pokyny pre daný typ produktu, register parametrov, prepínače, pripravenosť automatizácie a mapovanie na kategórie e-shopov. | Profil sa vyberá pri príprave rodiny. Definuje, ktoré parametre AI smie vrátiť a ktoré sú povinné. |
| **Cieľová kategória e-shopu** | Existujúci strom kategórií z Upgates s kódmi a rodičmi. | Vyberá sa v mapovaní profilu alebo ako náhradná kategória cieľa. Určuje zaradenie produktu v konkrétnom e-shope. |

Staršie knihy pravidiel môžu obsahovať aj samostatné `Rule` s podmienkou `scope.category`. Ak v načítanej verzii zostali pravidlá z pôvodnej kategóriovej skupiny, pod kategóriami sa zobrazí kompaktná rozbaľovacia sekcia na ich úpravu alebo odstránenie cez existujúci editor. Bez takých pravidiel sa sekcia nezobrazuje. Platí to aj pri načítaní historickej verzie; samotné otvorenie ani uloženie profilu pravidlá automaticky nepresúva, nezlučuje ani nemaže.

Podmienka `scope.category` naďalej obsahuje **ID profilu**, napríklad `inner_tubes`, a môže sa kombinovať s ďalšími podmienkami. Pravidlá zaradené podľa konkrétnejšej podmienky do skupiny e-shopu či produktu zostávajú v príslušnej skupine. Vyhodnotenie pravidiel sa nemení: všetky vyplnené podmienky musia súhlasiť, pokyny sa skladajú a kolízia prepínačov pri rovnakej priorite vráti `ai_policy_conflict`.

- PostgreSQL je zdrojom pravidiel. Každé uloženie vytvára nezávislú verziu konceptu. Publikovanie s kontrolou očakávanej verzie mení ukazovateľ pre nové úlohy; rozpracované úlohy si zachovávajú pôvodný kontext.
- Profil kategórie je malý spoločný register pravidiel a parametrov, nie druhý skladový strom. Napríklad jeden profil `inner_tubes` môže mať rozdielne cieľové kódy pre BikeTrek a xTrek.
- Počiatočné pravidlá sú odvodené prevádzkové pravidlá pre slovenský obsah, Paul Lange a Northfinder. Nahrané dokumenty, privátne URL a prihlasovacie údaje nie sú v Gite. Nie všetky podrobné kategóriové a značkové dodatky zo zdrojového balíka sú automaticky publikované; ďalšie profily sa doplnia a overia postupne.
- Register určuje presné názvy, povinnosť, rozsah parent/variant, povolené hodnoty, jednotku a inštrukcie. Povinné fakty bez podkladu blokujú import. Variantné osi z feedu sa nemenia; pri kategórii s registrom musia byť zaregistrované.
- AI pomocník navrhuje text vybraného publikovaného pravidla alebo kategórie a voliteľne celý register parametrov. Prijatím vznikne iba koncept. Publikovanie vždy vykonáva používateľ, nezávisle od prepínačov automatizácie produktov.
- Duše majú samostatný profil. `automatic_import_ready=false` ponecháva ľudskú kontrolu do implementácie a overenia deterministickej matice ETRTO v kroku C.

Verzia pravidiel, pokyny, register, politika, podklady a cieľ sa zmrazia pri vytvorení úlohy. Uloženie, schválenie ani opätovné otvorenie ju neprepne na nové pravidlá; na to vytvor novú prípravu. Aj kópia variantov načítava aktuálne publikovanú verziu. Pri profile `general` a jednoznačnom mapovaní cieľovej kategórie na jeden profil sa tento profil použije automaticky.

Pri importe sa doplnia nadradené produktové kategórie; zvolená zostane hlavná. Systémové korene menu sú viditeľné v strome, ale nemožno ich priradiť produktu. Rozpoznávajú sa z explicitných metadát Upgates, nie z názvu či pevného ID. Neúplný alebo cyklický strom blokuje prípravu. Aktualizácia zachová existujúce zaradenia, doplní rodičov a nastaví vybranú hlavnú kategóriu.

## Validácia a import

AI vracia striktne definovaný obsahový JSON, nie priamo Upgates payload. Server kontroluje povinné fakty, názvy a hodnoty parametrov, príslušnosť variantov, podporované HTML a evidenciu zdrojov. Kontrola formátu nenahrádza kontrolu faktickej správnosti; preto je počiatočne zapnutá ľudská kontrola.

**Upozornenia a blokujúce chyby sú oddelené.** Nevhodná dĺžka SEO titulku či meta popisu, chýbajúci doplňujúci oficiálny zdroj a nepovinné medzery sú upozornenia. Samy osebe neblokujú schválenie. Každá položka `missing_facts`, neoverený dôkaz, povinný chýbajúci parameter, zmena identity variantu alebo nepovolené HTML blokuje schválenie aj odoslanie. Profil s `automatic_import_ready=false` navyše vždy vyžaduje človeka, aj keď nemá validačnú chybu. Opravený koncept možno uložiť a znovu validovať bez plateného generovania; nepodložené tvrdenie treba odstrániť alebo opraviť spolu s jeho dôkazom.

Feedový dôkaz `feed:<id>` sa porovnáva so skutočnými skalárnymi hodnotami podkladov po odstránení HTML a zjednotení medzier, riadkov a veľkosti písmen. Oprava viacriadkových dôkazov nahrádza porovnanie s reprezentáciou Python slovníka, ktorá escapovala riadky a úvodzovky. Citát musí byť súvislou časťou jednej hodnoty, nie spojením nesúvisiacich polí. Pri oficiálnom zdroji sa overuje HTTPS URL a otvorenie presne tejto stránky; server samostatne nepreukazuje pravdivosť tvrdenia ani doslovnú zhodu citátu so stránkou.

`validate_upgates.py` zo zdrojového balíka je XML/XSD validátor. Nespúšťa ho model a nie je vložený do promptu. API import validuje na serveri reálne odosielaný JSON; nepredstiera úspech XML validácie ako dôkaz platnosti iného formátu. Samostatný XML export a zapojenie pôvodného XML validátora sa doplnia pri rozvoji tohto formátu.

Finálne dáta vzniknú ako obmedzené obsahové doplnenie existujúceho importu. Ceny, DPH, koeficienty, kódy, EAN, fotky a výber variantov zostávajú v jeho deterministickej časti. Bezpečnostný text sa doplní zo zdroja oddelene. Cieľové H1 vlastné polia majú overené definície; chýbajúce sa vytvoria až pri importe. Šablóna H1 e-shopu sa nemení.

Pred odoslaním sa overia aktuálne podklady, cieľ, duplicity a náhľad. Po odoslaní sa využíva existujúci GET na overenie aktivity, identít, príznaku kontroly, textov a H1 polí. Neisté zápisy sa nezopakujú naslepo. Každý e-shop má vlastný náhľad a stav; neúspech jedného nevracia späť úspešný import druhého.

## Existujúci produkt a aktualizácia vybraných polí

1. Zvoľ aktívny Upgates e-shop a presný parent kód. Hub načíta texty, identitu rodiny a detail parametrov. Výber dodávateľa slúži na pravidlá, nepripája jeho feed.
2. Vyber profil a prieskum, potvrď spustenie odhadu. Spracúvajú sa spoločné texty a parent parametre; povinný variantný register sa odmietne. Existujúci marketing nie je nezávislé technické overenie.
3. Ulož obsah, označ polia a priprav **Pred / Po**. Možno použiť aj platný uložený koncept; server ho validuje a vždy vyžaduje samostatné potvrdenie aktualizácie.
4. Potvrď náhľad platný 30 minút. Pred PUT sa znovu kontrolujú hodnoty, identita a cieľ. Zápisy rovnakého produktu sú serializované; výsledok sa overí čítaním Upgates a obnoví cache detailu.

Vybrať možno názov, popisy, SEO texty, H1 polia, parametre a kategórie. `metas` mení iba `h1_descriptor`, `future_name` a `h1_descr_suffix`; zachová ostatné polia a jazyky. Parametre nahradí podľa navrhovaných názvov, ostatné zachová. Aktualizácia neposiela cenu, skladové množstvo, aktivitu, identifikátory, obrázky ani nové varianty.

Porovnanie funguje aj z feedovej prípravy pre existujúci produkt; výber musí zodpovedať celej rodine vrátane kódov a dostupných EAN. Zmenený zdroj e-shopu treba načítať znovu. Staré úlohy bez zachyteného detailu parametrov sa na aktualizáciu nepoužijú.

**Neznámy výsledok:** `uncertain` môže znamenať neoverený už vykonaný zápis. Detail ukáže nezhodné polia a dostupnú skutočnú hodnotu. Opakované `update-confirm` pre `sending` alebo `uncertain` iba číta; neopakuje PUT. `completed` už nič neposiela, `rejected` umožní nové porovnanie. Ručné odblokovanie trvajúcej neistoty zatiaľ nie je implementované.

## Dostupnosť dodávateľa a sklad

Dostupnosť z feedu určuje dodaciu lehotu, nie vlastný sklad. Kladné dodávateľské množstvo/minimum alebo externá dostupnosť použije `orderable`, inak `unknown`. Jediným zdrojom textov je konfigurácia dodávateľa `adapter_settings.availability`, predvolene `do 5 dní` / `overíme`; prázdne hodnoty sa doplnia, explicitné hodnoty (napríklad `do 7 dní`) sa zachovajú. Polia majú najviac 100 znakov a upravujú sa v Dodávatelia → Obecné. Staré AI pravidlá `orderable`, `unknown` a `hide_zero_stock` zostávajú čitateľné v histórii, ale nepoužívajú sa ani nevytvárajú konflikty pri zostavení účinnej politiky. `supplier_name` ostáva podporované. Nulová ani neznáma dodávateľská zásoba sama neskryje variant a nezakáže košík: `overíme` je objednateľné. Nový parent naďalej zostáva skrytý do kontroly s `validation_required=1`.

Náhľad zachytí účinnú politiku. Zmenená konfigurácia alebo starý náhľad bez nej vyžaduje nové porovnanie pred novým zápisom. Overenie `sending`/`uncertain` naďalej iba číta a porovnáva pôvodný payload; kvôli novej politike sa už neistý zápis neopakuje. Detail úlohy zobrazuje účinnú politiku, nie neúčinné historické AI prepísanie.

Aktualizácia `availability` funguje iba z feedovej prípravy produktu bez variantov. Pri kladnom sklade e-shopu zachová dostupnosť; pri nekladnom alebo explicitnom `stock=null` použije dodávateľskú politiku. Chýbajúci/neplatný sklad blokuje porovnanie. `null` sa nemení na nulu, sklad sa neposiela. Mení sa iba dostupnosť, nie aktivita ani košík. Úloha načítaná len z e-shopu dodávateľskú dostupnosť nemá.

`import_policy.supplier_name` pri AI vytvorení dopĺňa vlastné pole v `metas` s kľúčom `supplier_name` na parentovi a variantoch. **Nie je to vstavaný Dodávateľ v Upgates.** Pôvodný feed ho nedopĺňa a aktualizácia `metas` ho nemení. [Verejná dokumentácia produktového API](https://docs.upgates.com/api-reference/produkty) nedokladá zápis vstavaného dodávateľa; táto funkcia ho neimplementuje. Overená cesta je [priradenie vrátane hromadných úprav v administrácii Upgates](https://www.upgates.cz/a/jak-funguji-dodavatele).

## Fronta a náklady

Úlohy a audit žijú v PostgreSQL. Jeden worker v existujúcom API kontajneri používa databázový advisory lock, ktorý bráni paralelnému vykonaniu pri viacerých API procesoch. Pri reštarte sa import obnoví cez existujúci zmrazený náhľad. Rozpracované platené AI volanie bez potvrdeného výsledku prejde do `uncertain`; nikdy sa automaticky neplatí druhý pokus.

Odhad je konzervatívny (veľkosť vstupu, maximálny výstup a rezerva na oficiálny prieskum). Hub rezervuje rozpočet pred volaním. Známa spotreba sa zaznamená aj pri neplatnom výsledku; pri neistom výsledku ostáva rezervácia. Limity Hubu pracujú s odhadom, nie s garantovanou konečnou faktúrou. Limity a upozornenia nastav aj v API projekte. Model má explicitný cenník v `ai_content_provider.py`; neznámy model sa odmietne.

Režim **Feed + oficiálne zdroje** vyžaduje webové vyhľadanie (`tool_choice=required`), umožňuje automaticky nájsť oficiálne stránky výrobcu/dodávateľa bez prednastaveného zoznamu domén a najviac šesť webových krokov na volanie. AI má cielene prejsť povinné parametre, nájsť presný model podľa značky/kódu/názvu, otvoriť produktovú stránku a podľa potreby dohľadať špecifikáciu, návod alebo balenie. Odhad nákladov zahŕňa celý limit krokov. To vynucuje použitie nástroja, nie dostupnosť alebo pravdivosť všetkých údajov.

Pole `official_domains` v pravidlách zostáva spätne kompatibilné, ale slúži už iba ako voliteľná pomôcka `preferred_official_domains`, nie filter vyhľadávania. Napríklad značku PRO možno dohľadať na `pro-bikegear.com` aj bez značkového pravidla. Model má overiť prevádzkovateľa a vzťah zdroja ku značke/dodávateľovi; maloobchod, marketplace, blog ani diskusia nie sú oficiálne technické podklady. Server overuje platnú HTTPS adresu a jej presnú zhodu so skutočne otvoreným zdrojom z odpovede nástroja. Samotná táto technická kontrola nezávisle nepotvrdzuje vlastníctvo webu ani správnosť tvrdenia; to zostáva úlohou prieskumu a kontroly obsahu. Výsledok bez doplňujúceho podkladu je viditeľne označený; chýbajúce povinné hodnoty zostávajú blokujúce. Rýchly režim **Iba feed** musí používateľ zvoliť výslovne. **Skopírovať obsah** nevykonáva nové vyhľadanie; pri požiadavke na nové dohľadanie alebo po zmene pravidiel použi **Nová AI príprava**.

Katalóg a sledovanie AI fronty nevolajú Upgates. Nastavenia a identifikačné kontroly používajú existujúce cache. Importy naďalej vyžadujú overenie pred zápisom a po ňom; reálnu spotrebu Upgates treba sledovať na malej pilotnej dávke.

## Konfigurácia nezávislého API účtu

1. Vo [firemnom OpenAI Platform projekte](https://platform.openai.com/) vytvor projektový API kľúč, nastav fakturáciu a limity. Kľúč nevkladaj do chatu, zdrojového kódu ani do verejného súborového úložiska Hubu.
2. Na serveri uprav `/opt/inventory-hub/ai-content.env` (práva `600`):

```dotenv
OPENAI_API_KEY=<projektovy-kluc>
AI_CONTENT_ACCESS_TOKEN=<nahodny-token-aspon-24-znakov>
AI_CONTENT_ENABLED=true
AI_CONTENT_MODEL=gpt-5.6-sol
AI_CONTENT_MONTHLY_USD=20
AI_CONTENT_JOB_USD=2
```

3. Použi existujúce nasadenie alebo reštartuj iba API s oboma compose súbormi:

```sh
cd /opt/inventory-hub
docker compose -f docker-compose.yml -f docker-compose.ai-content.yml up -d --force-recreate api
```

4. V Hube odomkni správu pomocou **Hub access tokenu**, nie OpenAI kľúča. Token je iba v pamäti stránky a po obnovení sa znovu zadáva. OpenAI kľúč nikdy neopúšťa server.
5. Pilot: jeden nový produkt, schválenie obsahu zapnuté, aktivita vypnutá, odhad aj potvrdenie zapnuté. Porovnaj feed, texty, parametre a skutočný výsledok v Upgates. Až potom znižuj počet ručných kontrol pre overené rozsahy.

Bez API kľúča sa dajú spustiť automatické testy s náhradným poskytovateľom; nemožno tým potvrdiť kvalitu reálnej generácie ani skutočnú cenu volania.

## API

Všetky cesty nižšie okrem `/ai-content/status` vyžadujú `Authorization: Bearer <Hub token>`.

| Cesta | Účel |
|---|---|
| `GET /ai-content/status` | Stav konfigurácie bez tajomstiev |
| `GET /ai-content/rules` | Publikované pravidlá, história a rozpočet |
| `GET /ai-content/rules/{id}` | Obsah konkrétnej uloženej verzie |
| `POST /ai-content/rules` | Nový koncept pravidiel |
| `POST /ai-content/rules/{id}/publish` | Publikovanie konkrétnej verzie |
| `POST /ai-content/rules/proposal` | Príprava AI návrhu pravidla |
| `POST /ai-content/jobs/{id}/accept-proposal` | Prijatie hotového návrhu ako konceptu, bez publikovania |
| `GET /ai-content/existing-products/options` | Aktívne Upgates e-shopy pre načítanie existujúceho produktu |
| `POST /ai-content/existing-products` | Načítanie presného parent kódu a vytvorenie úlohy iba na aktualizáciu |
| `GET /ai-content/shops/{shop}/parameter-registry` | Načítanie registra parametrov z e-shopu |
| `POST /ai-content/selection` | Zoskupenie explicitného výberu z feedu |
| `POST /ai-content/batches` | Trvalé úlohy; UUID chráni opakované odoslanie |
| `GET /ai-content/jobs` | História a stav bez platených volaní |
| `GET /ai-content/jobs/{id}` | Obsah, audit a náhľad/výsledok importu |
| `POST /ai-content/jobs/{id}/review` | Uloženie alebo schválenie obsahu |
| `POST /ai-content/jobs/{id}/prices` | Nový náhľad s ručnými cenami |
| `POST /ai-content/jobs/{id}/action` | `start`, `import`, `retry_import`, `cancel`, `archive`, `restore`, `reopen` podľa aktuálneho stavu |
| `POST /ai-content/jobs/{id}/fork` | Nová príprava vybraných variantov; voliteľné použitie uloženého obsahu |
| `POST /ai-content/jobs/{id}/update-preview` | Porovnanie vybraných polí existujúceho produktu bez zápisu |
| `POST /ai-content/jobs/{id}/update-confirm` | Potvrdenie konkrétneho náhľadu alebo overenie neistého výsledku bez ďalšieho PUT |

Vyhľadávanie vo feede zostáva pod `/suppliers/.../catalog`. Existujúci surový import zostáva pod `/shops/{shop}/import`. AI náhľady sa potvrdzujú iba cez chránenú akciu AI úlohy.

Zmeny existujúcej úlohy vyžadujú `expected_revision`; zastaraná revízia sa odmietne kódom `ai_job_changed` a HTTP 409. Potvrdenie aktualizácie vyžaduje aj `preview_id`. Uloženie a publikovanie pravidiel vyžaduje `expected_published`. Opakovaný `request_id` pre dávku, existujúci produkt alebo návrh vracia tú istú prípravu; zmenené zadanie s rovnakým ID sa odmietne.

Aktuálne limity: `GET /jobs` vracia predvolene posledných 100 záznamov, `limit=1..500`, voliteľne `batch_id` a `archived=true`; nemá stránkovací kurzor a filtre v UI pracujú len s načítaným zoznamom. História pravidiel vracia najviac 40 verzií; staršiu známu verziu možno načítať podľa ID. Kniha obsahuje najviac 300 pravidiel a 300 profilov, profil najviac 100 definícií parametrov. AI generovanie podporuje aktuálne schválené slovenské pravidlá, najviac 100 000 bajtov zostaveného požiadavku, 10 000 výstupných tokenov a šesť webových krokov. Neobsahuje samostatný hromadný vstup existujúcich produktov, generovanie obrázkov ani úplnú deterministickú kontrolu ETRTO.

## Mapa implementácie pre ďalší vývoj

| Súbor alebo skupina | Zodpovednosť |
|---|---|
| `api/inventory_hub/ai_content_types.py`, `ai_content_models.py` | API kontrakty, verzované pravidlá, úlohy a audit |
| `api/inventory_hub/routers/ai_content.py` | Chránené HTTP cesty a revízne požiadavky |
| `api/inventory_hub/services/ai_content.py`, `ai_content_worker.py` | Vytvorenie, review, archív, kópie a trvalá fronta |
| `api/inventory_hub/services/ai_content_rules.py`, `ai_content_provider.py`, `ai_content_validation.py` | Skladanie pravidiel, platené volanie a kontroly skutočného obsahu |
| `api/inventory_hub/services/ai_content_existing.py`, `ai_content_update.py`, `ai_content_upgates.py` | Zdroj z e-shopu, porovnanie, potvrdenie a overenie výsledku |
| `api/inventory_hub/services/catalog_import.py`, `catalog_merchandising.py` | Existujúci importer, strom kategórií a dodávateľská dostupnosť |
| `frontend/src/pages/AiContentPage.tsx`, `frontend/src/components/product/Ai*.tsx` | Príprava, pravidlá, fronta a kontrola obsahu; texty v `frontend/src/i18n/sk.json` a `en.json` |
| `api/tests/test_ai_content.py`, `test_ai_workflow.py`, `test_ai_existing.py`, `test_category_system_roots.py` | Regresie validácie, workflow, aktualizácie a kategórií; DB varianty v `test_ai_content_db.py` |

## Otvorené UX témy

Pozorovania z dostupných stránok, snímok a kódu; zamknutý AI tok nebol celý overený v prehliadači. Nasledujúce body zostávajú na ďalšiu úpravu:

- **Produkty** v navigácii vedú na stránku „V príprave“, skutočný katalóg je pod dodávateľmi.
- V slovenskom katalógu sa dátumy zobrazujú americkým formátom.
- **Feed: Online** označuje režim pripojenia aj pri neaktívnych dodávateľoch; nevyjadruje čerstvosť feedu.
- **Získať pôvodný feed** a **Stiahnuť feed** potrebujú jasnejšie odlíšenie účelu.
- Drobné sekundárne údaje, napríklad EAN a stav cache, majú nízky kontrast.

Zámena kategóriových pravidiel a profilov je vyriešená jedným tlačidlom **Kategórie** pre správu profilov; kompatibilita so staršími pravidlami je opísaná vyššie.

## Nasadenie a návrat

Migrácia `005_ai_content.sql` je aditívna a opakovateľná; pridáva iba tabuľky AI. Build ju spustí pred štartom nového API. Prázdny serverový súbor pre tajomstvá ponecháva platené spracovanie vypnuté. Mount je mimo verejného `/data`.

Pri návrate nasadenia ponechaj tabuľky a audit zachované; nepoužívaj DROP. Vypnutie `AI_CONTENT_ENABLED` zastaví nové AI generovanie. Už výslovne schválené importy majú samostatné stavy; zruš čakajúce úlohy v UI pred úplným zastavením workflow. Pôvodný katalóg/import funguje nezávisle.

Oficiálne zdroje: [Responses Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs), [web search](https://developers.openai.com/api/docs/guides/tools-web-search), [model a cenník](https://developers.openai.com/api/docs/models/gpt-5.6-sol), [ceny nástrojov](https://developers.openai.com/api/docs/pricing). Cenník overený 21. 9. 2026.
