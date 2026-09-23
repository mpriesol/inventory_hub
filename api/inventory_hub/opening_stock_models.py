"""Durable opening previews and their one-time ledger references."""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from inventory_hub.database import Base


class OpeningStockBatch(Base):
    __tablename__ = "opening_stock_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    warehouse_code: Mapped[str] = mapped_column(String(50), nullable=False)
    source_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    operator_name: Mapped[str] = mapped_column(String(100), nullable=False)
    counted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="prepared")
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint("status IN ('prepared', 'completed')", name="chk_opening_batch_status"),
        CheckConstraint("expires_at > created_at", name="chk_opening_batch_expiry"),
        CheckConstraint("(status = 'prepared' AND completed_at IS NULL AND result IS NULL) OR "
                        "(status = 'completed' AND completed_at IS NOT NULL AND result IS NOT NULL)",
                        name="chk_opening_batch_result"),
    )


class OpeningStockLine(Base):
    __tablename__ = "opening_stock_lines"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(36), ForeignKey("opening_stock_batches.id", ondelete="RESTRICT"), nullable=False)
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    sku: Mapped[str] = mapped_column(String(100), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(16, 4), nullable=False)
    unit: Mapped[str] = mapped_column(String(2), nullable=False)
    movement_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("stock_movements.id", ondelete="RESTRICT"), unique=True)

    __table_args__ = (
        UniqueConstraint("batch_id", "line_number", name="uq_opening_line_number"),
        UniqueConstraint("batch_id", "product_id", name="uq_opening_line_product"),
        CheckConstraint("line_number > 0", name="chk_opening_line_number"),
        CheckConstraint("quantity > 0 AND quantity <= 999999999 AND quantity = trunc(quantity)", name="chk_opening_quantity"),
        CheckConstraint("unit_cost >= 0", name="chk_opening_cost"),
        CheckConstraint("unit = 'ks'", name="chk_opening_unit"),
        CheckConstraint("value >= 0 AND value = round(quantity * unit_cost, 4)", name="chk_opening_value"),
    )
