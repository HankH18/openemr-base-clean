"""Unit coverage for the LLM-free rubric eval gate.

Collected by the project's own pytest (testpaths = tests, evals). Runs fully
in-process, deterministic, no API key / network — the same properties the gate
itself guarantees. The frozen acceptance suite
(``.swarm-loop/acceptance/evalgate``) exercises the CLI contract as a
subprocess; this file locks the rubric logic and the fault-injection so a future
change that silently weakens a detector is caught here too.

The ``test_audited_hole_*`` tests below are regression locks on two holes a
gate audit found in the aggregate-only, relative-only rule:

1. a 1-2 case regression (98.11 / 96.23 vs a 100.0 baseline) exited 0;
2. per-category counts were computed and then never used to gate, so any one
   of the five rubrics could rot invisibly while the aggregate held.

Each test first asserts the OLD rule would have let the regression through
(proving the hole was real, not hypothetical), then asserts the current gate
blocks it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evals import gate
from evals.rubrics import (
    RUBRICS,
    count_phi,
    evaluate_record,
    inject_regression,
)

_EVALS_DIR = Path(__file__).resolve().parent


def test_clean_run_scores_100_and_matches_committed_baseline() -> None:
    results, pass_rate = gate.evaluate(inject=False)
    assert results, "the golden set must be non-empty"
    assert pass_rate == 100.0
    assert all(case["passed"] for case in results)

    baseline = json.loads((_EVALS_DIR / "gate_baseline.json").read_text())
    # An honest committed baseline: what a clean run actually scores.
    assert baseline["pass_rate"] == pass_rate


def test_committed_baseline_carries_honest_per_category_values() -> None:
    """The per-category baselines must match what a clean run actually scores."""
    results, _ = gate.evaluate(inject=False)
    rates = gate.category_rates(results)
    counts = gate.category_counts(results)

    baseline = gate._load_baseline(None)
    assert baseline.per_category is not None, (
        "the committed baseline must carry per_category baselines — without "
        "them the per-category rule silently degrades to a no-op"
    )
    assert baseline.per_category == rates

    raw = json.loads((_EVALS_DIR / "gate_baseline.json").read_text())
    for rubric in RUBRICS:
        assert raw["per_category"][rubric]["passed"] == counts[rubric]
        assert raw["per_category"][rubric]["total"] == len(results)


def test_every_case_emits_all_five_rubric_booleans() -> None:
    results, _ = gate.evaluate(inject=False)
    for case in results:
        for rubric in RUBRICS:
            assert isinstance(case[rubric], bool), f"{case['id']} missing bool {rubric}"


def test_injection_trips_a_blocking_regression() -> None:
    _, clean = gate.evaluate(inject=False)
    _, injected = gate.evaluate(inject=True)
    assert injected < clean, "the injected run must record a measurably lower pass_rate"
    # A >5% relative regression vs a 100% baseline blocks.
    assert injected < 100.0 * (1.0 - gate.DEFAULT_TOLERANCE)


def test_injection_is_isolated_per_target_rubric() -> None:
    """Each injected case flips ONLY its target rubric — a per-detector proof."""
    injected, _ = _to_map(gate.evaluate(inject=True)[0])
    clean, _ = _to_map(gate.evaluate(inject=False)[0])
    for case_id, booleans in injected.items():
        targets = booleans["rubrics"]
        for rubric in RUBRICS:
            expected = rubric not in targets  # only the target rubric flips to False
            assert booleans[rubric] is expected, (
                f"{case_id}: rubric {rubric} expected {expected} under injection "
                f"(targets={targets})"
            )
        # The clean counterpart passes every rubric.
        assert all(clean[case_id][r] for r in RUBRICS)


def test_phi_scanner_catches_planted_phi() -> None:
    """Anti-vacuous self-proof: the PHI detector flags clearly-synthetic PHI."""
    planted = "patient_name: Jane Q. Public MRN: 000123456 SSN 999-00-1234 born 01/02/1980"
    assert count_phi(planted) >= 3
    assert count_phi("event=chat.answer correlation_id=req-abc123 subject=redacted") == 0


def test_no_phi_injection_uses_case_planted_phi() -> None:
    record = {"answer": "ok", "claims": [], "log": "clean=line"}
    corrupted = inject_regression(record, "no_phi_in_logs", planted_phi="SSN 999-00-1234")
    assert evaluate_record(record)["no_phi_in_logs"] is True
    assert evaluate_record(corrupted)["no_phi_in_logs"] is False


# --- the gate's blocking rules ----------------------------------------------


def test_clean_run_passes_every_blocking_rule() -> None:
    """The gate is not blocking-by-construction: a clean run reports no failures."""
    results, pass_rate = gate.evaluate(inject=False)
    failures = gate.check_regressions(
        pass_rate, gate.category_rates(results), gate._load_baseline(None)
    )
    assert failures == []


def test_audited_hole_1_single_case_regression_blocks() -> None:
    """AUDIT HOLE 1: a 1-case regression (98.11) used to exit 0. It must block."""
    results, _ = gate.evaluate(inject=False)
    degraded = _degrade(results, "schema_valid", 1)
    pass_rate = _pass_rate(degraded)
    assert pass_rate == 98.11, "one failing case out of 53 scores 98.11"

    # The hole was real: the OLD rule (aggregate, >5% relative, no floor)
    # tolerated this — 98.11 sits above the 95.0 block threshold.
    assert pass_rate >= 100.0 * (1.0 - gate.DEFAULT_TOLERANCE)

    # Closed: the absolute floor now blocks it.
    failures = gate.check_regressions(
        pass_rate, gate.category_rates(degraded), gate._load_baseline(None)
    )
    assert failures, "a single-case regression must now block"
    assert any("pass threshold" in f for f in failures)


def test_audited_hole_1_two_case_regression_blocks() -> None:
    """AUDIT HOLE 1: a 2-case regression (96.23) used to exit 0. It must block."""
    results, _ = gate.evaluate(inject=False)
    degraded = _degrade(results, "citation_present", 2)
    pass_rate = _pass_rate(degraded)
    assert pass_rate == 96.23, "two failing cases out of 53 score 96.23"
    assert pass_rate >= 100.0 * (1.0 - gate.DEFAULT_TOLERANCE)  # old rule: tolerated

    failures = gate.check_regressions(
        pass_rate, gate.category_rates(degraded), gate._load_baseline(None)
    )
    assert failures, "a two-case regression must now block"
    # The failing category is named, not just the aggregate.
    assert any("citation_present" in f for f in failures)


def test_audited_hole_2_single_category_regression_blocks_and_names_it() -> None:
    """AUDIT HOLE 2: one rubric rotting must block, and the message must name it.

    Two cases lose ONLY ``safe_refusal``; the other four rubrics stay perfect.
    """
    results, _ = gate.evaluate(inject=False)
    degraded = _degrade(results, "safe_refusal", 2)
    rates = gate.category_rates(degraded)
    assert rates["safe_refusal"] == 96.23
    assert all(rates[r] == 100.0 for r in RUBRICS if r != "safe_refusal"), (
        "only the targeted category regresses"
    )

    failures = gate.check_regressions(_pass_rate(degraded), rates, gate._load_baseline(None))
    named = [f for f in failures if "safe_refusal" in f]
    assert named, f"the blocking message must name the regressed category; got {failures}"
    # No other category is blamed.
    for rubric in RUBRICS:
        if rubric != "safe_refusal":
            assert not any(rubric in f for f in failures)


def test_audited_hole_2_per_category_rule_fires_where_aggregate_does_not() -> None:
    """The per-category RELATIVE rule carries signal the aggregate rule cannot.

    Against a non-uniform baseline (aggregate 90, safe_refusal 100), a run can
    hold the aggregate inside its 5% band while one category blows through its
    own. The floor is disabled here so ONLY the relative rules are live —
    isolating rule (4), the one the audit found unimplemented.
    """
    baseline = gate.Baseline(
        path=Path("synthetic"),
        pass_rate=90.0,
        per_category={**{r: 90.0 for r in RUBRICS}, "safe_refusal": 100.0},
    )
    rates = {**{r: 92.0 for r in RUBRICS}, "safe_refusal": 92.0}

    failures = gate.check_regressions(88.0, rates, baseline, min_pass_rate=0.0)
    # Aggregate 88 vs baseline 90 is only a 2.2% dip — the aggregate rule is silent.
    assert not any(f.startswith("aggregate") for f in failures)
    # safe_refusal 92 vs its own 100 baseline is an 8% dip — blocked, and named.
    assert any("safe_refusal" in f and "relative regression" in f for f in failures)


def test_baseline_without_per_category_still_applies_the_floor() -> None:
    """A legacy baseline (pass_rate only) must not disarm the absolute floor."""
    baseline = gate.Baseline(path=Path("legacy"), pass_rate=100.0, per_category=None)
    assert gate.check_regressions(100.0, {r: 100.0 for r in RUBRICS}, baseline) == []
    failures = gate.check_regressions(
        98.11, {**{r: 100.0 for r in RUBRICS}, "schema_valid": 98.11}, baseline
    )
    assert any("pass threshold" in f for f in failures)


def test_missing_baseline_still_applies_the_floor() -> None:
    """An absent baseline skips the RELATIVE checks; the floor is unconditional."""
    baseline = gate._load_baseline(Path("/nonexistent/baseline.json"))
    assert baseline.pass_rate is None
    failures = gate.check_regressions(
        50.0, {r: 50.0 for r in RUBRICS}, baseline, min_pass_rate=gate.MIN_PASS_RATE
    )
    assert failures, "a missing baseline must not disarm the absolute floor"


def test_write_baseline_refuses_a_fault_injected_run(monkeypatch) -> None:
    """A poisoned baseline would silently disarm the gate — refuse to write one."""
    monkeypatch.setattr("sys.argv", ["gate.py", "--write-baseline", "--inject-regression"])
    before = (_EVALS_DIR / "gate_baseline.json").read_text()
    assert gate.main() != 0
    assert (_EVALS_DIR / "gate_baseline.json").read_text() == before


# --- helpers ----------------------------------------------------------------


def _degrade(results: list[dict[str, Any]], rubric: str, count: int) -> list[dict[str, Any]]:
    """Flip ``rubric`` to False on the first ``count`` cases, recomputing ``passed``.

    Simulates a real regression in one detector against the real golden set.
    """
    degraded = [dict(case) for case in results]
    for case in degraded[:count]:
        case[rubric] = False
        case["passed"] = all(bool(case[r]) for r in RUBRICS)
    return degraded


def _pass_rate(results: list[dict[str, Any]]) -> float:
    return round(100.0 * sum(1 for c in results if c["passed"]) / len(results), 2)


def _to_map(results: list[dict[str, object]]) -> tuple[dict[str, dict[str, object]], None]:
    return {str(case["id"]): case for case in results}, None
