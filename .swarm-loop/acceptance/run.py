#!/usr/bin/env python3
"""Frozen metric runner for the E2E acceptance suite. Prints a BARE NUMBER last.

  --pass-rate   pass rate (%) over ALL acceptance features (chat+rounds+authz+background)
  --feature X   COUNT of passing tests in acceptance/X/  (per-feature metric)

Contract (see references/goal-setting.md): a suite that RAN and all failed prints 0;
a suite that COULD NOT RUN prints nothing and exits nonzero, so `measure` fails loudly
instead of poisoning the regression with a fake 0.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

ACC = Path(__file__).resolve().parent
FEATURES = ["chat", "rounds", "authz", "background"]


class _Tally:
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
    tally = _Tally()
    # Neutralise the agent pyproject's addopts/filterwarnings so unrelated warnings
    # in dependency code can't spuriously fail a frozen test the workers can't edit.
    pytest.main(
        [*paths, "-p", "no:cacheprovider", "-p", "no:terminal",
         "-o", "addopts=", "-o", "filterwarnings="],
        plugins=[tally],
    )
    return tally


def main() -> None:
    ap = argparse.ArgumentParser()
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--pass-rate", action="store_true")
    group.add_argument("--feature")
    args = ap.parse_args()

    if args.feature:
        feature_dir = ACC / args.feature
        if not feature_dir.is_dir():
            print(f"no acceptance dir for feature {args.feature!r}", file=sys.stderr)
            sys.exit(1)
        tally = _run([str(feature_dir)])
        if tally.passed + tally.failed + tally.errors == 0:
            print(f"no tests collected under {feature_dir}", file=sys.stderr)
            sys.exit(1)
        print(tally.passed)
        return

    dirs = [str(ACC / f) for f in FEATURES if (ACC / f).is_dir()]
    if not dirs:
        print("no acceptance feature dirs present", file=sys.stderr)
        sys.exit(1)
    tally = _run(dirs)
    denom = tally.passed + tally.failed + tally.errors
    if denom == 0:
        print("no acceptance tests collected", file=sys.stderr)
        sys.exit(1)
    print(round(100.0 * tally.passed / denom, 2))


if __name__ == "__main__":
    main()
