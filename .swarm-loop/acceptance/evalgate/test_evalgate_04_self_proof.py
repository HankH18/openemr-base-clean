"""feat_evalgate criterion 4 (F10a) — injected-regression self-proof: the
harness injects a known regression (the gate's own ``--inject-regression``
fault-injection flag) and asserts the gate exits nonzero against its COMMITTED
baseline, while a clean run against that same committed baseline exits 0.

FROZEN GOALS. This is the sensitivity proof: a gate that can no longer detect
its own injected regression (or whose committed baseline is dishonest) fails
here, even if every other criterion is green.
"""

from __future__ import annotations


def test_evalgate_04_injected_regression_trips_gate(run_gate, load_results, tmp_path):
    clean_out = tmp_path / "clean.json"
    clean = run_gate("--out", str(clean_out))
    assert clean.returncode == 0, (
        "against its COMMITTED baseline, a clean stubbed run must pass (exit "
        "0) — commit an honest baseline artifact for the current golden set; "
        f"rc={clean.returncode}\nstderr tail: {clean.stderr[-300:]}"
    )

    injected_out = tmp_path / "injected.json"
    injected = run_gate("--inject-regression", "--out", str(injected_out))
    assert injected.returncode != 0, (
        "SELF-PROOF: --inject-regression must make the gate exit nonzero "
        "against the committed baseline — a gate that cannot catch its own "
        "injected regression proves nothing"
    )

    # The injection must be a real, recorded regression (measurably lower).
    _, clean_rate = load_results(clean_out)
    _, injected_rate = load_results(injected_out)
    if isinstance(clean_rate, (int, float)) and isinstance(injected_rate, (int, float)):
        assert float(injected_rate) < float(clean_rate), (
            f"the injected run must record a measurably lower pass_rate than "
            f"the clean run (clean={clean_rate}, injected={injected_rate})"
        )
