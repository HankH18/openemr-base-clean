"""intake-fact category — extracted_fact.category (OpenEMR record type)

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-15

Adds a nullable ``category`` to ``extracted_fact`` tagging each intake-form fact
with the OpenEMR record it round-trips to (IntakeCategory: demographic /
chief_complaint / medication / allergy / medical_problem / family_history). NULL
for lab facts. Additive + reversible; holds no irreplaceable data — categories are
re-derivable by re-running extraction. batch_alter_table keeps it portable across
Postgres (prod) and SQLite (tests).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("extracted_fact") as batch:
        batch.add_column(sa.Column("category", sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("extracted_fact") as batch:
        batch.drop_column("category")
