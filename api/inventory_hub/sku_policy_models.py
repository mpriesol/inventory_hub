"""Registered namespaces and immutable provenance of reviewed SKU identities."""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, Text, false, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from inventory_hub.database import Base


class SkuBrand(Base):
    __tablename__ = "sku_brands"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(2), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    suppliers: Mapped[list[dict[str, str]]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SkuBrandReservedCode(Base):
    __tablename__ = "sku_brand_reserved_codes"
    code: Mapped[str] = mapped_column(String(2), primary_key=True)
    brand_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sku_brands.id", ondelete="RESTRICT"), nullable=False)


class SkuBrandAlias(Base):
    __tablename__ = "sku_brand_aliases"
    alias_key: Mapped[str] = mapped_column(String(300), primary_key=True)
    brand_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sku_brands.id", ondelete="RESTRICT"), nullable=False)
    alias: Mapped[str] = mapped_column(String(100), nullable=False)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ProductSkuIdentity(Base):
    __tablename__ = "product_sku_identities"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    brand_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sku_brands.id", ondelete="RESTRICT"), nullable=False)
    source_code: Mapped[str] = mapped_column(String(97), nullable=False)
    source_code_fold: Mapped[str] = mapped_column(String(300), nullable=False)
    code_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    supplier_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("suppliers.id", ondelete="RESTRICT"))
    identity_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    brand_raw: Mapped[str] = mapped_column(String(100), nullable=False)
    allocated_sku: Mapped[str] = mapped_column(String(100), nullable=False)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
