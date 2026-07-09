#!/usr/bin/env python3
"""Frozen metric: pass-rate (%) of the project's own suite (agent/tests + agent/evals).

Run faithfully with the project's own pytest config (CI parity). Skipped cases
(the 2 live-LLM evals) are excluded from the denominator. Prints nothing + exits
nonzero if nothing ran at all (broken runner -> loud failure).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parents[2] / "agent"
sys.path.insert(0, str(AGENT))  # make `copilot` importable regardless of cwd


class _Tally:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def pytest_runtest_logreport(self, report) -> None:  # noqa: ANN001
        if report.when == "call":
            if report.passed:
                self.passed += 1
            elif report.failed:
                self.failed += 1
        elif report.when in ("setup", "teardown") and report.failed:
            self.failed += 1


def main() -> None:
    tally = _Tally()
    # Run with the project's own config (CI parity); the bare number is the last line.
    pytest.main(
        [str(AGENT / "tests"), str(AGENT / "evals"), "-p", "no:cacheprovider", "-q"],
        plugins=[tally],
    )
    denom = tally.passed + tally.failed
    if denom == 0:
        print("no project tests ran", file=sys.stderr)
        sys.exit(1)
    print(round(100.0 * tally.passed / denom, 2))


if __name__ == "__main__":
    main()
