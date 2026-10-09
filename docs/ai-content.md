# AI obsah produktov – A + B

Funkcia používa OpenAI Responses API z Hubu. Nepotrebuje projekt/GPT v ChatGPT ani prístup k tomuto chatovému účtu. Projekt na OpenAI Platform môže patriť inému firemnému účtu a má vlastnú API fakturáciu.

## Použitie

Aktuálny výber v dodávateľskom katalógu otvára spoločnú [prípravu importu](product-imports.md). Pri úlohe s `staging_id` znamená **AI obsah pripravený** hotové texty pre tabuľku. Nové prípravy rešpektujú štyri zdedené prepínače: pri vypnutom odhade sa AI spustí hneď, pri vypnutom schválení prejde úspešne overený obsah automaticky do tabuľky a pri vypnutom potvrdení sa úplná AI príprava automaticky odošle. `active_after_import` určuje skutočnú aktivitu vytvoreného produktu. Staršie úlohy, zmiešané AI/feed dávky alebo prípravy upravené po spustení zostávajú manuálne; podrobnosti a ochrana obnovy sú v dokumentácii prípravy importu. Detail ponúka **Otvoriť prípravu importu**, po odoslaní **Otvoriť výsledok importu**, a ukazuje skutočný výsledok importéra. Ak starší import odišiel bez prevzatia AI, upozorní na to. Schválenie nezmeneného pripraveného obsahu sa už opakovane neponúka. Nižšie opísané samostatné importy a aktualizácie existujúcich produktov zostávajú dostupné pre pôvodné úlohy bez väzby na prípravu.

1. V katalógu dodávateľa vyber produkty a klikni **Pripraviť obsah / AI**. Jedna príprava prijíma najviac 500 vybraných variantov, päť e-shopov a 100 kombinácií rodina/e-shop.
2. Pre každú rodinu zvoľ **Vylepšiť obsah pomocou AI** alebo pôvodný feed. Pri AI je predvolené **Automaticky vyberie AI**: profil ani kategóriu nemusíš vyberať. Pracuje sa iba s označenými variantmi. Spoločný popis nemožno zapnúť iba pre časť variantov tej istej vybranej rodiny.
3. Vyber jeden alebo viac e-shopov. Bez zadanej kategórie AI vyberie koncovú produktovú kategóriu z existujúceho stromu; import doplní všetky jej produktové nadradené kategórie. Mapovaná alebo ručne zadaná koncová kategória sa zachová; ručne zvolená nadradená vetva sa pri automatickom výbere zúži na koncovú kategóriu. Jednoznačné prepojenie kategórie alebo jej najbližšieho nadradeného uzla na odborný profil použije Hub bez plateného triedenia; pri chýbajúcom prepojení AI vyberie iba profil podľa faktov produktu. Ručne vybraný odborný profil má prednosť pred automatickým odvodením.
4. Prepínače v príprave sú jednorazové výnimky. Trvalé nastavenia sú v **Nastavenia → AI obsah produktov → Pravidlá a kategórie**.
5. Spusti spracovanie podľa profilu. Výsledok obsahuje pôvodné podklady, upraviteľné texty, parametre, zdroje, chýbajúce fakty, upozornenia, verziu pravidiel a históriu.
6. Ceny sa naďalej upravujú v existujúcom náhľade importu vrátane hromadnej ceny variantov. Zmena ceny nepotrebuje nové platené AI spracovanie.

Chýbajúce povinné parametre sa v editore zobrazia automaticky podľa zmrazeného registra, aj pre jednotlivé varianty. Prázdne hodnoty sú zvýraznené; používateľ vyplní existujúce políčka alebo vyberie z povolených hodnôt. Žiadna hodnota vrátane Áno/Nie sa nepredpokladá. Nevyplnené riadky sa neposielajú ako neplatné prázdne parametre; povinný údaj naďalej kontroluje server. Po doplnení overeného údaja treba odstrániť iba jeho vyriešený záznam zo zoznamu chýbajúcich faktov, ostatné zostávajú.

## Pokračovanie v rozpracovanej úlohe

Úloha zostáva uložená po zatvorení stránky. Zoznam ponúka filtrovanie podľa potrebnej pozornosti, spracovania a dokončenia; detail obsahuje ďalší krok, zmrazené pravidlá, kontroly a audit.

**Otvoriť spracovanie a výsledky** otvorí samostatné modálne okno s vlastným posúvaním. Escape a zatvorenie chránia neuložené úpravy; počas prebiehajúcej požiadavky je zatvorenie dočasne vypnuté. Detail staršej úlohy upozorní, ak už existuje novšia publikovaná verzia pravidiel.

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

Pri vytvorení novej úlohy Hub najprv vyberie pravidlá podľa e-shopu, dodávateľa, značky, produktu a odborného profilu. Z rozpoznaných a obsahovo overených kapitol BIKETREK následne zostaví zadanie pre obsahové API. Zdrojová kniha zostáva nezmenená. Vynechajú sa technická XML syntax, obsluha súborov, prihlasovanie, odovzdávanie, história, nepodporované galérie a nesúvisiace spoločné kategóriové bloky. Zachovajú sa obsahové významy, pravidlá faktov, názvov, textov, SEO/H1, FAQ, parametrov a relevantné odborné kapitoly. Pri dušiach zostávajú kapitoly 7.1–7.8 vrátane úplnej tabuľky potvrdenej kompatibility a interného návodu; tabuľku musí AI vrátiť priamo v HTML popise, Hub ju dodatočne negeneruje.

Výber používa štruktúru kapitol, revíziu a presný odtlačok overeného textu, nie zhrnutie iným modelom ani filtrovanie slov. Neznáma alebo používateľom upravená kapitola sa zachová celá, kým jej nová verzia nie je overená. Starý duplicitný súhrn sa vynechá iba pri presnej zhode a prítomnosti jeho obsahových náhrad. Detail a export úlohy obsahujú použitý profil, verziu zostavovača, počty znakov, zahrnuté/vynechané kapitoly, dôvody a odtlačky. Táto diagnostika sa neposiela modelu. Staré zmrazené úlohy sa spätne neprepisujú; opravené skladanie sa použije pri novej príprave.

