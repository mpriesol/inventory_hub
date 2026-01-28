# Northfinder B2B Integration - Setup Guide

## Prehľad

Táto implementácia pridáva podporu pre dodávateľa Northfinder s automatickým sťahovaním faktúr cez Playwright (mode #2 - persistent session).

## Čo je implementované

### A) Backend - Northfinder Invoice Downloader
- **Súbor**: `api/inventory_hub/adapters/northfinder_web.py`
- Playwright-based login s persistent storage_state
- Fetch invoices cez DataTables AJAX API
- Download XLSX a PDF súborov
- Idempotent refresh (neopakuje sťahovanie existujúcich)
- Logging do `suppliers/northfinder/logs/`

### B) Backend - XLSX → CSV Parser
- **Súbor**: `api/inventory_hub/adapters/northfinder_xlsx_parser.py`
- Konvertuje Northfinder XLSX faktúry na kanonický CSV formát
- Stĺpce: SCM, TITLE, QTY, PRICE, EAN, CATALOG, COLOR, SIZE, RRP, ...

### C) Backend - Strategy Dispatch
- **Súbor**: `api/inventory_hub/routers/invoices.py`
- Podporované stratégie: `paul-lange-web`, `northfinder-web`, `manual`
- Jednoduché pridanie nových stratégií

### D) Frontend - Fixed Feeds Tab
- **Súbor**: `frontend/src/pages/SuppliersPage.tsx`
- Zobrazuje OBA feedy (products + stock) súčasne
- Každý feed má vlastné nastavenia

### E) Frontend - Fixed Supplier Code Bug
- **Súbor**: `frontend/src/pages/ReceivingPage.tsx`
- Používa `code` namiesto `supplier_code`
- API calls teraz používajú správny lowercase code

### F) Frontend - i18n Scaffold
- **Súbory**: `frontend/src/i18n/`
- Slovenčina (default) + Angličtina
- Language toggle v sidebar
- Preložené nav labels

---

## Konfigurácia Northfinder

### 1. Vytvorte supplier priečinok

```bash
mkdir -p /data/inventory-data/suppliers/northfinder
```

### 2. Vytvorte config.json

```bash
cp northfinder_config_sample.json /data/inventory-data/suppliers/northfinder/config.json
```

### 3. Vyplňte credentials

Editujte `config.json` a nahraďte:
- `FILL_ME_B2B_EMAIL` - váš Northfinder B2B email
- `FILL_ME_B2B_PASSWORD` - vaše Northfinder B2B heslo
- `FILL_ME_NORTHFINDER_TOKEN` - token pre XML feed (ak používate)

### 4. Storage State Location

Po prvom prihlásení sa uloží session do:
```
/data/inventory-data/suppliers/northfinder/state/storage_state.json
```

Tento súbor obsahuje cookies a umožňuje refresh bez opakovaného loginu.

---

## Spustenie Invoice Refresh

### Cez API

```bash
# Refresh posledných 6 mesiacov
curl -X POST "https://hub.biketrek.sk/api/suppliers/northfinder/invoices/refresh"

# Refresh posledných 12 mesiacov
curl -X POST "https://hub.biketrek.sk/api/suppliers/northfinder/invoices/refresh?months_back=12"
```

### Cez UI

1. Otvorte `/receiving`
2. Vyberte "Northfinder" z dropdown
3. Faktúry sa zobrazia automaticky po refresh

---

## Štruktúra súborov

```
/data/inventory-data/suppliers/northfinder/
├── config.json                    # Konfigurácia dodávateľa
├── state/
│   └── storage_state.json         # Playwright session (auto-created)
├── invoices/
│   ├── raw/                       # Stiahnuté XLSX súbory
│   │   ├── FV2026001__123.xlsx
│   │   └── FV2026002__124.xlsx
│   ├── csv/                       # Konvertované CSV
│   │   ├── FV2026001.csv
│   │   └── FV2026002.csv
│   ├── pdf/                       # PDF faktúry (ak dostupné)
│   │   └── FV2026001__123.pdf
│   └── index.latest.json          # Index faktúr
└── logs/
    └── invoices_refresh_20260128_104523.log
```

---

## Smoke Test

1. **Backend beží**:
   ```bash
   curl https://hub.biketrek.sk/api/health
   ```

2. **Supplier existuje**:
   ```bash
   curl https://hub.biketrek.sk/api/suppliers | jq '.[] | select(.code=="northfinder")'
   ```

3. **Invoice refresh**:
   ```bash
   curl -X POST https://hub.biketrek.sk/api/suppliers/northfinder/invoices/refresh
   ```

4. **Invoice index**:
   ```bash
   curl https://hub.biketrek.sk/api/suppliers/northfinder/invoices/index
   ```

5. **UI test**:
   - Otvorte `/receiving`
   - Vyberte "Northfinder"
   - Mali by sa zobraziť faktúry

---

## Dependencies

### Python (api/requirements.txt)
```
playwright>=1.40,<2
openpyxl>=3.1,<4
```

### Docker
Dockerfile obsahuje:
```dockerfile
RUN apt-get install -y libnss3 libnspr4 ... # Playwright deps
RUN playwright install chromium
```

### Frontend (package.json)
```json
"i18next": "^24.2.3",
"react-i18next": "^15.4.1"
```

---

## Troubleshooting

### Login Failed
- Skontrolujte credentials v config.json
- Skúste manuálne prihlásiť sa na https://b2b.northfinder.com
- Skontrolujte logy v `suppliers/northfinder/logs/`

### Session Expired
- Zmažte `state/storage_state.json`
- Spustite refresh znova - vytvorí novú session

### No XLSX Download
- Northfinder možno zmenil UI
- Skontrolujte HTML v detail page
- Aktualizujte regex v `_fetch_invoice_detail()`

### XLSX Parse Error
- Skontrolujte formát XLSX
- Overte že stĺpce "Kód", "Názov", "Množstvo", "Jednotková cena v EUR" existujú
