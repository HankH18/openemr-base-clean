"""feat_evalgate criterion 2 (F10a) — the gate runner emits ALL 5 rubric
booleans per case (schema_valid, citation_present, factually_consistent,
safe_refusal, no_phi_in_logs), stubbed/LLM-free.

FROZEN GOALS. The run happens in a subprocess with every API key stripped, so
a runner that needs a live model cannot pass. The exit code is NOT asserted
here (a regression verdict is criterion 3's concern) — only the per-case
record shape and the overall pass_rate emission.
"""

from __future__ import annotations


def test_evalgate_02_runner_emits_five_booleans_per_case(run_gate, load_results, tmp_path, rubrics):
    out = tmp_path / "gate_results.json"
    run_gate("--out", str(out))

    cases, pass_rate = load_results(out)
    assert cases, "gate results must contain at least one case record"
    for i, case in enumerate(cases):
        assert isinstance(case, dict), f"cases[{i}] must be an object; got {type(case).__name__}"
        bad = [r for r in rubrics if not isinstance(case.get(r), bool)]
        assert not bad, (
            f"cases[{i}] (id={case.get('id', '?')!r}) must emit ALL 5 rubric "
            f"booleans; missing/non-boolean: {bad}; got keys {sorted(case)}"
        )
    assert isinstance(pass_rate, (int, float)) and not isinstance(pass_rate, bool), (
        "the results JSON must carry a numeric overall 'pass_rate' (0..100) — "
        "the quantity the baseline comparison is defined over"
    )