API vracia `Content` JSON, z ktorého Hub vytvára požiadavku do produktového API Upgates. Odstránenie pokynov k XML preto nemení transport tohto toku a neznamená implementovaný samostatný XML export. Overenie prítomnosti všetkých požiadaviek v zostavenom zadaní nie je zárukou totožného textu pri každom generovaní.

**Import a export knihy pravidiel** v editore prenáša celú knihu vrátane kategórií, rozsahov a politík všetkých e-shopov. Exportuje práve načítanú knihu ako JSON. Súbor pre import môže byť čistý `RuleBook` alebo výstup s objektom `book`, najviac 2 MiB. Výber súboru ukáže názov a počty; nevloží neoverené položky do editora ani ich neodošle. Po vyplnení dôvodu akcia **Uložiť importovanú knihu ako koncept** použije existujúcu serverovú validáciu a kontrolu `expected_published`, uloží novú nepublikovanú verziu a načíta jej normalizovaný obsah. Chyba načítania už uloženého konceptu sa obnovuje iba GET bez druhého uloženia. Pred uložením treba skontrolovať zachovanie ostatných e-shopov a politík; publikovanie zostáva samostatnou akciou. Import knihy nespúšťa AI ani import produktov. Úplný zdrojový balík s `documents` a `modules` nie je priamo `RuleBook`.

Tlačidlo **Kategórie** otvára profily kategórií. Pokyny, registre parametrov aj mapovanie na e-shopy sa upravujú na jednom mieste; samostatné tlačidlo **Profily kategórií** ani pôvodná skupina kategóriových pravidiel už v navigácii nie sú.

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
- AI pomocník navrhuje text vybraného publikovaného pravidla alebo kategórie a voliteľne celý register parametrov. Prijatím vznikne iba koncept. Publikovanie je samostatná autorizovaná akcia, nezávisle od prepínačov automatizácie produktov.
- Duše majú samostatný profil. `automatic_import_ready=false` ponecháva ľudskú kontrolu do implementácie a overenia deterministickej matice ETRTO v kroku C.

Verzia pravidiel, pokyny, register, politika, podklady a cieľ sa zmrazia pri vytvorení úlohy. Uloženie, schválenie ani opätovné otvorenie ju neprepne na nové pravidlá; na to vytvor novú prípravu. Aj kópia variantov načítava aktuálne publikovanú verziu. Pri profile `general` a jednoznačnom mapovaní cieľovej kategórie na jeden profil sa tento profil použije automaticky.

Pri importe sa doplnia nadradené produktové kategórie; zvolená zostane hlavná. Systémové korene menu sú viditeľné v strome, ale nemožno ich priradiť produktu. Rozpoznávajú sa z explicitných metadát Upgates, nie z názvu či pevného ID. Neúplný alebo cyklický strom blokuje prípravu. Aktualizácia zachová existujúce zaradenia, doplní rodičov a nastaví vybranú hlavnú kategóriu.

### Štruktúrované registre a čiastočné parametre

Pravidlo môže cez `category_profiles` odkazovať na viac profilov. Zoznam je OR, ostatné podmienky `scope` zostávajú AND; prázdny zoznam zachová pôvodné správanie. Spoločný blok sa pripojí raz, bez kopírovania celého textu do každého profilu. Editor kategórie zobrazuje aj úplné priradené účinné bloky. Neexistujúci odkaz sa pri uložení odmietne.

`registry_status` odlišuje schválený register, návrh, zmiešaný základ a chýbajúci register. `approved=false` označuje návrhové pole: zostáva viditeľné v editore, ale resolver ho nedá do požiadavku a validátor ho neprijme. Označenie kategórie samo neschvaľuje jej jednotlivé návrhy. Staré knihy bez týchto polí zachovávajú pôvodné správanie.

`scope=choice` znamená skutočnú výberovú os: ak presný názov existuje vo `facts.variant_attributes`, platí variantový rozsah a presné pôvodné hodnoty. Inak je to parameter parenta s `product_id=null`. Nevytvára sa nová os ani nové SKU. Pevné rozsahy parent/variant fungujú ako doteraz.

Podľa aktuálneho pokynu BIKETREK sa prevedené registre publikujú s `required=false`. AI má preveriť všetky relevantné polia a doplniť doložené hodnoty; nezistené nepovinné údaje vynechá a relevantné medzery uvedie vo `warnings`. Samotná medzera neblokuje import. Konflikt identity, neplatná hodnota, zmena variantu a nepodložené tvrdenie zostávajú blokujúce. Označenia Z/P v pôvodných podkladoch nesmú obnoviť povinnosť parametra. Existujúce prepínače ľudskej kontroly sa tým nemenia.

**Automatický výber (`auto`)** je samostatné platené volanie pred tvorbou obsahu. Zmrazené podklady a existujúci strom umožnia vybrať iba aktívnu koncovú kategóriu a existujúci profil. Mapovanie z najbližšieho produktového predka obmedzí povolené profily. Neexistujúci kód, nadradená kategória, nekompatibilný profil alebo neistý výsledok zastaví spracovanie pred generovaním textu. Zvolený profil následne určí celý register parametrov a účinné pravidlá z pôvodnej zmrazenej verzie; detail ukáže cestu a dôvod výberu. Ide o AI klasifikáciu, nie záruku faktickej správnosti.

Ručný výber zachováva poradie: výslovný profil → jednoznačné presné `shop_categories` → jednoznačná zhoda v `shop_category_matches` → všeobecný profil. API bez výslovného `auto` zachováva tento starší kontrakt. Pri načítaní existujúceho produktu automatický výber rešpektuje jeho hlavnú vetvu; zmeny kategórií sa stále odosielajú iba cez výber polí a potvrdené porovnanie. Zoznam zhôd nevytvára kategórie v Upgates ani ďalšie nesúvisiace zaradenia. **Nová AI príprava** z detailu používa aktuálne pravidlá a nový automatický výber; kopírovanie hotového obsahu nevolá klasifikátor.

