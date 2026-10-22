"""Explicit card units and purchase conversions; never infer a package factor."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy import exists, or_, select

from inventory_hub.db_models_ext import Reservation, StockBalance, StockMovement
from inventory_hub.stock_tracking_models import StockTracking


UNITS = ("pcs", "pair", "pack", "m")
QUANTITY = Decimal("0.001")
COST = Decimal("0.0001")


class ItemUnitError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def validate_stock_quantity(quantity, stock_unit):
    quantity = Decimal(quantity)
    if (not quantity.is_finite() or quantity < 0 or quantity > Decimal("999999999.999")
            or quantity != quantity.quantize(QUANTITY) or stock_unit not in UNITS
            or (stock_unit != "m" and quantity != quantity.to_integral_value())):
        raise ItemUnitError("stock_quantity_unit_mismatch")
    return quantity


def receipt_values(quantity, unit_cost, factor=Decimal("1")):
    """Return base quantity and EUR cost per base unit at ledger precision.

    Purchase quantity and price stay on the invoice line. The existing FIFO
    engine stores acquisition unit costs to four decimals, rounded HALF_UP.
    """
    try:
        quantity, factor = Decimal(quantity), Decimal(factor)
        if (not quantity.is_finite() or quantity < 0 or not factor.is_finite()
                or not 0 < factor <= Decimal("999999.999") or factor != factor.quantize(QUANTITY)):
            raise ItemUnitError("receiving_unit_conversion_invalid")
        amount = quantity * factor
        if amount > Decimal("999999999.999") or amount != amount.quantize(QUANTITY):
            raise ItemUnitError("receiving_unit_quantity_precision")
        if unit_cost is None:
            return amount, None
        unit_cost = Decimal(unit_cost)
        if not unit_cost.is_finite() or unit_cost < 0:
            raise ItemUnitError("receiving_unit_cost_invalid")
        cost = (unit_cost / factor).quantize(COST, rounding=ROUND_HALF_UP)
        if unit_cost > 0 and cost == 0:
            raise ItemUnitError("receiving_unit_cost_precision")
        if cost > Decimal("99999999.9999"):
            raise ItemUnitError("receiving_unit_cost_invalid")
        if ((quantity * unit_cost).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                != (amount * cost).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)):
            raise ItemUnitError("receiving_unit_cost_precision")
        return amount, cost
    except (InvalidOperation, ValueError, TypeError):
        raise ItemUnitError("receiving_unit_conversion_invalid") from None


def definition_lock_condition(product_id):
    return or_(
        exists(select(StockMovement.id).where(StockMovement.product_id == product_id)),
        exists(select(Reservation.id).where(Reservation.product_id == product_id)),
        exists(select(StockTracking.product_id).where(StockTracking.product_id == product_id)),
        exists(select(StockBalance.id).where(StockBalance.product_id == product_id,
            or_(StockBalance.qty_on_hand != 0, StockBalance.qty_reserved != 0, StockBalance.qty_quarantined != 0))),
    )


async def definition_locked(db, product_id):
    """Any confirmed physical history or reservation fixes a card's unit."""
    return bool(await db.scalar(select(definition_lock_condition(product_id))))
