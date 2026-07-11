"""SQLAlchemy 2 models — the agent's own state.

Table set mirrors `ARCHITECTURE.md` §"Data model":

- ``memory_file``     one row per patient; regenerable from OpenEMR.
- ``sync_state``      one row per patient; hot, written every poller tick.
- ``last_seen``       one row per (clinician, patient); set by the "done" signal.
- ``rounding_cursor`` one row per clinician; survives refresh/crash.
- ``conversation`` / ``message``  chat history (PHI, retention-bound).
- ``audit_log``       append-only HIPAA access trail.

PHI-column encryption at rest lives at the DB layer (Postgres TDE / disk
encryption) — the app is not the place to invent that.  Retention TTLs are
enforced by a background sweep (deferred to a later unit).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from copilot.memory.db import Base, JSONType

# BigInteger PK on SQLite doesn't autoincrement (only INTEGER PRIMARY KEY does);
# BigInteger works fine on Postgres. Use this variant for autoinc surrogate ids.
AutoIncBigInt = BigInteger().with_variant(Integer(), "sqlite")


def _utc_default() -> datetime:
    """Server-agnostic UTC default (naive-in-DB but always UTC in Python)."""
    from datetime import UTC
    from datetime import datetime as _dt

    return _dt.now(UTC).replace(tzinfo=None)


class MemoryFileRow(Base):
    """Per-patient synthesized summary — the central grounded artifact."""

    __tablename__ = "memory_file"

    patient_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # JSONB in prod, JSON in SQLite — see JSONType in memory.db.
    summary: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)

    acuity_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    rank_reason: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    synthesized_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_default)
    source_watermark: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_memory_file_stale", "stale"),
        Index("ix_memory_file_synthesized_at", "synthesized_at"),
    )


class SyncStateRow(Base):
    """Per-patient poller bookkeeping — one row per known patient."""

    __tablename__ = "sync_state"

    patient_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    watermark: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (Index("ix_sync_state_polled", "last_polled_at"),)


class LastSeenRow(Base):
    """When each clinician marked each patient as "done" this round."""

    __tablename__ = "last_seen"
    __table_args__ = (UniqueConstraint("clinician_id", "patient_id", name="uq_last_seen_cln_pt"),)

    id: Mapped[int] = mapped_column(AutoIncBigInt, primary_key=True, autoincrement=True)
    clinician_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    patient_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_default)


class RoundingCursorRow(Base):
    """Persistent per-clinician round position — survives refresh/crash."""

    __tablename__ = "rounding_cursor"

    clinician_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ordered_patient_ids: Mapped[list[int]] = mapped_column(JSONType, nullable=False, default=list)
    current_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_ids: Mapped[list[int]] = mapped_column(JSONType, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_default)


class ConversationRow(Base):
    """One chat session, patient-scoped."""

    __tablename__ = "conversation"

    id: Mapped[int] = mapped_column(AutoIncBigInt, primary_key=True, autoincrement=True)
    clinician_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    patient_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_default)

    messages: Mapped[list[MessageRow]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class MessageRow(Base):
    """One turn inside a conversation."""

    __tablename__ = "message"

    id: Mapped[int] = mapped_column(AutoIncBigInt, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conversation.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # 'user'|'assistant'|'tool'
    content: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_default)

    conversation: Mapped[ConversationRow] = relationship(back_populates="messages")


class AuditLogRow(Base):
    """Append-only access trail — every read of PHI produces one row."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(AutoIncBigInt, primary_key=True, autoincrement=True)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    clinician_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    patient_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resources_returned: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    # Physician-attribution surface for write-back: 'human_direct' (Phase 1) or
    # 'agent_proposed_physician_confirmed' (Phase 2). Nullable — reads and all
    # pre-write-back rows leave it NULL, so the column is fully backward-compatible.
    entry_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utc_default, server_default=text("CURRENT_TIMESTAMP")
    )
