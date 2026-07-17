"""The unit gate: a claimed quantity must match the record's DIMENSION, not just its number.

The defect these pin: the value-match gate compared ``2.34`` to ``2.34`` and
served "Troponin I is 2.34 ng/mL — critical" against a record of **2.34 ng/L**,
a thousand-fold error the product's central safety mechanism could not see.

Covers both halves of the fix:
  * ``FhirReference.unit`` is grounded and re-compared on live re-fetch
    (:class:`TestUnitGate`), including the explicit no-unit policy.
  * ``claim_text`` renders a QUANTITY rather than a bare number
    (:class:`TestClaimTextUnit`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from copilot.agent.grounding import claim_text, extract_unit
from copilot.domain.contracts import Claim, MemoryFileSummary, VerificationAction
from copilot.domain.primitives import FhirReference, PatientId, ResourceType
from copilot.verification.core import Verifier, build_context_from_resources

# --- Helpers ---------------------------------------------------------------


def _obs(unit: str | None = "ng/L", value: float = 2.34) -> dict[str, Any]:
    """A troponin Observation. ``unit=None`` omits `valueQuantity.unit` entirely."""
    quantity: dict[str, Any] = {"value": value}
    if unit is not None:
        quantity["unit"] = unit
    return {
        "resourceType": "Observation",
        "id": "trop-1",
        "status": "final",
        "code": {"text": "Troponin I"},
        "valueQuantity": quantity,
    }


def _med() -> dict[str, Any]:
    """A MedicationRequest — a non-quantity resource: a name has no unit."""
    return {
        "resourceType": "MedicationRequest",
        "id": "med-1",
        "status": "active",
        "medicationCodeableConcept": {"text": "Hydromorphone"},
    }


def _claim(
    text: str,
    *,
    resource_id: str = "trop-1",
    resource_type: ResourceType = ResourceType.Observation,
    field: str = "valueQuantity.value",
    value: str = "2.34",
    unit: str | None = None,
) -> Claim:
    return Claim(
        text=text,
        source_ref=FhirReference(
            resource_type=resource_type,
            resource_id=resource_id,
            field=field,
            value=value,
            unit=unit,
        ),
    )


def _summary(*claims: Claim) -> MemoryFileSummary:
    return MemoryFileSummary(
        patient_id=PatientId(value=1015),
        claims=list(claims),
        acuity_score=0.0,
        rank_reason="",
        synthesized_at=datetime(2026, 7, 8, tzinfo=UTC),
        source_watermark=datetime(2026, 7, 8, tzinfo=UTC),
        content_hash="a" * 64,
    )


async def _verify(claim: Claim, *resources: dict[str, Any]) -> Any:
    return await Verifier().verify_memory_file(
        _summary(claim), build_context_from_resources(list(resources))
    )


# --- The gate --------------------------------------------------------------


class TestUnitGate:
    """The claimed unit must match the record's on re-fetch, or the claim dies."""

    @pytest.mark.anyio
    async def test_wrong_unit_is_withheld(self) -> None:
        """THE PROBE: claim says ng/mL, record says ng/L. Value matches; it is 1000x wrong."""
        result = await _verify(
            _claim("Observation Troponin I: 2.34 ng/mL.", unit="ng/mL"),
            _obs(unit="ng/L"),
        )
        assert result.action is VerificationAction.withheld
        assert result.passed is False
        assert result.claims[0].value_match is False
        # Attribution is fine — the resource IS there. The DIMENSION is the lie.
        assert result.claims[0].attribution_ok is True
        assert "unit mismatch" in (result.claims[0].reason or "")

    @pytest.mark.anyio
    async def test_matching_unit_still_verifies(self) -> None:
        """The regression guard: withholding everything must NOT be how the probe passes."""
        result = await _verify(
            _claim("Observation Troponin I: 2.34 ng/mL.", unit="ng/mL"),
            _obs(unit="ng/mL"),
        )
        assert result.action is VerificationAction.served
        assert result.passed is True
        assert result.claims[0].value_match is True

    @pytest.mark.anyio
    async def test_claim_unit_absent_from_record_is_withheld(self) -> None:
        """A claim asserting a unit the record does not have is a fabricated dimension."""
        result = await _verify(
            _claim("Observation Troponin I: 2.34 ng/mL.", unit="ng/mL"),
            _obs(unit=None),
        )
        assert result.action is VerificationAction.withheld
        assert "unit mismatch" in (result.claims[0].reason or "")

    @pytest.mark.anyio
    async def test_unit_compared_case_sensitively(self) -> None:
        """UCUM is case-sensitive: mg is a milligram, Mg a megagram (1e9 apart).

        Case-folding units would manufacture the very magnitude error this gate
        exists to catch, so `mg` must NOT verify against a record of `Mg`.
        """
        result = await _verify(
            _claim("Observation Troponin I: 2.34 mg.", unit="mg"),
            _obs(unit="Mg"),
        )
        assert result.action is VerificationAction.withheld
        assert "unit mismatch" in (result.claims[0].reason or "")

    @pytest.mark.anyio
    async def test_surrounding_whitespace_is_not_drift(self) -> None:
        """Padding carries no unit semantics — the one normalization applied."""
        result = await _verify(
            _claim("Observation Troponin I: 2.34 ng/mL.", unit="ng/mL"),
            _obs(unit=" ng/mL "),
        )
        assert result.action is VerificationAction.served

    @pytest.mark.anyio
    async def test_no_equivalence_table_is_invented(self) -> None:
        """An equivalent-but-differently-spelled unit fails CLOSED, not open.

        We do not own a UCUM conversion table, so `nanogram per milliliter` is
        not asserted equal to `ng/mL`. Withholding an honest claim is the safe
        failure; serving a claim on an INVENTED equivalence is not.
        """
        result = await _verify(
            _claim("Observation Troponin I: 2.34.", unit="nanogram per milliliter"),
            _obs(unit="ng/mL"),
        )
        assert result.action is VerificationAction.withheld

    @pytest.mark.anyio
    async def test_claim_without_unit_against_united_record_verifies(self) -> None:
        """THE DELIBERATE HOLE, pinned so it can never be changed silently.

        A claim that grounded no unit is not gated on units — exactly as a claim
        with no `timestamp` is not gated on time. Failing these closed would
        withhold every lab answer the product gives, and every claim persisted
        before `unit` existed (which rehydrates with unit=None).

        The cost: this gate cannot catch a unit error in a claim that asserts no
        unit. That half is closed at the grounding layer (every quantity claim
        grounds its unit), not here.
        """
        result = await _verify(
            _claim("Observation Troponin I: 2.34.", unit=None),
            _obs(unit="ng/L"),
        )
        assert result.action is VerificationAction.served
        assert result.claims[0].value_match is True

    @pytest.mark.anyio
    async def test_non_quantity_claim_verifies_with_no_unit_crash(self) -> None:
        """A medication NAME has no unit — a None unit must not crash or fail."""
        result = await _verify(
            _claim(
                "Medication: Hydromorphone.",
                resource_id="med-1",
                resource_type=ResourceType.MedicationRequest,
                field="medicationCodeableConcept.text",
                value="Hydromorphone",
                unit=None,
            ),
            _med(),
        )
        assert result.action is VerificationAction.served
        assert result.claims[0].value_match is True

    @pytest.mark.anyio
    async def test_wrong_unit_on_a_non_quantity_field_is_withheld(self) -> None:
        """A unit asserted against a resource type that has none cannot ground."""
        result = await _verify(
            _claim(
                "Medication: Hydromorphone.",
                resource_id="med-1",
                resource_type=ResourceType.MedicationRequest,
                field="medicationCodeableConcept.text",
                value="Hydromorphone",
                unit="mg",
            ),
            _med(),
        )
        assert result.action is VerificationAction.withheld


