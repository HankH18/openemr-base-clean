"""FIX 1 — a delta between two finite-but-enormous readings must not crash.

Two opposite-sign readings near the float limit (``±1e308``) each pass
``_numeric``'s ``math.isfinite`` guard individually, but their difference
``latest - prior`` overflows to ``inf``. That ``inf`` reached ``_fmt_num``'s
``int(x)`` → ``OverflowError``, which — ``RoundsService.start`` having no
try/except — 500s the whole round. Two defenses complete the R3 non-finite
work: ``_trend`` guards the delta (prints an "out of representable range" hint
instead of an arrow), and ``_fmt_num`` is made non-finite-safe.
"""

from __future__ import annotations

from typing import Any

from copilot.rounds.summary import _fmt_num, build_change_claims, build_summary_claims


def _obs(rid: str, name: str, value: float, when: str, unit: str = "/min") -> dict[str, Any]:
    return {
        "resourceType": "Observation",
        "id": rid,
        "code": {"coding": [{"display": name}]},
        "valueQuantity": {"value": value, "unit": unit},
        "effectiveDateTime": when,
    }


def test_summary_opposite_extreme_deltas_do_not_crash() -> None:
    # latest=+1e308, prior=-1e308 → delta = 2e308 = inf → int(inf) pre-fix.
    resources = [
        _obs("hr-2", "Heart rate", 1e308, "2026-07-11T05:00:00Z"),  # latest
        _obs("hr-1", "Heart rate", -1e308, "2026-07-10T05:00:00Z"),  # prior
    ]
    claims = build_summary_claims(resources)
    assert len(claims) == 1
    text = claims[0].text
    # The row still renders, headed by the metric label.
    assert text.startswith("Heart Rate:")
    # No fabricated numeric delta against an unrepresentable difference; the guard
    # branch says so out loud instead.
    assert "↑" not in text and "↓" not in text
    assert "representable" in text


def test_change_opposite_extreme_deltas_do_not_crash() -> None:
    # build_change_claims gates the row on _changed (the two readings differ) and
    # then renders _trend — the same overflow path as the summary card.
    resources = [
        _obs("g-2", "Glucose", -1e308, "2026-07-11T12:00:00Z", unit="mg/dL"),  # latest
        _obs("g-1", "Glucose", 1e308, "2026-07-11T05:00:00Z", unit="mg/dL"),  # prior
    ]
    claims = build_change_claims(resources)
    assert len(claims) == 1
    text = claims[0].text
    assert "↑" not in text and "↓" not in text
    assert "representable" in text


def test_fmt_num_is_non_finite_safe() -> None:
    # Defense-in-depth: any non-finite operand renders as its repr, never crashes.
    assert _fmt_num(float("inf")) == "inf"
    assert _fmt_num(float("-inf")) == "-inf"
    assert _fmt_num(float("nan")) == "nan"
    # The ordinary finite path is unchanged.
    assert _fmt_num(12.0) == "12"
    assert _fmt_num(0.03, 2) == "0.03"


def test_finite_extreme_pair_without_overflow_still_trends() -> None:
    # Two large but same-sign readings whose difference stays finite must still
    # render an ordinary arrow — the guard must not swallow representable deltas.
    resources = [
        _obs("hr-2", "Heart rate", 2e307, "2026-07-11T05:00:00Z"),  # latest
        _obs("hr-1", "Heart rate", 1e307, "2026-07-10T05:00:00Z"),  # prior
    ]
    text = build_summary_claims(resources)[0].text
    assert "↑" in text
    assert "representable" not in text
