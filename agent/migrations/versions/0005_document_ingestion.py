"""document ingestion — source_document + document_page + extraction + extracted_fact

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-13

Week 2 multimodal ingestion data model (W2_ARCHITECTURE.md §"Data model"). The
agent DB owns these derived artifacts; OpenEMR owns the source-document bytes
(referenced by ``source_document.openemr_document_id`` / readable back as a FHIR
DocumentReference). Additive — nothing in the existing demo path reads these
tables. Portable across SQLite (tests) and Postgres (prod): ``sa.JSON`` promotes
to JSONB via the JSONType decorator at runtime, ``sa.LargeBinary`` maps to
BLOB/BYTEA. Extractions are append-only (re-ingest = new ``extraction`` row).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_document",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("patient_id", sa.BigInteger(), nullable=False),
        sa.Column("openemr_document_id", sa.String(length=64), nullable=True),
        sa.Column("doc_type", sa.String(length=32), nullable=False),
        sa.Column("category_path", sa.String(length=255), nullable=True),
        sa.Column("filename", sa.String(length=255), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False, server_default=sa.text("''")),
        sa.Column("page_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'uploaded'")),
        sa.Column("uploaded_by", sa.BigInteger(), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_source_document_patient_id", "source_document", ["patient_id"])
    op.create_index(
        "ix_source_document_openemr_document_id", "source_document", ["openemr_document_id"]
    )
    op.create_index("ix_source_document_correlation_id", "source_document", ["correlation_id"])

    op.create_table(
        "document_page",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_document_id", sa.BigInteger(), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("image", sa.LargeBinary(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("ocr_tokens", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_document.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("source_document_id", "page_no", name="uq_document_page_doc_pageno"),
    )
    op.create_index("ix_document_page_source_document_id", "document_page", ["source_document_id"])

    op.create_table(
        "extraction",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_document_id", sa.BigInteger(), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False, server_default=sa.text("''")),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("confidence_overall", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'ok'")),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_document.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_extraction_source_document_id", "extraction", ["source_document_id"])
    op.create_index("ix_extraction_correlation_id", "extraction", ["correlation_id"])

    op.create_table(
        "extracted_fact",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("extraction_id", sa.BigInteger(), nullable=False),
        sa.Column("field_path", sa.String(length=255), nullable=False),
        sa.Column("value", sa.String(), nullable=True),
        sa.Column("unit", sa.String(length=64), nullable=True),
        sa.Column("reference_range", sa.String(length=128), nullable=True),
        sa.Column("abnormal_flag", sa.String(length=16), nullable=True),
        sa.Column("collection_date", sa.DateTime(), nullable=True),
        sa.Column("page_no", sa.Integer(), nullable=True),
        sa.Column("bbox", sa.JSON(), nullable=True),
        sa.Column("match_confidence", sa.Float(), nullable=True),
        sa.Column("supported", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.ForeignKeyConstraint(["extraction_id"], ["extraction.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_extracted_fact_extraction_id", "extracted_fact", ["extraction_id"])


def downgrade() -> None:
    op.drop_index("ix_extracted_fact_extraction_id", table_name="extracted_fact")
    op.drop_table("extracted_fact")
    op.drop_index("ix_extraction_correlation_id", table_name="extraction")
    op.drop_index("ix_extraction_source_document_id", table_name="extraction")
    op.drop_table("extraction")
    op.drop_index("ix_document_page_source_document_id", table_name="document_page")
    op.drop_table("document_page")
    op.drop_index("ix_source_document_correlation_id", table_name="source_document")
    op.drop_index("ix_source_document_openemr_document_id", table_name="source_document")
    op.drop_index("ix_source_document_patient_id", table_name="source_document")
    op.drop_table("source_document")
