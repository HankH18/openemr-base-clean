"""baseline — agent-owned state tables

Revision ID: 0001
Revises:
Create Date: 2026-07-08

Creates the seven tables ARCHITECTURE.md §"Data model" calls for. Schema
only, no data.  Uses ``sa.JSON`` (portable) — Postgres promotes to JSONB
via the JSONType decorator at runtime.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_file",
        sa.Column("patient_id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("acuity_score", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("rank_reason", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("synthesized_at", sa.DateTime(), nullable=False),
        sa.Column("source_watermark", sa.DateTime(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("stale", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_memory_file_stale", "memory_file", ["stale"])
    op.create_index("ix_memory_file_synthesized_at", "memory_file", ["synthesized_at"])

    op.create_table(
        "sync_state",
        sa.Column("patient_id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("last_polled_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("watermark", sa.DateTime(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_sync_state_polled", "sync_state", ["last_polled_at"])

    op.create_table(
        "last_seen",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("clinician_id", sa.BigInteger(), nullable=False),
        sa.Column("patient_id", sa.BigInteger(), nullable=False),
        sa.Column("seen_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("clinician_id", "patient_id", name="uq_last_seen_cln_pt"),
    )
    op.create_index("ix_last_seen_clinician_id", "last_seen", ["clinician_id"])
    op.create_index("ix_last_seen_patient_id", "last_seen", ["patient_id"])

    op.create_table(
        "rounding_cursor",
        sa.Column("clinician_id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("ordered_patient_ids", sa.JSON(), nullable=False),
        sa.Column("current_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_ids", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "conversation",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("clinician_id", sa.BigInteger(), nullable=False),
        sa.Column("patient_id", sa.BigInteger(), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_conversation_clinician_id", "conversation", ["clinician_id"])
    op.create_index("ix_conversation_patient_id", "conversation", ["patient_id"])
    op.create_index("ix_conversation_correlation_id", "conversation", ["correlation_id"])

    op.create_table(
        "message",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversation.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_message_conversation_id", "message", ["conversation_id"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("clinician_id", sa.BigInteger(), nullable=True),
        sa.Column("patient_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("resources_returned", sa.JSON(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_audit_log_correlation_id", "audit_log", ["correlation_id"])
    op.create_index("ix_audit_log_clinician_id", "audit_log", ["clinician_id"])
    op.create_index("ix_audit_log_patient_id", "audit_log", ["patient_id"])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("message")
    op.drop_table("conversation")
    op.drop_table("rounding_cursor")
    op.drop_table("last_seen")
    op.drop_table("sync_state")
    op.drop_table("memory_file")
