"""Acknowledged local/native checkpoints for one exact shop stock leaf."""
from datetime import datetime
from decimal import Decimal
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from inventory_hub.database import Base


class StockSyncCheckpoint(Base):
    __tablename__ = "stock_sync_checkpoints"
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shops.id", ondelete="RESTRICT"), primary_key=True)
    sku: Mapped[str] = mapped_column(String(100), primary_key=True)
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    target_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[dict] = mapped_column(JSONB, nullable=False)
    local_quantity: Mapped[Decimal] = mapped_column(Numeric(24, 0), nullable=False)
    native_quantity: Mapped[Decimal] = mapped_column(Numeric(24, 0), nullable=False)
    metadata_values: Mapped[dict] = mapped_column(JSONB, nullable=False)
    hold_id: Mapped[str] = mapped_column(String(36), ForeignKey("stock_publication_holds.id", ondelete="RESTRICT"), nullable=False)
    baseline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_audit_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_observation: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))


class StockSyncBaselineEvent(Base):
    __tablename__ = 'stock_sync_baseline_events'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('shops.id', ondelete='RESTRICT'), nullable=False)
    hold_id: Mapped[str] = mapped_column(String(36), ForeignKey('stock_publication_holds.id', ondelete='RESTRICT'), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)