Detail úlohy ponúka **Stiahnuť presné zadanie pre AI (bez spustenia)**. Chránený `GET /ai-content/jobs/{id}/request` zostaví reálny provider body zo zmrazeného kontextu a aktuálnej implementácie buildera; nespustí AI, rezerváciu nákladov ani import. Ide o zadanie, ktoré by sa odoslalo teraz, nie záznam historickej HTTP komunikácie. Súbor obsahuje súkromné podklady produktu a pravidlá, nie API kľúč. Nové pravidlá sa prejavia iba v novej príprave.

Pole `stage` v exporte odlišuje výber kategórie od tvorby produktu. Pred dokončením klasifikácie export obsahuje jej skutočné zadanie; potom zadanie obsahu s vybraným registrom.

Súkromný prevod dokumentu Kategórie, parametre a filtre nie je súčasťou verejného Git. Jeho uloženie a publikovanie sú samostatným krokom po nasadení tejto podpory. XML nástroje, úplné automatické zoskupovanie rodín, správa galérie a nastavenie zákazníckych filtrov tým nie sú implementované.

## Validácia a import

### Úplné súkromné balíky pravidiel

`python -m inventory_hub.ai_rule_package` pripravuje prenos úplných pravidiel
bez spojenia s databázou, Hubom, Upgates alebo plateným AI. Vstupný adresár
musí byť určený výslovne; produktové pokyny a chránené feedové endpointy sa
nepribaľujú do verejného repozitára. Balík zachováva celé bloky, ich pôvod,
revízie, rozsahy, návrhy aj históriu. História sa nepoužíva ako ďalšie účinné
pravidlá. Prístupové URL patria do bezpečnej konfigurácie, nie do promptu.

Dokument označený `required_full_read: true` zachová pri zostavení aj svoje
referenčné a historické bloky v zodpovedajúcom rozsahu. Hlavné inštrukcie
majú tento príznak, aby sa prečítal celý dokument vrátane histórie. Každá
časť uvádza typ a stav; história a referencie sú výslovne označené ako
neoperatívny kontext, ktorý neobnovuje staršie pravidlá. Pri ostatných
dokumentoch sa naďalej vyberajú iba relevantné účinné bloky. Príznak
neobchádza odmietnutie návrhových alebo zmiešaných pravidiel.

Príklady s privátnym vstupom a výstupom mimo Git:

```sh
PYTHONPATH=api python -m inventory_hub.ai_rule_package validate --package-dir /private/rule-package
PYTHONPATH=api python -m inventory_hub.ai_rule_package export --package-dir /private/rule-package --output /private/rules.json
PYTHONPATH=api python -m inventory_hub.ai_rule_package draft --package-dir /private/rule-package --shop biketrek --supplier paul-lange --brand Shimano --profile general --output /private/rules-draft.json
```

Export je úplný prenosný balík s kontrolným súčtom. `draft` je koncept
existujúceho formátu `RuleBook` pre jeden výslovne vybraný rozsah; nie je to
nová publikovaná verzia. Celé relevantné bloky rozdelí podľa limitu polí bez
straty textu. Návrhový alebo zmiešaný register automaticky neaktivuje.
Podmienené znalosti vyžadujú výslovný výber opretý o potvrdené vlastnosti.
Pri takom výbere je povinný presný parent kód cez `--product`; znalosť
potvrdeného materiálu sa nesmie preniesť na iný produkt rovnakej značky.
Pred prepísaním existujúceho súboru skončí chybou.

Pri `--current-book` zachová nedotknuté rozsahy; kolidujúce účinné pravidlá
alebo profil vyžadujú vedomé zjednotenie. Bez aktuálnej knihy nie je koncept
úplnou náhradou produkčnej knihy. Až samostatné autorizované uloženie a
publikovanie cez existujúce API s `expected_published` aktivuje novú verziu.
Zmena `initial_book` existujúcu publikovanú verziu nemení. Rozpracované úlohy
si zachovajú zmrazené pravidlá; nová príprava načíta aktuálnu verziu.

Prítomnosť celého textového registra neznamená jeho premenu na strojové
definície parametrov. Koncepty zostávajú s povinnou ľudskou kontrolou a bez
automatického importu. Rodiny, galérie, B2B, kontroly A/B/C, XML nástroje a
konkrétne dodávateľské výnimky potrebujú príslušnú vykonávaciu podporu.
Podrobnosti ďalšieho postupu sú v [návrhu príjmu a Upgates](receiving-upgates-plan.md).

Hranica zostaveného AI požiadavku je **512 000 bajtov**, aby sa úplné
relevantné pravidlá nemuseli skracovať. Odhad nákladov používa celý skutočný
požiadavok a existujúce rozpočtové limity zostávajú zachované. Prípravný
nástroj meria skutočnú schému požiadavku bez produktových dát; runtime
skontroluje kompletné dáta znovu. Žiadny úspech offline kontroly nepotvrdzuje
kvalitu generovania, dostupnosť zdrojov ani vykonaný import.

AI vracia striktne definovaný obsahový JSON, nie priamo Upgates payload. Server kontroluje povinné fakty, názvy a hodnoty parametrov, príslušnosť variantov, aktívny kód v HTML a evidenciu zdrojov. Kontrola formátu nenahrádza kontrolu faktickej správnosti; preto je počiatočne zapnutá ľudská kontrola.

Bežné HTML dlhého popisu sa neobmedzuje zoznamom značiek ani plošným zákazom atribútov. Tabuľky so `scope`, `colspan` a `rowspan`, `div`, `span`, triedy, statický inline štýl, odkazy a obrázky samy osebe neblokujú prípravu. Server HTML kvôli tejto kontrole neprepisuje. Zostáva úzka blokácia skriptov, udalostí `on*`, vložených dokumentov, SVG/MathML, zmien hlavičky dokumentu, stylesheetov, `srcdoc`, spustiteľných/data URL a aktívneho CSS. Ide o kontrolu aktívneho obsahu, nie o všeobecný HTML sanitizér alebo záruku vzhľadu v Upgates. Ostatné textové polia zostávajú bez HTML.

