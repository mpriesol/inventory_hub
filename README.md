# Inventory Hub

Interný systém pre spoločný sklad, produkty a dodávateľské doklady **BIKETREK** a **xTrek**.

## Čo je v aplikácii

- Jedna skladová karta a overené väzby na predajné produkty oboch Upgates e-shopov; editor, náhľad a zlučovanie kariet.
- Príjem podľa faktúry alebo skenera, nemenná história pohybov, potvrdené zásoby a nákupné náklady podľa FIFO.
- Dodávateľské katalógy, mapovanie feedov, príprava produktov a voliteľný AI obsah.
- Oddelené načítanie objednávok, lokálne skladové spracovanie a kontrolované odosielanie produktov, dostupností a nákupných cien.
- Archív externe vystavených faktúr, evidencia úhrad a prehľad produktu podľa EAN/SKU.

Jednotlivé automatizácie a externé zápisy majú vlastné nastavenia. Nasadenie kódu ich samo neaktivuje. Rozsah implementácie a zostávajúce priority sú v [prehľade projektu](OVERVIEW.md).

## Kde začať

| Potrebujem | Dokument |
| --- | --- |
| Pripraviť prostredie, aktualizovať alebo obnoviť Hub | [Inštalácia, aktualizácia a obnova](api/POSTGRES_SETUP.md) |
| Zorientovať sa v aktívnom kóde a funkciách | [OVERVIEW.md](OVERVIEW.md) |
| Pracovať na repozitári a overiť zmenu | [AGENTS.md](AGENTS.md) |
| Pochopiť CI a produkčný deployment | [CI a nasadzovanie](docs/ci-deployment.md) |
| Zistiť, čo ešte zostáva dokončiť | [Priority a zostávajúca práca](docs/mvp-progress.md) |
| Overiť identitu, SKU a väzby kariet | [Dátový model skladu](docs/warehouse-model.md), [pravidlá SKU](docs/sku-policy.md), [zlučovanie kariet](docs/product-consolidation.md) |

## Technológie a úložiská

| Vrstva | Použitá technológia |
| --- | --- |
| Backend | Python 3.12, FastAPI, SQLAlchemy 2 async, asyncpg |
| Frontend | React 18, TypeScript, Vite, Tailwind CSS v4; CI používa Node.js 20 |
| Databáza | PostgreSQL 16 |
| Nasadenie | Docker Compose, Caddy, obrazy v GHCR a GitHub Actions |
| Dodávateľské integrácie | Parsery feedov a podľa dodávateľa Playwright/B2B čítanie |

PostgreSQL uchováva obchodné údaje a audit. Filesystem uchováva konfigurácie, faktúry, feedy a importné súbory; prístupy majú samostatné chránené úložisko. Obnova preto musí pokrývať databázu aj súbory. Konkrétne prístupy, nastavenia e-shopov a produkčné údaje nepatria do Git.

Aktívne časti projektu: [api/](api/), [frontend/](frontend/), [infra/db-init/](infra/db-init/), [docs/](docs/) a [scripts/](scripts/). `main` je produkčná vetva; pravidlá zmien a existujúce oprávnenia určuje [AGENTS.md](AGENTS.md).
