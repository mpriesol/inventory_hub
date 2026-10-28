"""Durable notification hints, separate from order and stock evidence."""
from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from inventory_hub.database import Base


class OrderWebhookSettings(Base):
    __tablename__ = 'order_webhook_settings'
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('shops.id', ondelete='RESTRICT'), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    target_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    public_base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    catchup_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)
    registration_status: Mapped[str] = mapped_column(String(16), nullable=False, default='pending')
    registered_events: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registration_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_registration_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    registration_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(80))


class OrderWebhookEvent(Base):
    __tablename__ = 'order_webhook_events'
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('shops.id', ondelete='RESTRICT'), primary_key=True)
    order_number: Mapped[str] = mapped_column(String(100), primary_key=True)
    event: Mapped[str] = mapped_column(String(10), nullable=False)
    notification_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    notification_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default='pending')
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(80))
    __table_args__ = (Index('ix_order_webhook_events_due', 'status', 'next_attempt_at'),)
