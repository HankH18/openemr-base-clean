#!/usr/bin/env python3
"""Frozen metric: lint + type error count = ruff findings + mypy(strict) errors.

Bare integer on the last stdout line. A real error count of 0 is a valid result;
only a tool that cannot execute at all (broken venv) is an ENV ERROR → exit 3
(no number), so tooling breakage never reads as a regression.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import _bootstrap

AGENT = Path(__file__).resolve().parents[2] / "agent"


def main() -> None:
    _bootstrap.ensure_ready(["ruff", "mypy"])
    py = sys.executable  # after bootstrap this is the agent venv interpreter

    try:
        ruff = subprocess.run(
            [py, "-m", "ruff", "check", ".", "--output-format=json"],
            cwd=AGENT,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        _bootstrap.env_error(f"ruff could not run: {exc}")
    if ruff.returncode not in (0, 1):  # 0=clean, 1=findings, 2+=tool error
        _bootstrap.env_error(f"ruff errored (rc={ruff.returncode}): {ruff.stderr[-300:]}")
    try:
        ruff_n = len(json.loads(ruff.stdout or "[]"))
    except json.JSONDecodeError as exc:
        _bootstrap.env_error(f"ruff json parse failed: {exc}")

    try:
        mypy = subprocess.run(
            [py, "-m", "mypy", "copilot"],
            cwd=AGENT,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        _bootstrap.env_error(f"mypy could not run: {exc}")
    match = re.search(r"Found (\d+) error", mypy.stdout)
    if match:
        mypy_n = int(match.group(1))
    elif mypy.returncode == 0 or "Success" in mypy.stdout:
        mypy_n = 0
    else:
        mypy_n = sum(1 for line in mypy.stdout.splitlines() if ": error:" in line)

    print(ruff_n + mypy_n)


if __name__ == "__main__":
    main()
