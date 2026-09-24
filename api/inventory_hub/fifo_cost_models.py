"""Durable, opt-in publication of FIFO acquisition costs, separate from stock."""
from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from inventory_hub.database import Base


class FifoCostWarehouseSettings(Base):
    __tablename__ = "fifo_cost_warehouse_settings"
    warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False, default=20)


class FifoCostSettings(Base):
    __tablename__ = "fifo_cost_settings"
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shops.id", ondelete="RESTRICT"), primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    product_cost_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    order_cost_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    interval_seconds: Mapped[int | None] = mapped_column(Integer)
    batch_size: Mapped[int | None] = mapped_column(Integer)
    target_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    orders_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_after_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_batch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(100))
    scan_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    scan_manual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    product_cursor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    product_max: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    order_cursor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    order_max: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class FifoCostPublication(Base):
    __tablename__ = "fifo_cost_publications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shops.id", ondelete="RESTRICT"), nullable=False)
    warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target_key: Mapped[str] = mapped_column(String(100), nullable=False)
    subject: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    automatic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    settings_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    target_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[dict] = mapped_column(JSONB, nullable=False)
    target: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    document: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    before: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    after: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    error: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    __table_args__ = (
        Index("uq_fifo_cost_target_fence", "shop_id", "target_key", unique=True,
              postgresql_where="status IN ('queued','sending','uncertain')"),
        Index("idx_fifo_cost_history", "shop_id", "created_at"),
        Index("idx_fifo_cost_cache", "shop_id", "target_key", "source_hash"),
    )
