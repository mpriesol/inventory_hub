from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from inventory_hub.database import Base


class AiRuleVersion(Base):
    __tablename__ = "ai_rule_versions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    book: Mapped[dict] = mapped_column(JSONB)
    note: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(Text, default="operator")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AiRuleState(Base):
    __tablename__ = "ai_rule_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    published_id: Mapped[int] = mapped_column(ForeignKey("ai_rule_versions.id"))


class AiBatch(Base):
    __tablename__ = "ai_content_batches"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AiJob(Base):
    __tablename__ = "ai_content_jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("ai_content_batches.id"))
    kind: Mapped[str] = mapped_column(Text, default="product")
    status: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    context: Mapped[dict] = mapped_column(JSONB)
    output: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    checks: Mapped[dict] = mapped_column(JSONB, default=dict)
    events: Mapped[list] = mapped_column(JSONB, default=list)
    usage: Mapped[dict] = mapped_column(JSONB, default=dict)
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=0)
    actual_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True)
    preview_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AiContentRevision(Base):
    __tablename__ = "ai_content_revisions"
    job_id: Mapped[str] = mapped_column(ForeignKey("ai_content_jobs.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    content: Mapped[dict] = mapped_column(JSONB)
    decision: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
