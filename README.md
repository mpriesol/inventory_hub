# Invoice Module v2 - FINAL

## Čo obsahuje

### 1. Seed Script: `scripts/seed_suppliers.sh`
- Vytvorí supplier config files v `inventory-data/suppliers/*/config.json`
- API `/api/suppliers` číta z týchto filesystem configs
- **SPUSTI TOTO PRVÉ** ak dropdown dodávateľov je prázdny

### 2. SQL Migrácia: `infra/db-init/003_uploaded_invoices.sql`
- Nová tabuľka `uploaded_invoices` - pre zoznam faktúr (nezávislé od receiving_sessions)
- Nová tabuľka `uploaded_invoice_lines` - pre položky faktúr
- View `v_uploaded_invoices` s computed fields (is_overdue, days_until_due, receiving_status)
- **DÔLEŽITÉ**: receiving_session_id je NULL až kým nespustíš "Príjem"

### 3. Backend: `api/inventory_hub/routers/invoices_unified.py`
- `POST /invoices/upload` - upload ľubovoľného formátu (PDF, CSV, XLSX, DOC...)
- `GET /invoices` - server-side filtrovanie (pre filter row)
- `GET /invoices/{id}` - detail faktúry s položkami
- `GET /invoices/{id}/download` - stiahnutie originálneho súboru
- `PATCH /invoices/{id}` - manuálne úpravy
- `DELETE /invoices/{id}` - zmazanie

### 4. Frontend: `frontend/src/pages/InvoicesPage.tsx`
- **Filter row** priamo v tabuľke (Upgates štýl)
- Upload modal s formulárom
- Download link pre každú faktúru
- Debounced server-side filtre (400ms)
- Zachovaný dark/amber dizajn

### 5. Frontend: `frontend/src/pages/InvoiceDetailPage.tsx`
- Detail faktúry
- Zoznam položiek (ak sú vyparsované)
- Editácia metadát
- Prepojenie na produkty

---

## Deployment

### 1. Skopírovať súbory na VPS

```bash
cd /opt/inventory-hub/current

# Seed script (DÔLEŽITÉ!)
cp scripts/seed_suppliers.sh ./

# SQL migrácia
cp infra/db-init/003_uploaded_invoices.sql infra/db-init/

# Backend router
cp api/inventory_hub/routers/invoices_unified.py api/inventory_hub/routers/

# Frontend
cp frontend/src/pages/InvoicesPage.tsx frontend/src/pages/
cp frontend/src/pages/InvoiceDetailPage.tsx frontend/src/pages/
```

### 2. Seed supplier configs (DÔLEŽITÉ!)

Projekt používa **filesystem configs** pre suppliers (nie DB tabuľku).
API `/api/suppliers` číta z `inventory-data/suppliers/*/config.json`.

```bash
# Skopíruj seed script
cp scripts/seed_suppliers.sh /opt/inventory-hub/current/

# Spusti seed (vytvorí config.json pre všetkých dodávateľov)
dcp exec api bash /opt/inventory-hub/current/seed_suppliers.sh

# Alternatívne priamo na hoste:
INVENTORY_DATA_ROOT=/data/inventory-data bash seed_suppliers.sh
```

Overenie:
```bash
# Skontroluj či existujú configs
ls -la /data/inventory-data/suppliers/

# Test API
curl -s https://hub.biketrek.sk/api/suppliers | jq '.[].code'
```

### 3. Aplikovať SQL migráciu (uploaded_invoices tabuľka)

```bash
# Najprv migrácia pre uploaded_invoices tabuľku
dcp exec -T postgres psql -v ON_ERROR_STOP=1 \
  -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -f /docker-entrypoint-initdb.d/003_uploaded_invoices.sql
```

Overenie:
```bash
# Skontroluj či existuje uploaded_invoices tabuľka
dcp exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "SELECT column_name FROM information_schema.columns WHERE table_name='uploaded_invoices';"
```

### 3. Pridať router do main.py

V `api/inventory_hub/main.py` pridať:
```python
from inventory_hub.routers.invoices_unified import router as invoices_unified_router

# Niekde pod ostatné include_router:
app.include_router(invoices_unified_router)
```

### 4. Pridať route do frontendu

V `frontend/src/App.tsx` (alebo router config) pridať:
```tsx
import { InvoicesPage } from './pages/InvoicesPage';
import { InvoiceDetailPage } from './pages/InvoiceDetailPage';

// V routes:
<Route path="/invoices" element={<InvoicesPage />} />
<Route path="/invoices/:id" element={<InvoiceDetailPage />} />
```

### 5. Rebuild

```bash
# Backend
dcp up -d --build api

# Frontend
cd frontend && npm run build && cd ..
dcp restart caddy

# Logy
dcp logs --tail=100 api
```

---

## Test

```bash
# 1. Test suppliers (NAJPRV TOTO - mal by vrátiť 9 dodávateľov)
curl -s https://hub.biketrek.sk/api/suppliers | jq '.[].code'

# Ak vráti prázdne pole, spusti seed:
# dcp exec api bash /opt/inventory-hub/current/seed_suppliers.sh

# 2. Test upload
curl -X POST https://hub.biketrek.sk/api/invoices/upload \
  -F "supplier_code=paul-lange" \
  -F "file=@FA_paullange.pdf"

# 3. Test list
curl -s "https://hub.biketrek.sk/api/invoices?page=1&page_size=10" | jq '.items | length'

# 4. Test download
curl -I "https://hub.biketrek.sk/api/invoices/1/download"
```

---

## Štruktúra úložiska súborov

```
/data/inventory-data/suppliers/
├── paul-lange/
│   └── invoices/
│       └── raw/
│           └── 2026/
│               └── 01/
│                   ├── FA_2025072207.pdf
│                   └── FA_2025072208.xlsx
├── northfinder/
│   └── invoices/
│       └── raw/
│           └── 2026/
│               └── 01/
│                   └── ...
```

---

## Filter Row Parameters

| Parameter | Popis |
|-----------|-------|
| `f_invoice_number` | Číslo faktúry alebo názov súboru (ILIKE) |
| `f_supplier` | Dodávateľ - meno alebo kód (ILIKE) |
| `date_from`, `date_to` | Dátum vystavenia od/do |
| `due_from`, `due_to` | Splatnosť od/do |
| `f_amount_min`, `f_amount_max` | Suma min/max |
| `f_items_min`, `f_items_max` | Položky min/max |
| `f_currency` | Mena (EUR, CZK...) |
| `payment_status` | unpaid / partial / paid |
| `receiving_status` | not_started / new / in_progress / completed |
| `is_overdue` | true / false |

---

## Poznámky

1. **Suppliers sú filesystem-based** - API `/api/suppliers` číta z `inventory-data/suppliers/*/config.json`
2. **Ak dropdown "Dodávateľ" je prázdny** - spusti `seed_suppliers.sh` (vytvára config.json súbory)
3. **Upload modal** používa `/api/suppliers` endpoint
4. **Upload** ukladá súbory do: `suppliers/{code}/invoices/raw/{year}/{month}/`
5. **Faktúra sa zobrazí** hneď po uploade (v tabuľke `uploaded_invoices`)
6. **receiving_session** sa vytvorí až keď spustíš "Príjem" v inom tabe
7. **Parsing** zatiaľ neimplementovaný - položky treba pridať manuálne alebo dorobiť parser pre konkrétneho dodávateľa
8. **Manuálne úpravy** - možné cez PATCH endpoint alebo UI
