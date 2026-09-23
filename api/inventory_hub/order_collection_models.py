"""Durable, read-only order discovery; deliberately separate from the stock ledger."""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column

from inventory_hub.database import Base


class OrderCollectionSettings(Base):
    __tablename__ = "order_collection_settings"
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shops.id", ondelete="RESTRICT"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    target_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    cursor_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reconcile_cursor_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reconcile_until_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_poll_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retry_after_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(80))
    entries_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_mode: Mapped[str | None] = mapped_column(String(16))


class OrderCollectionRun(Base):
    __tablename__ = "order_collection_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shops.id", ondelete="RESTRICT"), nullable=False)
    settings_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    from_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    until_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(String(80))
    __table_args__ = (Index("ix_order_collection_runs_shop", "shop_id", "started_at"),)


class OrderInboxEntry(Base):
    __tablename__ = "order_inbox"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shops.id", ondelete="RESTRICT"), nullable=False)
    source_uuid: Mapped[str] = mapped_column(String(36), nullable=False)
    order_number: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    origin: Mapped[str] = mapped_column(String(40), nullable=False)
    status_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    review_reason: Mapped[str | None] = mapped_column(String(80))
    __table_args__ = (
        UniqueConstraint("shop_id", "source_uuid", name="uq_order_inbox_uuid"),
        UniqueConstraint("shop_id", "order_number", name="uq_order_inbox_number"),
        Index("ix_order_inbox_shop_updated", "shop_id", "updated_at"),
    )
