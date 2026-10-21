"""Keep historical CSV generators out of the current card-allocation workflow."""
from fastapi import HTTPException

from inventory_hub.settings import settings


def require_legacy_import_mode():
    # Legacy JSON installations can still read their original workflow. In the
    # PostgreSQL Hub every new card needs the shared, reviewed SKU identity.
    # A generated CSV with supplier-prefixed CODE would bypass that boundary.
    if settings.USE_POSTGRES:
        raise HTTPException(409, detail={
            "code": "sku_preparation_required",
            "message": "Nové karty priprav cez Založiť skladové karty v príjme alebo cez prípravu produktov z katalógu. Starý CSV postup neoveruje spoločné SKU.",
        })
