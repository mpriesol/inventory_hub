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
COPY api/pl_pdf_invoice_extractor.py /app/pl_pdf_invoice_extractor.py

EXPOSE 8000
CMD ["uvicorn", "inventory_hub.main:app", "--host", "0.0.0.0", "--port", "8000"]
