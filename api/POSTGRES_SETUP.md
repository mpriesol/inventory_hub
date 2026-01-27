# PostgreSQL Setup for Inventory Hub v12 FINAL

## Overview

Inventory Hub v12 uses PostgreSQL for:
- **30 tables** with proper foreign keys and constraints
- **Multi-EAN support** via `product_identifiers` table
- **Immutable stock ledger** with trigger protection
- **Receiving deduplication** via `line_number NOT NULL`

---

## Quick Start

### 1. Install PostgreSQL 15+

**Windows:**
```powershell
# Using winget
winget install PostgreSQL.PostgreSQL.15

# Or download installer from https://www.postgresql.org/download/windows/
```

**macOS:**
```bash
brew install postgresql@15
brew services start postgresql@15
```

**Linux (Ubuntu/Debian):**
```bash
sudo apt update
sudo apt install postgresql-15 postgresql-client-15
sudo systemctl start postgresql
```

### 2. Create Database

```bash
# Connect as postgres user
sudo -u postgres psql

# Create database and user
CREATE DATABASE inventory_hub;
CREATE USER inventory_user WITH PASSWORD 'your_secure_password';
GRANT ALL PRIVILEGES ON DATABASE inventory_hub TO inventory_user;

# Connect to new database
\c inventory_hub

# Grant schema permissions
GRANT ALL ON SCHEMA public TO inventory_user;
\q
```

### 3. Run Schema

```bash
# Using psql (recommended for error handling)
psql -v ON_ERROR_STOP=1 -U inventory_user -d inventory_hub -f schema_v12_FINAL.sql

# Expected output at end:
# ╔═══════════════════════════════════════════╗
# ║      === ALL SMOKE TESTS PASSED ===       ║
# ╚═══════════════════════════════════════════╝
```

### 4. Configure Python Application

Create `.env` file in `api/` directory:

```env
# PostgreSQL
DB_HOST=localhost
DB_PORT=5432
DB_NAME=inventory_hub
DB_USER=inventory_user
DB_PASSWORD=your_secure_password

# Feature flags
USE_POSTGRES=true

# File storage (update path for your system)
INVENTORY_DATA_ROOT=C:/path/to/inventory-data
```

### 5. Install Python Dependencies

```bash
cd api
pip install -r requirements.txt
```

New dependencies added for PostgreSQL:
- `sqlalchemy[asyncio]>=2.0` - ORM with async support
- `asyncpg>=0.29` - PostgreSQL async driver
- `psycopg2-binary>=2.9` - PostgreSQL sync driver (for migrations)
- `greenlet>=3.0` - SQLAlchemy async internals

### 6. Start Application

```bash
cd api
uvicorn inventory_hub.main:app --reload
```

Check health endpoint:
```bash
curl http://localhost:8000/health
```

Expected response:
```json
{
  "status": "ok",
  "version": "12.0.0",
  "use_postgres": true,
  "database": {
    "status": "healthy",
    "database": "connected"
  }
}
```

---

## Key Tables Overview

### Core Entities
| Table | Purpose |
|-------|---------|
| `warehouses` | Physical storage locations |
| `suppliers` | Vendors (Paul-Lange, etc.) |
| `shops` | E-commerce platforms (Upgates, Atomeri) |
| `products` | Master product catalog |
| `product_identifiers` | **Multi-EAN support** - multiple barcodes per product |

### Stock Management
| Table | Purpose |
|-------|---------|
| `stock_balances` | Current quantity per warehouse |
| `stock_movements` | **Immutable ledger** - all in/out transactions |

### Receiving Workflow
| Table | Purpose |
|-------|---------|
| `receiving_sessions` | Invoice import sessions |
| `receiving_lines` | Individual invoice lines with `line_number NOT NULL` |
| `scan_events` | Barcode scan history with undo support |

### Sync & Availability
| Table | Purpose |
|-------|---------|
| `shop_product_availability` | Computed availability per shop |
| `shop_sync_outbox` | Reliable delivery queue for shop updates |
| `supplier_feeds` | Automated supplier data imports |

---

## Multi-EAN Support

The v12 schema supports multiple barcodes per product:

