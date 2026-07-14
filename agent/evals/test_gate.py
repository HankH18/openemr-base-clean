"""Unit coverage for the LLM-free rubric eval gate.

Collected by the project's own pytest (testpaths = tests, evals). Runs fully
in-process, deterministic, no API key / network — the same properties the gate
itself guarantees. The frozen acceptance suite
(``.swarm-loop/acceptance/evalgate``) exercises the CLI contract as a
subprocess; this file locks the rubric logic and the fault-injection so a future
change that silently weakens a detector is caught here too.
"""

from __future__ import annotations

import json
from pathlib import Path

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


def _to_map(results: list[dict[str, object]]) -> tuple[dict[str, dict[str, object]], None]:
    return {str(case["id"]): case for case in results}, None
