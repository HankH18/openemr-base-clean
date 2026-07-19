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


# --- the LIVE tier ----------------------------------------------------------
#
# The audited defect: this gate graded a pre-baked `record` field and never
# imported the agent, so an auditor could turn `deidentify` into an identity
# function (PHI scrubbing OFF) and `_passed_claims` into `[]` (every answer
# uncited) and still get `no_phi_in_logs 53/53`, `citation_present 53/53`,
# pass_rate 100.0, exit 0. These tests lock the tier that closes it: cases whose
# graded value is produced by REAL copilot code at gate time. The sabotage tests
# below are the in-process form of that proof — if either stops going red, the
# gate has gone vacuous again.


def test_fixture_tier_still_carries_the_full_committed_corpus() -> None:
    """The live tier ADDS to the golden set; it must never shrink it.

    Pins the property the 98.11 / 96.23 arithmetic above encodes — the fixture
    corpus is 53 cases, comfortably over the spec's >=50 floor — so that the
    audited-hole regression locks keep measuring what they were written to
    measure.
    """
    fixture, rate = gate.evaluate(inject=False)
    assert len(fixture) == 53
    assert len(fixture) >= 50, "the spec's golden-set floor"
    assert rate == 100.0
    assert all(case["live"] is False for case in fixture)


def test_live_tier_is_non_empty_and_green_on_healthy_code() -> None:
    live, rate = gate.evaluate_live(inject=False)
    assert live, "an empty live tier is the audited defect — the gate would grade fixtures only"
    assert rate == 100.0, f"healthy code must score 100 on the live tier; got {rate}"
    assert all(case["live"] is True for case in live)


def test_live_tier_covers_every_rubric() -> None:
    """At minimum one live case per rubric — no rubric may be fixture-only."""
    live, _ = gate.evaluate_live(inject=False)
    covered = {rubric for case in live for rubric in case["rubrics"]}
    assert covered == set(RUBRICS), f"rubrics with no live case: {set(RUBRICS) - covered}"


def test_every_live_case_emits_all_five_rubric_booleans() -> None:
    live, _ = gate.evaluate_live(inject=False)
    for case in live:
        for rubric in RUBRICS:
            assert isinstance(case[rubric], bool), f"{case['id']} missing bool {rubric}"


def test_evaluate_all_merges_both_tiers() -> None:
    fixture, _ = gate.evaluate(inject=False)
    live, _ = gate.evaluate_live(inject=False)
    merged, rate = gate.evaluate_all(inject=False)
    assert len(merged) == len(fixture) + len(live)
    assert rate == 100.0
    assert sum(1 for c in merged if c["live"]) == len(live)


def test_sabotaging_the_real_deidentify_turns_the_gate_red() -> None:
    """PHI scrubbing OFF must fail `no_phi_in_logs`. The auditor's sabotage #1.

    The live log line is the OUTPUT of the real scrub, so neutering the scrub
    lets the probe's identifiers through and the gate's independent PHI detector
    flags them. Before the live tier existed this exact sabotage scored 53/53.
    """
    import evals.live_cases as live_mod

    original = live_mod.deidentify
    try:
        live_mod.deidentify = lambda text: text  # PHI scrubbing OFF
        live, rate = gate.evaluate_live(inject=False)
    finally:
        live_mod.deidentify = original

    assert rate < 100.0, "an identity `deidentify` must not score 100 on the live tier"
    leaked = [c["id"] for c in live if not c["no_phi_in_logs"]]
    assert leaked, "no_phi_in_logs must go red when the real scrub is disabled"
    failures = gate.check_regressions(
        100.0, dict.fromkeys(RUBRICS, 100.0), gate._load_baseline(None), live_results=live
    )
    assert failures, "rule (5) must block a failing live case even at a perfect fixture rate"


def test_sabotaging_the_real_passed_claims_turns_the_gate_red() -> None:
    """Every answer uncited must fail `citation_present`. The auditor's sabotage #2.

    Patches the production symbol `copilot.chat.service._passed_claims`, which
    `_answer_inline` resolves at call time — so this is the real function being
    sabotaged in-process, not a stand-in.
    """
    import copilot.chat.service as chat_service

    original = chat_service._passed_claims
    try:
        chat_service._passed_claims = lambda result: []  # every answer uncited
        live, rate = gate.evaluate_live(inject=False)
    finally:
        chat_service._passed_claims = original

    assert rate < 100.0, "an empty `_passed_claims` must not score 100 on the live tier"
    uncited = [c["id"] for c in live if not c["citation_present"]]
    assert uncited, "citation_present must go red when the real citation path returns no claims"


