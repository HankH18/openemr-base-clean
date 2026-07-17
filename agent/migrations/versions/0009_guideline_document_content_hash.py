"""guideline_document.content_hash — make a corrected guideline actually apply

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-17

Adds a nullable ``content_hash`` to ``guideline_document`` so the corpus ingest
can tell "already ingested" from "already ingested AND still current".

**The bug this closes.** ``copilot/rag/ingest.py`` skipped a document on the
front-matter ``source`` key alone. ``source`` is the natural key — it identifies
*which* document, never *which version*. So an operator who fixed a wrong dose in
a corpus file and re-ran the documented ingest got ``skipped (already ingested)``
— which reads as success — while the co-pilot went on citing the superseded text.
Worse, the serve-time verifier (``copilot/verification/serve.py``) re-materializes
the quoted chunk from that same stale row, so the stale quote matches itself
verbatim and the claim is served as **grounded**. The staleness is self-consistent,
which is why the verification gate — the product's core safety mechanism — could
not catch it. A content hash is the only thing that distinguishes the versions.

**Nullable, deliberately.** ``guideline_document`` is pre-existing (0006) and
populated on every deployment, so this ALTER must not fail on a non-empty table —
matching 0002 / 0007 / 0008, which all add nullable columns to existing tables.
NOT NULL + ``server_default`` is reserved here for columns born on a
``create_table`` (0005), where no legacy row can contradict the default.

**NULL is load-bearing, not filler.** It means *unknown*, not *empty*: a row
written before this migration was hashed by nothing, so its freshness cannot be
established from the DB. ``ingest_corpus`` treats NULL as stale and rebuilds the
document once, which self-heals every already-deployed corpus on the next ingest
and replaces the NULL with a real hash. Back-filling a hash here instead would be
a lie — it would assert that rows of unknown provenance match the current corpus
file, re-arming the exact bug for every corpus already in production, and it is
also impossible offline: the correct value depends on the corpus file's bytes,
which a migration must never read.

Additive and reversible; ``batch_alter_table`` keeps the drop portable across
Postgres (prod) and SQLite (tests). The column holds no irreplaceable data — the
corpus is reproducible from the repo by re-running the ingest.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("guideline_document") as batch:
        batch.add_column(sa.Column("content_hash", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("guideline_document") as batch:
        batch.drop_column("content_hash")
