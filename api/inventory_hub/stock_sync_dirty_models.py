"""Per-target wakeups, committed atomically with stock/native changes."""
from datetime import datetime
from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column
from inventory_hub.database import Base


class StockSyncDirty(Base):
    __tablename__ = 'stock_sync_dirty'
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('shops.id', ondelete='RESTRICT'), primary_key=True)
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('products.id', ondelete='RESTRICT'), primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('warehouses.id', ondelete='RESTRICT'), primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default='1')
    acknowledged_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default='0')
    dispatched_generation: Mapped[int | None] = mapped_column(BigInteger)
    run_id: Mapped[str | None] = mapped_column(String(36), ForeignKey('stock_sync_runs.id', ondelete='RESTRICT'))
    dispatched_sku: Mapped[str | None] = mapped_column(String(100))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_error: Mapped[str | None] = mapped_column(String(100))
