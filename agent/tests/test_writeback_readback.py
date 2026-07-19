"""Post-write read-back must be metric-specific and its outcome must be recorded.

Covers the P3 defect in ``writeback/service.py``: the read-back confirmation was
metric-agnostic (``_value_round_trips`` for a vital confirmed the write if ANY
Observation ``valueQuantity.value`` was ``math.isclose`` to the written number,
with no filter on the metric's FHIR code), so a heart-rate 72 "confirmed" against
a coincidental weight 72. Compounding it, a not-observed value was only a warning
log line — never surfaced, never on the audit trail.

These are unit-level tests: they drive ``_value_round_trips`` and ``_read_back``
directly with in-memory doubles (no DB, no network, no real clients).

Append-only. Nothing here weakens an existing assertion.
"""

from __future__ import annotations

from typing import Any

import pytest

from copilot.config import Settings
from copilot.domain.primitives import ClinicianId, PatientId, ResourceType, utcnow
from copilot.domain.writes import (
    CommittedWrite,
    VitalWrite,
    WritableMetric,
    WriteCandidate,
    WriteKind,
)
from copilot.writeback.service import WriteService

CLIN = 9001
PID = 1015

_LOINC = "http://loinc.org"
# OpenEMR's LOINC codes for the metrics exercised here (see
# FhirObservationVitalsService): heart rate 8867-4, body weight 29463-7,
# systolic 8480-6, diastolic 8462-4, the BP panel 85354-9.


# --- FHIR search-Bundle doubles --------------------------------------------


def _obs(loinc_code: str, value: float, *, system: str = _LOINC) -> dict[str, Any]:
    """A simple vitals Observation: top-level LOINC code + valueQuantity."""
    return {
        "resourceType": "Observation",
        "status": "final",
        "code": {"coding": [{"system": system, "code": loinc_code}]},
        "valueQuantity": {"value": value, "unit": "x"},
    }


def _bp_panel(systolic: float, diastolic: float) -> dict[str, Any]:
    """A blood-pressure panel whose numbers live in coded components."""
    return {
        "resourceType": "Observation",
        "status": "final",
        "code": {"coding": [{"system": _LOINC, "code": "85354-9"}]},
        "component": [
            {
                "code": {"coding": [{"system": _LOINC, "code": "8480-6"}]},
                "valueQuantity": {"value": systolic, "unit": "mm[Hg]"},
            },
            {
                "code": {"coding": [{"system": _LOINC, "code": "8462-4"}]},
                "valueQuantity": {"value": diastolic, "unit": "mm[Hg]"},
            },
        ],
    }


def _bundle(*resources: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(resources),
        "entry": [{"resource": r} for r in resources],
    }


class _FakeReader:
    """Async-context read-back double returning a fixed search Bundle."""

    def __init__(self, bundle: dict[str, Any]) -> None:
        self._bundle = bundle
        self.searches: list[tuple[ResourceType, dict[str, str]]] = []

    async def __aenter__(self) -> _FakeReader:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def search(self, rtype: ResourceType, params: dict[str, str]) -> dict[str, Any]:
        self.searches.append((rtype, params))
        return self._bundle


class _BoomReader:
    """A read client that fails on entry — exercises the swallowed-error path."""

    async def __aenter__(self) -> _BoomReader:
        raise RuntimeError("read-back reader is down")

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _vital_candidate(metric: WritableMetric, value: float, unit: str) -> WriteCandidate:
    return WriteCandidate(
        kind=WriteKind.vital,
        patient_id=PatientId(value=PID),
        clinician_id=ClinicianId(value=CLIN),
        idempotency_key="k-readback-test",
        vital=VitalWrite(metric=metric, value=value, unit=unit),
    )


def _committed() -> CommittedWrite:
    return CommittedWrite(
        resource_kind=WriteKind.vital,
        new_id="vid-1",
        encounter_id="42",
        committed_at=utcnow(),
    )


# --- (1) the read-back is metric-specific -----------------------------------


