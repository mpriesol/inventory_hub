"""Explicit stock policy and durable operator previews for individual orders."""
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from inventory_hub.database import Base


class OrderStockPolicy(Base):
    __tablename__ = "order_stock_policies"

    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shops.id", ondelete="RESTRICT"), primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status_actions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    statuses: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("revision > 0", name="chk_order_stock_policy_revision"),
    )


class OrderStockPreview(Base):
    __tablename__ = "order_stock_previews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shops.id", ondelete="RESTRICT"), nullable=False)
    order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shop_orders.id", ondelete="RESTRICT"), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="prepared")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Prepared previews have no result yet; the database constraint expects SQL NULL.
    result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))

    __table_args__ = (
        CheckConstraint("status IN ('prepared', 'completed')", name="chk_order_stock_preview_status"),
        CheckConstraint("(status = 'prepared' AND result IS NULL AND completed_at IS NULL) OR "
                        "(status = 'completed' AND result IS NOT NULL AND completed_at IS NOT NULL)",
                        name="chk_order_stock_preview_result"),
    )
