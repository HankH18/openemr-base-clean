"""Stdlib-only venv bootstrap shared by every acceptance entry point.

FROZEN GOAL HARNESS. This module guarantees that whatever Python launched an
entry point, the process ends up running under the agent venv
(``agent/.venv/bin/python``) with the harness's dependencies importable.

Exit-code contract (frozen with the goals):
- ``0`` + a bare number on the last stdout line = a real measurement.
- ``2`` = usage error (argparse handles this).
- ``3`` = ENVIRONMENT ERROR (stale venv, missing dep/binary, empty scan corpus,
  failed scanner self-proof) with NO number, so env noise never reads as a
  regression.

On an import-probe failure an entry point self-syncs the venv ONCE
(``uv pip install --python .venv/bin/python -e '.[dev]'``) and re-execs itself
under the venv interpreter. If the probe still fails after that, it exits 3.

Only the standard library may be imported here — this runs *before* the venv is
known to be usable.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ACC_DIR = Path(__file__).resolve().parent
REPO_ROOT = ACC_DIR.parents[1]
AGENT_DIR = REPO_ROOT / "agent"
VENV_PY = AGENT_DIR / ".venv" / "bin" / "python"
_SYNC_GUARD = "_SWARM_ACC_VENV_SYNCED"


def env_error(msg: str) -> None:
    """Print an env-error diagnostic to stderr and exit 3 with NO number."""
    print(f"ENV ERROR: {msg}", file=sys.stderr)
    raise SystemExit(3)


def _agent_on_path() -> None:
    if str(AGENT_DIR) not in sys.path:
        sys.path.insert(0, str(AGENT_DIR))


def _probe(modules: list[str]) -> bool:
    _agent_on_path()
    for name in modules:
        try:
            __import__(name)
        except Exception:
            return False
    return True


def ensure_ready(modules: list[str]) -> None:
    """Ensure ``modules`` import; else sync the venv once and re-exec under it.

    ``modules`` is the set of importable names this entry point needs (e.g.
    ``["pytest", "fastapi", "copilot"]``). An empty list is a valid no-op for
    entry points with no Python-import dependency (``web_check`` needs node, not
    pip packages).
    """
    if _probe(modules):
        return
    if os.environ.get(_SYNC_GUARD) == "1":
        env_error("required dependencies unavailable after venv sync: " + ",".join(modules))
    if not VENV_PY.exists():
        env_error(f"agent venv interpreter not found at {VENV_PY}")
    try:
        subprocess.run(
            ["uv", "pip", "install", "--python", str(VENV_PY), "-e", ".[dev]"],
            cwd=str(AGENT_DIR),
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        env_error("uv not found on PATH; cannot sync the agent venv")
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or exc.stdout or "")[-400:]
        env_error(f"venv sync failed: {tail}")
    env = dict(os.environ)
    env[_SYNC_GUARD] = "1"
    os.execve(str(VENV_PY), [str(VENV_PY), *sys.argv], env)
