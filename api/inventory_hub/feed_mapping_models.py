from datetime import datetime
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from inventory_hub.database import Base


class ProductFeedMapping(Base):
    __tablename__ = "product_feed_mappings"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    feed_key: Mapped[str] = mapped_column(String(50), nullable=False)
    shop_code: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("supplier_id", "feed_key", "shop_code", name="uq_product_feed_mapping_scope"),)


class ProductFeedMappingRevision(Base):
    __tablename__ = "product_feed_mapping_revisions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mapping_id: Mapped[int] = mapped_column(ForeignKey("product_feed_mappings.id"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("mapping_id", "revision", name="uq_product_feed_mapping_revision"),)
