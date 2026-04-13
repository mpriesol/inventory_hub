# Inventory Hub — Prehľad projektu

## Čo je Inventory Hub?

Inventory Hub je **interný nástroj na správu skladových zásob** pre cyklistický e-shop BikeTrek / xTrek. Umožňuje:

- **Nahrávať a spravovať faktúry** od dodávateľov (PDF, CSV, XLSX…)
- **Prijímať tovar** — skenovanie čiarových kódov, porovnanie s objednávkou
- **Sledovať stav skladu** — množstvá, pohyby, rezervácie
- **Spravovať produktový katalóg** — multi-EAN podpora, prepojenie na dodávateľské feedy
- **Synchronizovať** dáta s e-shopovou platformou (Upgates)
- **Automaticky sťahovať** cenníky a faktúry z B2B portálov dodávateľov

Systém beží na VPS na adrese `hub.biketrek.sk`.

---

## Architektúra

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   Frontend   │────▶│   Caddy      │────▶│   FastAPI     │
│  React + TS  │     │  (rev proxy) │     │   (Python)    │
│  Vite build  │     │  port 80/443 │     │  port 8000    │
└──────────────┘     └──────────────┘     └──────┬───────┘
                                                  │
                                    ┌─────────────┼─────────────┐
                                    │             │             │
                             ┌──────▼───────┐ ┌───▼────┐ ┌─────▼──────┐
                             │  PostgreSQL   │ │ Redis  │ │ Filesystem │
                             │  (port 5432)  │ │ (7)    │ │ /opt/      │
                             └──────────────┘ └────────┘ │ inventory- │
                                                         │ data/      │
                                                         └────────────┘
```

### Technológie

| Vrstva     | Technológia                                  |
|------------|----------------------------------------------|
| Frontend   | React 18, TypeScript, Vite, Tailwind CSS v4   |
| Backend    | FastAPI (Python 3.12), SQLAlchemy 2 (async)   |
| Databáza   | PostgreSQL 16 (asyncpg driver)                |
| Cache      | Redis 7 (pripravené na budúcnosť)             |
| Reverse proxy | Caddy 2 (auto HTTPS)                      |
| Kontajnerizácia | Docker Compose                          |
| Automatizácia | Playwright (B2B portál scraping)           |

---

## Produkčný server

### Štruktúra na serveri

```
/opt/inventory-hub/                # Aplikácia (Docker Compose)
├── .env                           # DB credentials (DB_NAME, DB_USER, DB_PASSWORD)
├── docker-compose.yml             # Compose — postgres, redis, api, caddy
├── db-init/                       # SQL migrácie
│   ├── 001_schema.sql                 # Hlavná schéma (30+ tabuliek)
│   ├── 002_invoice_management.sql     # Rozšírenie pre invoice management
│   └── 003_uploaded_invoices.sql      # Tabuľky uploaded_invoices + lines
├── caddy/
│   ├── Caddyfile                  # Caddy konfigurácia (reverse proxy)
│   └── site/                      # Buildnutý frontend (statické súbory)
└── logs/

/opt/inventory-data/               # Aplikačné dáta (mimo Docker)
├── suppliers/
│   ├── paul-lange/
│   │   ├── config.json                # Konfigurácia dodávateľa
│   │   ├── feeds/xml/                 # Surové XML feedy
│   │   ├── feeds/converted/           # Skonvertované CSV
│   │   ├── invoices/csv/              # Parsované CSV faktúry
│   │   ├── invoices/raw/              # Originálne súbory (PDF, XLSX)
│   │   ├── imports/upgates/           # Výstup pre Upgates import
│   │   └── logs/                      # Logy operácií
│   ├── northfinder/
│   └── ... (9 dodávateľov)
└── shops/
    └── biketrek/
        └── latest.csv                 # Posledný export z e-shopu