Kontrola používa HTML5 parser `html5lib` so zapnutou interpretáciou `noscript` ako v prehliadači s JavaScriptom. Neštandardné komentáre a deklarácie sa posudzujú podľa výsledných elementov, aby neskryli skript alebo udalosť pred kontrolou. Pôvodný limit 50 000 znakov dlhého popisu zostáva zachovaný.

U staršej úlohy blokovanej iba `ai_unsafe_html` otvor detail a použi **Znovu overiť obsah bez AI**. Kontrola načíta uložený obsah a pri bežnom formátovaní odstráni zastaranú chybu; platená príprava sa neopakuje. Nasleduje bežné schválenie a náhľad importu podľa politiky úlohy. Nasadenie samo neschváli ani neodošle rozpracované produkty.

Produktový kontrakt automaticky mení typografické spojovníky a pomlčky na bežné `-` v názve, krátkom aj dlhom popise, SEO titulku, meta popise a troch H1 poliach. Platí pri načítaní výstupu AI, uložení ľudskej úpravy aj zostavení importného/aktualizačného obsahu vrátane staršieho uloženého konceptu. Menia sa aj HTML entity pomlčiek (`&ndash;`, `&mdash;`, číselné zápisy); ostatné entity, HTML a matematické mínus sa zachovajú. URL podkladov, doslovné citáty a hodnoty registrovaných parametrov sa týmto pravidlom neprepisujú. Nasadenie samo nespúšťa hromadnú opravu už importovaných produktov.

**Ručné schválenie môže prijať obsahové výhrady.** Tlačidlo **Schváliť obsah a pripraviť import** prijme `ai_missing_facts`. Táto výhrada sa zmení na upozornenie; pôvodné chýbajúce fakty zostanú uložené. `ai_unverified_feed_evidence` a `ai_unverified_official_evidence` sú neblokujúce upozornenia už pri bežnej validácii, vrátane automatického schválenia a importu; citáty a diagnostika zdrojov zostávajú uložené. Detail označí ručné prijatie a história zaznamená prijaté kódy. Netreba mazať upozornenia ani znovu volať AI. Rovnaké rozhodnutie rešpektuje následná príprava importu, schválenie existujúceho produktu, náhľad aktualizácie aj prevzatie AI do importnej tabuľky.

Automatické schválenie neprijíma `ai_missing_facts`. Nesúlad citátu, formát URL ani chýbajúci záznam otvorenia zdroja samy import neblokujú. **Uložiť koncept obsahu** ani **Znovu overiť obsah bez AI** nie sú schválením: vykonajú plnú kontrolu a zrušia predchádzajúce schválenie. Povinný chýbajúci parameter, neplatná hodnota či rozsah, zmena identity variantu, aktívny kód v HTML a technické kontroly importu zostávajú blokujúce aj pri ručnom schválení. Profil s `automatic_import_ready=false` vyžaduje človeka. Dĺžka SEO titulku/meta popisu a nepovinné medzery zostávajú bežnými upozorneniami.

Feedový dôkaz `feed:<id>` sa porovnáva so skalárnymi hodnotami zmrazených podkladov po odstránení HTML a zjednotení medzier, riadkov, veľkosti písmen a spojovníkov U+2010/U+2011. Znamienko mínus, pomlčky rozsahov, čísla a jednotky sa nemenia. Citát môže byť súvislým úsekom jednej hodnoty alebo presnými dvojicami **názov: hodnota** z `parameters`/`variant_attributes` rovnakého produktu, oddelenými bodkočiarkou alebo novým riadkom. Napríklad `Veľkosť plášťa: 19"; Ventil duše: AV - autoventil` sa overí po dvojiciach. Každý názov musí patriť k uvedenej hodnote; neprepájajú sa ľubovoľné polia ani produkty. Normalizácia neprepisuje uložený citát ani feed.

Schéma citátu a produktové zadanie odporúčajú krátky doslovný úsek zdroja alebo uvedené štruktúrované dvojice. Zhrnutie, preklad či vysvetlenie patrí do `claim` a popisu. Pri oficiálnom zdroji sa kontroluje HTTPS URL a zaznamenané otvorenie tejto stránky. Chýbajúci záznam otvorenia sám osebe neznamená, že vydavateľ je neoficiálny; server samostatne nepreukazuje pravdivosť tvrdenia ani doslovnú zhodu citátu so stránkou.

Schéma aj zadanie AI výslovne určujú `evidence.source`: `feed:<id>` alebo presná úplná otvorená HTTPS URL. Starší formát `official:https://…` sa pri načítaní obsahového kontraktu normalizuje odstránením samotného prefixu. Diagnostická kontrola porovnáva presnú zhodu s uloženými `opened_sources`, bez prihlasovacích údajov a query parametrov; nesúlad sa zobrazí ako neblokujúce upozornenie. Slovné označenie ako `official:manufacturer.example Model` sa nikdy automaticky nepriradí k stránke. Neplatná URL vrátane chybného IPv6 či portu sa zobrazí ako upozornenie podkladu namiesto výnimky pri validácii.

V sekcii **Parametre, zdroje a chýbajúce údaje** možno opraviť zdroj, citát aj súvisiace tvrdenie; návrhy adries vychádzajú iba zo stránok zaznamenaných pri danej príprave. Vybrať treba podklad, ktorý skutočne dokladá konkrétne tvrdenie a správny model. Pri výhrade k feedovému citátu možno opraviť podklad a tvrdenie; samotná výhrada nevyžaduje ručné schválenie. Opravu potvrď cez **Uložiť koncept obsahu**. Kontrola vráti chybný riadok a odlíši neplatnú adresu, zakázané query parametre, nezaznamenané otvorenie a chýbajúci feedový citát. **Znovu overiť obsah bez AI** používa existujúce uloženie konceptu a novú validáciu; nevolá poskytovateľa, neschvaľuje obsah ani nespúšťa import. Staršie uložené chyby sa aktualizujú pri tomto overení alebo pri uložení opravy. Pri neuzavretom zápise do e-shopu sú úpravy naďalej blokované.

