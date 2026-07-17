"""feat_evalgate criterion 3 (F10a) — baseline + >5% RELATIVE regression =>
nonzero exit; equal-or-tolerable results => exit 0.

FROZEN GOALS. Exercises the ``--baseline PATH`` mechanics in both directions,
independent of the currently-achieved quality level:

- baseline == the run's own measured pass_rate      -> exit 0 (no regression);
- baseline = current * 1.03 (inside 5% tolerance)   -> exit 0 (dip tolerated);
- baseline = 100 with ``--inject-regression``       -> nonzero (blocking).
"""

from __future__ import annotations

import json


def test_evalgate_03_baseline_and_regression_exit_nonzero(run_gate, load_results, tmp_path):
    out = tmp_path / "current.json"
    run_gate("--out", str(out))
    _, current = load_results(out)
    assert isinstance(current, (int, float)) and not isinstance(current, bool), (
        "gate results must carry a numeric 'pass_rate' — required for the "
        "baseline comparison"
    )
    current = float(current)

    same = tmp_path / "baseline_same.json"
    same.write_text(json.dumps({"pass_rate": current}))
    no_regression = run_gate("--baseline", str(same))
    assert no_regression.returncode == 0, (
        f"pass_rate identical to the baseline is NOT a regression — the gate "
        f"must exit 0; rc={no_regression.returncode}\n"
        f"stderr tail: {no_regression.stderr[-300:]}"
    )

    tolerated = tmp_path / "baseline_within_5pct.json"
    tolerated.write_text(json.dumps({"pass_rate": min(100.0, current * 1.03)}))
    small_dip = run_gate("--baseline", str(tolerated))
    assert small_dip.returncode == 0, (
        f"a dip WITHIN the 5% relative tolerance must not block — the gate "
        f"must exit 0; rc={small_dip.returncode}\n"
        f"stderr tail: {small_dip.stderr[-300:]}"
    )

    perfect = tmp_path / "baseline_high.json"
    perfect.write_text(json.dumps({"pass_rate": 100.0}))
    tripped = run_gate("--baseline", str(perfect), "--inject-regression")
    assert tripped.returncode != 0, (
        "a >5% relative regression vs the baseline (fault-injected run against "
        "a 100% baseline) must exit NONZERO — this is the PR-blocking behavior"
    )
