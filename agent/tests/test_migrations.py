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
    }
    engine.dispose()

    command.downgrade(alembic_config, "base")
    engine = create_engine(url_sync)
    tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert tables == set()
    engine.dispose()
