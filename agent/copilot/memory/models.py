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
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from copilot.memory.db import EMBEDDING_DIM, Base, JSONType, embedding_column

# BigInteger PK on SQLite doesn't autoincrement (only INTEGER PRIMARY KEY does);
# BigInteger works fine on Postgres. Use this variant for autoinc surrogate ids.
AutoIncBigInt = BigInteger().with_variant(Integer(), "sqlite")


def _utc_default() -> datetime:
    """Server-agnostic UTC default (naive-in-DB but always UTC in Python)."""
    from datetime import UTC
    from datetime import datetime as _dt

    return _dt.now(UTC).replace(tzinfo=None)


def _utc_aware_default() -> datetime:
    """Timezone-aware UTC default for the auth/session tables.

    The auth tables use ``DateTime(timezone=True)`` so they round-trip aware on
    Postgres. SQLite has no tz support and returns naive values, so readers must
    re-attach UTC (see ``copilot.auth.session.ensure_utc``) before comparing.
    """
    from datetime import UTC
    from datetime import datetime as _dt

    return _dt.now(UTC)


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
    # Write-back provenance: the serialized ``WriteSource`` (source_document_id +
    # extracted_fact_id + enough to rebuild the DocumentCitation) a derived write
    # descends from. Distinct from ``resources_returned``, which names the FHIR
    # resources an action RETURNED/created — a source document is an agent-store
    # input, and naming it there would misreport the PHI access trail (the same
    # rule chat/service.py follows). NULL for reads and physician-direct writes,
    # which honestly have no source document.
    source_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utc_default, server_default=text("CURRENT_TIMESTAMP")
    )

    # Mirrors migration 0003: the retention sweep (§164.312(b)) range-scans by
    # timestamp, so ``at`` needs an index. Declaring it here keeps the SQLite
    # ``create_all`` schema (used by every functional test) in step with the
    # migration-built Postgres schema — without it the index is a test blind spot.
    __table_args__ = (Index("ix_audit_log_at", "at"),)


# --- Per-physician SMART login (auth_mode="smart"; inert while "disabled") ----
#
# See agent/research/PRODUCTION_GRADE_PLAN.md §1. These three tables back the
# opaque server-side session that replaces the hardcoded demo clinician. They
# are additive and unused while auth_mode="disabled", so the no-login demo is
# byte-for-byte unchanged. Datetimes are timezone-aware (DateTime(timezone=True))
# and the OAuth token material is stored ONLY as Fernet ciphertext (LargeBinary).


class ClinicianRow(Base):
    """Stable integer surrogate for an OpenEMR ``fhirUser`` (Practitioner).

    Mints the ``ClinicianId.value`` the int-keyed tables (``rounding_cursor``,
    ``audit_log``, ``last_seen``, ``conversation``) already use, so none of them
    change. Auto-provisioned on first SMART login and reused thereafter.
    """

    __tablename__ = "clinician"
    # Name the unique constraint to match migration 0004 (``uq_clinician_fhir_user``)
    # instead of leaving it anonymous, so --autogenerate doesn't see the named
    # migration constraint and the anonymous model one as a drift to reconcile.
    # Semantics are identical: still a single-column UNIQUE on ``fhir_user`` that
    # raises IntegrityError on a duplicate (the R1 auth-race fix keys on that error,
    # not on the constraint name).
    __table_args__ = (UniqueConstraint("fhir_user", name="uq_clinician_fhir_user"),)

    id: Mapped[int] = mapped_column(AutoIncBigInt, primary_key=True, autoincrement=True)
    fhir_user: Mapped[str] = mapped_column(String(512), nullable=False)
    openemr_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    npi: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_aware_default
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PhysicianSessionRow(Base):
    """One logged-in physician session — the crown-jewel token holder.

    ``session_id`` is ``sha256(cookie_value)`` (never the plaintext cookie, so a
    DB leak yields no live cookies). The access/refresh tokens live only as
    Fernet ciphertext. Idle + absolute expiry implement automatic logoff
    (§164.312(a)(2)(iii)).
    """

    __tablename__ = "physician_session"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    clinician_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("clinician.id", ondelete="CASCADE"), nullable=False, index=True
    )
    access_token_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    refresh_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scope: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    fhir_user: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_aware_default
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_aware_default
    )
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class LoginTxnRow(Base):
    """Short-lived server-side login transaction (OAuth ``state`` + PKCE verifier).

    Persisted at ``begin_login`` and consumed (deleted) at the callback. Keyed on
    the opaque ``state`` that round-trips through OpenEMR, binding the callback to
    the request that started it; ``code_verifier`` proves PKCE possession.
    """

    __tablename__ = "login_txn"

    state: Mapped[str] = mapped_column(String(128), primary_key=True)
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    redirect_target: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_aware_default
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# --- Week 2: multimodal document ingestion (W2_ARCHITECTURE.md §"Data model") ---
#
# Authority split: OpenEMR owns the *source document* (uploaded via the Standard
# REST API, addressable by ``openemr_document_id`` / readable back as a FHIR
# DocumentReference). The agent DB owns the *derived* artifacts below — page
# renders + OCR tokens, and schema-validated extracted facts with pixel-level
# provenance. Extractions are APPEND-ONLY: re-ingesting a document inserts a new
# ``extraction`` row, never mutates an old one (grounding re-checks a stored,
# immutable extraction). Naive-UTC datetimes via ``_utc_default`` like the other
# agent-state tables.


