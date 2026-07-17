"""audit_log.source_ref — write-back (document, fact) provenance

Revision ID: 0008
Revises: 0007

Adds a nullable ``source_ref`` JSON column to ``audit_log`` so a write-back row
can record the source document + extracted fact a derived write descends from.
Closes the traceability gap at the OpenEMR write boundary: the agent store's
FK chain already guarantees no fact exists without provenance, but the audit
trail for ``write_proposed`` / ``write_committed`` had nowhere honest to name it.

**Why a new column rather than reusing ``resources_returned``.** That column has
an established, deliberate meaning: the *FHIR resources this action returned or
created*. ``chat/service.py`` documents the rule explicitly — "a document or
guideline citation names an agent-store row, not a FHIR resource — listing its
id here would misreport the PHI access trail" — and filters non-FHIR citations
out for exactly that reason. A ``source_document`` row id is an agent-store id
and an *input* to the write, not a resource returned by it; writing it there
would overstate what a proposal touched (a proposal creates nothing at all) and
corrupt any "what did we disclose/create" audit query. So there is no existing
field this fits, and provenance gets its own.

Nullable and column-only, mirroring 0002 (``entry_mode``): reads, physician-direct
writes, and every pre-existing row leave it NULL, so the change is fully
backward-compatible and touches no other table. ``sa.JSON`` is portable —
``JSONType`` in ``memory.db`` maps JSON→JSONB on Postgres at runtime, matching
the existing ``resources_returned`` column. batch_alter_table keeps the drop
portable across Postgres (prod) and SQLite (tests).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("audit_log") as batch:
        batch.add_column(sa.Column("source_ref", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("audit_log") as batch:
        batch.drop_column("source_ref")
