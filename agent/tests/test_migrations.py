"""End-to-end migration test — upgrade + downgrade against SQLite.

Postgres in tests would be heavier; the migration is authored with
portable types (`sa.JSON`), and `JSONType` in `memory.db` maps JSON→JSONB
on Postgres at runtime.  A Postgres-specific migration test is deferred
until we have testcontainers wired in CI.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


@pytest.fixture
def alembic_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    from copilot.config import get_settings

    get_settings.cache_clear()

    cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).parent.parent / "migrations"))
    return cfg


def test_upgrade_head_then_downgrade_base_leaves_no_tables(alembic_config: Config) -> None:
    from sqlalchemy import create_engine, inspect

    command.upgrade(alembic_config, "head")

    url_async = os.environ["COPILOT_DATABASE_URL"]
    url_sync = url_async.replace("+aiosqlite", "")
    engine = create_engine(url_sync)
    tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert tables == {
        "memory_file",
        "sync_state",
        "last_seen",
        "rounding_cursor",
        "conversation",
        "message",
        "audit_log",
        # 0004 — per-physician SMART login backbone.
        "clinician",
        "physician_session",
        "login_txn",
    }
    engine.dispose()

    command.downgrade(alembic_config, "base")
    engine = create_engine(url_sync)
    tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert tables == set()
    engine.dispose()


def test_upgrade_head_adds_audit_entry_mode_column(alembic_config: Config) -> None:
    """Migration 0002 adds the nullable ``entry_mode`` write-back attribution column."""
    from sqlalchemy import create_engine, inspect

    command.upgrade(alembic_config, "head")

    url_sync = os.environ["COPILOT_DATABASE_URL"].replace("+aiosqlite", "")
    engine = create_engine(url_sync)
    columns = {c["name"]: c for c in inspect(engine).get_columns("audit_log")}
    assert "entry_mode" in columns
    assert columns["entry_mode"]["nullable"] is True
    engine.dispose()


def test_head_creates_audit_at_index(alembic_config: Config) -> None:
    """Migration 0003 adds the ``audit_log(at)`` index for retention range scans."""
    from sqlalchemy import create_engine, inspect

    command.upgrade(alembic_config, "head")

    url_sync = os.environ["COPILOT_DATABASE_URL"].replace("+aiosqlite", "")
    engine = create_engine(url_sync)
    index_names = {ix["name"] for ix in inspect(engine).get_indexes("audit_log")}
    assert "ix_audit_log_at" in index_names
    engine.dispose()


def test_head_creates_smart_session_tables(alembic_config: Config) -> None:
    """Migration 0004 adds clinician / physician_session / login_txn with key columns."""
    from sqlalchemy import create_engine, inspect

    command.upgrade(alembic_config, "head")

    url_sync = os.environ["COPILOT_DATABASE_URL"].replace("+aiosqlite", "")
    engine = create_engine(url_sync)
    inspector = inspect(engine)

    clinician_cols = {c["name"] for c in inspector.get_columns("clinician")}
    assert {"id", "fhir_user", "display_name", "created_at", "last_login_at"} <= clinician_cols

    session_cols = {c["name"] for c in inspector.get_columns("physician_session")}
    assert {
        "session_id",
        "clinician_id",
        "access_token_enc",
        "refresh_token_enc",
        "access_expires_at",
        "absolute_expires_at",
        "revoked",
    } <= session_cols
    # Token columns are opaque binary (Fernet ciphertext), never text.
    session_types = {
        c["name"]: type(c["type"]).__name__ for c in inspector.get_columns("physician_session")
    }
    assert session_types["access_token_enc"] in {"BLOB", "LARGEBINARY", "BYTEA"}

    txn_cols = {c["name"] for c in inspector.get_columns("login_txn")}
    assert {"state", "code_verifier", "nonce", "expires_at"} <= txn_cols
    engine.dispose()


def test_downgrade_one_step_drops_audit_entry_mode_column(alembic_config: Config) -> None:
    """Downgrading 0002→0001 removes ``entry_mode`` while leaving the table intact."""
    from sqlalchemy import create_engine, inspect

    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "0001")

    url_sync = os.environ["COPILOT_DATABASE_URL"].replace("+aiosqlite", "")
    engine = create_engine(url_sync)
    inspector = inspect(engine)
    assert "audit_log" in inspector.get_table_names()
    columns = {c["name"] for c in inspector.get_columns("audit_log")}
    assert "entry_mode" not in columns
    engine.dispose()
