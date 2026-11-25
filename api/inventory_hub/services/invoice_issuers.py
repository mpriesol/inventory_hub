"""Comparable issuer identifiers for externally issued documents."""
import re
import unicodedata


VAVRYS_ISSUER_KEYS = frozenset(('vavrysczsro', 'vavryssksro'))


def indexed_invoice_key(supplier, entry):
    if supplier == 'vavrys':
        return str(entry.get('number') or '')
    return str(entry.get('original_number') or entry.get('number') or entry.get('invoice_id') or '')


def issuer_key(value):
    plain = unicodedata.normalize("NFKD", value.casefold())
    return re.sub(r"[^a-z0-9]+", "", plain)
