"""clinician + physician_session + login_txn — per-physician SMART login

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-11

Creates the three tables the SMART session backbone needs
(PRODUCTION_GRADE_PLAN.md §1). Additive and inert while ``auth_mode="disabled"``
— nothing reads or writes them until the operator enables SMART, so the
no-login demo is byte-for-byte unchanged.

- ``clinician``          stable integer surrogate for an OpenEMR ``fhirUser``.
- ``physician_session``  the opaque server session; holds the physician OAuth
                         tokens ONLY as Fernet ciphertext (``LargeBinary``).
- ``login_txn``          short-lived OAuth ``state`` + PKCE verifier.

Portable across SQLite (tests) and Postgres (prod): ``sa.LargeBinary`` maps to
BLOB/BYTEA, ``sa.BigInteger`` to INTEGER/BIGINT, and every timestamp is
``sa.DateTime(timezone=True)``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "clinician",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("fhir_user", sa.String(length=512), nullable=False),
        sa.Column("openemr_username", sa.String(length=255), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("npi", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("fhir_user", name="uq_clinician_fhir_user"),
    )

    op.create_table(
        "physician_session",
        sa.Column("session_id", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column("clinician_id", sa.BigInteger(), nullable=False),
        sa.Column("access_token_enc", sa.LargeBinary(), nullable=False),
        sa.Column("refresh_token_enc", sa.LargeBinary(), nullable=True),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scope", sa.String(length=1024), nullable=True),
        sa.Column("fhir_user", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.ForeignKeyConstraint(["clinician_id"], ["clinician.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_physician_session_clinician_id", "physician_session", ["clinician_id"])

    op.create_table(
        "login_txn",
        sa.Column("state", sa.String(length=128), primary_key=True, nullable=False),
        sa.Column("code_verifier", sa.String(length=128), nullable=False),
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("redirect_target", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("login_txn")
    op.drop_index("ix_physician_session_clinician_id", table_name="physician_session")
    op.drop_table("physician_session")
    op.drop_table("clinician")