```

### Docker Compose služby

| Služba | Image | Port | Úloha |
|--------|-------|------|-------|
| `postgres` | postgres:16 | 5432 (interný) | Hlavná databáza |
| `redis` | redis:7 | 6379 (interný) | Cache (pripravené) |
| `api` | ghcr.io/.../inventory-hub-api | 8000 (interný) | FastAPI backend |
| `frontend-build` | ghcr.io/.../inventory-hub-frontend | — | One-shot: kopíruje build do Caddy |
| `caddy` | caddy:2 | 80, 443 | Reverse proxy + auto HTTPS |

### Dôležité premenné prostredia (API kontajner)

| Premenná | Hodnota | Popis |
|----------|---------|-------|
| `INVENTORY_DATA_ROOT` | `/data/inventory-data` | Cesta k dátam (vnútri kontajnera) |
| `USE_POSTGRES` | `true` | API používa PostgreSQL |
| `ROOT_PATH` | `/api` | Prefix za reverse proxy (Swagger docs) |
| `DB_HOST` | `postgres` | Hostname DB (Docker service name) |

> **Volume mount:** `/opt/inventory-data` (host) → `/data/inventory-data` (kontajner)

### API dokumentácia (Swagger)

- **Swagger UI:** `https://hub.biketrek.sk/api/docs`
- **ReDoc:** `https://hub.biketrek.sk/api/redoc`
- **OpenAPI JSON:** `https://hub.biketrek.sk/api/openapi.json`

---

## Adresárová štruktúra repozitára

```
inventory_hub/
├── api/                          # Backend (FastAPI)
│   ├── inventory_hub/
│   │   ├── main.py               # Hlavný vstupný bod, FastAPI app
│   │   ├── database.py           # PostgreSQL connection pool
│   │   ├── db_models.py          # SQLAlchemy modely
│   │   ├── settings.py           # Konfigurácia (env variables)
│   │   ├── config_io.py          # Filesystem config I/O (suppliers, shops)
│   │   ├── routers/              # API endpointy (po moduloch)
│   │   │   ├── invoices_unified.py   # CRUD faktúr (DB-backed)
│   │   │   ├── receiving_db.py       # Príjem tovaru (scanning sessions)
│   │   │   ├── suppliers.py          # Správa dodávateľov (filesystem)
│   │   │   ├── shops.py              # Správa predajní/e-shopov
│   │   │   ├── imports.py            # Import do Upgates
│   │   │   ├── invoices.py           # Staršie invoice operácie (filesystem)
│   │   │   ├── logs.py               # Logy per-supplier
│   │   │   └── logs_global.py        # Globálne logy
│   │   ├── adapters/             # Parser/connector moduly per dodávateľ
│   │   │   ├── paul_lange_v1.py      # Paul Lange CSV parser
│   │   │   ├── northfinder_web.py    # Northfinder B2B scraper
│   │   │   └── ...
│   │   └── services/             # Business logika
│   ├── Dockerfile
│   └── requirements.txt
│
├── frontend/                     # Frontend (React)
│   ├── src/
│   │   ├── App.tsx               # Router (React Router v6)
│   │   ├── pages/                # Hlavné stránky
│   │   │   ├── DashboardPage.tsx
│   │   │   ├── InvoicesPage.tsx      # Zoznam faktúr s filtrami
│   │   │   ├── InvoiceDetailPage.tsx # Detail faktúry
│   │   │   ├── ReceivingPage.tsx     # Príjem tovaru
│   │   │   ├── ReceivingSessionPage.tsx
│   │   │   ├── SuppliersPage.tsx     # Dodávatelia + feedy
│   │   │   └── StockPage.tsx
│   │   ├── features/             # Feature-specific komponenty
│   │   ├── components/           # Shared UI komponenty
│   │   ├── api/                  # API client (fetch wrappery)
│   │   ├── i18n/                 # Preklady (SK/EN)
│   │   └── types.ts              # TypeScript typy
│   ├── Dockerfile
│   └── package.json
│
├── infra/                        # Infraštruktúra
│   ├── docker-compose.prod.yml   # Produkčný compose (referencia)
│   ├── Caddyfile                 # Caddy konfigurácia
│   └── db-init/                  # SQL migrácie
│       ├── 001_schema.sql
│       ├── 002_invoice_management.sql
│       └── 003_uploaded_invoices.sql
│
├── docs/                         # Dokumentácia
├── scripts/                      # Utility skripty
│   └── seed_suppliers.sh         # Inicializácia dodávateľov (filesystem)
└── README.md
```

