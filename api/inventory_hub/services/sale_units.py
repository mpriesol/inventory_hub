"""Explicit sale-unit conversions; physical balances always stay in base pieces.

Legacy one-to-one mappings deliberately retain their old snapshot shape. A
shared mapping has a stable leaf key even when its conversion factor is one.
No barcode, title, pack_size or supplier quantity implies a conversion.
"""
from decimal import Decimal, InvalidOperation


MAX_FACTOR = Decimal("1000000")
FIELDS = frozenset({"sale_key", "sale_mapping_id", "sale_to_stock_factor"})


class SaleUnitError(ValueError):
    pass


def factor(value=1) -> Decimal:
    """Return an explicit positive whole-piece sale factor, without rounding."""
    if value is None or isinstance(value, bool):
        raise SaleUnitError("sale_factor_invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise SaleUnitError("sale_factor_invalid") from None
    if not result.is_finite() or not 1 <= result <= MAX_FACTOR or result != result.to_integral_value():
        raise SaleUnitError("sale_factor_invalid")
    return result


def sale_key(mapping) -> str:
    if getattr(mapping, "shared_stock", False) is not True:
        return ""
    identifier = getattr(mapping, "id", None)
    if type(identifier) is not int or identifier <= 0:
        raise SaleUnitError("sale_mapping_invalid")
    return str(identifier)


def mapping_fields(mapping) -> dict:
    key = sale_key(mapping)
    raw = getattr(mapping, "sale_to_stock_factor", 1)
    # SQLAlchemy Python defaults are not populated before an ordinary INSERT.
    value = factor(1 if raw is None and not key else raw)
    if not key:
        if value != 1:
            raise SaleUnitError("sale_factor_unconfirmed")
        return {}
    return {"sale_key": key, "sale_mapping_id": mapping.id,
            "sale_to_stock_factor": str(int(value))}


def line_fields(line: dict) -> dict:
    """Validate a frozen conversion proof; partial metadata is never legacy."""
    if not FIELDS.intersection(line):
        return {}
    identifier = line.get("sale_mapping_id")
    if (not FIELDS <= line.keys() or type(identifier) is not int or identifier <= 0
            or line.get("sale_key") != str(identifier)):
        raise SaleUnitError("sale_mapping_invalid")
    value = factor(line.get("sale_to_stock_factor"))
    return {"sale_key": str(identifier), "sale_mapping_id": identifier,
            "sale_to_stock_factor": str(int(value))}


def line_factor(line: dict) -> Decimal:
    proof = line_fields(line)
    return factor(proof.get("sale_to_stock_factor", 1))
