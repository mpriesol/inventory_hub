"""Per-target supplier label delivery; never an own-stock balance."""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from inventory_hub.database import Base


class SupplierAvailabilityDelivery(Base):
    __tablename__ = "supplier_availability_deliveries"
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True)
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shops.id", ondelete="RESTRICT"), primary_key=True)
    desired: Mapped[dict] = mapped_column(JSONB, nullable=False)
    confirmed: Mapped[dict | None] = mapped_column(JSONB)
    confirmed_label: Mapped[str | None] = mapped_column(String(100))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publication_id: Mapped[str] = mapped_column(String(36), ForeignKey("product_editor_publications.id", ondelete="RESTRICT"), nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
