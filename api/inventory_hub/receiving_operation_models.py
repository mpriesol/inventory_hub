"""Audited receipt reversals and reversible archival, without deleting history."""
from datetime import datetime
from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from inventory_hub.database import Base


class ReceivingOperation(Base):
    __tablename__ = "receiving_operations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("receiving_sessions.id", ondelete="RESTRICT"))
    action: Mapped[str] = mapped_column(String(16))
    request_hash: Mapped[str] = mapped_column(String(64))
    preview_hash: Mapped[str] = mapped_column(String(64))
    preview_data: Mapped[dict] = mapped_column(JSONB)
    actor: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16), default="prepared")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
