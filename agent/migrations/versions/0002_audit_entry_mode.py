"""audit_log.entry_mode — physician write-back attribution

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-11

Adds a nullable ``entry_mode`` column to ``audit_log`` so a write-back row can
record how the value reached the record (``human_direct`` in Phase 1). Nullable
and column-only: reads and every pre-write-back row leave it NULL, so the change
is fully backward-compatible and touches no other table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("audit_log", sa.Column("entry_mode", sa.String(length=32), nullable=True))


def downgrade() -> None:
    # Batch mode so the drop works portably on SQLite as well as Postgres.
    with op.batch_alter_table("audit_log") as batch:
        batch.drop_column("entry_mode")