---

## Hlavné moduly a stránky

### 1. Dashboard (`/`)
Prehľadová stránka so súhrnom — počty faktúr, stav príjmov, upozornenia.

### 2. Faktúry (`/invoices`)
Centrálna správa faktúr od dodávateľov:
- **Upload** ľubovoľného formátu (PDF, CSV, XLSX, DOC…)
- **Server-side filtrovanie** — číslo faktúry, dodávateľ, dátumy, sumy, stav platby
- **Detail faktúry** (`/invoices/:id`) — metadata, položky, prepojenie na produkty
- **Stav platby** — unpaid / partial / paid + tracking po splatnosti
- **Stav príjmu** — not_started / new / in_progress / completed

### 3. Príjem tovaru (`/receiving`)
Workflow pre fyzický príjem tovaru:
- Vytvorenie **receiving session** naviazanej na faktúru
- **Skenovanie EAN kódov** — systém nájde produkt, zaznamená množstvo
- **Porovnanie** objednaného vs. prijatého množstva
- **Finalizácia** — zápis do stock_movements (imutabilný ledger)

### 4. Dodávatelia (`/suppliers`)
- Konfigurácia dodávateľov (filesystem-based `config.json`)
- **XML feedy** — automatické sťahovanie produktových feedov
- **Adaptéry** — per-supplier parsery (Paul Lange, Northfinder…)
- **Upload faktúr** — cez B2B portál alebo manuálne
- **História konfigurácií** s verzionovaním

### 5. Sklad (`/stock`)
Sledovanie skladových zásob — množstvá, pohyby, rezervácie.

### 6. Produkty (`/products`)
Katalóg produktov s multi-EAN podporou:
- Jeden produkt môže mať viacero EAN/UPC/čiarových kódov
- Prepojenie na supplier_products (dodávateľské položky)
- Variantné atribúty (veľkosť, farba…)

### 7. Predajne (`/shops`)
Konfigurácia predajní / e-shopov (Upgates platforma):
- Export CSV synchronizácia
- Nastavenie dostupnosti a oversell mode

---

## Duálny dátový model

Systém aktuálne používa **dva zdroje dát**, čo je dôležité pochopiť:

### Filesystem (config.json + súbory)
- **Zoznam dodávateľov** — priečinky v `/opt/inventory-data/suppliers/*/config.json`
- **Konfigurácia** — feed URL, credentials, adaptér nastavenia
- **Súbory** — faktúry (PDF/CSV), XML feedy, logy, Upgates CSV výstupy
- Používajú: `GET /suppliers`, `GET /suppliers/{s}/config`, `GET /suppliers/{s}/files`

### PostgreSQL (tabuľky)
- **Dodávatelia** — tabuľka `suppliers` (code, name, is_active)
- **Faktúry** — `uploaded_invoices` + `uploaded_invoice_lines`
- **Príjem** — `receiving_sessions` + `receiving_lines`
- **Produkty** — `products` + `product_identifiers` (multi-EAN)
- **Sklad** — `stock_balances` + `stock_movements`
- Používajú: `GET /invoices`, `/receiving/sessions`, `/invoices/suppliers`

> **Dôležité:** Dodávatelia musia byť evidovaní **na oboch miestach** — filesystem config pre konfiguráciu a DB tabuľka pre relácie (faktúry, receiving). Pri pridaní nového dodávateľa treba vytvoriť priečinok s `config.json` a zároveň INSERT do `suppliers` tabuľky.

---

## Databázová schéma (kľúčové tabuľky)

Databáza obsahuje 30+ tabuliek. Tu sú najdôležitejšie:

