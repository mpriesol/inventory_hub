#!/bin/bash
# ============================================================================
# SEED: Create supplier configs in filesystem
# 
# This script creates config.json files for all known suppliers.
# The API /api/suppliers reads from these filesystem configs.
#
# Usage:
#   chmod +x seed_suppliers.sh
#   ./seed_suppliers.sh
#
# Or directly:
#   bash seed_suppliers.sh
# ============================================================================

set -e

# Base directory for supplier data
DATA_ROOT="${INVENTORY_DATA_ROOT:-/data/inventory-data}"
SUPPLIERS_DIR="${DATA_ROOT}/suppliers"

echo "Creating supplier configs in: ${SUPPLIERS_DIR}"

# Function to create supplier config
create_supplier() {
    local code="$1"
    local name="$2"
    local country="${3:-SK}"
    
    local supplier_dir="${SUPPLIERS_DIR}/${code}"
    local config_file="${supplier_dir}/config.json"
    
    # Create directories
    mkdir -p "${supplier_dir}/invoices/raw"
    mkdir -p "${supplier_dir}/invoices/csv"
    mkdir -p "${supplier_dir}/feeds/xml"
    mkdir -p "${supplier_dir}/feeds/converted"
    mkdir -p "${supplier_dir}/config_history"
    
    # Create config if not exists
    if [ ! -f "${config_file}" ]; then
        cat > "${config_file}" << EOF
{
    "code": "${code}",
    "name": "${name}",
    "is_active": true,
    "country": "${country}",
    "product_prefix": "${code^^}_",
    "download_strategy": "manual",
    "invoice_settings": {
        "default_currency": "EUR",
        "vat_included": true,
        "default_vat_rate": 23
    },
    "feed_settings": {
        "mode": "none"
    },
    "created_at": "$(date -Iseconds)",
    "updated_at": "$(date -Iseconds)"
}
EOF
        echo "  ✓ Created: ${code} (${name})"
    else
        echo "  - Exists: ${code}"
    fi
}

# ============================================================================
# Create all suppliers from project context
# ============================================================================

echo ""
echo "Creating supplier configs..."
echo ""

# Slovak suppliers (VAT included)
create_supplier "paul-lange" "Paul Lange Oslany" "SK"
create_supplier "northfinder" "Northfinder" "SK"
create_supplier "husky" "Husky SK" "SK"
create_supplier "sloger" "Sloger" "SK"

# Czech suppliers (reverse charge - VAT not included)
create_supplier "vertone" "Vertone" "CZ"
create_supplier "ariga" "Ariga" "CZ"
create_supplier "warmpeace" "Warmpeace" "CZ"
create_supplier "zookee" "Zookee" "CZ"

# Polish supplier
create_supplier "spokey" "Spokey" "PL"

echo ""
echo "Done! Suppliers created in ${SUPPLIERS_DIR}"
echo ""

# List created suppliers
echo "Current suppliers:"
ls -la "${SUPPLIERS_DIR}" 2>/dev/null | grep "^d" | awk '{print "  - " $NF}' | grep -v "^\.$"

echo ""
echo "API should now return these suppliers at: GET /api/suppliers"
