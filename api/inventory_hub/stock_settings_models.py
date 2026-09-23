"""Warehouse defaults and explicitly authorized per-shop stock automation."""
from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from inventory_hub.database import Base


class StockWarehouseSettings(Base):
    __tablename__ = "stock_warehouse_settings"
    warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    values: Mapped[dict] = mapped_column(JSONB, nullable=False)
    processing_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StockShopSettings(Base):
    __tablename__ = "stock_shop_settings"
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shops.id", ondelete="RESTRICT"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    overrides: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    automation_starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issue_starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    authorized_policy_revision: Mapped[int | None] = mapped_column(Integer)
    target_fingerprint: Mapped[str | None] = mapped_column(String(64))
    processing_retry_after_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
