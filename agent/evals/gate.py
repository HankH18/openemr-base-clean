#!/usr/bin/env python3
"""Blocking, LLM-free eval GATE for the Clinical Co-Pilot.

The blocking tier of the two-tier eval strategy: a deterministic, stubbed
(no Anthropic key, no network) runner over the Week-2 golden set that emits the
five rubric booleans per case, computes an overall ``pass_rate``, and compares
it against a committed baseline. It exits NONZERO on a > 5% *relative*
regression so a git pre-push hook and the GitLab ``agent:tests`` CI job can
block the change.

CLI contract (pinned by ``.swarm-loop/acceptance/evalgate``):

    python evals/gate.py [--out PATH] [--baseline PATH] [--inject-regression]

- ``--out PATH``     write the machine-readable results JSON
  (``{"pass_rate": <0..100>, "cases": [{..., "<rubric>": bool, ...}]}``).
- ``--baseline PATH`` compare against an alternate baseline JSON carrying a
  ``pass_rate``; without it the committed ``gate_baseline.json`` is used.
- ``--inject-regression`` deterministically breaks each golden case's target
  rubric (fault injection) so the gate provably trips — the self-proof.
- Exit 0 = no blocking regression; nonzero = regression detected.

The golden set is every ``*.jsonl`` case under ``agent/evals/`` (filename not
containing "result") that declares a target rubric via ``rubric`` / ``rubrics``
/ ``category``. Week-1 grounding cases (invariant/boundary/authorization) carry
no rubric field and are ignored here. Growing the set to >= 50 cases is task
F10b — this runner is stable against that growth.

@package   OpenEMR
@link      https://www.open-emr.org
@license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
"""

from __future__ import annotations

import argparse
import json
import sys
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


def _write_out(path: Path, results: list[dict[str, Any]], pass_rate: float) -> None:
    payload = {
        "pass_rate": pass_rate,
        "case_count": len(results),
        "rubrics": list(RUBRICS),
        "cases": results,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _load_baseline(explicit: Path | None) -> tuple[float | None, Path]:
    path = explicit or COMMITTED_BASELINE
    if not path.is_file():
        print(f"eval-gate: baseline not found at {path}; skipping regression check", file=sys.stderr)
        return None, path
    try:
        data = json.loads(path.read_text())
    except ValueError:
        print(f"eval-gate: baseline {path} is not valid JSON", file=sys.stderr)
        return None, path
    rate = data.get("pass_rate", data.get("overall_pass_rate")) if isinstance(data, dict) else None
    if not isinstance(rate, (int, float)) or isinstance(rate, bool):
        print(f"eval-gate: baseline {path} carries no numeric pass_rate", file=sys.stderr)
        return None, path
    return float(rate), path


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
    args = parser.parse_args()

    results, pass_rate = evaluate(args.inject_regression)
    if args.out is not None:
        _write_out(args.out, results, pass_rate)

    per_rubric = {r: sum(1 for c in results if c[r]) for r in RUBRICS}
    print("eval-gate: LLM-free rubric gate")
    print(f"  cases       : {len(results)}")
    print(f"  pass_rate   : {pass_rate}")
    for rubric in RUBRICS:
        print(f"  {rubric:<22}: {per_rubric[rubric]}/{len(results)} pass")
    if args.inject_regression:
        print("  MODE        : --inject-regression (fault injected)")

    if not results:
        print("eval-gate: no golden cases discovered; nothing to gate", file=sys.stderr)
        return 0

    baseline_rate, baseline_path = _load_baseline(args.baseline)
    if baseline_rate is None:
        return 0

    threshold = baseline_rate * (1.0 - args.tolerance)
    regressed = pass_rate < threshold
    print(
        f"  baseline    : {baseline_rate} (from {baseline_path.name}); "
        f"block threshold {round(threshold, 2)}"
    )
    if regressed:
        print(
            f"eval-gate: BLOCKED — pass_rate {pass_rate} is a >"
            f"{round(args.tolerance * 100)}% relative regression vs baseline {baseline_rate}",
            file=sys.stderr,
        )
        return 1
    print("eval-gate: OK — no blocking regression")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
