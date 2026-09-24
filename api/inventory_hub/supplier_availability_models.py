"""Supplier observations are separate from both our stock and listing snapshots."""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from inventory_hub.database import Base


class SupplierAvailabilitySettings(Base):
    __tablename__ = "supplier_availability_settings"
    supplier_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("suppliers.id", ondelete="RESTRICT"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    feed_key: Mapped[str] = mapped_column(String(50), nullable=False)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)
    freshness_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=21600)
    min_coverage_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    manual_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(100))
    last_item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    running_run_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("supplier_feed_runs.id", ondelete="RESTRICT"))


class SupplierAvailabilityObservation(Base):
    __tablename__ = "supplier_availability_observations"
    supplier_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("suppliers.id", ondelete="RESTRICT"), primary_key=True)
    supplier_sku: Mapped[str] = mapped_column(String(100), primary_key=True)
    feed_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("supplier_feeds.id", ondelete="RESTRICT"), nullable=False)
    run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("supplier_feed_runs.id", ondelete="RESTRICT"), nullable=False)
    available: Mapped[bool | None] = mapped_column(Boolean)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    quantity_kind: Mapped[str] = mapped_column(String(12), nullable=False)
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