class TestFhirReferenceUnitField:
    """`unit` is optional and defaults to None — a required unit would break every
    non-quantity claim, and every claim persisted before the field existed."""

    def test_unit_defaults_to_none(self) -> None:
        ref = FhirReference(
            resource_type=ResourceType.Observation,
            resource_id="trop-1",
            field="valueQuantity.value",
            value="2.34",
        )
        assert ref.unit is None

    def test_unit_round_trips_through_serialization(self) -> None:
        """A persisted claim must carry its unit back, or the gate silently reopens."""
        ref = FhirReference(
            resource_type=ResourceType.Observation,
            resource_id="trop-1",
            field="valueQuantity.value",
            value="2.34",
            unit="ng/mL",
        )
        assert FhirReference.model_validate(ref.model_dump()).unit == "ng/mL"

    def test_legacy_payload_without_unit_rehydrates(self) -> None:
        """Byte-compatibility: a pre-unit persisted row must still validate."""
        assert (
            FhirReference.model_validate(
                {
                    "resource_type": "Observation",
                    "resource_id": "trop-1",
                    "field": "valueQuantity.value",
                    "value": "2.34",
                }
            ).unit
            is None
        )


class TestExtractUnit:
    """Grounding reads the unit with the SAME extractor the gate re-reads it with."""

    def test_grounds_observation_unit(self) -> None:
        assert extract_unit(_obs(unit="ng/L")) == "ng/L"

    def test_unitless_observation_grounds_none(self) -> None:
        assert extract_unit(_obs(unit=None)) is None

    def test_non_observation_grounds_none(self) -> None:
        assert extract_unit(_med()) is None


