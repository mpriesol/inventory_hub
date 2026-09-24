FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

# System deps for lxml/pyarrow + Playwright browser requirements
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    # Playwright Chromium dependencies
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    libpango-1.0-0 libcairo2 libatspi2.0-0 \
  && rm -rf /var/lib/apt/lists/*

COPY api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Install Playwright browsers (Chromium only)
RUN playwright install chromium

COPY api/inventory_hub /app/inventory_hub
COPY infra/db-init/005_ai_content.sql /app/inventory_hub/migrations/005_ai_content.sql
COPY infra/db-init/006_opening_stock.sql /app/inventory_hub/migrations/006_opening_stock.sql
COPY infra/db-init/007_order_stock.sql /app/inventory_hub/migrations/007_order_stock.sql
COPY infra/db-init/008_order_collection.sql /app/inventory_hub/migrations/008_order_collection.sql
COPY infra/db-init/009_stock_automation.sql /app/inventory_hub/migrations/009_stock_automation.sql
COPY infra/db-init/010_stock_publication.sql /app/inventory_hub/migrations/010_stock_publication.sql
COPY infra/db-init/011_fifo.sql /app/inventory_hub/migrations/011_fifo.sql
COPY infra/db-init/012_product_editor.sql /app/inventory_hub/migrations/012_product_editor.sql
COPY infra/db-init/013_receiving_scan_requests.sql /app/inventory_hub/migrations/013_receiving_scan_requests.sql
COPY infra/db-init/014_supplier_availability.sql /app/inventory_hub/migrations/014_supplier_availability.sql
COPY infra/db-init/015_stock_sync.sql /app/inventory_hub/migrations/015_stock_sync.sql
COPY infra/db-init/016_product_publication.sql /app/inventory_hub/migrations/016_product_publication.sql
COPY infra/db-init/017_stock_adjustments.sql /app/inventory_hub/migrations/017_stock_adjustments.sql
COPY api/pl_pdf_invoice_extractor.py /app/pl_pdf_invoice_extractor.py

EXPOSE 8000
CMD ["uvicorn", "inventory_hub.main:app", "--host", "0.0.0.0", "--port", "8000"]