class SourceDocumentRow(Base):
    """Agent-side handle for a document whose bytes live authoritatively in OpenEMR."""

    __tablename__ = "source_document"

    id: Mapped[int] = mapped_column(AutoIncBigInt, primary_key=True, autoincrement=True)
    patient_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    # Set after the OpenEMR upload succeeds; NULL while status='uploaded' in-flight.
    openemr_document_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    doc_type: Mapped[str] = mapped_column(String(32), nullable=False)  # 'lab_pdf' | 'intake_form'
    category_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 'uploaded' | 'extracting' | 'extracted' | 'failed'
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="uploaded")
    uploaded_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_default)

    pages: Mapped[list[DocumentPageRow]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    extractions: Mapped[list[ExtractionRow]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentPageRow(Base):
    """One rasterized page + its OCR word boxes (re-derivable cache for the overlay)."""

    __tablename__ = "document_page"
    __table_args__ = (
        UniqueConstraint("source_document_id", "page_no", name="uq_document_page_doc_pageno"),
    )

    id: Mapped[int] = mapped_column(AutoIncBigInt, primary_key=True, autoincrement=True)
    source_document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("source_document.id", ondelete="CASCADE"), nullable=False, index=True
    )
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    image: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)  # PNG cache
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # [{"text": str, "bbox": [x, y, w, h], "conf": float}] — word-level OCR tokens.
    ocr_tokens: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, nullable=False, default=list)

    document: Mapped[SourceDocumentRow] = relationship(back_populates="pages")


class ExtractionRow(Base):
    """One append-only extraction run over a source document."""

    __tablename__ = "extraction"

    id: Mapped[int] = mapped_column(AutoIncBigInt, primary_key=True, autoincrement=True)
    source_document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("source_document.id", ondelete="CASCADE"), nullable=False, index=True
    )
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)  # VLM model id
    confidence_overall: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ok")  # ok|partial|failed
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_default)

    document: Mapped[SourceDocumentRow] = relationship(back_populates="extractions")
    facts: Mapped[list[ExtractedFactRow]] = relationship(
        back_populates="extraction", cascade="all, delete-orphan"
    )


