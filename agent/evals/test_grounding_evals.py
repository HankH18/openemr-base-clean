"""End-to-end eval cases against seeded-style FHIR fixtures.

These are the "does the whole stack behave" cases MVP_BUILD_PLAN §3
Task 4 (eval suite) calls for.  Every deterministic case must pass
without an API key; the LLM-judge cases (`@pytest.mark.llm`) skip when
``ANTHROPIC_API_KEY`` is absent.

Cases (ordered by importance):

- **1006 drug-allergy conflict must surface.** PCN allergy + active
  Amoxicillin-clavulanate → critical flag with must_surface=True.
- **1015 overnight critical trop must surface.** New Observation with
  HH interpretation → critical_lab flag.
- **1004 severe-sepsis critical lactate must surface.**
- **1003 DKA: multiple critical labs each produce a flag.**
- **Fabricated citation (no resource) → withheld.**
- **Fabricated number (real resource, invented value) → withheld.**
- **Unit-only mismatch is allowed (numeric equal).**
- **Temporal grounding.** A claim carrying an ``effectiveDateTime`` timestamp
  that matches the live re-fetch is served; a drifted timestamp is withheld.
- **Chart-summary classification.** ``build_summary_claims`` attaches a
  record-grounded ``severity`` + ``value_direction`` to an observation claim.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from copilot.domain.contracts import (
    Claim,
    ClaimSeverity,
    MemoryFileSummary,
    ValueDirection,
    VerificationAction,
)
from copilot.domain.primitives import FhirReference, PatientId, ResourceType
from copilot.rounds.summary import build_summary_claims
from copilot.verification.core import Verifier, build_context_from_resources
from copilot.verification.rules import default_rules
from copilot.worker.synthesizer import StubSynthesizer, SynthesisInput
from evals.fixtures import (
    observation,
    pt1003_dka_bundle,
    pt1004_severe_sepsis_bundle,
    pt1006_drug_allergy_conflict_bundle,
    pt1015_overnight_change_bundle,
)

pytestmark = pytest.mark.asyncio


async def _stub_summary(patient_id: int, bundle: list[dict]) -> MemoryFileSummary:
    synth = StubSynthesizer()
    return await synth.synthesize(
        SynthesisInput(
            patient_id=PatientId(value=patient_id),
            resources=bundle,
            source_watermark=datetime(2026, 7, 8, tzinfo=UTC),
        )
    )


# --- Domain-flag surfacing ------------------------------------------------


class TestPt1006DrugAllergyConflict:
    async def test_pcn_allergy_and_amoxi_clav_produce_must_surface_flag(self) -> None:
        bundle = pt1006_drug_allergy_conflict_bundle()
        ctx = build_context_from_resources(bundle)
        verifier = Verifier(rules=default_rules())
        summary = await _stub_summary(1006, bundle)
        result = await verifier.verify_memory_file(summary, ctx)

        # Deterministic gate must pass (stub emits claims with valid refs).
        assert result.passed is True

        # Exactly one allergy-med conflict, and it must surface.
        conflicts = [f for f in result.domain_flags if f.rule == "allergy_medication_conflict"]
        assert len(conflicts) == 1
        assert conflicts[0].must_surface is True
        assert "Amoxicillin" in conflicts[0].message


class TestPt1015OvernightTrop:
    async def test_critical_trop_produces_critical_lab_flag(self) -> None:
        bundle = pt1015_overnight_change_bundle()
        ctx = build_context_from_resources(bundle)
        verifier = Verifier(rules=default_rules())
        summary = await _stub_summary(1015, bundle)
        result = await verifier.verify_memory_file(summary, ctx)

        crits = [f for f in result.domain_flags if f.rule == "critical_lab"]
        assert len(crits) == 1
        assert "Troponin" in crits[0].message
        assert "critically high" in crits[0].message
        assert crits[0].must_surface is True


class TestPt1004SevereSepsis:
    async def test_critical_lactate_surfaces_but_high_wbc_only_warns(self) -> None:
        bundle = pt1004_severe_sepsis_bundle()
        ctx = build_context_from_resources(bundle)
        verifier = Verifier(rules=default_rules())
        summary = await _stub_summary(1004, bundle)
        result = await verifier.verify_memory_file(summary, ctx)

        crits = [f for f in result.domain_flags if f.rule == "critical_lab"]
        assert len(crits) == 1
        assert "Lactate" in crits[0].message

        warnings = [f for f in result.domain_flags if f.rule == "abnormal_lab"]
        assert len(warnings) == 1
        assert warnings[0].severity == "warning"
        assert warnings[0].must_surface is False


class TestPt1003DKA:
    async def test_multiple_criticals_each_flagged(self) -> None:
        bundle = pt1003_dka_bundle()
        ctx = build_context_from_resources(bundle)
        verifier = Verifier(rules=default_rules())
        summary = await _stub_summary(1003, bundle)
        result = await verifier.verify_memory_file(summary, ctx)

        crits = [f for f in result.domain_flags if f.rule == "critical_lab"]
        crit_labels = sorted(f.message.split(" is ")[0] for f in crits)
        assert crit_labels == ["Glucose", "Potassium"]


# --- Fail-closed gate behavior --------------------------------------------


class TestFabricatedClaims:
    async def test_missing_citation_withheld(self) -> None:
        """A claim citing a resource NOT in the context — reject entirely."""
        bundle = pt1015_overnight_change_bundle()
        ctx = build_context_from_resources(bundle)
        verifier = Verifier(rules=())

        fake = Claim(
            text="Troponin I 2.34 ng/mL (critical high).",
            source_ref=FhirReference(
                resource_type=ResourceType.Observation,
                resource_id="does-not-exist",  # not in the bundle
                field="valueQuantity.value",
                value="2.34",
            ),
        )
        summary = MemoryFileSummary(
            patient_id=PatientId(value=1015),
            claims=[fake],
            acuity_score=0.0,
            rank_reason="",
            synthesized_at=datetime(2026, 7, 8, tzinfo=UTC),
            source_watermark=datetime(2026, 7, 8, tzinfo=UTC),
            content_hash="a" * 64,
        )
        result = await verifier.verify_memory_file(summary, ctx)
        assert result.action == VerificationAction.withheld
        assert result.claims[0].attribution_ok is False

    async def test_fabricated_number_in_claim_text_withheld(self) -> None:
        """Real resource, but claim text adds a number not present in source."""
        bundle = pt1015_overnight_change_bundle()
        ctx = build_context_from_resources(bundle)
        verifier = Verifier(rules=())

        # `trop-overnight` has value 2.34. Claim invents "up from 0.99".
        drifted = Claim(
            text="Troponin I is 2.34, up from 0.99 last check.",
            source_ref=FhirReference(
                resource_type=ResourceType.Observation,
                resource_id="trop-overnight",
                field="valueQuantity.value",
                value="2.34",
            ),
        )
        summary = MemoryFileSummary(
            patient_id=PatientId(value=1015),
            claims=[drifted],
            acuity_score=0.0,
            rank_reason="",
            synthesized_at=datetime(2026, 7, 8, tzinfo=UTC),
            source_watermark=datetime(2026, 7, 8, tzinfo=UTC),
            content_hash="a" * 64,
        )
        result = await verifier.verify_memory_file(summary, ctx)
        assert result.action == VerificationAction.withheld
        assert result.claims[0].value_match is False
        assert "0.99" in result.claims[0].reason


class TestNumericEquivalence:
    async def test_2p34_matches_source_value_240_ok_but_precision_forgiving(self) -> None:
        """The gate is strict on identity but forgiving on `2.34 == 2.340`."""
        # Source has 2.34; claim uses 2.34.
        bundle = pt1015_overnight_change_bundle()
        ctx = build_context_from_resources(bundle)
        verifier = Verifier(rules=())
        c = Claim(
            text="Troponin I 2.34 ng/mL.",
            source_ref=FhirReference(
                resource_type=ResourceType.Observation,
                resource_id="trop-overnight",
                field="valueQuantity.value",
                value="2.340",  # different precision — must still pass
            ),
        )
        summary = MemoryFileSummary(
            patient_id=PatientId(value=1015),
            claims=[c],
            acuity_score=0.0,
            rank_reason="",
            synthesized_at=datetime(2026, 7, 8, tzinfo=UTC),
            source_watermark=datetime(2026, 7, 8, tzinfo=UTC),
            content_hash="a" * 64,
        )
        result = await verifier.verify_memory_file(summary, ctx)
        assert result.action == VerificationAction.served


# --- Temporal grounding gate ----------------------------------------------


class TestTemporalGrounding:
    """The serve-time temporal gate: a claim's grounded clinical instant must
    re-derive equal from the live re-fetch, or the claim fails closed.

    Grounds on an Observation carrying ``effectiveDateTime`` — the value +
    numeric checks pass in both cases, so the *only* deciding factor is whether
    the claim's ``source_ref.timestamp`` matches what ``extract_temporal``
    re-reads from the source.
    """

    @staticmethod
    def _bundle() -> list[dict]:
        return [
            observation(
                id="trop-timed",
                label="Troponin I",
                loinc="6598-7",
                value=2.34,
                unit="ng/mL",
                abnormal="HH",
                effective="2026-07-08T03:00:00Z",
            )
        ]

    @staticmethod
    def _summary(claim: Claim) -> MemoryFileSummary:
        return MemoryFileSummary(
            patient_id=PatientId(value=1015),
            claims=[claim],
            acuity_score=0.0,
            rank_reason="",
            synthesized_at=datetime(2026, 7, 8, tzinfo=UTC),
            source_watermark=datetime(2026, 7, 8, tzinfo=UTC),
            content_hash="a" * 64,
        )

    async def test_matching_timestamp_served(self) -> None:
        """Claim's timestamp == the source's effectiveDateTime → served."""
        ctx = build_context_from_resources(self._bundle())
        verifier = Verifier(rules=())
        claim = Claim(
            text="Troponin I 2.34 ng/mL.",
            source_ref=FhirReference(
                resource_type=ResourceType.Observation,
                resource_id="trop-timed",
                field="valueQuantity.value",
                value="2.34",
                timestamp=datetime(2026, 7, 8, 3, 0, 0, tzinfo=UTC),
            ),
        )
        result = await verifier.verify_memory_file(self._summary(claim), ctx)
        assert result.action == VerificationAction.served
        assert result.claims[0].value_match is True

    async def test_drifted_timestamp_withheld(self) -> None:
        """A claim whose timestamp differs from the live re-fetch is withheld."""
        ctx = build_context_from_resources(self._bundle())
        verifier = Verifier(rules=())
        claim = Claim(
            text="Troponin I 2.34 ng/mL.",
            source_ref=FhirReference(
                resource_type=ResourceType.Observation,
                resource_id="trop-timed",
                field="valueQuantity.value",
                value="2.34",
                # A full day earlier than the source's effectiveDateTime.
                timestamp=datetime(2026, 7, 7, 3, 0, 0, tzinfo=UTC),
            ),
        )
        result = await verifier.verify_memory_file(self._summary(claim), ctx)
        assert result.action == VerificationAction.withheld
        assert result.claims[0].value_match is False
        assert "temporal drift" in result.claims[0].reason


# --- Chart-summary classification (severity + value_direction) -------------


class TestSummaryClassification:
    """``build_summary_claims`` attaches record-grounded presentation hints to
    an observation claim — never part of the value-match gate, but they must be
    derived from the record's own successive values + abnormal flag.
    """

    async def test_rising_critical_observation_is_up_and_critical(self) -> None:
        # pt1015: Troponin I rose 0.02 → 2.34 (latest flagged HH).
        claims = build_summary_claims(pt1015_overnight_change_bundle())
        assert len(claims) == 1
        claim = claims[0]
        assert claim.value_direction == ValueDirection.up
        assert claim.severity == ClaimSeverity.critical
        # The verbatim gate value is untouched by the classification.
        assert claim.source_ref.value == "2.34"

    async def test_falling_warning_observation_is_down_and_warning(self) -> None:
        # A potassium that fell 6.1 → 5.2, latest flagged 'H' (mild-high warning).
        bundle = [
            observation(
                id="k-late", label="Potassium", loinc="2823-3", value=5.2,
                unit="mEq/L", abnormal="H", last_updated="2026-07-08T06:00:00Z",
            ),
            observation(
                id="k-early", label="Potassium", loinc="2823-3", value=6.1,
                unit="mEq/L", abnormal="HH", last_updated="2026-07-07T06:00:00Z",
            ),
        ]
        claims = build_summary_claims(bundle)
        assert len(claims) == 1
        claim = claims[0]
        assert claim.value_direction == ValueDirection.down
        assert claim.severity == ClaimSeverity.warning


# --- LLM-judge cases (guarded) --------------------------------------------


REQUIRES_LLM = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="LLM eval requires ANTHROPIC_API_KEY",
)


@pytest.mark.llm
@REQUIRES_LLM
class TestEntailmentLive:
    """Live LLM entailment tests — only run when the operator supplies a key."""

    async def test_entailed_claim_returns_yes(self) -> None:
        from copilot.verification.entailment import LlmEntailment

        entailer = LlmEntailment(
            anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
            model="claude-haiku-4-5-20251001",
        )
        resource = {
            "resourceType": "Observation",
            "id": "trop-overnight",
            "code": {"text": "Troponin I"},
            "valueQuantity": {"value": 2.34, "unit": "ng/mL"},
            "interpretation": [{"coding": [{"code": "HH"}]}],
        }
        claim = Claim(
            text="Troponin I is 2.34 ng/mL (critical high).",
            source_ref=FhirReference(
                resource_type=ResourceType.Observation,
                resource_id="trop-overnight",
                field="valueQuantity.value",
                value="2.34",
            ),
        )
        assert (await entailer.entails(claim, resource)) is True

    async def test_hallucinated_claim_returns_no(self) -> None:
        from copilot.verification.entailment import LlmEntailment

        entailer = LlmEntailment(
            anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
            model="claude-haiku-4-5-20251001",
        )
        resource = {
            "resourceType": "Observation",
            "id": "trop-overnight",
            "code": {"text": "Troponin I"},
            "valueQuantity": {"value": 2.34, "unit": "ng/mL"},
        }
        # "AKI resolving" is not in this Observation.
        claim = Claim(
            text="Kidneys are improving; creatinine has fallen dramatically.",
            source_ref=FhirReference(
                resource_type=ResourceType.Observation,
                resource_id="trop-overnight",
                field="valueQuantity.value",
                value="2.34",
            ),
        )
        assert (await entailer.entails(claim, resource)) is False
