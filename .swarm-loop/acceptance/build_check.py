#!/usr/bin/env python3
"""Frozen metric `build_ok`: prints 1 if the service builds and boots, else 0.

Checks, each independently (a failure of any -> 0, which is a valid measurement):
  1. `copilot.api.app.create_app` imports and boots; `GET /health` == 200.
  2. `alembic upgrade head` applies the full migration chain on a clean SQLite file.

Prints nothing + exits nonzero only if the check harness itself cannot run at all.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

AGENT = Path(__file__).resolve().parents[2] / "agent"
sys.path.insert(0, str(AGENT))  # make `copilot` importable regardless of cwd


def _health_ok(db_file: str) -> bool:
    try:
        os.environ["COPILOT_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_file}"
        os.environ.setdefault("COPILOT_ANTHROPIC_API_KEY", "")
        from copilot.config import get_settings
        from copilot.memory.db import get_engine, get_session_factory

        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()

        import sqlalchemy as sa

        import copilot.memory.models  # noqa: F401
        from copilot.memory.db import Base

        sync = sa.create_engine(f"sqlite:///{db_file}")
        Base.metadata.create_all(sync)
        sync.dispose()

        from fastapi.testclient import TestClient

        from copilot.api.app import create_app

        client = TestClient(create_app(get_settings(), probe_factories=[]))
        return client.get("/health").status_code == 200
    except Exception as exc:  # noqa: BLE001 - a broken app is a valid 0
        print(f"health check failed: {exc}", file=sys.stderr)
        return False


def _migrations_ok(db_file: str) -> bool:
    try:
        env = {**os.environ, "COPILOT_DATABASE_URL": f"sqlite+aiosqlite:///{db_file}"}
        proc = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=AGENT, env=env, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            print(f"alembic failed: {proc.stderr[-300:]}", file=sys.stderr)
        return proc.returncode == 0
    except Exception as exc:  # noqa: BLE001
        print(f"migration check failed: {exc}", file=sys.stderr)
        return False


def main() -> None:
    try:
        tmp = tempfile.mkdtemp()
    except OSError as exc:
        print(f"cannot create tempdir: {exc}", file=sys.stderr)
        sys.exit(1)
    ok = _health_ok(os.path.join(tmp, "boot.db")) and _migrations_ok(os.path.join(tmp, "mig.db"))
    print(1 if ok else 0)


if __name__ == "__main__":
    main()
