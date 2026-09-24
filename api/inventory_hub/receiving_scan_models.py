"""Committed scan receipts: a new physical scan has a new client operation UUID."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from inventory_hub.database import Base


class ReceivingScanRequest(Base):
    __tablename__ = "receiving_scan_requests"

    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    session_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("receiving_sessions.id", ondelete="RESTRICT"), nullable=False)
    scan_event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("scan_events.id", ondelete="RESTRICT"), nullable=False, unique=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