def test_sabotaging_value_match_turns_the_drift_case_red() -> None:
    """A drifted-value claim must be WITHHELD; a permissive value gate serving it
    is the fail-open regression the LIVE tier must catch.

    The live drift case (`live-value-drift-withheld`) builds a claim citing
    trop-1's value as 9.99 while the fake record holds 2.34, then runs it through
    the REAL serve-time verifier. Honest code withholds it, so the case is green.
    Monkeypatching `copilot.verification.core._values_equal` to always return True
    is the one-line fail-open sabotage: the drifted claim is then served, the case
    goes red, and the live tier drops below 100. Before this case existed the SAME
    sabotage still scored 100.0 on the live tier — the coverage gap this closes.
    """
    import copilot.verification.core as core

    drift_id = "live-value-drift-withheld"

    # Without the monkeypatch the drift case PASSES (the claim is withheld).
    live_clean, rate_clean = gate.evaluate_live(inject=False)
    clean = {c["id"]: c for c in live_clean}
    assert drift_id in clean, "the value-drift live case must be present in the tier"
    assert clean[drift_id]["passed"], "healthy code must withhold the drifted claim"
    assert rate_clean == 100.0

    original = core._values_equal
    try:
        core._values_equal = lambda source, claimed: True  # value gate fail-open
        live, rate = gate.evaluate_live(inject=False)
    finally:
        core._values_equal = original

    assert rate < 100.0, "a permissive value gate must not score 100 on the live tier"
    sabotaged = {c["id"]: c for c in live}[drift_id]
    assert not sabotaged["passed"], "the drift case must go red when value-match is sabotaged"
    assert not sabotaged["safe_refusal"], (
        "a served drifted claim breaks the expected refusal"
    )
    # Rule (5) holds the failing live case regardless of the fixture rate / knobs.
    failures = gate.check_regressions(
        100.0, dict.fromkeys(RUBRICS, 100.0), gate._load_baseline(None), live_results=live
    )
    assert failures, "rule (5) must block the failing drift live case"


def test_rule_5_blocks_a_failing_live_case_regardless_of_the_knobs() -> None:
    """A live case is a binary assertion, so no tolerance/floor setting excuses it.

    Rule (5) is what stops a loosened `--min-pass-rate` from buying a broken
    live probe a pass: here the floor is disabled and the tolerance is wide open,
    and the failing live case still blocks.
    """
    failing = [{"id": "live-x", "passed": False, **dict.fromkeys(RUBRICS, True), "no_phi_in_logs": False}]
    failures = gate.check_regressions(
        100.0,
        dict.fromkeys(RUBRICS, 100.0),
        gate.Baseline(path=Path("synthetic"), pass_rate=100.0),
        tolerance=1.0,
        min_pass_rate=0.0,
        live_results=failing,
    )
    assert any("LIVE case 'live-x'" in f for f in failures)
    assert any("no_phi_in_logs" in f for f in failures), "the failing rubric must be named"


def test_rule_5_is_silent_when_every_live_case_passes() -> None:
    """Not blocking-by-construction: a healthy live tier adds no failures."""
    live, _ = gate.evaluate_live(inject=False)
    failures = gate.check_regressions(
        100.0, dict.fromkeys(RUBRICS, 100.0), gate._load_baseline(None), live_results=live
    )
    assert failures == []


def test_check_regressions_without_live_results_is_unchanged() -> None:
    """Rule (5) is opt-in: the four original rules behave exactly as before."""
    baseline = gate.Baseline(path=Path("synthetic"), pass_rate=100.0)
    assert gate.check_regressions(100.0, dict.fromkeys(RUBRICS, 100.0), baseline) == []
    assert gate.check_regressions(100.0, dict.fromkeys(RUBRICS, 100.0), baseline, live_results=[]) == []


def test_live_harness_error_fails_closed() -> None:
    """A live tier that cannot run must BLOCK, never silently vanish.

    An ImportError or a raised production exception collapsing the live tier to
    "no cases" would restore the vacuous pass this tier exists to kill.
    """
    import evals.live_cases as live_mod

    original = live_mod._build
    try:

        async def _boom() -> list[dict[str, Any]]:
            raise RuntimeError("copilot exploded")

        live_mod._build = _boom
        cases = live_mod.live_cases()
    finally:
        live_mod._build = original

    assert len(cases) == 1
    assert cases[0]["id"] == "live-harness-error"
    assert "copilot exploded" in cases[0]["error"]
    booleans = evaluate_record(cases[0]["record"])
    assert not booleans["schema_valid"], "an empty envelope must fail schema_valid"
    assert not booleans["citation_present"], "an empty envelope must fail citation_present"


def test_live_case_records_come_from_real_code_not_fixtures() -> None:
    """Anti-vacuous: the live records must carry real agent output.

    Guards against a future 'live' tier that quietly hardcodes its records —
    the served claim's citation must be the real FHIR resource the fake record
    holds, with the value read from it by the real extractor.
    """
    import evals.live_cases as live_mod

    cases = live_mod.live_cases()
    by_id = {case["id"]: case for case in cases}
    served = by_id["live-citation-present"]["record"]
    assert served["claims"], "the live served turn must carry real claims"
    citation = served["claims"][0]["citation"]
    assert citation["source_type"] == "fhir"
    assert citation["resource_id"] == "trop-1"
    assert citation["value"] == "2.34", "the cited value must come from the real resource"
    assert served["claims"][0]["source_value"] == "2.34"
    # The live log is the real scrub's output: the probe's PHI is gone.
    assert count_phi(served["log"]) == 0
    assert "Marisol" not in served["log"] and "123-45-6789" not in served["log"]
    # And the probe itself is genuinely PHI-bearing — otherwise the scrub proves nothing.
    assert count_phi(live_mod.PHI_PROBE) >= 3
