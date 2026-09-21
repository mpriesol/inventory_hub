"""Display explicit variants by colour, then apparel or natural numeric size."""
import re
import unicodedata

from inventory_hub.catalog_types import CatalogProduct


def _fold(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c)).casefold().strip()


def _natural(value: str) -> tuple:
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in re.split(r"(\d+)", _fold(value)))


def variant_sort_key(product: CatalogProduct) -> tuple:
    attributes = {_fold(a.name): a.value for a in product.variant_attributes}
    color = next((attributes[k] for k in ("farba", "color", "colour", "farbe") if k in attributes), "")
    size = _fold(next((attributes[k] for k in ("velkost", "velikost", "size", "grosse") if k in attributes), ""))
    size = size.replace(" ", "")
    sizes = {"xxxs": -3, "3xs": -3, "xxs": -2, "2xs": -2, "xs": -1, "s": 0, "m": 1, "l": 2, "xl": 3}
    large = re.fullmatch(r"(?:(\d+)x|(x{2,}))l", size)
    rank = sizes.get(size)
    if large:
        rank = 2 + (int(large[1]) if large[1] else len(large[2]))
    return (_natural(color), (0, rank) if rank is not None else (1, _natural(size)),
            _natural(product.shop_code), product.id)
