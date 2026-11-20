# Inštalácia, aktualizácia a obnova Inventory Hubu

Toto je spoločný návod pre prostredie a databázu. [README](../README.md) je vstupný prehľad; [CI a nasadzovanie](../docs/ci-deployment.md) opisuje podrobnosti existujúceho release workflow. Použiť len prostredie a operácie pokryté aktuálnym zadaním a [oprávneniami](../AGENTS.md).

## Prostredie a lokálny vývoj

Zdrojové nastavenia používajú Python 3.12, PostgreSQL 16 a Node.js 20 v CI. Príkazy nižšie sa spúšťajú z koreňa repozitára a predpokladajú **samostatnú lokálnu databázu s pripravenou schémou**.

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r api/requirements.txt
npm --prefix frontend ci
```

Do lokálneho necommitovaného `.env` nastaviť vlastné hodnoty podľa [settings.py](inventory_hub/settings.py):

| Nastavenie | Účel |
| --- | --- |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | Vlastná izolovaná PostgreSQL DB a jej vlastník; pri vývoji localhost, nikdy produkčná DB. |
| `USE_POSTGRES=true` | Aktuálny databázový skladový režim. |
| `INVENTORY_DATA_ROOT` | Existujúci absolútny adresár oddelených lokálnych dát. |
| `SUPPLIER_CREDENTIALS_ROOT` | Oddelený chránený adresár dodávateľských prístupov mimo verejných dát. |
| `AI_CONTENT_ACCESS_TOKEN` | Vlastný lokálny operátorský token aspoň 24 znakov; používa ho aj prvé prihlásenie. |

`ROOT_PATH` číta `main.py` priamo z prostredia procesu, nie zo Settings `.env`. Pri priamom lokálnom API ho ponechať prázdny; za proxy s prefixom `/api` ho pred spustením API nastaviť cez `export ROOT_PATH=/api`.

Prístupy ani produkčné dáta nekopírovať do Git. Na nový vývoj nie sú potrebné živé e-shopové či AI prístupy. Predvolené nastavenia `STOCK_PUBLICATION_WRITE_ENABLED`, `STOCK_SYNC_WRITE_ENABLED`, `FIFO_COST_SYNC_WRITE_ENABLED` a `ATOMER_CATALOG_WORKER_ENABLED` zostávajú `false`.

Po príprave schémy:

```bash
PYTHONPATH=api python -m inventory_hub.migrate
PYTHONPATH=api python -m uvicorn inventory_hub.main:app --reload --host 127.0.0.1 --port 8000
```

V druhom termináli možno spustiť `npm --prefix frontend run dev`. Samotný Vite server nie je kompletná prihlasovacia zostava: [vite.config.ts](../frontend/vite.config.ts) nemá API proxy a časť klienta používa priamo `/api`. Pre celé UI treba rovnaký HTTPS pôvod pre frontend, `/api` a súbory `/data`, s proxy do lokálneho API. Samotné `VITE_API_BASE` toto nenahrádza. Cookie relácie vyžadujú HTTPS a kontrolu rovnakého pôvodu; [autentizačný kontrakt](../docs/user-accounts.md) sa pri vývoji nevypína.

Kontroly bez externých zápisov:

```bash
python -m compileall -q api/inventory_hub
npm --prefix frontend run typecheck
npm --prefix frontend run build
```

Databázové testy používajú vlastnú localhost DB s názvom končiacim `_catalog_test`; presné príkazy a UI sady sú v [AGENTS.md](../AGENTS.md) a [CI](../.github/workflows/ci.yml).

## Nová prázdna databáza

Repozitár obsahuje **59 SQL súborov `001`–`059`** v [infra/db-init](../infra/db-init/). `001_schema.sql` vytvára základné typy a tabuľky bez ochrany proti opakovanému vytvoreniu; patrí iba do prázdnej databázy. Neexistuje súbor `schema_v12_FINAL.sql` z pôvodného návodu a samotné `001`–`003` nie sú aktuálna schéma.

[Referenčný Compose](../infra/docker-compose.prod.yml) pripája celý adresár do `/docker-entrypoint-initdb.d`; inicializátor PostgreSQL ho použije iba pri prázdnom dátovom volume. Pri existujúcej databáze sa tento adresár znovu nespúšťa. Nevytvárať preto nové produkčné volume ako spôsob aktualizácie a nepúšťať `001` nad existujúcimi dátami.

**Overené 8. 11. 2026:** v novej izolovanej PostgreSQL 16.2 DB prešlo všetkých 50 SQL súborov `001`–`050` v poradí s `ON_ERROR_STOP=1`, potom aj skutočný 39-krokový `inventory_hub.migrate`. Overenie sa týkalo SQL a runnera; nebolo zostavením Docker obrazu ani testom celej novej serverovej inštalácie. Testovacia DB bola po kontrole odstránená, produkcia sa nemenila.

**Overené 9. 11. 2026:** rovnakou izolovanou kontrolou v PostgreSQL 16.2 prešlo všetkých 52 SQL súborov `001`–`052` a následne skutočný 41-modulový `inventory_hub.migrate`. Testovacia DB bola odstránená; toto overenie ešte neznamená produkčné nasadenie migrácií 051 a 052.

**Overené 16. 11. 2026:** v novej izolovanej localhost PostgreSQL 16.10 DB s príponou `_catalog_test` prešlo všetkých 54 SQL súborov `001`–`052`, `054` a `055` v poradí s `ON_ERROR_STOP=1`. Následne dvakrát úspešne prebehol skutočný 43-modulový `inventory_hub.migrate`, vrátane opakovania oboch nových migrácií. Testovací server bol potom zastavený. Overenie sa týka SQL a runnera; nie je zostavením Docker obrazu ani potvrdením produkčného nasadenia.

**Overené 19. 11. 2026:** v novej izolovanej localhost PostgreSQL 16.2 DB s príponou `_catalog_test` prešlo všetkých 57 SQL súborov `001`–`057` a potom dvakrát skutočný 46-modulový runner. Schéma obsahovala 141 tabuliek. Opakovanie zachovalo premenovaný/deaktivovaný dostupnostný seed; trigger označil iba autorizovaný zapnutý delta kanál, bez fyzického skladu. Táto kontrola nepotvrdzuje zostavenie obrazu ani produkčné nasadenie.

Pre už vytvorenú **prázdnu lokálnu DB** nastav v prostredí `PGHOST=127.0.0.1`, správny `PGPORT`, `PGDATABASE`, `PGUSER` a bezpečný lokálny spôsob prihlásenia pre `psql`. Tieto hodnoty musia ukazovať na tú istú izolovanú DB ako `DB_*` v aplikácii. Potom z koreňa repozitára:

```bash
for sql in infra/db-init/[0-9][0-9][0-9]_*.sql; do
  psql --no-psqlrc --set=ON_ERROR_STOP=1 --file="$sql" || exit 1
