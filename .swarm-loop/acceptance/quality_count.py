#!/usr/bin/env python3
"""Frozen metric: lint + type error count = ruff findings + mypy(strict) errors.

Bare integer on the last stdout line. Prints nothing + exits nonzero only if a tool
cannot be executed at all (missing venv) — a real error count of 0 is a valid result.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parents[2] / "agent"


def main() -> None:
    py = sys.executable

    try:
        ruff = subprocess.run(
            [py, "-m", "ruff", "check", ".", "--output-format=json"],
            cwd=AGENT, capture_output=True, text=True,
        )
    except OSError as exc:
        print(f"ruff could not run: {exc}", file=sys.stderr)
        sys.exit(1)
    if ruff.returncode not in (0, 1):  # 0=clean, 1=findings, 2+=tool error
        print(f"ruff errored (rc={ruff.returncode}): {ruff.stderr[-300:]}", file=sys.stderr)
        sys.exit(1)
    try:
        ruff_n = len(json.loads(ruff.stdout or "[]"))
    except json.JSONDecodeError as exc:
        print(f"ruff json parse failed: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        mypy = subprocess.run(
            [py, "-m", "mypy", "copilot"],
            cwd=AGENT, capture_output=True, text=True,
        )
    except OSError as exc:
        print(f"mypy could not run: {exc}", file=sys.stderr)
        sys.exit(1)
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
