# AI obsah produktov – A + B

Funkcia používa OpenAI Responses API z Hubu. Nepotrebuje projekt/GPT v ChatGPT ani prístup k tomuto chatovému účtu. Projekt na OpenAI Platform môže patriť inému firemnému účtu a má vlastnú API fakturáciu.

## Použitie

1. V katalógu dodávateľa vyber produkty a klikni **Pripraviť obsah / AI**. Prvá verzia prijíma najviac 500 vybraných variantov a 100 kombinácií produkt/e-shop na jednu prípravu.
2. Pre každú rodinu zvoľ **Vylepšiť obsah pomocou AI** alebo pôvodný feed a profil kategórie. Pracuje sa iba s označenými variantmi. Spoločný popis nemožno zapnúť iba pre časť variantov tej istej vybranej rodiny.
3. Vyber jeden alebo viac e-shopov. Kategória z profilu má prednosť pred náhradnou kategóriou nastavenou pre cieľ. Kódy kategórií overí existujúci náhľad importu proti Upgates.
4. Prepínače v príprave sú jednorazové výnimky. Trvalé nastavenia sú v **Nastavenia → AI obsah produktov → Pravidlá a kategórie**.
5. Spusti spracovanie podľa profilu. Výsledok obsahuje pôvodné podklady, upraviteľné texty, parametre, zdroje, chýbajúce fakty, upozornenia, verziu pravidiel a históriu.
6. Ceny sa naďalej upravujú v existujúcom náhľade importu vrátane hromadnej ceny variantov. Zmena ceny nepotrebuje nové platené AI spracovanie.

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

- PostgreSQL je zdrojom pravidiel. Každé uloženie vytvára nezávislú verziu konceptu. Publikovanie s kontrolou očakávanej verzie mení ukazovateľ pre nové úlohy; rozpracované úlohy si zachovávajú pôvodný kontext.
- Profil kategórie je malý spoločný register pravidiel a parametrov, nie druhý skladový strom. Napríklad jeden profil `inner_tubes` môže mať rozdielne cieľové kódy pre BikeTrek a xTrek.
- Počiatočné pravidlá sú odvodené prevádzkové pravidlá pre slovenský obsah, Paul Lange a Northfinder. Nahrané dokumenty, privátne URL a prihlasovacie údaje nie sú v Gite. Nie všetky podrobné kategóriové a značkové dodatky zo zdrojového balíka sú automaticky publikované; ďalšie profily sa doplnia a overia postupne.
- Register určuje presné názvy, povinnosť, rozsah parent/variant, povolené hodnoty, jednotku a inštrukcie. Povinné fakty bez podkladu blokujú import. Variantné osi z feedu sa nemenia; pri kategórii s registrom musia byť zaregistrované.
- AI pomocník navrhuje text vybraného publikovaného pravidla alebo kategórie a voliteľne celý register parametrov. Prijatím vznikne iba koncept. Publikovanie vždy vykonáva používateľ, nezávisle od prepínačov automatizácie produktov.
- Duše majú samostatný profil. `automatic_import_ready=false` ponecháva ľudskú kontrolu do implementácie a overenia deterministickej matice ETRTO v kroku C.

## Validácia a import

AI vracia striktne definovaný obsahový JSON, nie priamo Upgates payload. Server kontroluje povinné fakty, názvy a hodnoty parametrov, príslušnosť variantov, podporované HTML a evidenciu zdrojov. Kontrola formátu nenahrádza kontrolu faktickej správnosti; preto je počiatočne zapnutá ľudská kontrola.

`validate_upgates.py` zo zdrojového balíka je XML/XSD validátor. Nespúšťa ho model a nie je vložený do promptu. API import validuje na serveri reálne odosielaný JSON; nepredstiera úspech XML validácie ako dôkaz platnosti iného formátu. Samostatný XML export a zapojenie pôvodného XML validátora sa doplnia pri rozvoji tohto formátu.

Finálne dáta vzniknú ako obmedzené obsahové doplnenie existujúceho importu. Ceny, DPH, koeficienty, kódy, EAN, fotky a výber variantov zostávajú v jeho deterministickej časti. Bezpečnostný text sa doplní zo zdroja oddelene. Cieľové H1 vlastné polia majú overené definície; chýbajúce sa vytvoria až pri importe. Šablóna H1 e-shopu sa nemení.

