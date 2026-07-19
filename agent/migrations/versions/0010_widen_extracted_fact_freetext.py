"""extracted_fact free-text columns — widen to hold VLM-extracted values

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-19

The medication-list and intake extractors reuse the lab fact columns to hold
free text the VLM extracts verbatim: a medication's dosing frequency lands in
``abnormal_flag`` (sized ``varchar(16)`` for lab flags like ``H``/``L``), its
dose in ``unit`` (``varchar(64)``), longer notes in ``reference_range``
(``varchar(128)``). A real medication frequency ("Twice daily (with meals)",
"QHS (once daily, bedtime)") is 24+ chars, so the INSERT raised
``psycopg StringDataRightTruncation: value too long for type character
varying(16)`` and the whole ``POST /v1/documents`` 500'd. Because the VLM is
non-deterministic, the *same* upload failed then succeeded on retry (a shorter
extraction happened to fit), which read as an intermittent, doc-type-agnostic
500 on medication_list and intake_form alike.

These columns hold arbitrary VLM-extracted text; a length cap buys nothing (the
no-invention gate and the UI do not depend on it) and only crashes ingestion.
Widen ``abnormal_flag`` / ``unit`` / ``reference_range`` to unbounded TEXT.
``value`` is already unbounded; ``field_path`` / ``category`` hold controlled
keys (a dotted path, an ``IntakeCategory`` enum value), not free text, and keep
their caps.

Widening is instant + non-lossy on Postgres and safe under ``batch_alter_table``
on SQLite (tests). The downgrade re-imposes the prior caps on a best-effort
basis (it would fail on any row already exceeding them — the columns exist to
*stop* that from mattering).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (column, prior capped type) — widened to TEXT in upgrade, restored in downgrade.
_WIDEN: tuple[tuple[str, sa.String], ...] = (
    ("abnormal_flag", sa.String(length=16)),
    ("unit", sa.String(length=64)),
    ("reference_range", sa.String(length=128)),
)


def upgrade() -> None:
    with op.batch_alter_table("extracted_fact") as batch:
        for name, capped in _WIDEN:
            batch.alter_column(
                name, existing_type=capped, type_=sa.Text(), existing_nullable=True
            )


def downgrade() -> None:
    with op.batch_alter_table("extracted_fact") as batch:
        for name, capped in _WIDEN:
            batch.alter_column(
                name, existing_type=sa.Text(), type_=capped, existing_nullable=True
            )
