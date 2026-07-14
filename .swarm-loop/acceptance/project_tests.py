#!/usr/bin/env python3
"""Frozen metric: pass-rate (%) of the project's own suite (agent/tests + agent/evals).

Regression guard, NOT a new-feature metric — run faithfully with the project's own
pytest config (CI parity). Skipped cases (the live-LLM evals) are excluded from the
denominator. Bare number on the last stdout line.

Exit 3 (ENV ERROR, no number) if the suite could not run at all (broken venv) so a
non-runnable environment never reads as a 0% regression.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import _bootstrap

AGENT = Path(__file__).resolve().parents[2] / "agent"


def main() -> None:
    _bootstrap.ensure_ready(["pytest", "fastapi", "respx", "copilot"])
    # CI parity via a CLEAN subprocess. An in-process pytest.main() emits
    # "Module already imported so cannot be rewritten; anyio" — our import probe
    # (fastapi) pulls in anyio before pytest installs assertion rewriting, and the
    # project's filterwarnings=error escalates that warning to a hard error. A
    # fresh subprocess installs the rewrite hook first, exactly like CI does.
    proc = subprocess.run(
        [sys.executable, "-m", "pytest",
         str(AGENT / "tests"), str(AGENT / "evals"),
         "-p", "no:cacheprovider", "-q", "--tb=no"],
        capture_output=True, text=True, cwd=str(AGENT),
    )
    out = proc.stdout + "\n" + proc.stderr

    def _n(word: str) -> int:
        m = re.search(rf"(\d+) {word}", out)
        return int(m.group(1)) if m else 0

    passed = _n("passed")
    failed = _n("failed")
    errors = _n(r"errors?")
    denom = passed + failed + errors  # skipped excluded (live-LLM evals), per the docstring
    if denom == 0:
        _bootstrap.env_error(
            "no project tests ran (pytest produced no pass/fail summary):\n" + out.strip()[-500:]
        )
    print(round(100.0 * passed / denom, 2))


if __name__ == "__main__":
    main()