done
PYTHONPATH=api python -m inventory_hub.migrate
```

Pri chybe nepokračovať ďalším súborom. Základná schéma nie je opakovateľný upgrade; čiastočne vytvorenú lokálnu testovaciu DB treba posúdiť pred novým pokusom. Tento postup sa nepoužíva nad existujúcou produkciou.

Dodávateľské/shop konfigurácie a prístupy spravovať cez existujúce konfiguračné postupy; filesystem šablóna sama nevytvorí zodpovedajúce databázové väzby. Nepreberať zo starých návodov ručné SQL zápisy pohybov, cien či identít.

## Aktualizácia existujúcej databázy

Autoritatívne poradie je v [migrate.py](inventory_hub/migrate.py), SQL balenie v [API Dockerfile](Dockerfile).

| Časť | Aktuálny rozsah |
| --- | --- |
| Základ schémy | SQL `001`–`004`; upgrade runner existujúci základ predpokladá. |
| Upgrade runner | 48 modulov Pythonu od `005` po `038` a od `046` po `059`. |
| Spoločný Atomer krok | [atomer_review_migrate.py](inventory_hub/atomer_review_migrate.py) vykonáva SQL `038`–`045` spolu v jednej transakcii. |
| SQL v API obraze | Všetkých 55 upgrade súborov `005`–`059`. |

V existujúcom release workflow sa po stiahnutí obrazov vykoná `python -m inventory_hub.migrate` v jednom dočasnom API kontajneri, pred obnovením služieb. Každý modul zachováva vlastnú transakciu a advisory lock. Nie je to jedna transakcia cez celý upgrade: pri chybe sa runner zastaví, hotové moduly zostanú potvrdené a reštart API sa nedosiahne.

Nová migrácia potrebuje nové číslo, zapojenie do runnera, balenie do obrazu a overenie. Už nasadené SQL sa neupravujú. Schéma sama neaktivuje skladové zápisy. Konkrétne dátové účinky existujúcich migrácií, napríklad úprava zapnutých dodávateľských intervalov v `034`, zostávajú súčasťou ich [prevádzkového kontraktu](../docs/ci-deployment.md).

## Produkčné nasadenie

Používať [build.yml](../.github/workflows/build.yml) na `main` podľa existujúceho oprávnenia. Obrazy API/frontend sa zostavia paralelne; celý release je serializovaný s vypnutým rušením bežiaceho nasadenia. Markdown/docs-only zmeny automatický release nespúšťajú. Ručný dispatch `deploy=false` zostaví iba SHA obrazy, bez zmeny produkčného tagu a bez SSH deploymentu.

Server pracuje s vlastným `docker-compose.yml` a generovaným `docker-compose.ai-content.yml`. Overlay zachováva tajné konfigurácie, supplier secrets volume a Caddy sieť `sps-proxy`. Referenčný Compose v repozitári sa nesmie zamieňať za overený aktuálny serverový súbor.

Workflow kontroluje sieť a Compose konfiguráciu, stiahne obrazy, vykoná migrácie a obnoví `api`, `frontend-build`, `caddy`. Následne overí verejné zdravie DB, zamietnutie anonymného prístupu, autorizovaný stav AI a nedostupnosť raw konfigurácie; samostatne aj `/sps/healthz`. Obrazy čistí až po úspechu. Presné limity a diagnostika sú v [CI a nasadzovaní](../docs/ci-deployment.md).

## Záloha a obnova

Konzistentný bod obnovy zahŕňa PostgreSQL, aplikačné dáta, deployment konfiguráciu a supplier secrets. [backup_inventory.py](../scripts/backup_inventory.py) tento rozsah archivuje a overuje obnovu DB v izolovanom kontajneri. Nástroj sám nezastavuje ani nespúšťa služby a nikdy neobnovuje produkciu.

1. Pri tvorbe novej zálohy správca v dohodnutom okne pozastaví všetkých writerov a zastaví API. Nástroj odmietne bežiace API; `--writers-paused` je potvrdenie obsluhy, nie detekcia všetkých zápisov. Zachovať nastavenia a následne zosúladiť objednávky vzniknuté počas okna.
2. Použiť presné serverové Compose súbory vrátane overlayu a existujúci [postup vytvorenia zálohy](../docs/sku-migration.md#overiteľná-serverová-záloha-pred-pilotom). Archívy obsahujú tajomstvá a zákaznícke doklady; uchovávať ich chránené mimo Git.
3. Za overenú považovať iba zálohu s úspešným manifestom, kontrolami SHA-256 a dokončenou izolovanou obnovou. Režim `--verify-existing` môže znovu overiť presnú dokončenú zálohu bez zastavenia produkcie; potrebuje jej pôvodný lokálny PostgreSQL image a dostatok zdrojov. [Podrobný postup](../docs/sku-migration.md#opätovné-overenie-existujúcej-zálohy-bez-odstávky).
4. Skutočná obnova servera vyžaduje výber konzistentnej zálohy, kontrolu oprávnení súborov, konfigurácií, volumes, kompatibility kódu a všetkých externých operácií po bode obnovy. Izolovaná kontrola DB sama nepotvrdzuje kompletnú obnovu aplikácie. Repo nemá automatický príkaz na obnovu produkcie.

Pri neúspešnom upgrade najprv identifikovať fázu a modul. Dokončené migrácie nemaže revert kódu; zachovať nemenné pohyby, FIFO, aliasy, UUID výsledky, denníky a neisté zápisy. Oprava musí byť kompatibilná s uloženými dátami. Obnova DB nevracia už vykonaný Upgates zápis; riadi sa aj [kontraktom dotknutej funkcie](../OVERVIEW.md#aktuálny-stav-funkcií-a-obrazoviek).

### Rozšírenie 051–052

`051_invoice_processing.sql` pridáva ručné spracovanie faktúr, vratnú archiváciu a nemenné audity vrátane vyradenia chybných zdrojových testov. `052_receiving_operations.sql` pridáva náhľady a výsledky opráv príjemiek. Runner ich vykoná po 050 a API obraz balí oba súbory. Ani jedna migrácia neresetuje doklady, nemení zásoby ani nezapína prenos do e-shopov. Prevádzkové operácie sa vykonajú osobitne po overení kódu a konkrétneho náhľadu.

### Rozšírenie 053

`053_attention.sql` pridáva iba tabuľky spoločných upozornení a pravidiel. Je zapojená do explicitného runnera a API obrazu. Nemení sklad ani nastavenia dodávateľov. Stav upozornení sa naplní z existujúcich výsledkov workerom po štarte. Návrat predchádzajúceho kódu tabuľky ponechá; nevyžaduje sa spätná migrácia.

### Rozšírenie 054–055

`054_mixed_receiving.sql` pridáva dávky zmiešaného príjmu, ich skeny, nemenné UUID výsledky a väzby na zaúčtované pohyby. `055_atomer_availability.sql` pridáva lokálne nastavenia, náhľady, administratívne podklady a CSV výsledky exportu dostupností. Runner ich vykoná po 053 a API obraz balí oba súbory. Inštalácia nepotvrdzuje staré množstvá, neprijíma faktúry, nezapína skladovú autoritu ani denné exporty. Zdrojové URL sa nastavujú osobitne cez chránenú konfiguráciu.

Po použití dávkového príjmu zachovať nové dôkazy pohybov aj pri oprave kódu; starší writer ich nemusí rozpoznať. Už ručne importovaný Atomer CSV sa návratom kódu nevráti. Použiť kompatibilnú doprednú opravu a existujúce [opravy príjmu](../docs/receiving-recovery.md), bez mazania histórie alebo exportných výsledkov.

### Rozšírenie 056–057

`056_receiving_manual.sql` pridáva oddelené ručné skeny bez faktúry, nemenné väzby na pohyby a vratnú archiváciu dávok. `057_setting_catalogs.sql` pridáva DB číselník dostupností a výsledky idempotentných požiadaviek. Predvolené dostupnosti sa vložia iba pri prvom vytvorení tabuľky; opakované nasadenie zachová premenovanie a deaktiváciu. Trigger ručnej dostupnosti znovu používa `stock_sync_mark_dirty` iba pre zapnuté autorizované delta nastavenia.

`058_supplier_availability_policy.sql` pridáva predvolené nastavenia chýbajúcich položiek a evidenciu zmien dostupnosti. História začína od prvého nového pozorovania po nasadení. Nastavenie viditeľnosti sa dá vrátiť na **Ponechať v ponuke** a prenos pri ďalšom úspechu obnoví vlastné zmeny na overených prepojeniach.

Obe migrácie sú aditívne, zapojené za 055 a zabalené v API obraze. Samy nevytvárajú príjem ani neaktivujú autoritu. Vratná archivácia zachová pôvodné doklady a účtovnú stopu. Po použití nových dát zachovať tabuľky a aplikovať kompatibilnú opravu vpred; nenasadzovať writer bez podpory ručných príjmov.

### Register výrobcov a prefixov SKU

Migrácia `059_sku_brand_management.sql` dopĺňa stav archivácie výrobcu, odkazy na dodávateľské B2B portály a tabuľku rezervovaných prefixov.
Po zmene použitého prefixu si staré karty ponechajú svoj kód a výrobca svoj pôvodný identifikátor.
Pri opakovaní migrácie 029 sa pôvodných sedem značiek dopĺňa iba pri založení novej tabuľky.
Po prvom použití archivácie alebo zmene použitého prefixu ponechajte údaje migrácie 059
a pri oprave nasaďte verziu, ktorá rešpektuje archív a rezervácie.

V izolovanej PostgreSQL 16.2 prešlo 20. 11. 2026 všetkých 59 SQL súborov v poradí.
Následne dvakrát prešlo všetkých 48 modulov `inventory_hub.migrate`.
