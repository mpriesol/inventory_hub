"""Immutable operator return, release and acquisition-cost correction audits."""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from inventory_hub.database import Base


class FifoReturn(Base):
    __tablename__ = "fifo_returns"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    issue_movement_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stock_movements.id", ondelete="RESTRICT"), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    case_reference: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    condition: Mapped[str] = mapped_column(String(20), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class FifoReturnLine(Base):
    __tablename__ = "fifo_return_lines"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    return_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("fifo_returns.id", ondelete="RESTRICT"), nullable=False)
    allocation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("fifo_allocations.id", ondelete="RESTRICT"), nullable=False)
    layer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("fifo_layers.id", ondelete="RESTRICT"), nullable=False)
    movement_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stock_movements.id", ondelete="RESTRICT"), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    unit_cost_at_return: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    cost_status_at_return: Mapped[str] = mapped_column(String(16), nullable=False)
    __table_args__ = (UniqueConstraint("return_id", "allocation_id", name="uq_fifo_return_allocation"),)


class FifoRelease(Base):
    __tablename__ = "fifo_releases"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_layer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("fifo_layers.id", ondelete="RESTRICT"), nullable=False)
    target_warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class FifoCostRevision(Base):
    __tablename__ = "fifo_cost_revisions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    root_layer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("fifo_layers.id", ondelete="RESTRICT"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    previous_status: Mapped[str] = mapped_column(String(20), nullable=False)
    new_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    new_status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    document_reference: Mapped[str] = mapped_column(String(200), nullable=False)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    impact: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint("root_layer_id", "revision", name="uq_fifo_cost_revision"),)
