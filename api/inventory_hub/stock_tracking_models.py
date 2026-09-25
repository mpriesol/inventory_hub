"""One immutable start of current inventory per product and warehouse."""
from datetime import datetime
from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from inventory_hub.database import Base


class StockTracking(Base):
    __tablename__ = "stock_tracking"
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    source_type: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(100))
    operator_name: Mapped[str] = mapped_column(String(100))
    historical_last_movement_id: Mapped[int] = mapped_column(BigInteger, default=0)
    historical_last_layer_id: Mapped[int] = mapped_column(BigInteger, default=0)
    archived_balance: Mapped[dict] = mapped_column(JSONB)