Oprava kódu nemení sama uložené úlohy ani publikovanú knihu pravidiel. Príprava s prázdnym výstupom v stave `uncertain` nemá obsah, ktorý by sa dal touto cestou opraviť; presný dôvod treba čítať z `error` a histórie udalostí. Platený pokus sa automaticky neopakuje.

`validate_upgates.py` zo zdrojového balíka je XML/XSD validátor. Nespúšťa ho model a nie je vložený do promptu. API import validuje na serveri reálne odosielaný JSON; nepredstiera úspech XML validácie ako dôkaz platnosti iného formátu. Samostatný XML export a zapojenie pôvodného XML validátora sa doplnia pri rozvoji tohto formátu.

Finálne dáta vzniknú ako obmedzené obsahové doplnenie existujúceho importu. Ceny, DPH, koeficienty, kódy, EAN, fotky a výber variantov zostávajú v jeho deterministickej časti. Bezpečnostný text sa doplní zo zdroja oddelene. Cieľové H1 vlastné polia majú overené definície; chýbajúce sa vytvoria až pri importe. Šablóna H1 e-shopu sa nemení.

Pred odoslaním sa overia aktuálne podklady, cieľ, duplicity a náhľad. Po odoslaní sa využíva existujúci GET na overenie aktivity, identít, príznaku kontroly, textov a H1 polí. Neisté zápisy sa nezopakujú naslepo. Každý e-shop má vlastný náhľad a stav; neúspech jedného nevracia späť úspešný import druhého.

## Existujúci produkt a aktualizácia vybraných polí

1. Zvoľ aktívny Upgates e-shop a presný parent kód. Hub načíta texty, identitu rodiny a detail parametrov. Výber dodávateľa slúži na pravidlá, nepripája jeho feed.
2. Ponechaj automatický profil alebo ho vyber ručne, zvoľ prieskum a potvrď spustenie odhadu. Spracúvajú sa spoločné texty a parent parametre; povinný variantný register sa odmietne. Existujúci marketing nie je nezávislé technické overenie.
3. Ulož obsah, označ polia a priprav **Pred / Po**. Možno použiť aj platný uložený koncept; server ho validuje a vždy vyžaduje samostatné potvrdenie aktualizácie.
4. Potvrď náhľad platný 30 minút. Pred PUT sa znovu kontrolujú hodnoty, identita a cieľ. Zápisy rovnakého produktu sú serializované; výsledok sa overí čítaním Upgates a obnoví cache detailu.

Vybrať možno názov, popisy, SEO texty, H1 polia, parametre a kategórie. `metas` mení iba `h1_descriptor`, `future_name` a `h1_descr_suffix`; zachová ostatné polia a jazyky. Parametre nahradí podľa navrhovaných názvov, ostatné zachová. Aktualizácia neposiela cenu, skladové množstvo, aktivitu, identifikátory, obrázky ani nové varianty.

Porovnanie funguje aj z feedovej prípravy pre existujúci produkt; výber musí zodpovedať celej rodine vrátane kódov a dostupných EAN. Zmenený zdroj e-shopu treba načítať znovu. Staré úlohy bez zachyteného detailu parametrov sa na aktualizáciu nepoužijú.

**Neznámy výsledok:** `uncertain` môže znamenať neoverený už vykonaný zápis. Detail ukáže nezhodné polia a dostupnú skutočnú hodnotu. **Overiť výsledok aktualizácie** iba číta; neopakuje PUT a samotná zhodná hodnota neuvoľní blokovanie. Platí to aj po timeout-e zápisu, pretože pôvodná požiadavka môže doraziť neskôr. Až po overení, že pôvodná požiadavka už nemôže doraziť, obsluha vyplní dôvod, označí príslušné potvrdenie a použije **Uzavrieť nejasný výsledok**. Zhoda sa uzavrie ako `completed`, potvrdená ukončená požiadavka s odlišnou hodnotou ako `rejected`, aby sa dal pripraviť nový náhľad. Pri nedostupnom čítaní alebo zmenenej identite blokovanie zostáva. Stav `sending` umožní toto ručné vyriešenie najskôr po piatich minútach; čas sám nepreukazuje ukončenie požiadavky. Dôvod a potvrdenie zostávajú v uloženom náhľade.

AI aktualizácia a ručné odoslanie polí z tabuľky produktov používajú spoločnú trvalú blokáciu cieľa cez `services/merchandising_write_guard.py`. Tesne pred zápisom stavu `sending` získajú spoločný transakčný `IDENTITY_WRITE_LOCK` a skontrolujú druhý workflow v tabuľkách `ai_content_jobs` a `product_editor_publications`. Stav `sending` alebo `uncertain` blokuje ďalší zápis rovnakého parent kódu v danom e-shope aj po reštarte. Varianty pod pokladňovým parentom sa tu posudzujú konzervatívne ako jeden cieľ vzdialeného zápisu; nemení to ich samostatnú skladovú identitu. Kontrola čerstvých vzdialených údajov pred PUT a overenie po ňom zostávajú zachované. Blokácia nebráni úpravám vykonaným priamo v administrácii Upgates.

## Dostupnosť dodávateľa a sklad

Dostupnosť z feedu určuje dodaciu lehotu, nie vlastný sklad. Kladné dodávateľské množstvo/minimum alebo externá dostupnosť použije `orderable`, inak `unknown`. Jediným zdrojom textov je konfigurácia dodávateľa `adapter_settings.availability`, predvolene `do 5 dní` / `overíme`; prázdne hodnoty sa doplnia, explicitné hodnoty (napríklad `do 7 dní`) sa zachovajú. Polia majú najviac 100 znakov a upravujú sa v Dodávatelia → Obecné. Staré AI pravidlá `orderable`, `unknown` a `hide_zero_stock` zostávajú čitateľné v histórii, ale nepoužívajú sa ani nevytvárajú konflikty pri zostavení účinnej politiky. `supplier_name` ostáva podporované. Nulová ani neznáma dodávateľská zásoba sama neskryje variant a nezakáže košík: `overíme` je objednateľné. Bežný feedový parent zostáva skrytý do kontroly s `validation_required=1`; schválený AI import rešpektuje zmrazené `active_after_import` vrátane spoločnej prípravy importu.