### Example: Paul-Lange Product with Multiple Codes
```sql
-- Product with compound EAN from feed: "398828/6927116185329/6938112675813"
INSERT INTO products (sku, name, supplier_id) VALUES ('PL-398828', 'Test Product', 1);

INSERT INTO product_identifiers (product_id, identifier_type, value, is_primary) VALUES
  (1, 'unverified_barcode', '398828', false),
  (1, 'ean', '6927116185329', true),       -- Primary barcode
  (1, 'ean', '6938112675813', false);

-- Lookup by ANY code returns same product
SELECT p.* FROM products p
JOIN product_identifiers pi ON p.id = pi.product_id
WHERE pi.value IN ('398828', '6927116185329', '6938112675813');
```

### Barcode Classification
| Type | Pattern | Uniqueness |
|------|---------|------------|
| `ean` | 13/8 digits with valid checksum | Globally unique |
| `upc` | 12 digits with valid checksum | Globally unique |
| `unverified_barcode` | 4-10 digits | Unique per product |
| `supplier_sku` | Any | Unique per supplier |
| `internal_sku` | Any | Globally unique |

### Primary Barcode Constraint
Only ONE barcode can be primary across the "barcode group" (ean, upc, unverified_barcode):
```sql
-- This enforced by partial unique index:
-- idx_identifiers_primary_barcode_group
```

---

## Immutable Stock Ledger

Stock movements cannot be modified after creation:

```sql
-- This will FAIL:
UPDATE stock_movements SET quantity = 10 WHERE id = 1;
-- ERROR: stock_movements is immutable

-- CORRECT: Create compensating movement
INSERT INTO stock_movements (
  idempotency_key, product_id, warehouse_id, movement_type, quantity,
  balance_after, avg_cost_after, notes
) VALUES (
  'correction:1:20260126', 1, 1, 'ADJUSTMENT_OUT', -2,
  18, 10.00, 'Correction for movement #1'
);
```

---

## Receiving Dedup

v12 FINAL uses `line_number NOT NULL` for duplicate prevention:

```sql
-- Each import assigns sequential line numbers: 1, 2, 3...
INSERT INTO receiving_lines (session_id, line_number, ean, ordered_qty, ...)
VALUES 
  (100, 1, '1234567890123', 5, ...),
  (100, 2, '9876543210987', 3, ...),
  (100, 3, '1234567890123', 2, ...);  -- Same EAN, different line = OK!

-- Double-import blocked by UNIQUE(session_id, line_number)
INSERT INTO receiving_lines (session_id, line_number, ean, ordered_qty, ...)
VALUES (100, 1, '1234567890123', 5, ...);  -- FAILS - line_number 1 already exists
```

---

## Troubleshooting

### Connection Refused
```
connection refused (os error 111)
```
**Fix:** Ensure PostgreSQL is running:
```bash
# Linux
sudo systemctl status postgresql

# Windows
net start postgresql-x64-15

# macOS
brew services list
```

### Permission Denied
```
permission denied for schema public
```
**Fix:** Grant schema permissions:
```sql
GRANT ALL ON SCHEMA public TO inventory_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO inventory_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO inventory_user;
```

### Extension Errors (uuid-ossp, pg_trgm)
These extensions are **optional** and commented out in schema. If you need them:
```sql
-- Connect as postgres superuser first
\c inventory_hub postgres
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
```

### Smoke Tests Failed
If any smoke test fails during schema creation, check the specific error. Common issues:
- Missing seed data (warehouses, shops)
- Incorrect enum values
- Constraint violations in test data

---

## Files Reference

| File | Purpose |
|------|---------|
| `schema_v12_FINAL.sql` | Complete DDL with 30 tables, 12 smoke tests |
| `DATABASE_DESIGN_v12_FINAL.md` | 10 invariants, design decisions |
| `MIGRATION_NOTES_v12_FINAL.md` | Upgrade guide from v11 |
| `IMPLEMENTATION_CHECKLIST_v12_FINAL.md` | Service implementation patterns |

---

## Next Steps

1. ✅ Database created and schema loaded
2. ✅ Python dependencies installed
3. ✅ Application connected
4. 🔲 Seed initial data (suppliers, warehouses)
5. 🔲 Test receiving workflow
6. 🔲 Configure shop sync
