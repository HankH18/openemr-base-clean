"""guideline RAG corpus — guideline_document + guideline_chunk (+ pgvector)

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-13

Week 2 hybrid-RAG corpus (W2_ARCHITECTURE.md §"Data model" / §RAG). Dense vectors
use pgvector on Postgres; SQLite (tests) falls back to a JSON list — value
semantics (a list[float]) are identical. Sparse retrieval is computed at query
time via Postgres full-text; a GIN index on ``to_tsvector('english', content)``
is created Postgres-only. Additive; the corpus is reproducible from the repo
ingest script, so this schema holds no irreplaceable data.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMBEDDING_DIM = 1024  # Voyage voyage-3.5 default dimension.


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    if is_pg:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "guideline_document",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("source", sa.String(length=512), nullable=True),
        sa.Column("license", sa.String(length=128), nullable=True),
        sa.Column("ingested_at", sa.DateTime(), nullable=False),
    )

    # Vector(dim) on Postgres; JSON list on SQLite — mirrors memory.db.embedding_column.
    embedding_type: sa.types.TypeEngine[object] = (
        Vector(_EMBEDDING_DIM) if is_pg else sa.JSON()
    )
    op.create_table(
        "guideline_chunk",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("guideline_document_id", sa.BigInteger(), nullable=False),
        sa.Column("section", sa.String(length=255), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("embedding", embedding_type, nullable=True),
        sa.ForeignKeyConstraint(
            ["guideline_document_id"], ["guideline_document.id"], ondelete="CASCADE"
        ),
    )
    op.create_index("ix_guideline_chunk_document_id", "guideline_chunk", ["guideline_document_id"])
    if is_pg:
        op.execute(
            "CREATE INDEX ix_guideline_chunk_fts ON guideline_chunk "
            "USING gin (to_tsvector('english', content))"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_guideline_chunk_fts")
    op.drop_index("ix_guideline_chunk_document_id", table_name="guideline_chunk")
    op.drop_table("guideline_chunk")
    op.drop_table("guideline_document")
