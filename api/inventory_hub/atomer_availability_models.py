"""Durable local export previews. These records never authorize shop writes."""

from datetime import datetime
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from inventory_hub.database import Base


class AtomerAvailabilitySettings(Base):
    __tablename__ = "atomer_availability_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    warehouse_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    admin_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(100))
    last_request_id: Mapped[str | None] = mapped_column(String(36))
    last_request_hash: Mapped[str | None] = mapped_column(String(64))


class AtomerAvailabilityRun(Base):
    __tablename__ = "atomer_availability_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    warehouse_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    receiving_batch_id: Mapped[str | None] = mapped_column(String(36))
    product_ids: Mapped[list | None] = mapped_column(JSONB(none_as_null=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    source_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_sha256: Mapped[str | None] = mapped_column(String(64))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(100))
    document: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    export_hash: Mapped[str | None] = mapped_column(String(64))
    csv_content: Mapped[bytes | None] = mapped_column(LargeBinary)
    csv_sha256: Mapped[str | None] = mapped_column(String(64))


class AtomerAvailabilityAdminSource(Base):
    __tablename__ = "atomer_availability_admin_sources"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
