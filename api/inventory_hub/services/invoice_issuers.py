"""Comparable issuer identifiers for externally issued documents."""
import re
import unicodedata


def issuer_key(value):
    plain = unicodedata.normalize("NFKD", value.casefold())
    return re.sub(r"[^a-z0-9]+", "", plain)
