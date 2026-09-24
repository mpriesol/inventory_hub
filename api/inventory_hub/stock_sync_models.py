"""Scheduled central-authority publication with durable per-leaf write fences."""
from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from inventory_hub.database import Base


class StockSyncWarehouseSettings(Base):
    __tablename__ = "stock_sync_warehouse_settings"
    warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    max_order_age_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=900)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class StockSyncSettings(Base):
    __tablename__ = "stock_sync_settings"
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shops.id", ondelete="RESTRICT"), primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    authorized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    interval_seconds: Mapped[int | None] = mapped_column(Integer)
    batch_size: Mapped[int | None] = mapped_column(Integer)
    max_order_age_seconds: Mapped[int | None] = mapped_column(Integer)
    target_fingerprint: Mapped[str | None] = mapped_column(String(64))
    authority_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    authority_snapshot: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(100))
    retry_after_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StockSyncRun(Base):
    __tablename__ = "stock_sync_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shops.id", ondelete="RESTRICT"), nullable=False)
    warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    target_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    settings_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    selected_skus: Mapped[list | None] = mapped_column(JSONB(none_as_null=True))
    cursor_product_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    max_product_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_batch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(100))


class StockSyncItem(Base):
    __tablename__ = "stock_sync_items"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("stock_sync_runs.id", ondelete="RESTRICT"), nullable=False)
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shops.id", ondelete="RESTRICT"), nullable=False)
    warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    sku: Mapped[str] = mapped_column(String(100), nullable=False)
    target: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    desired: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    before: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    after: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="prepared")
    error: Mapped[str | None] = mapped_column(String(100))
    attempt_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    __table_args__ = (Index("uq_stock_sync_target_fence", "shop_id", "sku", unique=True,
                           postgresql_where="status IN ('sending','uncertain')"),)
