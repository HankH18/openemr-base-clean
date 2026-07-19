"""Model⇄migration schema parity — close the ``create_all`` test blind spot.

Every functional test builds its schema with ``Base.metadata.create_all`` on
SQLite; only ``test_migrations.py`` exercises the Alembic migrations. When the
ORM models and the migrations disagree on which indexes/constraints exist, the
functional suite silently tests a *different* schema than production runs — a
regression in an index the models never declared is uncatchable there.

These tests assert that the ``create_all`` schema declares the same named
indexes/constraints the migrations create, for the three objects the round-2
audit flagged as drifting:

- ``audit_log(at)`` → ``ix_audit_log_at`` (migration 0003) — the retention
  range-scan index that ``create_all`` previously never built.
- ``guideline_chunk(guideline_document_id)`` → ``ix_guideline_chunk_document_id``
  (migration 0006), not SQLAlchemy's auto ``ix_guideline_chunk_guideline_document_id``.
- ``clinician(fhir_user)`` unique → ``uq_clinician_fhir_user`` (migration 0004),
  not an anonymous constraint.

Pure schema inspection: a synchronous in-memory SQLite engine, no repository or
app wiring — same style as ``test_migrations.py``.
"""

from __future__ import annotations

from sqlalchemy import create_engine, inspect

from copilot.memory import Base


def _build_schema() -> object:
    """Build the ORM schema via ``create_all`` on a throwaway SQLite engine."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return engine


def test_create_all_builds_audit_at_index() -> None:
    """``create_all`` declares ``ix_audit_log_at`` — the retention-sweep index.

    RED before the fix: ``AuditLogRow`` had no ``__table_args__``, so migration
    0003's ``ix_audit_log_at`` existed only in the migration-built schema and the
    functional (create_all) schema silently lacked it.
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    index_names = {ix["name"] for ix in inspect(engine).get_indexes("audit_log")}
    assert "ix_audit_log_at" in index_names
    engine.dispose()


def test_guideline_chunk_fk_index_name_matches_migration() -> None:
    """The FK index carries migration 0006's name, not SQLAlchemy's auto name."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    index_names = {ix["name"] for ix in inspect(engine).get_indexes("guideline_chunk")}
    assert "ix_guideline_chunk_document_id" in index_names
    # The old auto-generated name must be gone, or a --autogenerate diff would
    # still see drift (drop one, add the other).
    assert "ix_guideline_chunk_guideline_document_id" not in index_names
    engine.dispose()


def test_clinician_fhir_user_unique_constraint_is_named() -> None:
    """The ``fhir_user`` unique constraint carries migration 0004's name."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    uniques = inspect(engine).get_unique_constraints("clinician")
    by_name = {uc["name"]: uc["column_names"] for uc in uniques}
    assert "uq_clinician_fhir_user" in by_name
    assert by_name["uq_clinician_fhir_user"] == ["fhir_user"]
    engine.dispose()
