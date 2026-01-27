FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

# (optional) system deps - safe for lxml/pyarrow wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
  && rm -rf /var/lib/apt/lists/*

COPY api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY api/inventory_hub /app/inventory_hub
COPY api/pl_pdf_invoice_extractor.py /app/pl_pdf_invoice_extractor.py

EXPOSE 8000
CMD ["uvicorn", "inventory_hub.main:app", "--host", "0.0.0.0", "--port", "8000"]
