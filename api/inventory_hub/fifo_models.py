"""FIFO quantity layers with immutable issue-cost snapshots and explicit cutovers."""
from datetime import datetime
from decimal import Decimal
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from inventory_hub.database import Base


class FifoCutover(Base):
    __tablename__ = "fifo_cutovers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="prepared", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))


class FifoState(Base):
    __tablename__ = "fifo_states"
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    activation_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    cutover_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("fifo_cutovers.id", ondelete="RESTRICT"))


class FifoLayer(Base):
    __tablename__ = "fifo_layers"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    receipt_movement_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("stock_movements.id", ondelete="RESTRICT"))
    cutover_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("fifo_cutovers.id", ondelete="RESTRICT"))
    physical_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quantity_original: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    quantity_remaining: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    cost_status: Mapped[str] = mapped_column(String(16), nullable=False)
    cost_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stock_status: Mapped[str] = mapped_column(String(16), default="available", nullable=False)
    root_cost_layer_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("fifo_layers.id", ondelete="RESTRICT"))
    provenance: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class FifoAllocation(Base):
    __tablename__ = "fifo_allocations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    issue_movement_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stock_movements.id", ondelete="RESTRICT"), nullable=False)
    layer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("fifo_layers.id", ondelete="RESTRICT"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    quantity_before: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    returned_quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0, nullable=False)
    unit_cost_at_issue: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    total_cost_at_issue: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    cost_status_at_issue: Mapped[str] = mapped_column(String(16), nullable=False)
    unit_cost_current: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    total_cost_current: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    cost_status_current: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint("issue_movement_id", "sequence", name="uq_fifo_allocation_sequence"),)


class FifoReceipt(Base):
    __tablename__ = "fifo_receipts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="prepared", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
