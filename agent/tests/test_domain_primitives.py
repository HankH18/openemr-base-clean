"""Tests for typed domain primitives — reject bad input at construction."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from copilot.domain.primitives import ClinicianId, FhirReference, PatientId, ResourceType


class TestPatientId:
    def test_accepts_positive_int(self) -> None:
        assert PatientId(value=1015).value == 1015

    def test_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            PatientId(value=0)

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValidationError):
            PatientId(value=-1)

    def test_stringifies_bare_value(self) -> None:
        assert str(PatientId(value=1015)) == "1015"

    def test_is_frozen(self) -> None:
        pid = PatientId(value=1015)
        with pytest.raises(ValidationError):
            pid.value = 42  # type: ignore[misc]


class TestClinicianId:
    def test_positive_only(self) -> None:
        assert ClinicianId(value=101).value == 101
        with pytest.raises(ValidationError):
            ClinicianId(value=0)


class TestFhirReference:
    def test_minimal_required_fields(self) -> None:
        ref = FhirReference(
            resource_type=ResourceType.Observation,
            resource_id="90045",
            field="valueQuantity.value",
            value="2.34",
        )
        assert ref.resource_type == "Observation"
        assert ref.resource_id == "90045"
        assert ref.value == "2.34"

    def test_rejects_empty_resource_id(self) -> None:
        with pytest.raises(ValidationError):
            FhirReference(
                resource_type=ResourceType.Observation,
                resource_id="",
                field="valueQuantity.value",
                value="2.34",
            )

    def test_rejects_empty_field(self) -> None:
        with pytest.raises(ValidationError):
            FhirReference(
                resource_type=ResourceType.Observation,
                resource_id="90045",
                field="",
                value="2.34",
            )

    def test_resource_type_is_closed_set(self) -> None:
        with pytest.raises(ValidationError):
            FhirReference(
                resource_type="NotAResource",  # type: ignore[arg-type]
                resource_id="1",
                field="x",
                value="y",
            )
