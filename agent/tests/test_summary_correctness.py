"""Correctness guards for the rounds card: the card must never state a falsehood.

Each class here pins one defect that was **observed rendering false text to a
clinician**, not a hypothetical:

1. ``TestDeltaPrecision`` — a doubling troponin printed ``↑0.0``. Troponin's whole
   reference band is ``<0.04``, so a hardcoded one-decimal delta rendered every
   clinically decisive move as "up by zero".
2. ``TestFutureDatedReading`` — one glucose mistyped as 2027 anchored the
   "since you last saw" window into the future, so every real reading fell
   outside it and the card emptied. The UI renders an empty card as the
   affirmative "No recorded changes since your last review", so a brand-new
   tachycardia was replaced by a sentence saying nothing had changed.
3. ``TestMixedUnits`` / ``TestSeriesUnits`` — the same temperature recorded ``Cel``
   then ``degF`` read as "↑61.6 · improving", and the series endpoint labelled
   every point with the first reading's unit.

The regression guards (``TestSameUnitTrendUnchanged``) exist because each fix
withholds output in a bad case; they prove nothing was withheld in the good case.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from copilot.domain.primitives import ResourceType, utcnow
from copilot.rounds.summary import build_change_claims, build_summary_claims


def _obs(
    rid: str,
    name: str,
    value: float,
    when: str,
    unit: str = "/min",
    *,
    interp: str | None = None,
    high: float | None = None,
) -> dict[str, Any]:
    res: dict[str, Any] = {
        "resourceType": "Observation",
        "id": rid,
        "code": {"coding": [{"display": name}]},
        "valueQuantity": {"value": value, "unit": unit},
        "effectiveDateTime": when,
    }
    if interp is not None:
        res["interpretation"] = [{"coding": [{"code": interp}]}]
    if high is not None:
        res["referenceRange"] = [{"high": {"value": high}}]
    return res


def _only_text(resources: list[dict[str, Any]]) -> str:
    claims = build_summary_claims(resources)
    assert len(claims) == 1
    return claims[0].text


# --- Defect 1: delta precision ---------------------------------------------


class TestDeltaPrecision:
    """A delta is never rendered coarser than the values it was derived from."""

    def test_troponin_rise_0p01_to_0p04_is_not_up_by_zero(self) -> None:
        # Observed: 'Troponin I: 0.04 ng/mL  ↑0.0 · 3h since prior' — a 4x rise,
        # flagged H, reported as no movement at all.
        resources = [
            _obs("t1", "Troponin I", 0.01, "2026-07-10T02:00:00Z", "ng/mL", high=0.04),
            _obs("t2", "Troponin I", 0.04, "2026-07-10T05:00:00Z", "ng/mL", interp="H", high=0.04),
        ]
        text = _only_text(resources)
        assert "↑0.0 " not in text and not text.endswith("↑0.0")
        assert "↑0.03" in text, text
        assert "3h since prior" in text

    def test_troponin_rise_0p04_to_0p08_is_not_up_by_zero(self) -> None:
        # Observed: 'Troponin I: 0.08 ng/mL  ↑0.0 · 3h since prior', severity
        # critical (HH) — the serial rise that rules in MI, printed as zero.
        resources = [
            _obs("t1", "Troponin I", 0.04, "2026-07-10T02:00:00Z", "ng/mL", high=0.04),
            _obs("t2", "Troponin I", 0.08, "2026-07-10T05:00:00Z", "ng/mL", interp="HH", high=0.04),
        ]
        text = _only_text(resources)
        assert "↑0.0 " not in text and not text.endswith("↑0.0")
        assert "↑0.04" in text, text

    def test_sub_unit_deltas_keep_the_source_precision(self) -> None:
        # TSH, digoxin, bilirubin, magnesium — same failure class as troponin.
        resources = [
            _obs("d1", "Digoxin", 1.8, "2026-07-10T02:00:00Z", "ng/mL"),
            _obs("d2", "Digoxin", 2.45, "2026-07-10T05:00:00Z", "ng/mL"),
        ]
        assert "↑0.65" in _only_text(resources)

    def test_integer_delta_still_renders_as_an_integer(self) -> None:
        # The .1f formatter got this right; a naive .3g fix would not. Guard both:
        # no '92.0', and no '1.23e+03' from significant-figure formatting.
        resources = [
            _obs("hr-2", "Heart rate", 104, "2026-07-09T05:00:00Z"),
            _obs("hr-1", "Heart rate", 92, "2026-07-10T05:00:00Z"),
        ]
        text = _only_text(resources)
        assert "↓12 " in text
        assert "↓12.0" not in text

    def test_large_integer_delta_is_not_scientific_notation(self) -> None:
        resources = [
            _obs("p1", "Platelet count", 1000, "2026-07-09T05:00:00Z", "K/uL"),
            _obs("p2", "Platelet count", 2234, "2026-07-10T05:00:00Z", "K/uL"),
        ]
        text = _only_text(resources)
        assert "↑1234 " in text, text
        assert "e+" not in text

    def test_whole_number_value_from_decimal_sources_renders_whole(self) -> None:
        # Operands carry 2 decimals but the delta is exactly 1 — render '1', not '1.00'.
        resources = [
            _obs("x1", "Lactate", 1.25, "2026-07-09T05:00:00Z", "mmol/L"),
            _obs("x2", "Lactate", 2.25, "2026-07-10T05:00:00Z", "mmol/L"),
        ]
        assert "↑1 " in _only_text(resources)


# --- Defect 2: a future-dated reading must not empty the card ---------------


def _iso(delta: timedelta) -> str:
    return (utcnow() + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def _observed_probe_cohort() -> list[dict[str, Any]]:
    """The probed cohort: real recent vitals + one glucose mistyped as 2027."""
    return [
        _obs("hr-old", "Heart rate", 70, _iso(timedelta(hours=-4))),
        _obs("hr-new", "Heart rate", 92, _iso(timedelta(hours=-2)), interp="H"),
        _obs("glu-old", "Glucose", 99, _iso(timedelta(hours=-3)), "mg/dL"),
        _obs("glu-future", "Glucose", 100, "2027-01-01T00:00:00Z", "mg/dL"),
    ]


class TestFutureDatedReading:
    def test_future_reading_does_not_empty_the_card(self) -> None:
        # Observed: with the 2027 glucose present the whole card collapsed to the
        # glucose row; without it, the tachycardia showed. One typo, and the UI
        # told the clinician "No recorded changes since your last review".
        claims = build_change_claims(_observed_probe_cohort())
        assert claims, "a future-dated reading must never empty the change card"

    def test_new_tachycardia_survives_a_future_dated_glucose(self) -> None:
        claims = build_change_claims(_observed_probe_cohort())
        hr = [c for c in claims if c.text.startswith("Heart Rate")]
        assert len(hr) == 1, [c.text for c in claims]
        assert "↑22" in hr[0].text  # 70 -> 92, the abnormal rise that must not vanish
        assert "2h since prior" in hr[0].text

    def test_the_same_cohort_without_the_future_reading_reports_the_same_tachycardia(
        self,
    ) -> None:
        # The control probe: the anchor fix must make the future-dated cohort
        # agree with the clean one about the real reading.
        clean = [r for r in _observed_probe_cohort() if r["id"] != "glu-future"]
        hr = [c for c in build_change_claims(clean) if c.text.startswith("Heart Rate")]
        assert len(hr) == 1 and "↑22" in hr[0].text

    def test_future_reading_is_surfaced_not_silently_dropped(self) -> None:
        # Silently dropping the bad record is how this bug started. It stays on the
        # card, flagged — a physician can see the date is wrong; they cannot see an
        # omission.
        claims = build_change_claims(_observed_probe_cohort())
        glucose = [c for c in claims if c.text.startswith("Glucose")]
        assert len(glucose) == 1, [c.text for c in claims]
        assert "dated in the future" in glucose[0].text

    def test_future_reading_asserts_no_movement_or_trend(self) -> None:
        # Which reading is really latest is unknowable, so the delta's sign is a
        # coin flip: assert nothing. Observed bug text was '↑1 · 168d since prior'.
        claims = build_change_claims(_observed_probe_cohort())
        glucose = next(c for c in claims if c.text.startswith("Glucose"))
        assert "↑" not in glucose.text and "↓" not in glucose.text
        assert "since prior" not in glucose.text
        assert glucose.value_direction is not None
        assert glucose.value_direction.value == "none"
        assert glucose.trend_direction is None

    def test_future_reading_does_not_hide_days_old_real_readings(self) -> None:
        # The near-miss guard. Clamping the *anchor* to now — while still letting
        # the future reading into the anchor set — passes every probe above,
        # because those real readings are only hours old. Real charts are not:
        # a patient's last labs are routinely days back. Anchor on now and this
        # tachycardia falls outside the 12h window and vanishes exactly as before.
        # The anchor must be the latest reading that is NOT in the future.
        resources = [
            _obs("hr-old", "Heart rate", 70, _iso(timedelta(days=-5, hours=-2))),
            _obs("hr-new", "Heart rate", 92, _iso(timedelta(days=-5)), interp="H"),
            _obs("glu-future", "Glucose", 100, "2027-01-01T00:00:00Z", "mg/dL"),
        ]
        hr = [c for c in build_change_claims(resources) if c.text.startswith("Heart Rate")]
        assert len(hr) == 1, "a future-dated reading must not push the window past real data"
        assert "↑22" in hr[0].text

    def test_all_readings_future_still_reports_rather_than_denies(self) -> None:
        # A device clock a year ahead on every reading: there is no trustworthy
        # patient-timeline anchor, so fall back to the wall clock and report the
        # readings (flagged) — never "nothing changed".
        resources = [
            _obs("hr-1", "Heart rate", 70, "2027-01-01T00:00:00Z"),
            _obs("hr-2", "Heart rate", 92, "2027-01-01T02:00:00Z", interp="H"),
        ]
        claims = build_change_claims(resources)
        assert len(claims) == 1
        assert "dated in the future" in claims[0].text

    def test_past_only_data_anchors_on_the_patient_timeline_as_before(self) -> None:
        # Regression guard: seed/demo data is days old, and the window must still
        # anchor on the patient's own latest reading, NOT on the wall clock —
        # clamping the anchor to now would empty every historical card.
        resources = [
            _obs("hr-2", "Heart rate", 104, "2026-07-10T00:00:00Z"),
            _obs("hr-1", "Heart rate", 92, "2026-07-10T05:00:00Z"),
        ]
        claims = build_change_claims(resources)
        assert len(claims) == 1 and "↓12" in claims[0].text


# --- Defect 3: unit-blind comparison ---------------------------------------


class TestMixedUnits:
    def _temp_cohort(self) -> list[dict[str, Any]]:
        # Observed: 'Temperature: 98.6 °F  ↑61.6 · 2h since prior', severity normal,
        # trend improving. The patient's temperature did not change at all.
        return [
            _obs("temp-c", "Body temperature", 37.0, "2026-07-10T03:00:00Z", "Cel"),
            _obs("temp-f", "Body temperature", 98.6, "2026-07-10T05:00:00Z", "degF"),
        ]

    def test_mixed_units_do_not_assert_a_61_degree_rise(self) -> None:
        text = _only_text(self._temp_cohort())
        assert "61.6" not in text, text
        assert "↑" not in text and "↓" not in text

    def test_mixed_units_produce_no_trend_and_no_direction(self) -> None:
        claim = build_summary_claims(self._temp_cohort())[0]
        assert claim.trend_direction is None, "a cross-unit trend is not derivable"
        assert claim.value_direction is not None and claim.value_direction.value == "none"

    def test_mixed_units_say_why_the_trend_is_withheld(self) -> None:
        # Fail-closed, but visibly: the card names the mismatch rather than
        # quietly rendering a bare value.
        text = _only_text(self._temp_cohort())
        assert "no trend" in text
        assert "°C" in text  # the prior reading's unit, named

    def test_mixed_units_report_no_false_trend_but_still_surface(self) -> None:
        # Was: `assert build_change_claims(self._temp_cohort()) == []`, on the
        # rationale "a re-recorded unit is not a clinical change; it must not
        # surface as one". That assertion encoded the defect it was meant to guard.
        #
        # It credits this module with knowledge it does not have. summary.py does
        # not convert units (`_trusted_pair`: "No unit conversion is attempted"),
        # so 37.0 Cel -> 98.6 degF (a re-record) and 37.0 Cel -> 104.0 degF (a
        # fever) are the SAME input to it: "not comparable". `== []` dropped both
        # -- and the UI renders an empty change card as the affirmative "No
        # recorded changes since your last review". Verified on the pre-fix code:
        # the 104.0 degF fever returned [] too. The fixture is a re-record, but the
        # rule the test named ("mixed units are not a reportable change") is false
        # the moment the value actually moves, and nothing in the fixture could
        # have caught that.
        #
        # The corrected contract, per this module's own rule at summary.py:95-98
        # ("withholds visibly or not at all"): a pair we cannot compare SURFACES,
        # carrying text that names why -- and still asserts no false trend, which
        # is what this test was really protecting.
        claims = build_change_claims(self._temp_cohort())
        assert len(claims) == 1, "an uncomparable pair must be shown, not silently dropped"
        text = claims[0].text
        assert "61.6" not in text and "↑" not in text and "↓" not in text, text
        assert "no trend" in text, text

    def test_unit_spelling_variants_are_the_same_unit(self) -> None:
        # degC and Cel are one unit; comparing them must still trend normally.
        resources = [
            _obs("t1", "Body temperature", 37.0, "2026-07-10T03:00:00Z", "Cel"),
            _obs("t2", "Body temperature", 38.5, "2026-07-10T05:00:00Z", "degC"),
        ]
        assert "↑1.5" in _only_text(resources)

    def test_weight_in_kg_then_lb_produces_no_trend(self) -> None:
        # OpenEMR permits both; 80 kg -> 176 lb is the same patient.
        resources = [
            _obs("w1", "Body weight", 80.0, "2026-07-09T05:00:00Z", "kg"),
            _obs("w2", "Body weight", 176.0, "2026-07-10T05:00:00Z", "lb_av"),
        ]
        claim = build_summary_claims(resources)[0]
        assert "↑96" not in claim.text and "↑" not in claim.text
        assert claim.trend_direction is None


class TestSameUnitTrendUnchanged:
    """Regression guard: the good case must behave exactly as it did before."""

    def test_same_unit_numeric_trend_is_unaffected(self) -> None:
        resources = [
            _obs("hr-3", "Heart rate", 118, "2026-07-08T05:00:00Z"),
            _obs("hr-2", "Heart rate", 104, "2026-07-09T05:00:00Z"),
            _obs("hr-1", "Heart rate", 92, "2026-07-10T05:00:00Z"),
        ]
        claim = build_summary_claims(resources)[0]
        assert claim.text == "Heart Rate: 92 /min  ↓12 · 24h since prior"
        assert claim.value_direction is not None and claim.value_direction.value == "down"
        assert claim.trend_direction is not None and claim.trend_direction.value == "improving"

    def test_same_unit_unchanged_reading_still_says_no_change(self) -> None:
        resources = [
            _obs("h2", "Body height", 71, "2026-07-09T05:00:00Z", "in"),
            _obs("h1", "Body height", 71, "2026-07-10T05:00:00Z", "in"),
        ]
        assert "no change" in _only_text(resources)

    def test_unitless_readings_still_trend(self) -> None:
        # Both readings carry no unit: nothing mismatches, so nothing is withheld.
        resources = [
            {
                "resourceType": "Observation",
                "id": "p1",
                "code": {"coding": [{"display": "Pain score"}]},
                "valueQuantity": {"value": 4},
                "effectiveDateTime": "2026-07-09T05:00:00Z",
            },
            {
                "resourceType": "Observation",
                "id": "p2",
                "code": {"coding": [{"display": "Pain score"}]},
                "valueQuantity": {"value": 7},
                "effectiveDateTime": "2026-07-10T05:00:00Z",
            },
        ]
        assert "↑3" in _only_text(resources)


# --- Defect 4: the unit-safety fix dropped the row instead of showing it -------


def _obs_no_unit(rid: str, name: str, value: float, when: str) -> dict[str, Any]:
    """An Observation whose valueQuantity carries NO unit key at all."""
    return {
        "resourceType": "Observation",
        "id": rid,
        "code": {"coding": [{"display": name}]},
        "valueQuantity": {"value": value},
        "effectiveDateTime": when,
    }


class TestUncomparablePairStillSurfaces:
    """A pair we cannot compare must be SHOWN, never dropped.

    The unit-safety fix (397a39e) stopped the card asserting a false cross-unit
    trend — correctly. But it withheld the *row*, not just the trend: the row gate
    in ``build_change_claims`` required abnormal-or-changed-or-future, and a
    mixed-unit pair is none of those, so the row never reached the change card. An
    empty change card renders in ``PatientHero.tsx`` as the affirmative "No
    recorded changes since your last review" — so a real, unexaminable change was
    replaced by a sentence denying it. The "· prior in X — no trend" text existed
    to defeat exactly this, and was unreachable in the case it was written for.

    These pin BOTH halves of "withholds visibly or not at all": the trend stays
    withheld, and the withholding is visible on the card the physician reads.
    """

    def _glucose_rise(self, prior_unit: str) -> list[dict[str, Any]]:
        # A REAL 80-point rise: 100 -> 180 mg/dL. Not a re-record — the value moved.
        return [
            _obs("glu-1", "Glucose", 100, "2026-07-10T00:00:00Z", prior_unit),
            _obs("glu-2", "Glucose", 180, "2026-07-10T05:00:00Z", "mg/dL"),
        ]

    def test_real_rise_survives_a_trailing_space_on_the_prior_unit(self) -> None:
        # THE HEADLINE. 'mg/dL ' and 'mg/dL' are the same unit; only an unstripped
        # _unit() made them differ, and that spurious mismatch withheld a real
        # 80-point rise from the change card entirely.
        #
        # Note this is NOT a mixed-unit case once normalized — so the correct
        # outcome is the RECOVERED ↑80, not the "no trend" text. Anything that
        # renders "no trend" here has papered over the mismatch instead of fixing
        # it: the pair is comparable and the delta is real.
        claims = build_change_claims(self._glucose_rise("mg/dL "))
        assert len(claims) == 1, "an 80-point glucose rise must not vanish over whitespace"
        assert "↑80" in claims[0].text, claims[0].text
        assert "no trend" not in claims[0].text, "the units match after stripping"

    def test_real_rise_with_an_unlabelled_prior_surfaces_saying_why(self) -> None:
        # Genuinely uncomparable (a labelled reading vs an unlabelled one), so the
        # trend is correctly withheld — but the ROW must still reach the change
        # card, naming the reason, rather than being dropped into "nothing changed".
        claims = build_change_claims(self._glucose_rise(""))
        assert len(claims) == 1, "an uncomparable pair must be shown, not dropped"
        text = claims[0].text
        assert "no trend" in text, text
        assert "180" in text, "the physician must still see the current value"
        assert "↑" not in text and "↓" not in text, "no trend may be asserted across units"

    def test_a_mixed_unit_row_surfaces_even_when_not_independently_abnormal(self) -> None:
        # THE EXACT HOLE. The "no trend" text was only ever reachable when the
        # reading was independently abnormal or future-dated — i.e. when something
        # ELSE opened the gate. A perfectly in-range reading whose prior is in
        # another unit had no such escape hatch and vanished. 96 mg/dL is normal,
        # carries no interpretation flag, and is not future-dated.
        resources = [
            _obs_no_unit("glu-1", "Glucose", 92, "2026-07-10T00:00:00Z"),
            _obs("glu-2", "Glucose", 96, "2026-07-10T05:00:00Z", "mg/dL"),
        ]
        claims = build_change_claims(resources)
        assert len(claims) == 1, "mixed units alone must open the row gate"
        assert "no trend" in claims[0].text, claims[0].text

    def test_unit_case_variants_are_the_same_unit(self) -> None:
        # The UCUM case-insensitive code set (CEL) denotes the same unit as the
        # case-sensitive one (Cel); a feed emitting it must still trend normally
        # rather than read as a unit change.
        resources = [
            _obs("t1", "Body temperature", 37.0, "2026-07-10T03:00:00Z", "CEL"),
            _obs("t2", "Body temperature", 38.5, "2026-07-10T05:00:00Z", "Cel"),
        ]
        text = _only_text(resources)
        assert "↑1.5" in text, text
        assert "no trend" not in text

    def test_case_folding_never_merges_ucum_units_that_differ_by_case(self) -> None:
        # The guard on the casefold. UCUM is case-sensitive: 'mg' is a milligram,
        # 'Mg' a megagram — a 1e9 difference. Neither is in _UNIT_DISPLAY, so both
        # take the passthrough, which must preserve case. If folding ever leaked
        # into the returned value, these would compare equal and the card would
        # assert a trend across a millionfold unit change.
        resources = [
            _obs("d1", "Some drug level", 5.0, "2026-07-10T03:00:00Z", "mg"),
            _obs("d2", "Some drug level", 7.0, "2026-07-10T05:00:00Z", "Mg"),
        ]
        claims = build_change_claims(resources)
        assert len(claims) == 1, "an uncomparable pair must still surface"
        text = claims[0].text
        assert "no trend" in text, text
        assert "↑2" not in text, "mg and Mg are different units — no trend is derivable"


class TestIdenticalUnitRowsUnaffected:
    """The gate must open for mixed units WITHOUT dragging in ordinary rows."""

    def test_unremarkable_same_unit_row_is_still_not_a_change(self) -> None:
        # The regression guard on the gate: an in-range, unflagged, unchanged
        # reading in a consistent unit is still not "a change since last review".
        # If this goes red, the mixed-unit term is over-broad.
        resources = [
            _obs("hr-1", "Heart rate", 72, "2026-07-10T00:00:00Z"),
            _obs("hr-2", "Heart rate", 72, "2026-07-10T05:00:00Z"),
        ]
        assert build_change_claims(resources) == []

    def test_same_unit_real_change_still_reports_its_trend(self) -> None:
        resources = [
            _obs("hr-1", "Heart rate", 72, "2026-07-10T00:00:00Z"),
            _obs("hr-2", "Heart rate", 92, "2026-07-10T05:00:00Z"),
        ]
        claims = build_change_claims(resources)
        assert len(claims) == 1
        assert "↑20" in claims[0].text and "no trend" not in claims[0].text


# --- Defect 3 (series endpoint): a point is never labelled with another's unit ---

CLIN = 9002
PID = 1016

_MIXED_TEMPS = [
    # Same patient, same metric, two units — realistic in OpenEMR (C and F both
    # permitted). Deliberately shuffled; the endpoint sorts oldest→newest.
    #
    # The newest reading deliberately carries NO referenceRange (OpenEMR routinely
    # omits one on vitals). That is what makes the band a real hazard and not just
    # a label one: "first derivable band across the readings" then walks back past
    # the newest °F reading and bounds a °F chart with the °C record's 36.1-37.5 —
    # under which every normal Fahrenheit temperature plots as wildly high.
    {
        "resourceType": "Observation",
        "id": "temp-f",  # newest → decides the series unit
        "code": {"text": "Body temperature"},
        "valueQuantity": {"value": 98.6, "unit": "degF"},
        "effectiveDateTime": "2026-07-10T05:00:00Z",
    },
    {
        "resourceType": "Observation",
        "id": "temp-c",
        "code": {"text": "Body temperature"},
        "valueQuantity": {"value": 37.0, "unit": "Cel"},
        "effectiveDateTime": "2026-07-10T03:00:00Z",
        "referenceRange": [{"low": {"value": 36.1}, "high": {"value": 37.5}}],
    },
    {
        "resourceType": "Observation",
        "id": "temp-f-older",
        "code": {"text": "Body temperature"},
        "valueQuantity": {"value": 99.1, "unit": "degF"},
        "effectiveDateTime": "2026-07-10T01:00:00Z",
        "referenceRange": [{"low": {"value": 97.0}, "high": {"value": 99.5}}],
    },
]

def _temp(rid: str, value: float, unit: str, when: str) -> dict[str, Any]:
    return {
        "resourceType": "Observation",
        "id": rid,
        "code": {"text": "Body temperature"},
        "valueQuantity": {"value": value, "unit": unit},
        "effectiveDateTime": when,
    }


# A °F history whose NEWEST reading switched to °C — here "the first non-empty
# unit across the readings" and "the newest reading's unit" disagree, which pins
# which rule is in force. Getting this wrong drops the latest reading (the one
# the physician came to see) while plotting the stale ones.
PID_C_LATEST = 1017
_C_LATEST_TEMPS = [
    _temp("cl-f1", 98.6, "degF", "2026-07-10T01:00:00Z"),
    _temp("cl-f2", 99.1, "degF", "2026-07-10T03:00:00Z"),
    _temp("cl-c", 37.8, "Cel", "2026-07-10T05:00:00Z"),  # newest
]

_COHORT: dict[str, dict[str, list[dict[str, Any]]]] = {
    str(PID): {"Observation": _MIXED_TEMPS},
    str(PID_C_LATEST): {"Observation": _C_LATEST_TEMPS},
}


class _FakeFhir:
    """Async-context FHIR double: ``search`` over the fixed cohort by patient id."""

    async def __aenter__(self) -> _FakeFhir:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def search(self, rtype: ResourceType, params: dict[str, str]) -> dict[str, Any]:
        resources = _COHORT.get(params.get("patient", ""), {}).get(rtype.value, [])
        return {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": len(resources),
            "entry": [{"resource": r} for r in resources],
        }


@pytest.fixture
def _db_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    from copilot.config import get_settings
    from copilot.memory.db import Base, get_engine, get_session_factory

    db_file = tmp_path / "summary_correctness.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("COPILOT_FHIR_BASE_URL", "http://oe.test/fhir")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    import copilot.memory.models  # noqa: F401  (registers tables on Base.metadata)

    sync_engine = sa.create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    yield str(db_file)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture(autouse=True)
def _fake_fhir(monkeypatch: pytest.MonkeyPatch) -> None:
    from copilot.api.routes import observations

    monkeypatch.setattr(observations, "_fhir_client", lambda: _FakeFhir())


@pytest.fixture(autouse=True)
def _authorize_clinician(_db_file: str) -> None:
    import asyncio

    from copilot.domain.primitives import ClinicianId
    from copilot.memory.db import get_engine, get_session_factory, session_scope
    from copilot.memory.repository import MemoryRepository

    async def _seed() -> None:
        async with session_scope() as session:
            await MemoryRepository(session).upsert_rounding_cursor(
                ClinicianId(value=CLIN), [PID, PID_C_LATEST], 0, []
            )

    asyncio.run(_seed())
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def _series(metric: str = "Body Temperature", patient_id: int = PID) -> dict[str, Any]:
    from copilot.api.app import create_app
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    get_engine.cache_clear()
    get_session_factory.cache_clear()
    client = TestClient(create_app(get_settings(), probe_factories=[]))
    r = client.get(
        f"/v1/patients/{patient_id}/observations",
        params={"metric": metric, "clinician_id": CLIN},
    )
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


class TestSeriesUnits:
    def test_no_point_is_labelled_with_another_points_unit(self, _db_file: str) -> None:
        # Observed: points (98.6, 'degF') and (37.0, 'Cel') were both served under
        # series unit '°F' — 37.0 on a °F axis reads as profound hypothermia.
        body = _series()
        assert body["unit"] == "°F"
        values = [p["value"] for p in body["points"]]
        assert "37" not in values and "37.0" not in values, values
        assert set(values) == {"99.1", "98.6"}

    def test_newest_reading_decides_the_unit_and_is_never_the_one_dropped(
        self, _db_file: str
    ) -> None:
        # A °F history whose newest reading is in °C. "First non-empty unit wins"
        # would report °F and drop the newest reading — the chart would then omit
        # the only reading the physician opened it for, and say nothing about it.
        # The newest reading decides, so the axis matches the card's current value.
        body = _series(patient_id=PID_C_LATEST)
        assert body["unit"] == "°C"
        assert [p["value"] for p in body["points"]] == ["37.8"]
        assert "98.6" not in {p["value"] for p in body["points"]}

    def test_kept_points_stay_verbatim_and_ordered(self, _db_file: str) -> None:
        # Dropping, not converting: every served value is still the source string.
        body = _series()
        assert [p["value"] for p in body["points"]] == ["99.1", "98.6"]
        stamps = [p["timestamp"] for p in body["points"]]
        assert stamps == sorted(stamps)
        assert all(p["resource_id"] for p in body["points"])

    def test_reference_band_comes_from_the_series_unit(self, _db_file: str) -> None:
        # The °C record's 36.1-37.5 must never bound a °F chart, even though the
        # newest °F reading carries no band of its own.
        assert _series()["reference_range"] == {"low": 97.0, "high": 99.5}

    def test_single_unit_series_is_unaffected(self, _db_file: str) -> None:
        # Regression guard: drop nothing when the history is unit-consistent.
        _MIXED_TEMPS[1]["valueQuantity"] = {"value": 99.4, "unit": "degF"}
        try:
            body = _series()
            assert body["unit"] == "°F"
            assert [p["value"] for p in body["points"]] == ["99.1", "99.4", "98.6"]
        finally:
            _MIXED_TEMPS[1]["valueQuantity"] = {"value": 37.0, "unit": "Cel"}
