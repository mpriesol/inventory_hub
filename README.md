# Inventory Hub

Interný systém na správu skladových zásob pre BikeTrek / xTrek e-shop.

## Čo to robí

- **Faktúry** — upload a správa faktúr od dodávateľov (PDF, CSV, XLSX), server-side filtrovanie, platobné stavy
- **Príjem tovaru** — skenovanie EAN kódov, porovnanie objednaného vs. prijatého množstva, finalizácia do skladu
- **Dodávatelia** — konfigurácia, automatické sťahovanie XML feedov, B2B portál scraping (Playwright)
- **Produkty** — katalóg s multi-EAN podporou (jeden produkt = viacero čiarových kódov)
- **Sklad** — stavy, pohyby (imutabilný ledger), rezervácie
- **Upgates sync** — generovanie CSV pre import do e-shopu

## Tech stack

| | |
|--|--|
| **Backend** | FastAPI, Python 3.12, SQLAlchemy 2 (async), asyncpg |
| **Frontend** | React 18, TypeScript, Vite, Tailwind CSS v4 |
| **Databáza** | PostgreSQL 16 |
| **Infra** | Docker Compose, Caddy 2 (auto HTTPS), Redis 7 |
| **Automatizácia** | Playwright (B2B portál scraping) |

## Spustenie (lokálny vývoj)

### Prerekvizity
- Python 3.11+
- Node.js 20+
- PostgreSQL 16

### Backend
```bash
cd api
pip install -r requirements.txt

# Premenné prostredia
export INVENTORY_DATA_ROOT=./inventory-data
export USE_POSTGRES=true
export DB_HOST=localhost DB_PORT=5432 DB_NAME=inventory_hub DB_USER=postgres DB_PASSWORD=postgres

uvicorn inventory_hub.main:app --reload --port 8000
```

### Frontend
```bash
cd frontend
npm install
npm run dev    # http://localhost:5173
```

### Databáza
```bash
psql -d inventory_hub -f infra/db-init/001_schema.sql
psql -d inventory_hub -f infra/db-init/002_invoice_management.sql
psql -d inventory_hub -f infra/db-init/003_uploaded_invoices.sql

# Seed dodávateľov
psql -d inventory_hub -c "INSERT INTO suppliers (code, name) VALUES
  ('paul-lange', 'Paul-Lange'),
  ('northfinder', 'Northfinder')
ON CONFLICT (code) DO NOTHING;"
```

## Produkčný deploy

Systém beží na VPS (`hub.biketrek.sk`) cez Docker Compose.

```
/opt/inventory-hub/          # Docker Compose + config
/opt/inventory-data/         # Aplikačné dáta (suppliers, shops)
```

```bash
cd /opt/inventory-hub
docker compose up -d

# Health check
curl https://hub.biketrek.sk/api/health
```

### API dokumentácia

- **Swagger UI:** https://hub.biketrek.sk/api/docs
- **ReDoc:** https://hub.biketrek.sk/api/redoc

## Štruktúra projektu

```
api/
├── inventory_hub/
│   ├── main.py                   # FastAPI app
│   ├── database.py               # PostgreSQL pool
│   ├── config_io.py              # Filesystem config I/O
│   ├── routers/
│   │   ├── invoices_unified.py   # CRUD faktúr (DB)
│   │   ├── receiving_db.py       # Príjem tovaru (DB)
│   │   ├── suppliers.py          # Dodávatelia (filesystem)
│   │   ├── shops.py              # E-shopy
│   │   └── ...
│   └── adapters/                 # Per-supplier parsery
│       ├── paul_lange_v1.py
│       └── northfinder_web.py
frontend/
├── src/
│   ├── pages/                    # Stránky (Dashboard, Invoices, Receiving, ...)
│   ├── features/                 # Feature komponenty
│   ├── components/               # Shared UI
│   └── api/                      # API client
infra/
├── docker-compose.prod.yml
├── Caddyfile
└── db-init/                      # SQL migrácie (001-003)
```

## Dátový model

Systém používa **dva zdroje dát**:

**PostgreSQL** — dodávatelia, produkty (multi-EAN), faktúry, receiving sessions, skladové pohyby, objednávky. 30+ tabuliek.

**Filesystem** — konfigurácia dodávateľov (`config.json`), súbory faktúr (PDF/CSV), XML feedy, logy, Upgates CSV výstupy.

> Dodávatelia musia byť evidovaní na oboch miestach — filesystem config pre konfiguráciu + DB tabuľka pre relácie.

## Dodávatelia

| Kód | Názov | Adaptér | Feed |
|-----|-------|---------|------|
| `paul-lange` | Paul-Lange | paul_lange_v1 | XML (remote) |
| `northfinder` | Northfinder | northfinder_web | Playwright B2B |
| `ariga` | Ariga | manual | — |
| `husky` | Husky SK | manual | — |
| `sloger` | Sloger | manual | — |
| `spokey` | Spokey | manual | — |
| `vertone` | Vertone | manual | — |
| `warmpeace` | Warmpeace | manual | — |
| `zookee` | Zookee | manual | — |

Podrobná dokumentácia: [`docs/OVERVIEW.md`](docs/OVERVIEW.md)
