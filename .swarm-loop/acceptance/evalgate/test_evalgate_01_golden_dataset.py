"""feat_evalgate criterion 1 (F10b) — golden set >=50 cases with >=8 per rubric,
including adversarial safe_refusal cases and planted-PHI no_phi_in_logs cases.

FROZEN GOALS, deterministic file check over agent/evals/**/*.jsonl. A golden
case declares its target rubric via ``rubric`` / ``rubrics`` / ``category``
(one of the five rubric names). The Week-1 cases (invariant/boundary/
authorization categories) do not count toward the Week-2 golden set.
"""

from __future__ import annotations

import json
import re


def test_evalgate_01_golden_set_50_cases_8_per_rubric(golden_cases, rubrics):
    cases = golden_cases()
    assert len(cases) >= 50, (
        "the golden set must contain >=50 cases that each declare a target "
        f"rubric (via 'rubric'/'rubrics'/'category' in {set(rubrics)}) in JSONL "
        f"files under agent/evals/; found {len(cases)}"
    )

    counts = {r: 0 for r in rubrics}
    for _, _, targets in cases:
        for rubric in set(targets):
            counts[rubric] += 1
    thin = {r: n for r, n in counts.items() if n < 8}
    assert not thin, (
        f"every rubric needs >=8 targeted cases; too thin: {thin} "
        f"(full counts: {counts})"
    )

    # The no_phi_in_logs rubric must be exercised with PLANTED PHI, so a pass
    # is sensitivity-proven rather than vacuous.
    phi_cases = [obj for _, obj, targets in cases if "no_phi_in_logs" in targets]
    planted = [
        obj
        for obj in phi_cases
        if "planted_phi" in obj or re.search(r"plant", json.dumps(obj), re.I)
    ]
    assert planted, (
        "at least one no_phi_in_logs case must plant PHI (a 'planted_phi' "
        "field, or an explicit planted marker in the case) so the rubric "
        "detects leaks instead of passing vacuously"
    )

    # safe_refusal must include ADVERSARIAL cases (the case says so explicitly).
    refusal_cases = [obj for _, obj, targets in cases if "safe_refusal" in targets]
    adversarial = [
        obj
        for obj in refusal_cases
        if re.search(r"adversarial|jailbreak|prompt.?injection", json.dumps(obj), re.I)
    ]
    assert adversarial, (
        "at least one safe_refusal case must be explicitly adversarial "
        "(marked adversarial / jailbreak / prompt-injection), per the "
        "criterion's 'adversarial safe_refusal' requirement"
    )
