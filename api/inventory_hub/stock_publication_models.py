"""Explicit maintenance publication; ambiguity keeps the warehouse hold active."""
from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from inventory_hub.database import Base


class StockPublicationPolicy(Base):
    __tablename__ = "stock_publication_policies"
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shops.id", ondelete="RESTRICT"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    target_fingerprint: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class StockPublicationHold(Base):
    __tablename__ = "stock_publication_holds"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shops.id", ondelete="RESTRICT"), nullable=False)
    warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    assertions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    __table_args__ = (Index("uq_stock_publication_active_hold", "warehouse_id", unique=True, postgresql_where="active = true"),)


class StockPublicationBatch(Base):
    __tablename__ = "stock_publication_batches"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shops.id", ondelete="RESTRICT"), nullable=False)
    hold_id: Mapped[str] = mapped_column(String(36), ForeignKey("stock_publication_holds.id", ondelete="RESTRICT"), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="prepared", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(80))
    result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    __table_args__ = (Index("ix_stock_publication_batches_shop", "shop_id", "created_at"),)


class StockPublicationItem(Base):
    __tablename__ = "stock_publication_items"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(36), ForeignKey("stock_publication_batches.id", ondelete="RESTRICT"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    sku: Mapped[str] = mapped_column(String(100), nullable=False)
    target: Mapped[dict] = mapped_column(JSONB, nullable=False)
    quantity: Mapped[str] = mapped_column(String(32), nullable=False)
    before_quantity: Mapped[str | None] = mapped_column(String(32))
    after_quantity: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="prepared", nullable=False)
    attempt_id: Mapped[str | None] = mapped_column(String(36))
    attempt_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(80))
    observation: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    acknowledgement: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    resolution: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    __table_args__ = (UniqueConstraint("batch_id", "position", name="uq_stock_publication_item_position"),
                      UniqueConstraint("batch_id", "sku", name="uq_stock_publication_item_sku"))