Náhľad zachytí účinnú politiku. Zmenená konfigurácia alebo starý náhľad bez nej vyžaduje nové porovnanie pred novým zápisom. Overenie `sending`/`uncertain` naďalej iba číta a porovnáva pôvodný payload; kvôli novej politike sa už neistý zápis neopakuje. Detail úlohy zobrazuje účinnú politiku, nie neúčinné historické AI prepísanie.

Aktualizácia `availability` funguje iba z feedovej prípravy produktu bez variantov. Pri kladnom sklade e-shopu zachová dostupnosť; pri nekladnom alebo explicitnom `stock=null` použije dodávateľskú politiku. Chýbajúci/neplatný sklad blokuje porovnanie. `null` sa nemení na nulu, sklad sa neposiela. Mení sa iba dostupnosť, nie aktivita ani košík. Úloha načítaná len z e-shopu dodávateľskú dostupnosť nemá.

Ak má e-shop potvrdenú správu zásob cez pravidelný prenos (`stock_sync_settings.authorized=true`), AI nesmie odoslať pole `availability`; z výberu ho treba odstrániť. Platí to aj pri dočasne vypnutom rozvrhu či serverovom prepínači zápisu, pretože vlastníctvo dostupnosti zostáva skladu. Ani odobratie oprávnenia neuvoľní dostupnosť pre AI, kým existuje prenos zásob v stave `sending` alebo `uncertain`. Kontrola prebehne pod spoločným `IDENTITY_WRITE_LOCK` tesne pred uložením zámeru odoslania. Opačne sa správa zásob nesmie aktivovať počas neuzavretej AI aktualizácie dostupnosti. Aktivácia získava tento zámok pred zámkami e-shopu/skladu a až potom uloží oprávnenie; AI zámer nemôže vzniknúť medzi kontrolou a aktiváciou. Ostatné obsahové polia AI týmto vlastníctvom blokované nie sú. Táto kontrola navyše vyžaduje existujúcu migráciu `015`.

