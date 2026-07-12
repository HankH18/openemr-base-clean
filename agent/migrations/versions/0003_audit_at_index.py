"""audit_log.at index — efficient retention range scans

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-11

Adds an index on ``audit_log.at`` so the retention sweep (§164.312(b),
PRODUCTION_GRADE_PLAN.md §5) can range-scan by timestamp without a full table
scan. Index-only and backward-compatible: no column or row changes.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_audit_log_at", "audit_log", ["at"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_at", table_name="audit_log")