class ExtractedFactRow(Base):
    """One schema-validated field with its reconciled page/bbox provenance.

    ``supported`` is the "no-invention" gate: True only when the extracted value
    was located in the page's OCR tokens (``bbox`` + ``match_confidence`` set).
    An unsupported fact is surfaced as such and cannot pass document grounding.
    """

    __tablename__ = "extracted_fact"

    id: Mapped[int] = mapped_column(AutoIncBigInt, primary_key=True, autoincrement=True)
    extraction_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("extraction.id", ondelete="CASCADE"), nullable=False, index=True
    )
    field_path: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[str | None] = mapped_column(String, nullable=True)
    # unit / reference_range / abnormal_flag hold free text the VLM extracts
    # verbatim (a lab flag, but also a medication dose/frequency or an intake
    # note), so they are unbounded like ``value``. A small varchar cap here only
    # 500'd the whole ingestion on a longer-than-expected extraction — see
    # migration 0010.
    unit: Mapped[str | None] = mapped_column(String, nullable=True)
    reference_range: Mapped[str | None] = mapped_column(String, nullable=True)
    abnormal_flag: Mapped[str | None] = mapped_column(String, nullable=True)
    collection_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    page_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bbox: Mapped[list[float] | None] = mapped_column(JSONType, nullable=True)  # normalized [x,y,w,h]
    match_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    supported: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # OpenEMR record type for an intake-form fact (IntakeCategory value); NULL for
    # lab facts. Lets intake facts map 1:1 to their OpenEMR home (lists / patient_data
    # / form_encounter.reason / history_data).
    category: Mapped[str | None] = mapped_column(String(32), nullable=True)

    extraction: Mapped[ExtractionRow] = relationship(back_populates="facts")


# --- Week 2: hybrid-RAG guideline corpus (W2_ARCHITECTURE.md §RAG) ---------------
#
# Owned by the agent DB and reproducible from the repo ingest script (no PHI —
# public clinical guidelines). Dense retrieval uses ``embedding`` (pgvector on
# Postgres, JSON on SQLite); sparse retrieval is Postgres full-text over
# ``content`` at query time (GIN index added in migration 0006, Postgres-only).


class GuidelineDocumentRow(Base):
    """One source guideline document in the local corpus."""

    __tablename__ = "guideline_document"

    id: Mapped[int] = mapped_column(AutoIncBigInt, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    source: Mapped[str | None] = mapped_column(String(512), nullable=True)
    license: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # sha256 of the material this document was ingested FROM (title + license +
    # every chunk's section/content) — see copilot.rag.ingest.CorpusDocument.
    # ``source`` alone cannot answer "is the stored copy still what the file says?",
    # so an ingest keyed on it alone re-registers a corrected guideline as
    # "skipped (already ingested)" and keeps serving the superseded text. The
    # serve-time verifier re-reads the same stale row, so the stale quote matches
    # itself verbatim and passes the grounding gate — the staleness is
    # self-consistent and structurally invisible to verification. This column is
    # what makes a changed file detectable.
    #
    # NULL = unknown, not "no content": rows written before migration 0009 have no
    # recorded hash, so their freshness cannot be established. Ingest treats NULL
    # as stale and rebuilds once (see ingest_corpus) rather than trusting them.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utc_default)

    chunks: Mapped[list[GuidelineChunkRow]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class GuidelineChunkRow(Base):
    """A retrievable chunk: text + its dense embedding + section metadata."""

    __tablename__ = "guideline_chunk"
    # Name the FK index to match migration 0006 (``ix_guideline_chunk_document_id``)
    # rather than SQLAlchemy's auto name (``ix_guideline_chunk_guideline_document_id``),
    # so a future --autogenerate diff doesn't treat the two as drift. Same column,
    # same index — name only.
    __table_args__ = (Index("ix_guideline_chunk_document_id", "guideline_document_id"),)

    id: Mapped[int] = mapped_column(AutoIncBigInt, primary_key=True, autoincrement=True)
    guideline_document_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("guideline_document.id", ondelete="CASCADE"),
        nullable=False,
    )
    section: Mapped[str | None] = mapped_column(String(255), nullable=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content: Mapped[str] = mapped_column(String, nullable=False)
    # Vector(EMBEDDING_DIM) on Postgres; JSON list on SQLite (tests) — see memory.db.
    embedding: Mapped[list[float] | None] = mapped_column(
        embedding_column(EMBEDDING_DIM), nullable=True
    )

    document: Mapped[GuidelineDocumentRow] = relationship(back_populates="chunks")
