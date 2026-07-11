"""Verification core: attribution + numeric value match, fail-closed."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from copilot.domain.contracts import Claim, MemoryFileSummary, VerificationAction
from copilot.domain.primitives import FhirReference, PatientId, ResourceType
from copilot.verification.core import (
    Verifier,
    build_context_from_resources,
    extract_field_value,
    extract_numbers,
)

# --- Helpers ---------------------------------------------------------------


def _trop_resource(
    value: str = "2.34", abnormal: str = "HH", effective: str | None = None
) -> dict:
    r: dict = {
        "resourceType": "Observation",
        "id": "trop-1",
        "status": "final",
        "code": {"text": "Troponin I", "coding": [{"code": "6598-7", "display": "Troponin I"}]},
        "valueQuantity": {"value": float(value), "unit": "ng/mL"},
        "interpretation": [{"coding": [{"code": abnormal, "display": "critical high"}]}],
    }
    if effective is not None:
        r["effectiveDateTime"] = effective
    return r


def _claim(
    text: str,
    resource_id: str = "trop-1",
    field: str = "valueQuantity.value",
    value: str = "2.34",
    timestamp: str | None = None,
) -> Claim:
    return Claim(
        text=text,
        source_ref=FhirReference(
            resource_type=ResourceType.Observation,
            resource_id=resource_id,
            field=field,
            value=value,
            timestamp=timestamp,
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


# --- Field extraction ------------------------------------------------------


class TestExtractFieldValue:
    def test_dotted_path_hits_leaf(self) -> None:
        assert extract_field_value(_trop_resource(), "valueQuantity.value") == "2.34"

    def test_indexed_path(self) -> None:
        assert extract_field_value(_trop_resource(), "code.coding[0].code") == "6598-7"

    def test_missing_key_returns_none(self) -> None:
        assert extract_field_value({"a": 1}, "b.c") is None

    def test_index_out_of_range_returns_none(self) -> None:
        assert extract_field_value({"list": [{"x": 1}]}, "list[5].x") is None


class TestExtractNumbers:
    def test_finds_integers_and_decimals(self) -> None:
        assert extract_numbers("Troponin 2.34 up from 0.02; HR 112") == ["2.34", "0.02", "112"]

    def test_no_numbers(self) -> None:
        assert extract_numbers("Assessment: chest pain resolved") == []


# --- Verifier: attribution -------------------------------------------------


@pytest.mark.asyncio
class TestAttribution:
    async def test_passes_when_source_exists_and_value_matches(self) -> None:
        ctx = build_context_from_resources([_trop_resource()])
        verifier = Verifier(rules=())
        summary = _summary(_claim("Troponin I 2.34 ng/mL — critical high."))
        result = await verifier.verify_memory_file(summary, ctx)
        assert result.passed is True
        assert result.action == VerificationAction.served
        assert result.claims[0].attribution_ok is True
        assert result.claims[0].value_match is True

    async def test_fails_when_source_missing_from_context(self) -> None:
        ctx = build_context_from_resources([])
        verifier = Verifier(rules=())
        summary = _summary(_claim("Troponin I 2.34 ng/mL."))
        result = await verifier.verify_memory_file(summary, ctx)
        assert result.action == VerificationAction.withheld
        assert result.claims[0].attribution_ok is False
        assert "not found" in result.claims[0].reason


# --- Verifier: value / number match ---------------------------------------


@pytest.mark.asyncio
class TestValueMatch:
    async def test_fails_when_source_value_disagrees(self) -> None:
        ctx = build_context_from_resources([_trop_resource(value="0.02")])
        verifier = Verifier(rules=())
        summary = _summary(_claim("Troponin 2.34 ng/mL.", value="2.34"))
        result = await verifier.verify_memory_file(summary, ctx)
        assert result.action == VerificationAction.withheld
        assert result.claims[0].attribution_ok is True
        assert result.claims[0].value_match is False
        assert "mismatch" in result.claims[0].reason

    async def test_fails_when_extra_number_in_text_not_in_resource(self) -> None:
        ctx = build_context_from_resources([_trop_resource(value="2.34")])
        verifier = Verifier(rules=())
        # Claim adds "up from 0.02" but the source doesn't contain 0.02.
        summary = _summary(_claim("Troponin 2.34 up from 0.02.", value="2.34"))
        result = await verifier.verify_memory_file(summary, ctx)
        assert result.action == VerificationAction.withheld
        assert result.claims[0].value_match is False
        assert "0.02" in result.claims[0].reason

    async def test_numeric_equivalence_2p34_vs_2p340(self) -> None:
        """`float('2.34') == float('2.340')` so different-precision numbers pass."""
        ctx = build_context_from_resources([_trop_resource(value="2.340")])
        verifier = Verifier(rules=())
        summary = _summary(_claim("Troponin 2.34.", value="2.34"))
        result = await verifier.verify_memory_file(summary, ctx)
        assert result.action == VerificationAction.served


@pytest.mark.asyncio
class TestFailClosedActions:
    async def test_mixed_pass_fail_returns_degraded(self) -> None:
        ctx = build_context_from_resources([_trop_resource(value="2.34")])
        verifier = Verifier(rules=())
        good = _claim("Troponin 2.34.", value="2.34")
        bad = _claim("HR 112.", resource_id="MISSING", value="112")
        result = await verifier.verify_memory_file(_summary(good, bad), ctx)
        assert result.action == VerificationAction.degraded
        assert result.claims[0].value_match is True
        assert result.claims[1].attribution_ok is False

    async def test_empty_claim_list_still_served_with_flags(self) -> None:
        """Memory file with no claims — verification should permit surfacing
        domain flags rather than withholding the whole document."""
        ctx = build_context_from_resources([_trop_resource()])
        verifier = Verifier(rules=())
        result = await verifier.verify_memory_file(_summary(), ctx)
        assert result.action == VerificationAction.served
        assert result.passed is True


@pytest.mark.asyncio
class TestTemporalGate:
    """The grounded `source_ref.timestamp` gate — shared extractor, fail-closed.

    A None timestamp must skip the check entirely (no regression for the entire
    existing corpus); a present timestamp must re-derive an equal instant from
    the live re-fetch or the claim is withheld.
    """

    async def test_timestamp_absent_unaffected(self) -> None:
        # No effectiveDateTime on the resource, no timestamp on the claim: the
        # temporal gate is skipped, so behavior is identical to pre-change.
        ctx = build_context_from_resources([_trop_resource()])
        verifier = Verifier(rules=())
        summary = _summary(_claim("Troponin I 2.34 ng/mL — critical high."))
        result = await verifier.verify_memory_file(summary, ctx)
        assert result.action == VerificationAction.served
        assert result.claims[0].value_match is True

    async def test_timestamp_match_served(self) -> None:
        stamp = "2026-07-08T03:00:00Z"
        ctx = build_context_from_resources([_trop_resource(effective=stamp)])
        verifier = Verifier(rules=())
        summary = _summary(_claim("Troponin I 2.34 ng/mL.", timestamp=stamp))
        result = await verifier.verify_memory_file(summary, ctx)
        assert result.action == VerificationAction.served
        assert result.claims[0].value_match is True

    async def test_zulu_vs_offset_same_instant_still_served(self) -> None:
        # Grounding stored "...Z"; a re-fetch reporting "+00:00" is the SAME
        # instant and must NOT withhold an honest claim (instant, not string, eq).
        ctx = build_context_from_resources(
            [_trop_resource(effective="2026-07-08T03:00:00+00:00")]
        )
        verifier = Verifier(rules=())
        summary = _summary(_claim("Troponin I 2.34 ng/mL.", timestamp="2026-07-08T03:00:00Z"))
        result = await verifier.verify_memory_file(summary, ctx)
        assert result.action == VerificationAction.served

    async def test_timestamp_drift_withheld(self) -> None:
        # Claim grounded at 03:00; the live re-fetch now reads 09:00 → drift.
        ctx = build_context_from_resources([_trop_resource(effective="2026-07-08T09:00:00Z")])
        verifier = Verifier(rules=())
        summary = _summary(_claim("Troponin I 2.34 ng/mL.", timestamp="2026-07-08T03:00:00Z"))
        result = await verifier.verify_memory_file(summary, ctx)
        assert result.action == VerificationAction.withheld
        assert result.claims[0].attribution_ok is True
        assert result.claims[0].value_match is False
        assert "temporal drift" in result.claims[0].reason

    async def test_timestamp_removed_from_source_withheld(self) -> None:
        # Claim carried a timestamp, but the live re-fetch no longer has one → drift.
        ctx = build_context_from_resources([_trop_resource()])  # no effectiveDateTime
        verifier = Verifier(rules=())
        summary = _summary(_claim("Troponin I 2.34 ng/mL.", timestamp="2026-07-08T03:00:00Z"))
        result = await verifier.verify_memory_file(summary, ctx)
        assert result.action == VerificationAction.withheld
        assert result.claims[0].value_match is False
        assert "temporal drift" in result.claims[0].reason