`import_policy.supplier_name` pri AI vytvorení dopĺňa vlastné pole v `metas` s kľúčom `supplier_name` na parentovi a variantoch. **Nie je to vstavaný Dodávateľ v Upgates.** Pôvodný feed ho nedopĺňa a aktualizácia `metas` ho nemení. [Verejná dokumentácia produktového API](https://docs.upgates.com/api-reference/produkty) nedokladá zápis vstavaného dodávateľa; táto funkcia ho neimplementuje. Overená cesta je [priradenie vrátane hromadných úprav v administrácii Upgates](https://www.upgates.cz/a/jak-funguji-dodavatele).

## Fronta a náklady

**Nastavenia → AI obsah produktov → Model a náklady** obsahuje trvalý výber modelu. Podporované modely sú `gpt-5.6-sol`, `gpt-6.1-sol`, `gpt-6-luna` a `gpt-6-astra`; všetky používajú existujúci štruktúrovaný výstup a podľa režimu oficiálny webový prieskum. Nové úlohy Luna používajú `reasoning.effort=high`, ostatné modely ponechávajú `low`. Úroveň a limity sa zmrazia pri vytvorení úlohy: Luna má 25 000 tokenov na obsah vrátane uvažovania a 8 000 na klasifikáciu; staršie úlohy bez týchto polí zachovajú pôvodné `low` a limity 10 000/1 000. Bez uloženého výberu platí serverová hodnota `AI_CONTENT_MODEL`; ak nie je explicitne nastavená, predvoľba v kóde je lacnejší `gpt-6-luna`. Uloženie vyžaduje prihlásenie a zhodnú revíziu nastavení; nevolá model a nepotrebuje reštart servera. Nová dávka si model uloží do svojho kontextu. Rozpracované úlohy ani opakované spustenie tej istej úlohy sa po zmene globálneho nastavenia neprepnú. Nová príprava, návrh pravidiel aj príprava existujúceho produktu používajú aktuálne nastavenie.

Cenník počíta bežný vstup, načítanie z cache, zápis do cache, výstup a platené vyhľadávania osobitne. `web_calls` zaznamenáva všetky webové kroky; poplatok používa `billable_search_calls`. Otvorenie stránky a hľadanie v nej nemajú samostatný poplatok za vyhľadanie, ich tokeny zostávajú súčasťou spotreby. Neznámy alebo starý typ webovej akcie sa účtuje konzervatívne. Nad 272 000 vstupných tokenov používa sadzby dlhého kontextu pre celé volanie. Konzervatívna rezervácia počíta aj s možnosťou zápisu vstupu do cache. Zobrazené ceny modelov nie sú garantovanou cenou za produkt; skutočné náklady závisia od spotreby. Nasadenie zachováva uložený výber v Hube aj explicitnú serverovú konfiguráciu. Zmena predvoľby v kóde tieto nastavenia neprepisuje; produkčný výber treba overiť v nastaveniach alebo cez `GET /ai-content/settings`.

Úlohy a audit žijú v PostgreSQL. Jeden worker v existujúcom API kontajneri používa databázový advisory lock, ktorý bráni paralelnému vykonaniu pri viacerých API procesoch. Pri reštarte sa import obnoví cez existujúci zmrazený náhľad. Rozpracované platené AI volanie bez potvrdeného výsledku prejde do `uncertain`; nikdy sa automaticky neplatí druhý pokus.

Odhad je konzervatívny (veľkosť vstupu, maximálny výstup a rezerva na oficiálny prieskum). Hub rezervuje rozpočet pred volaním. Známa spotreba sa zaznamená aj pri neplatnom výsledku; pri neistom výsledku ostáva rezervácia. Limity Hubu pracujú s odhadom, nie s garantovanou konečnou faktúrou. Limity a upozornenia nastav aj v API projekte. Model má explicitný cenník v `ai_content_provider.py`; neznámy model sa odmietne.

Pri automatickej kategórii odhad zahŕňa klasifikáciu (bez webu, najviac 1 000 výstupných tokenov) a najväčší obsahový požiadavok z možných profilov. Ak niektorý profil vyžaduje potvrdenie odhadu, zastávka sa zachová. Spotreba klasifikácie sa uloží ešte pred parsovaním; pri prerušení následného generovania zostáva plná rezervácia. Žiadna fáza sa po nejasnom výsledku automaticky neopakuje.

Režim **Feed + oficiálne zdroje** vyžaduje webové vyhľadanie (`tool_choice=required`), umožňuje automaticky nájsť oficiálne stránky výrobcu/dodávateľa bez prednastaveného zoznamu domén a najviac šesť webových krokov na volanie. AI má cielene prejsť povinné parametre, nájsť presný model podľa značky/kódu/názvu, otvoriť produktovú stránku a podľa potreby dohľadať špecifikáciu, návod alebo balenie. Odhad nákladov zahŕňa celý limit krokov. To vynucuje použitie nástroja, nie dostupnosť alebo pravdivosť všetkých údajov.

Pole `official_domains` v pravidlách zostáva spätne kompatibilné, ale slúži už iba ako voliteľná pomôcka `preferred_official_domains`, nie filter vyhľadávania. Napríklad značku PRO možno dohľadať na `pro-bikegear.com` aj bez značkového pravidla. Model má overiť prevádzkovateľa a vzťah zdroja ku značke/dodávateľovi; maloobchod, marketplace, blog ani diskusia nie sú oficiálne technické podklady. Server overuje platnú HTTPS adresu a jej presnú zhodu so skutočne otvoreným zdrojom z odpovede nástroja. Nesúlad je neblokujúce upozornenie. Samotná táto technická kontrola nezávisle nepotvrdzuje vlastníctvo webu ani správnosť tvrdenia; to zostáva úlohou prieskumu a kontroly obsahu. Výsledok bez doplňujúceho podkladu je viditeľne označený; chýbajúce povinné hodnoty zostávajú blokujúce. Rýchly režim **Iba feed** musí používateľ zvoliť výslovne. **Skopírovať obsah** nevykonáva nové vyhľadanie; pri požiadavke na nové dohľadanie alebo po zmene pravidiel použi **Nová AI príprava**.

Katalóg a sledovanie AI fronty nevolajú Upgates. Nastavenia a identifikačné kontroly používajú existujúce cache. Importy naďalej vyžadujú overenie pred zápisom a po ňom; reálnu spotrebu Upgates treba sledovať na malej pilotnej dávke.

## Konfigurácia nezávislého API účtu

1. Vo [firemnom OpenAI Platform projekte](https://platform.openai.com/) vytvor projektový API kľúč, nastav fakturáciu a limity. Kľúč nevkladaj do chatu, zdrojového kódu ani do verejného súborového úložiska Hubu.
2. Na serveri uprav `/opt/inventory-hub/ai-content.env` (práva `600`):

```dotenv
OPENAI_API_KEY=<projektovy-kluc>
AI_CONTENT_ACCESS_TOKEN=<nahodny-token-aspon-24-znakov>
AI_CONTENT_ENABLED=true
AI_CONTENT_MODEL=gpt-6-luna
AI_CONTENT_MONTHLY_USD=20
AI_CONTENT_JOB_USD=2
```

3. Použi existujúce nasadenie alebo reštartuj iba API s oboma compose súbormi:

```sh
cd /opt/inventory-hub
docker compose -f docker-compose.yml -f docker-compose.ai-content.yml up -d --force-recreate api
```

4. V Hube sa prihlás cez **Používateľské účty**. Prvé prihlásenie podporuje **Hub access token**, potom správca vytvorí účty s menom a heslom. Relácia pretrvá obnovenie aj zatvorenie prehliadača; samotný token sa do jeho úložiska neukladá. [Správa účtov a relácií](user-accounts.md). OpenAI kľúč nikdy neopúšťa server.
5. Pilot: jeden nový produkt, schválenie obsahu zapnuté, aktivita vypnutá, odhad aj potvrdenie zapnuté. Porovnaj feed, texty, parametre a skutočný výsledok v Upgates. Až potom znižuj počet ručných kontrol pre overené rozsahy.

Bez API kľúča sa dajú spustiť automatické testy s náhradným poskytovateľom; nemožno tým potvrdiť kvalitu reálnej generácie ani skutočnú cenu volania.

## API

Všetky cesty nižšie okrem `/ai-content/status` vyžadujú platnú používateľskú reláciu alebo `Authorization: Bearer <Hub token>`. Zápisy cez cookie navyše vyžadujú rovnaký pôvod a hlavičku `X-Hub-Request: 1`.

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

Rušenie nejasnosti pri `update-confirm` navyše vyžaduje skutočný JSON boolean `original_request_settled: true` a `resolution_note` s aspoň 10 neprázdnymi znakmi po orezaní. Bez nich ide iba o čítaciu kontrolu. Spoločná blokácia potrebuje existujúce migrácie `005` a `016`; nepridáva ďalšiu tabuľku ani automaticky neopakuje neisté zápisy.

Aktuálne limity: `GET /jobs` vracia predvolene posledných 100 záznamov, `limit=1..500`, voliteľne `batch_id` a `archived=true`; nemá stránkovací kurzor a filtre v UI pracujú len s načítaným zoznamom. História pravidiel vracia najviac 40 verzií; staršiu známu verziu možno načítať podľa ID. Kniha obsahuje najviac 300 pravidiel a 300 profilov, profil najviac 100 definícií parametrov. AI generovanie podporuje aktuálne schválené slovenské pravidlá, najviac 512 000 bajtov zostaveného požiadavku, 10 000 výstupných tokenov a šesť webových krokov. Neobsahuje samostatný hromadný vstup existujúcich produktov, generovanie obrázkov ani úplnú deterministickú kontrolu ETRTO.

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

Migrácia `023_ai_content_settings.sql` pridáva iba prázdnu tabuľku trvalého výberu modelu. API obraz ju obsahuje a deployment ju explicitne vykoná po 022 pred reštartom API. Nič negeneruje, nemení pôvodné úlohy ani nepublikuje produkty. Pri návrate kódu tabuľku ponechaj; starší kód bude pre nové úlohy opäť používať serverové nastavenie. Už uložený model jednotlivých úloh zostáva súčasťou ich kontextu. Pred návratom na kód bez cenníka nových modelov dokonči alebo zruš ich čakajúce úlohy a zastav nové generovanie; starší poskytovateľ neznámy model odmietne.

Migrácia `005_ai_content.sql` je aditívna a opakovateľná; pridáva iba tabuľky AI. Build ju spustí pred štartom nového API. Prázdny serverový súbor pre tajomstvá ponecháva platené spracovanie vypnuté. Mount je mimo verejného `/data`.

Pri návrate nasadenia ponechaj tabuľky a audit zachované; nepoužívaj DROP. Vypnutie `AI_CONTENT_ENABLED` zastaví nové AI generovanie. Už výslovne schválené importy majú samostatné stavy; zruš čakajúce úlohy v UI pred úplným zastavením workflow. Pôvodný katalóg/import funguje nezávisle.

Pred návratom na verziu bez automatickej klasifikácie vypni AI generovanie a vyrieš čakajúce nové `auto` úlohy. Starý worker ich nesmie spracovať ako všeobecný obsah. Relácie vyžadujú migráciu 020; pri návrate aplikácie sa účtové tabuľky zachovajú a stará verzia opäť používa token. Nasadenie samo nemení uložené pravidlá ani produkty v Upgates.

Oficiálne zdroje: [Responses Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs), [web search](https://developers.openai.com/api/docs/guides/tools-web-search), [modely](https://developers.openai.com/api/docs/models), [cenník a ceny nástrojov](https://developers.openai.com/api/docs/pricing), [účtovanie cache](https://developers.openai.com/api/docs/guides/prompt-caching). Cenník a podpora modelov overené 8. 10. 2026.


## Vzorové produkty a obsahový kontext v2

V pravidlách kategórie možno uložiť najviac dva vzorové produkty. `POST /ai-content/reference-products/preview` načíta presný kód z vybraného nakonfigurovaného e-shopu vrátane parametrov. Nevolá AI, neukladá pravidlá ani nemení produkt. Náhľad obsahuje len texty, statické HTML, SEO, tri H1 metadáta a parametre; ceny, sklad, kontaktné a ľubovoľné systémové metadáta sa neprenášajú. Operátor prijme náhľad, uloží koncept pravidiel a publikuje ho cez existujúcu kontrolu verzie. Obnova z e-shopu je opäť iba náhľad až do prijatia a publikovania.

Každá úloha si uloží snímku vzorov svojej kategórie a e-shopu, bez volania Upgates pri generovaní. Pri generovaní rovnakého produktu sa jeho vlastný vzor vynechá. Pravidlá majú prednosť a vzor slúži len na štýl, hĺbku a štruktúru; jeho rozmery, identita ani parametre nie sú faktami nového produktu. Dostupný je výber cez presný kód, nie automatické rozpoznanie ľubovoľnej URL.

Kompilátor `content-v2` odstraňuje z presne identifikovaných revidovaných modulov administratívne postupy a jednorazové nastavenie bicyklových kategórií/filtrov. Tabuľkový riadok registra vynechá len vtedy, keď všetky jeho stĺpce už doslovne obsahuje odosielaná štruktúrovaná definícia parametra (ignoruje iba Markdown zvýraznenie). Zmenené a neznáme dokumenty ponecháva. Zdrojová kniha zostáva nedotknutá a audit každého nového zadania uvádza počty znakov a odtlačky.

Nové katalógové úlohy pridávajú obmedzené technické riadky `STA_PARAMS/SIZE_TABLE` pre Paul Lange a verejné HTTPS PDF odkazy `STA_URL1–3` bez prihlasovania a query. Nejde o posielanie celého feedu. Prázdne bunky a väzby riadkov zostávajú zachované; originál ani ručne upravené mapované parametre sa neprepisujú. Verzia technických podkladov je súčasťou snímky a kontroly zmeny zdroja; staré úlohy si zachovajú pôvodný výpočet odtlačku. Iné dodávateľské polia vyžadujú osobitné mapovanie.

`usage.reasoning_tokens` zobrazuje časť výstupných tokenov spotrebovanú uvažovaním, nikdy druhú položku navyše vo výpočte ceny. Náklad je výpočet Hubu z metadát poskytovateľa a uloženého cenníka, nie faktúra ani suma vygenerovaná modelom. Referenčné príklady, uvažovanie aj webové výsledky môžu zvýšiť spotrebu napriek kratším pravidlám.

Obmedzenie bicyklov: dnešný klasifikátor vyberá jednu aktívnu koncovú kategóriu a Hub pridá jej predkov. Viaceré paralelné vetvy a pripravované neaktívne kategórie z pravidiel §13.1 zatiaľ automaticky nepriraďuje. AI generátor obsahu ani vyššie uvažovanie túto funkciu nenahrádzajú.
## Zoskupenie parametrov vzoru

Nová verzia zadania 3 zoskupuje riadky parametrov vzoru z Upgates do rovnakého formátu `name`, `values`, `product_id`, aký očakáva výstup AI. Staršie verzie zachovávajú pôvodný odoslaný tvar. Iba v nových úlohách pre duše sa opakované skupiny schválených parent parametrov `Priemer kolesa`, `Šírka plášťa`, `Rozmer ETRTO` a `Šírka plášťa v palcoch` spoja bez pridávania či prepočtu hodnôt, s informačným upozornením. Ostatné duplicity, neregistrované názvy, zmeny variantovej identity a nesprávny rozsah naďalej kontroluje pôvodný validátor. Zlúčenie nerieši rozporné dĺžky ventilu, materiály ani identitu výrobku.
