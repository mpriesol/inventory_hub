"""Bounded technical feed data; never forward an arbitrary raw source record."""
import ipaddress
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from inventory_hub.services.ai_content_validation import text_of


def public_document_url(value):
    if not isinstance(value, str) or len(value) > 1500:
        return None
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        if (url.scheme != "https" or url.username or url.password or url.query or url.fragment
                or url.port not in (None, 443) or "." not in host
                or host.endswith((".local", ".internal", ".localhost"))
                or not url.path.lower().endswith(".pdf")):
            return None
        try:
            ipaddress.ip_address(host)
            return None
        except ValueError:
            return value
    except ValueError:
        return None


def technical_sources(product):
    # Reviewed Paul Lange fields. New supplier paths require an explicit adapter
    # or mapping decision, not a recursive dump of potentially private raw data.
    if product.supplier != "paul-lange":
        return {}
    static = product.static_parameters
    tables, documents, warnings = [], [], []
    values = static.get("SIZE_TABLE", [])
    for value in values if isinstance(values, list) else [values]:
        if not isinstance(value, str):
            continue
        if len(value) > 100000:
            warnings.append("Technical table exceeds the source limit; consult the product document.")
            continue
        soup = BeautifulSoup(value, "html.parser")
        for element in soup(["script", "style", "iframe", "object"]):
            element.decompose()
        rows = [[text_of(str(cell)) for cell in row.find_all(["td", "th"], recursive=False)]
                for row in soup.find_all("tr")]
        rows = [row for row in rows if any(row)]
        if len(rows) > 200 or sum(len(cell) for row in rows for cell in row) > 16000:
            warnings.append("Technical table exceeds the source limit; consult the product document.")
        elif rows:
            tables.append({"source_path": "STA_PARAMS/SIZE_TABLE", "rows": rows})
        if len(tables) == 2:
            break
    for key in ("STA_URL1", "STA_URL2", "STA_URL3"):
        values = static.get(key, [])
        for value in values if isinstance(values, list) else [values]:
            if (url := public_document_url(value)) and url not in [d["url"] for d in documents]:
                documents.append({"source_path": "STA_PARAMS/" + key, "url": url})
    return {key: value for key, value in {"technical_tables": tables,
        "source_documents": documents[:6], "source_warnings": warnings}.items() if value}
