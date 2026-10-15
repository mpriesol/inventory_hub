"""Durable supplier-card decisions and atomic results; no stock authority."""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from inventory_hub.database import Base


class SupplierCardLinkPreview(Base):
    __tablename__ = 'supplier_card_link_previews'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('products.id', ondelete='RESTRICT'), nullable=False)
    supplier_product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('supplier_products.id', ondelete='RESTRICT'), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    document: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default='prepared', nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
