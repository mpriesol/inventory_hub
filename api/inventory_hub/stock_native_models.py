"""Audited native Upgates order obligations, separate from physical stock."""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from inventory_hub.database import Base


class StockNativeEvent(Base):
    __tablename__ = "stock_native_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shop_orders.id", ondelete="RESTRICT"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    facts: Mapped[dict] = mapped_column(JSONB, nullable=False)
    error: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class StockNativeOrder(Base):
    __tablename__ = "stock_native_orders"
    order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shop_orders.id", ondelete="RESTRICT"), primary_key=True)
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shops.id", ondelete="RESTRICT"), nullable=False)
    warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    order_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stock_native_events.id", ondelete="RESTRICT"), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status_type: Mapped[str] = mapped_column(String(50), nullable=False)
    canceled_seen: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(String(100))


class StockNativeLine(Base):
    __tablename__ = "stock_native_lines"
    order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stock_native_orders.order_id", ondelete="RESTRICT"), primary_key=True)
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 0), nullable=False)