class TestReadBackIsMetricSpecific:
    async def test_decoy_metric_with_equal_value_does_not_confirm(self) -> None:
        """BITE: a heart-rate 72 must NOT be confirmed by a coincidental weight 72.

        The read-back Bundle holds exactly one Observation — the target metric
        (heart_rate / 8867-4) ABSENT, a decoy weight (29463-7) whose value equals
        the written 72. On the pre-fix, metric-agnostic read-back this returns
        True (false-confirm); after the fix the weight is skipped as a different
        metric and it returns False. This assertion FAILS red on the pre-fix code.
        """
        svc = WriteService(Settings())
        candidate = _vital_candidate(WritableMetric.heart_rate, 72.0, "bpm")
        reader = _FakeReader(_bundle(_obs("29463-7", 72.0)))  # weight 72, no HR

        confirmed = await svc._value_round_trips(reader, PatientId(value=PID), candidate)

        assert confirmed is False, (
            "a heart-rate write must not be confirmed by a coincidentally-equal "
            "weight reading — the read-back must filter by the metric's FHIR code"
        )

    async def test_same_metric_equal_value_confirms(self) -> None:
        """Positive control: a heart-rate 72 DOES confirm against a heart-rate 72,
        even when a decoy weight 72 is also present."""
        svc = WriteService(Settings())
        candidate = _vital_candidate(WritableMetric.heart_rate, 72.0, "bpm")
        reader = _FakeReader(_bundle(_obs("29463-7", 72.0), _obs("8867-4", 72.0)))

        confirmed = await svc._value_round_trips(reader, PatientId(value=PID), candidate)

        assert confirmed is True

    async def test_same_metric_different_value_does_not_confirm(self) -> None:
        svc = WriteService(Settings())
        candidate = _vital_candidate(WritableMetric.heart_rate, 72.0, "bpm")
        reader = _FakeReader(_bundle(_obs("8867-4", 99.0)))

        confirmed = await svc._value_round_trips(reader, PatientId(value=PID), candidate)

        assert confirmed is False

    async def test_blood_pressure_component_confirms_by_its_own_code(self) -> None:
        """Systolic/diastolic live in a BP-panel COMPONENT; the read-back matches
        each on its own LOINC code, and a systolic write is not confirmed off the
        diastolic component's coincidental value."""
        svc = WriteService(Settings())
        reader = _FakeReader(_bundle(_bp_panel(120.0, 80.0)))

        # systolic 120 confirms off the 8480-6 component ...
        syst = _vital_candidate(WritableMetric.systolic_bp, 120.0, "mm[Hg]")
        assert await svc._value_round_trips(reader, PatientId(value=PID), syst) is True

        # ... but a systolic 80 must NOT confirm off the diastolic (8462-4) 80.
        decoy = _vital_candidate(WritableMetric.systolic_bp, 80.0, "mm[Hg]")
        assert await svc._value_round_trips(reader, PatientId(value=PID), decoy) is False

    async def test_same_code_in_a_different_system_does_not_confirm(self) -> None:
        """A coding that reuses the code string under a NON-LOINC system must not
        match — the read-back is scoped to LOINC vitals."""
        svc = WriteService(Settings())
        candidate = _vital_candidate(WritableMetric.heart_rate, 72.0, "bpm")
        reader = _FakeReader(_bundle(_obs("8867-4", 72.0, system="http://snomed.info/sct")))

        confirmed = await svc._value_round_trips(reader, PatientId(value=PID), candidate)

        assert confirmed is False


# --- (2) the read-back OUTCOME is recorded, not just logged ------------------


class TestReadBackRecordsOutcome:
    async def test_value_not_observed_flags_unconfirmed_and_audits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 201 whose value the read-back cannot observe returns unconfirmed=True
        AND appends a ``write_unconfirmed`` audit row — recorded, not just logged."""
        svc = WriteService(
            Settings(), read_client_factory=lambda: _FakeReader(_bundle())
        )
        actions: list[str] = []

        async def _capture(action: str, *args: Any, **kwargs: Any) -> None:
            actions.append(action)

        monkeypatch.setattr(svc, "_record_write_audit", _capture)
        candidate = _vital_candidate(WritableMetric.heart_rate, 72.0, "bpm")
        committed = _committed()
        assert committed.unconfirmed is False  # the write itself landed (201)

        out = await svc._read_back(
            ClinicianId(value=CLIN), PatientId(value=PID), candidate, committed
        )

        assert out.unconfirmed is True
        assert actions == ["write_unconfirmed"]
        # Non-gating: same resource, nothing rolled back.
        assert out.new_id == committed.new_id

    async def test_value_observed_leaves_confirmed_and_audits_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc = WriteService(
            Settings(),
            read_client_factory=lambda: _FakeReader(_bundle(_obs("8867-4", 72.0))),
        )
        actions: list[str] = []

        async def _capture(action: str, *args: Any, **kwargs: Any) -> None:
            actions.append(action)

        monkeypatch.setattr(svc, "_record_write_audit", _capture)
        candidate = _vital_candidate(WritableMetric.heart_rate, 72.0, "bpm")

        out = await svc._read_back(
            ClinicianId(value=CLIN), PatientId(value=PID), candidate, _committed()
        )

        assert out.unconfirmed is False
        assert actions == []

    async def test_read_back_error_is_swallowed_and_flags_unconfirmed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A read-back that RAISES is swallowed — a committed write never becomes a
        failure — but the write is still recorded unconfirmed."""
        svc = WriteService(Settings(), read_client_factory=lambda: _BoomReader())
        actions: list[str] = []

        async def _capture(action: str, *args: Any, **kwargs: Any) -> None:
            actions.append(action)

        monkeypatch.setattr(svc, "_record_write_audit", _capture)
        candidate = _vital_candidate(WritableMetric.heart_rate, 72.0, "bpm")

        out = await svc._read_back(  # must not raise
            ClinicianId(value=CLIN), PatientId(value=PID), candidate, _committed()
        )

        assert out.unconfirmed is True
        assert actions == ["write_unconfirmed"]