| Tabuľka | Popis |
|---------|-------|
| `suppliers` | Dodávatelia (code, name, adapter, currency) |
| `products` | Produktový katalóg (SKU, brand, cena, dostupnosť) |
| `product_identifiers` | Multi-EAN — EAN, UPC, supplier_sku, unverified_barcode |
| `supplier_products` | Položky z dodávateľských feedov |
| `supplier_feeds` | Konfigurácia XML feedov |
| `supplier_feed_runs` | História behu feedov |
| `uploaded_invoices` | Nahrané faktúry (súbor + metadata) |
| `uploaded_invoice_lines` | Položky faktúr |
| `receiving_sessions` | Príjmové relácie (session per faktúra) |
| `receiving_lines` | Riadky príjmu (objednané vs. prijaté) |
| `stock_balances` | Aktuálne stavy skladu (per produkt × sklad) |
| `stock_movements` | Imutabilný ledger pohybov (receiving, sales, adjustments) |
| `warehouses` | Sklady (s jedným default) |
| `shops` | E-shopy (Upgates platforma) |
| `config_versions` | Verzionované konfigurácie (supplier, shop, system) |

### Multi-EAN systém
Produkt nemá stĺpec `primary_ean`. Namiesto toho sa používa tabuľka `product_identifiers` s typmi: `ean`, `upc`, `unverified_barcode`, `supplier_sku`, `internal_sku`, `manufacturer`, `custom`. Každý typ má vlastné pravidlá unikátnosti (EAN globálne unikátny, supplier_sku unikátny per dodávateľ, atď.).

---

## Lokálny vývoj

### Backend
```bash
cd api
pip install -r requirements.txt
uvicorn inventory_hub.main:app --reload --port 8000
```
Premenné prostredia: `INVENTORY_DATA_ROOT`, `USE_POSTGRES`, `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`.

### Frontend
```bash
cd frontend
npm install
npm run dev    # Vite dev server na porte 5173
```

### Databáza
```bash
# Init schéma
psql -d inventory_hub -f infra/db-init/001_schema.sql
psql -d inventory_hub -f infra/db-init/002_invoice_management.sql
psql -d inventory_hub -f infra/db-init/003_uploaded_invoices.sql

# Seed dodávateľov do DB
psql -d inventory_hub -c "INSERT INTO suppliers (code, name) VALUES
  ('paul-lange', 'Paul-Lange'),
  ('northfinder', 'Northfinder')
ON CONFLICT (code) DO NOTHING;"
```

---

## Deploy na server

```bash
cd /opt/inventory-hub

# Rebuild a reštart
docker compose down
docker compose up -d

# Over
docker ps
curl -s https://hub.biketrek.sk/api/health

# Logy
docker logs inventory-hub-api-1 --tail=50
```

---

## Lokalizácia

Aplikácia podporuje slovenčinu (default) a angličtinu. Preklady sú v `frontend/src/i18n/`. Prepínanie jazykov je v sidebar menu.

---

## Kľúčové koncepty

### Supplier Adapters
Každý dodávateľ má vlastný "adaptér" — Python modul, ktorý rozumie jeho formátom (CSV, XML, PDF). Napríklad `paul_lange_v1` vie spracovať Paul Lange faktúry a vytvoriť z nich import CSV pre Upgates.

### Receiving Workflow
1. Používateľ vyberie faktúru a spustí "Príjem"
2. Vytvorí sa `receiving_session` naviazaná na faktúru
3. Skenuje EAN kódy → systém nájde produkt a zapíše prijaté množstvo
4. Po finalizácii sa vytvoria záznamy v `stock_movements`

### Filesystem Configs
Konfigurácia dodávateľov je uložená ako `config.json` na disku. API endpoint `GET /suppliers` číta z `inventory-data/suppliers/*/config.json`. Pre DB-backed endpointy (faktúry, receiving) musia byť dodávatelia evidovaní aj v PostgreSQL tabuľke `suppliers`.

### Upgates Integrácia
Upgates je e-shopová platforma. Systém generuje CSV súbory v Upgates formáte (s hranatými zátvorkami v hlavičkách) pre import produktov — rozdelené na "existujúce" (aktualizácia cien/skladov) a "nové" (nové produkty).
