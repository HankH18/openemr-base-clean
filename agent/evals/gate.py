#!/usr/bin/env python3
"""Blocking, LLM-free eval GATE for the Clinical Co-Pilot.

The blocking tier of the two-tier eval strategy: a deterministic, stubbed
(no Anthropic key, no network) runner over the Week-2 golden set that emits the
five rubric booleans per case, computes an overall ``pass_rate`` AND a
per-category pass rate, and compares both against a committed baseline. It
exits NONZERO when any of the four rules below trips, so a git pre-push hook
and the GitLab ``agent:tests`` CI job can block the change.

The spec: *"the build must fail if any category regresses by more than 5% or
drops below the pass threshold."* That is four checks, not one:

1. aggregate ``pass_rate`` below the absolute floor (:data:`MIN_PASS_RATE`);
2. aggregate ``pass_rate`` a > 5% *relative* regression vs the baseline;
3. ANY category below the absolute floor;
4. ANY category a > 5% *relative* regression vs its baseline.

Checks 1/3/4 close audited holes: the aggregate-only, relative-only rule let a
1-2 case regression (98.11 / 96.23 against a 100.0 baseline — a 1.89% / 3.77%
relative dip) exit 0, and let any single rubric rot invisibly so long as the
aggregate held. Every failing check is reported (not just the first), and a
category failure names the category.

Why the floor is 100.0: this gate is deterministic — no model, no network, no
sampling. Every golden case is a fixture with exactly one correct outcome, so a
failing case is a real regression, never noise. "Fewer than all 53 passing" has
no benign reading, and on a 53-case set a single failure is only a 1.89% dip —
far inside any percentage band. The relative tolerance still governs the
``--baseline`` override path and per-category comparison against a non-perfect
baseline; the floor is what makes small, real regressions blocking.

CLI contract (pinned by ``.swarm-loop/acceptance/evalgate``):

    python evals/gate.py [--out PATH] [--baseline PATH] [--inject-regression]

- ``--out PATH``     write the machine-readable results JSON
  (``{"pass_rate": <0..100>, "cases": [{..., "<rubric>": bool, ...}]}``).
- ``--baseline PATH`` compare against an alternate baseline JSON carrying a
  ``pass_rate`` (and optionally ``per_category``); without it the committed
  ``gate_baseline.json`` is used.
- ``--inject-regression`` deterministically breaks each golden case's target
  rubric (fault injection) so the gate provably trips — the self-proof.
- ``--tolerance F`` relative regression tolerance (default 0.05 = 5%).
- ``--min-pass-rate F`` absolute floor, aggregate and per-category.
- ``--write-baseline`` rewrite the committed baseline from a clean run.
- Exit 0 = no blocking regression; nonzero = regression detected.

The golden set is every ``*.jsonl`` case under ``agent/evals/`` (filename not
containing "result") that declares a target rubric via ``rubric`` / ``rubrics``
/ ``category``. Week-1 grounding cases (invariant/boundary/authorization) carry
no rubric field and are ignored here.

@package   OpenEMR
@link      https://www.open-emr.org
@license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Make `copilot` and the `evals` package importable no matter how the runner was
# invoked (a bare `python evals/gate.py` puts evals/ on sys.path, not agent/).
_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from evals.rubrics import RUBRICS, evaluate_record, inject_regression  # noqa: E402

EVALS_DIR = Path(__file__).resolve().parent
COMMITTED_BASELINE = EVALS_DIR / "gate_baseline.json"
DEFAULT_TOLERANCE = 0.05  # 5% relative dip is tolerated; more blocks.
MIN_PASS_RATE = 100.0  # absolute floor — the spec's "pass threshold" clause.


@dataclass(frozen=True)
class Baseline:
    """A parsed baseline artifact.

    ``pass_rate`` is ``None`` when the baseline is absent/unreadable — the
    RELATIVE checks are then skipped, but the absolute floor still applies
    (it is baseline-independent, so a missing baseline must not disarm it).
    """

    path: Path
    pass_rate: float | None = None
    per_category: dict[str, float] | None = None


def discover_cases() -> list[tuple[Path, dict[str, Any], list[str]]]:
    """Collect every golden case that declares a target rubric.

    Mirrors the acceptance harness's ``golden_cases`` collector so the gate
    scores exactly the set the frozen criterion measures.
    """
    found: list[tuple[Path, dict[str, Any], list[str]]] = []
    for path in sorted(EVALS_DIR.rglob("*.jsonl")):
        if "result" in path.name.lower():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            declared = obj.get("rubrics")
            if not isinstance(declared, list):
                declared = [obj.get("rubric") or obj.get("category")]
            targets = [r for r in declared if r in RUBRICS]
            if targets:
                found.append((path, obj, targets))
    return found


def evaluate(inject: bool) -> tuple[list[dict[str, Any]], float]:
    """Score the golden set; return per-case records and the overall pass_rate."""
    results: list[dict[str, Any]] = []
    for path, case, targets in discover_cases():
        record = case.get("record")
        record = dict(record) if isinstance(record, dict) else {}
        if inject:
            for target in targets:
                record = inject_regression(record, target, case.get("planted_phi"))
        booleans = evaluate_record(record)
        results.append(
            {
                "id": case.get("id", "?"),
                "source": path.name,
                "rubrics": targets,
                **booleans,
                "passed": all(booleans.values()),
            }
        )
    total = len(results)
    pass_rate = round(100.0 * sum(r["passed"] for r in results) / total, 2) if total else 0.0
    return results, pass_rate


def category_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    """Per-rubric passing-case count over the scored set."""
    return {rubric: sum(1 for case in results if case[rubric]) for rubric in RUBRICS}


def category_rates(results: list[dict[str, Any]]) -> dict[str, float]:
    """Per-rubric pass rate (0..100) over the scored set.

    Every case emits all five rubric booleans, so each category is measured
    over the whole set — the quantity the per-category gate is defined on.
    """
    total = len(results)
    if not total:
        return {rubric: 0.0 for rubric in RUBRICS}
    counts = category_counts(results)
    return {rubric: round(100.0 * counts[rubric] / total, 2) for rubric in RUBRICS}


def check_regressions(
    pass_rate: float,
    rates: dict[str, float],
    baseline: Baseline,
    tolerance: float = DEFAULT_TOLERANCE,
    min_pass_rate: float = MIN_PASS_RATE,
) -> list[str]:
    """Apply all four blocking rules; return one message per FAILING rule.

    An empty list means the run is clean. Every rule is evaluated (no
    short-circuit) so one run reports every reason it is blocked.
    """
    failures: list[str] = []
    pct = round(tolerance * 100)

    # (1) aggregate absolute floor — the spec's "pass threshold".
    if pass_rate < min_pass_rate:
        failures.append(
            f"aggregate pass_rate {pass_rate} is below the {min_pass_rate} pass threshold"
        )

    # (2) aggregate relative regression vs baseline.
    if baseline.pass_rate is not None and pass_rate < baseline.pass_rate * (1.0 - tolerance):
        failures.append(
            f"aggregate pass_rate {pass_rate} is a >{pct}% relative regression "
            f"vs baseline {baseline.pass_rate}"
        )

    for rubric in RUBRICS:
        rate = rates.get(rubric)
        if rate is None:
            continue
        # (3) per-category absolute floor.
        if rate < min_pass_rate:
            failures.append(
                f"category '{rubric}' pass_rate {rate} is below the {min_pass_rate} pass threshold"
            )
        # (4) per-category relative regression vs its own baseline.
        base = (baseline.per_category or {}).get(rubric)
        if base is not None and rate < base * (1.0 - tolerance):
            failures.append(
                f"category '{rubric}' pass_rate {rate} is a >{pct}% relative "
                f"regression vs baseline {base}"
            )
    return failures


def _baseline_payload(results: list[dict[str, Any]], pass_rate: float) -> dict[str, Any]:
    counts = category_counts(results)
    rates = category_rates(results)
    return {
        "pass_rate": pass_rate,
        "case_count": len(results),
        "dataset": "gate_dataset.jsonl + golden_dataset.jsonl",
        "captured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tolerance": DEFAULT_TOLERANCE,
        "min_pass_rate": MIN_PASS_RATE,
        "per_category": {
            rubric: {
                "passed": counts[rubric],
                "total": len(results),
                "pass_rate": rates[rubric],
            }
            for rubric in RUBRICS
        },
        "note": (
            "Honest baseline for the LLM-free rubric gate: every golden case (13 seed cases "
            "in gate_dataset.jsonl + 40 F10b cases in golden_dataset.jsonl; >=8 per rubric "
            "across schema_valid/citation_present/factually_consistent/safe_refusal/"
            "no_phi_in_logs, incl. adversarial safe_refusal and planted-PHI no_phi_in_logs "
            "sensitivity cases) passes all five rubrics in the deterministic stubbed run. "
            "The gate blocks on FOUR rules: aggregate or per-category pass_rate below "
            "min_pass_rate (the absolute 'pass threshold' floor), or a >5% relative "
            "regression vs the aggregate/per-category baselines below. Because the run is "
            "deterministic, the floor is 100.0 — a single failing case out of 53 is only a "
            "1.89% dip and would slip through a percentage band alone. "
            "`python evals/gate.py --inject-regression` drops this to 0.0 (each case's "
            "target rubric flips), proving the gate is non-vacuous. Regenerate after a "
            "dataset change with `python evals/gate.py --write-baseline`."
        ),
    }


def _write_out(path: Path, results: list[dict[str, Any]], pass_rate: float) -> None:
    payload = {
        "pass_rate": pass_rate,
        "case_count": len(results),
        "rubrics": list(RUBRICS),
        "per_category": category_rates(results),
        "cases": results,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _coerce_rate(value: object) -> float | None:
    """A pass rate is a plain number; ``bool`` is not a rate."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _parse_per_category(data: dict[str, Any]) -> dict[str, float] | None:
    """Read per-category baselines; accept a bare rate or a {"pass_rate": ...} block."""
    raw = data.get("per_category", data.get("per_rubric"))
    if not isinstance(raw, dict):
        return None
    parsed: dict[str, float] = {}
    for rubric in RUBRICS:
        entry = raw.get(rubric)
        if isinstance(entry, dict):
            entry = entry.get("pass_rate")
        rate = _coerce_rate(entry)
        if rate is not None:
            parsed[rubric] = rate
    return parsed or None