# --- Served evidence -------------------------------------------------------


class TestClaimTextUnit:
    """The evidence a clinician reads must be a quantity, not a bare number."""

    def test_renders_unit_when_present(self) -> None:
        assert (
            claim_text("Observation", "Troponin I", "2.34", "ng/mL")
            == "Observation Troponin I: 2.34 ng/mL."
        )

    def test_omits_unit_when_absent_and_never_renders_none(self) -> None:
        text = claim_text("Observation", "Troponin I", "2.34", None)
        assert text == "Observation Troponin I: 2.34."
        assert "None" not in text

    def test_blank_unit_is_not_rendered(self) -> None:
        assert claim_text("Observation", "Troponin I", "2.34", "  ") == "Observation Troponin I: 2.34."

    def test_unit_defaults_to_absent(self) -> None:
        """The default keeps every existing 3-arg caller byte-identical."""
        assert claim_text("Observation", "Troponin I", "2.34") == "Observation Troponin I: 2.34."

    def test_non_quantity_claim_never_shows_a_unit(self) -> None:
        assert claim_text("MedicationRequest", "Hydromorphone", "Hydromorphone") == (
            "Medication: Hydromorphone."
        )

    def test_unit_is_emitted_verbatim_not_humanized(self) -> None:
        """`humanize_label` would title-case "mg" into "Mg" — milligrams to megagrams."""
        assert claim_text("Observation", "Morphine", "4", "mg") == "Observation Morphine: 4 mg."

    def test_united_claim_text_survives_the_numeric_gate(self) -> None:
        """A unit carrying digits (UCUM `10*3/uL`) must not read as a fabricated number.

        `claim_text` feeds the numeric-literal check, which requires every number
        in the text to appear in the source. The unit's digits come FROM the
        record, so they are found there.
        """
        assert (
            claim_text("Observation", "WBC", "11.2", "10*3/uL") == "Observation WBC: 11.2 10*3/uL."
        )

    @pytest.mark.anyio
    async def test_digit_bearing_unit_still_verifies(self) -> None:
        wbc = {
            "resourceType": "Observation",
            "id": "trop-1",
            "status": "final",
            "code": {"text": "WBC"},
            "valueQuantity": {"value": 11.2, "unit": "10*3/uL"},
        }
        result = await _verify(
            _claim(claim_text("Observation", "WBC", "11.2", "10*3/uL"), value="11.2", unit="10*3/uL"),
            wbc,
        )
        assert result.action is VerificationAction.served
