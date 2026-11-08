# Priority a zostávajúca práca

Aktualizované podľa kódu 8. 11. 2026. Tento súbor nahrádza priebežný denník balíkov A–E zo septembra; jeho staršie verzie zostávajú v Git histórii. Stav konkrétneho nasadenia alebo živej operácie sa overuje v Actions a v uloženom výsledku, nie podľa starej poznámky „pripravené“.

## Čo už má implementáciu

Spoločná identita kariet, skenovacie UUID, jednotný produktový editor, história pohybov, potvrdený začiatok skladu, FIFO, dostupnosti, vybrané publikovanie, účty, archív dokladov a riadené zlučovanie sú implementované. Presný rozsah a dokumenty sú v [prehľade projektu](../OVERVIEW.md). Existujúce automatizácie majú vlastné aktivácie; prítomnosť kódu neznamená zapnutú skladovú autoritu.

## Zostávajúce priority

| Téma | Čo ešte treba rozhodnúť alebo dokončiť |
| --- | --- |
| Tržby a hrubá marža | Doplniť skutočné tržby bez DPH, zľavy, refundácie a ich väzbu na vydané kusy. Existujúce FIFO náklady a archív úhrad samy netvoria úplné vyúčtovanie marže. |
| Prevádzkový pilot spoločného skladu | Overiť konkrétne shop pravidlá, potvrdený začiatok evidencie, objednávkový cyklus a jedinú autoritu zápisov. Podmienky sú v [pripravenosti Upgates](upgates-sync-readiness.md); nasadenie kódu nie je aktivácia pilotu. |
| Širšia inventúra a manuálne rezervácie | Samostatná používateľská agenda zostáva nižšou prioritou. Existujúce počiatočné stavy a [opravné pohyby](stock-adjustments.md) sa zachovávajú. |
| Rozšírené vratky, odpisy a servis | Doriešiť scenáre mimo existujúcich fyzických vratiek/FIFO alokácií, najmä historické vratky bez alokácie a širší servisný postup. Pravidlá nemenných nákladov a pohybov zostávajú. |
| Ďalšie jednotky a oprávnenia | Rozšírenia nad aktuálne [explicitné jednotky a balenia](stock-units.md) a [zdieľané kusové predaje](shared-stock-sales.md); všeobecné prevádzkové RBAC nad existujúcou správou účtov. |
| Používateľské doladenie | Rozšírenie podporovaných publikovaných polí a overenie konkrétnych variantov. Vizuálny redesign je samostatná neskoršia etapa, nie podmienka zachovania dnešných funkcií. |

Prioritu konkrétneho ďalšieho zásahu určuje aktuálne zadanie vlastníka. Staré číslovanie balíkov ani historické zoznamy testov nie sú zoznamom otvorených PR.

## Platné obchodné rozhodnutia

- Každá predajná položka má jednu kanonickú kartu. Nové SKU vychádza z dvojpísmenového prefixu **výrobcu** a jeho overeného článku podľa [pravidiel SKU](sku-policy.md); stará politika dodávateľského prefixu nie je pravidlom pre nové karty. Existujúce SKU a overené väzby sa zachovávajú.
- Desať nových skenov pridá desať kusov. Opakovanie jedného request UUID nepridá ďalší kus; EAN sa časovo neblokuje.
- Nákupný náklad vychádza z príjmu a konkrétnych vydaných vrstiev. V obslužných textoch používať Nákupné ceny/Príjmy/Náklad predaja; technické pomenovanie FIFO zostáva.
- Dodávateľská ponuka a vlastná fyzická zásoba sú oddelené. Čerstvosť a lehoty majú vlastnú konfiguráciu; chýbajúci údaj nie je nula.
- Manuálne bezkódové riadky sú mimo fyzického skladu. Potvrdený výdaj je jeden uzamknutý celok; oprava histórie používa kompenzačný pohyb.
- Dátová akcia má zrozumiteľne označiť čítanie/zmenu Hubu a čítanie/zápis Upgates vrátane nadväzujúcej úlohy. Počet API volaní uvádzať ako meranie alebo doložený odhad, inak ako neznámy.
- Real-time nie je podmienkou MVP. Nasadenie ani migrácia nesmie samo zapnúť publikovanie alebo ostrú skladovú autoritu. Prechod spoločného skladu zostáva výslovným prevádzkovým krokom s vlastníkom.

## Pokračovanie v práci

Overiť aktuálny `main`, otvorené zmeny a [AGENTS.md](../AGENTS.md), potom aktívny kód a dokument konkrétnej oblasti. Dokončiť najmenšiu súdržnú zmenu a uviesť skutočne vykonané kontroly. Platné oprávnenia pre PR, merge a nasadenie sa riadia AGENTS a aktuálnym zadaním; tento zoznam nepridáva nové oprávnenia.
