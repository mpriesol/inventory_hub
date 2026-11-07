"""Audited one-hop identity redirects; original cards and history stay intact."""
from datetime import datetime
from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from inventory_hub.database import Base


class ProductConsolidation(Base):
    __tablename__ = 'product_consolidations'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    document: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default='prepared')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))


class ProductCanonicalAlias(Base):
    __tablename__ = 'product_canonical_aliases'
    retired_product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('products.id', ondelete='RESTRICT'), primary_key=True)
    canonical_product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('products.id', ondelete='RESTRICT'), nullable=False)
    consolidation_id: Mapped[str] = mapped_column(String(36), ForeignKey('product_consolidations.id', ondelete='RESTRICT'), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
