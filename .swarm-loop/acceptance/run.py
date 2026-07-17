#!/usr/bin/env python3
"""Frozen metric runner for the Week-2 acceptance suite. Prints a BARE NUMBER last.

  --pass-rate   overall pass rate (%) over every pytest feature suite
  --feature X   COUNT of passing criteria under acceptance/X/  (per-feature metric)

Each criterion is exactly one pytest test function whose name encodes the
criterion id, so a feature's passing-criteria count == its passing-test count.
The frontend feature (F9) is measured separately by ``web_check.py`` (it needs
node/vitest, not pytest) and is deliberately NOT in ``FEATURES`` here.

Contract (see acceptance-criteria.md): a suite that RAN and all-failed prints 0;
an ENVIRONMENT ERROR (stale venv / missing dep) prints nothing and exits 3, so a
broken environment never poisons the regression with a fake 0. Usage errors exit
2 (argparse). The features do not exist yet, so today every feature prints ~0.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import _bootstrap

ACC = Path(__file__).resolve().parent

# The pytest-measured feature suites. Frontend (F9) -> web_check.py.
FEATURES = [
    "verification",
    "ingestion",
    "writeback",
    "rag",
    "graph",
    "api",
    "evalgate",
]


class _Tally:
    """Counts per-test call outcomes; setup/teardown failures count as errors."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.errors = 0

    def pytest_runtest_logreport(self, report) -> None:  # noqa: ANN001
        if report.when == "call":
            if report.passed:
                self.passed += 1
            elif report.failed:
                self.failed += 1
        elif report.when in ("setup", "teardown") and report.failed:
            self.errors += 1


def _run(paths: list[str]) -> _Tally:
    import pytest

    tally = _Tally()
    # Neutralise the agent pyproject's addopts/filterwarnings (unrelated dependency
    # warnings must never fail a frozen test the workers cannot edit) and force
    # asyncio auto-mode so async criteria run regardless of discovered ini config.
    pytest.main(
        [
            *paths,
            "-p",
            "no:cacheprovider",
            "-p",
            "no:terminal",
            "-o",
            "addopts=",
            "-o",
            "filterwarnings=",
            "-o",
            "asyncio_mode=auto",
        ],
        plugins=[tally],
    )
    return tally


def main() -> None:
    ap = argparse.ArgumentParser()
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--pass-rate", action="store_true")
    group.add_argument("--feature")
    args = ap.parse_args()  # argparse exits 2 on a usage error, per the contract.

    # Only after arg parsing (so usage errors stay 2) ensure the venv is usable.
    _bootstrap.ensure_ready(
        ["pytest", "fastapi", "respx", "httpx", "sqlalchemy", "aiosqlite", "copilot"]
    )

    if args.feature:
        feature_dir = ACC / args.feature
        if not feature_dir.is_dir():
            _bootstrap.env_error(f"no acceptance dir for feature {args.feature!r}")
        tally = _run([str(feature_dir)])
        if tally.passed + tally.failed + tally.errors == 0:
            _bootstrap.env_error(f"no tests collected under {feature_dir}")
        print(tally.passed)
        return

    dirs = [str(ACC / f) for f in FEATURES if (ACC / f).is_dir()]
    if not dirs:
        _bootstrap.env_error("no acceptance feature dirs present")
    tally = _run(dirs)
    denom = tally.passed + tally.failed + tally.errors
    if denom == 0:
        _bootstrap.env_error("no acceptance tests collected")
    print(round(100.0 * tally.passed / denom, 2))


if __name__ == "__main__":
    main()