def _load_baseline(explicit: Path | None) -> Baseline:
    path = explicit or COMMITTED_BASELINE
    if not path.is_file():
        print(
            f"eval-gate: baseline not found at {path}; skipping the relative "
            f"regression check (the absolute floor still applies)",
            file=sys.stderr,
        )
        return Baseline(path=path)
    try:
        data = json.loads(path.read_text())
    except ValueError:
        print(f"eval-gate: baseline {path} is not valid JSON", file=sys.stderr)
        return Baseline(path=path)
    if not isinstance(data, dict):
        print(f"eval-gate: baseline {path} is not a JSON object", file=sys.stderr)
        return Baseline(path=path)
    rate = _coerce_rate(data.get("pass_rate", data.get("overall_pass_rate")))
    if rate is None:
        print(f"eval-gate: baseline {path} carries no numeric pass_rate", file=sys.stderr)
    return Baseline(path=path, pass_rate=rate, per_category=_parse_per_category(data))


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM-free rubric eval gate.")
    parser.add_argument("--out", type=Path, default=None, help="write results JSON here")
    parser.add_argument("--baseline", type=Path, default=None, help="alternate baseline JSON")
    parser.add_argument(
        "--inject-regression",
        action="store_true",
        help="fault-inject a regression (self-proof the gate trips)",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        help="relative regression tolerance (default 0.05 = 5%%)",
    )
    parser.add_argument(
        "--min-pass-rate",
        type=float,
        default=MIN_PASS_RATE,
        help=f"absolute floor, aggregate and per-category (default {MIN_PASS_RATE})",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="rewrite the committed baseline from this (clean) run",
    )
    args = parser.parse_args()

    if args.write_baseline and args.inject_regression:
        print(
            "eval-gate: refusing to write a baseline from a fault-injected run",
            file=sys.stderr,
        )
        return 2

    results, pass_rate = evaluate(args.inject_regression)
    if args.out is not None:
        _write_out(args.out, results, pass_rate)

    counts = category_counts(results)
    rates = category_rates(results)
    print("eval-gate: LLM-free rubric gate")
    print(f"  cases       : {len(results)}")
    print(f"  pass_rate   : {pass_rate}")
    for rubric in RUBRICS:
        print(f"  {rubric:<22}: {counts[rubric]}/{len(results)} pass ({rates[rubric]}%)")
    if args.inject_regression:
        print("  MODE        : --inject-regression (fault injected)")

    if not results:
        print("eval-gate: no golden cases discovered; nothing to gate", file=sys.stderr)
        return 0

    if args.write_baseline:
        COMMITTED_BASELINE.write_text(
            json.dumps(_baseline_payload(results, pass_rate), indent=2) + "\n"
        )
        print(f"eval-gate: wrote baseline {COMMITTED_BASELINE.name} (pass_rate {pass_rate})")
        return 0

    baseline = _load_baseline(args.baseline)
    print(
        f"  baseline    : {baseline.pass_rate} (from {baseline.path.name}); "
        f"floor {args.min_pass_rate}, relative tolerance {round(args.tolerance * 100)}%"
    )

    failures = check_regressions(pass_rate, rates, baseline, args.tolerance, args.min_pass_rate)
    if failures:
        for failure in failures:
            print(f"eval-gate: BLOCKED — {failure}", file=sys.stderr)
        return 1
    print("eval-gate: OK — no blocking regression")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