Pred odoslaním sa overia aktuálne podklady, cieľ, duplicity a náhľad. Po odoslaní sa využíva existujúci GET na overenie aktivity, identít, príznaku kontroly, textov a H1 polí. Neisté zápisy sa nezopakujú naslepo. Každý e-shop má vlastný náhľad a stav; neúspech jedného nevracia späť úspešný import druhého.

## Fronta a náklady

Úlohy a audit žijú v PostgreSQL. Jeden worker v existujúcom API kontajneri používa databázový advisory lock, ktorý bráni paralelnému vykonaniu pri viacerých API procesoch. Pri reštarte sa import obnoví cez existujúci zmrazený náhľad. Rozpracované platené AI volanie bez potvrdeného výsledku prejde do `uncertain`; nikdy sa automaticky neplatí druhý pokus.

Odhad je konzervatívny (veľkosť vstupu, maximálny výstup a rezerva na oficiálny prieskum). Hub rezervuje rozpočet pred volaním. Známa spotreba sa zaznamená aj pri neplatnom výsledku; pri neistom výsledku ostáva rezervácia. Limity Hubu pracujú s odhadom, nie s garantovanou konečnou faktúrou. Limity a upozornenia nastav aj v API projekte. Model má explicitný cenník v `ai_content_provider.py`; neznámy model sa odmietne.

Režim **Feed + oficiálne zdroje** povoľuje iba domény zo zodpovedajúcich pravidiel a najviac tri webové kroky na volanie. Citovaný externý technický podklad musí byť zaznamenaný ako otvorená povolená stránka. Výsledok bez doplňujúceho podkladu je viditeľne označený. Rozsiahlejší výskum/PDF a médiá sa budú pridávať postupne. Rýchly režim **Iba feed** musí používateľ zvoliť výslovne.

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
| `POST /ai-content/rules` | Nový koncept pravidiel |
| `POST /ai-content/rules/{id}/publish` | Publikovanie konkrétnej verzie |
| `POST /ai-content/rules/proposal` | Príprava AI návrhu pravidla |
| `POST /ai-content/selection` | Zoskupenie explicitného výberu z feedu |
| `POST /ai-content/batches` | Trvalé úlohy; UUID chráni opakované odoslanie |
| `GET /ai-content/jobs` | História a stav bez platených volaní |
| `GET /ai-content/jobs/{id}` | Obsah, audit a náhľad/výsledok importu |
| `POST /ai-content/jobs/{id}/review` | Uloženie alebo schválenie obsahu |
| `POST /ai-content/jobs/{id}/prices` | Nový náhľad s ručnými cenami |
| `POST /ai-content/jobs/{id}/action` | Spustenie, potvrdenie importu, zrušenie alebo kontrolovaný pokus o import |

Vyhľadávanie vo feede zostáva pod `/suppliers/.../catalog`. Existujúci surový import zostáva pod `/shops/{shop}/import`. AI náhľady sa potvrdzujú iba cez chránenú akciu AI úlohy.

## Nasadenie a návrat

Migrácia `005_ai_content.sql` je aditívna a opakovateľná; pridáva iba tabuľky AI. Build ju spustí pred štartom nového API. Prázdny serverový súbor pre tajomstvá ponecháva platené spracovanie vypnuté. Mount je mimo verejného `/data`.

Pri návrate nasadenia ponechaj tabuľky a audit zachované; nepoužívaj DROP. Vypnutie `AI_CONTENT_ENABLED` zastaví nové AI generovanie. Už výslovne schválené importy majú samostatné stavy; zruš čakajúce úlohy v UI pred úplným zastavením workflow. Pôvodný katalóg/import funguje nezávisle.

Oficiálne zdroje: [Responses Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs), [web search](https://developers.openai.com/api/docs/guides/tools-web-search), [model a cenník](https://developers.openai.com/api/docs/models/gpt-5.6-sol), [ceny nástrojov](https://developers.openai.com/api/docs/pricing). Cenník overený 21. 9. 2026.
