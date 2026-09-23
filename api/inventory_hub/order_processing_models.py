"""Durable local stock processing, separate from read-only order discovery."""
from datetime import datetime
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from inventory_hub.database import Base


class OrderProcessingJob(Base):
    __tablename__ = "order_processing_jobs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shops.id", ondelete="RESTRICT"), nullable=False)
    inbox_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("order_inbox.id", ondelete="RESTRICT"), nullable=False)
    source_uuid: Mapped[str] = mapped_column(String(36), nullable=False)
    order_number: Mapped[str] = mapped_column(String(100), nullable=False)
    observation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_hash: Mapped[str | None] = mapped_column(String(64))
    configuration_hash: Mapped[str | None] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(String(80))
    result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    audit_preview_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("order_stock_previews.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    __table_args__ = (
        UniqueConstraint("shop_id", "source_uuid", name="uq_order_processing_jobs_source"),
        CheckConstraint("status IN ('pending','running','completed','retry','review')", name="chk_order_processing_status"),
        CheckConstraint("generation > 0 AND attempts >= 0", name="chk_order_processing_counters"),
        Index("ix_order_processing_jobs_due", "status", "next_attempt_at"),
        Index("ix_order_processing_jobs_shop", "shop_id", "next_attempt_at"),
    )
