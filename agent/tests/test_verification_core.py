"""Verification core: attribution + numeric value match, fail-closed."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from copilot.domain.contracts import Claim, MemoryFileSummary, VerificationAction
from copilot.domain.primitives import (
    DocumentCitation,
    FhirReference,
    GuidelineCitation,
    PatientId,
    ResourceType,
)
from copilot.verification.core import (
    DocumentFact,
    VerificationContext,
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


# --- Non-fhir claim-text numeric fabrication (F2) --------------------------
#
# The fhir path re-checks every numeric literal in `claim.text` against the
# cited resource (`_numbers_not_in_resource`); the covering fhir case is
# `TestValueMatch.test_fails_when_extra_number_in_text_not_in_resource`. The
# document and guideline paths ground only their stored value / verbatim quote,
# so a fabricated number in the surrounding `claim.text` was ungated: a document
# claim whose stored fact == its cited quote, or a guideline claim whose quote
# appears verbatim, would pass the gate while the prose asserted a number no
# source ever recorded. These lock the mirrored check on both non-fhir paths.


def _doc_claim(text: str, quote: str = "2.34", fact_id: str = "907") -> Claim:
    return Claim(
        text=text,
        source_ref=DocumentCitation(
            source_id="41",
            page_or_section=1,
            field_or_chunk_id=fact_id,
            quote_or_value=quote,
        ),
    )


def _doc_context(
    value: str = "2.34", fact_id: str = "907", supported: bool = True, confidence: float = 0.99
) -> VerificationContext:
    return build_context_from_resources(
        [],
        document_facts={
            fact_id: DocumentFact(value=value, supported=supported, match_confidence=confidence)
        },
        doc_confidence_threshold=0.5,
    )


def _guideline_claim(text: str, quote: str, chunk_id: str = "338") -> Claim:
    return Claim(
        text=text,
        source_ref=GuidelineCitation(
            source_id="12",
            page_or_section="Insulin therapy",
            field_or_chunk_id=chunk_id,
            quote_or_value=quote,
        ),
    )


def _guideline_context(content: str, chunk_id: str = "338") -> VerificationContext:
    return build_context_from_resources([], guideline_chunks={chunk_id: content})


@pytest.mark.asyncio
class TestDocumentNumericFabrication:
    async def test_fabricated_number_in_text_absent_from_fact_withheld(self) -> None:
        # Stored fact 2.34 == cited quote 2.34 (value match passes), but the
        # claim prose asserts 9.99 — a number no document fact recorded.
        ctx = _doc_context(value="2.34")
        verifier = Verifier(rules=())
        summary = _summary(_doc_claim("troponin is 9.99 ng/mL", quote="2.34"))
        result = await verifier.verify_memory_file(summary, ctx)
        assert result.action == VerificationAction.withheld
        assert result.claims[0].attribution_ok is True
        assert result.claims[0].value_match is False
        assert "9.99" in result.claims[0].reason

    async def test_number_in_text_present_in_fact_still_served(self) -> None:
        # The honest case must not be over-withheld: the prose's number IS the
        # stored fact's value.
        ctx = _doc_context(value="2.34")
        verifier = Verifier(rules=())
        summary = _summary(_doc_claim("Troponin 2.34 ng/mL on the outside lab.", quote="2.34"))
        result = await verifier.verify_memory_file(summary, ctx)
        assert result.action == VerificationAction.served
        assert result.claims[0].value_match is True


@pytest.mark.asyncio
class TestGuidelineNumericFabrication:
    async def test_fabricated_number_in_text_absent_from_chunk_withheld(self) -> None:
        # The quote appears verbatim in the chunk (value match passes), but the
        # claim prose fabricates a dose — "100 units" — the chunk never states.
        content = "Administer insulin per sliding scale as clinically indicated."
        ctx = _guideline_context(content)
        verifier = Verifier(rules=())
        summary = _summary(
            _guideline_claim(
                "Administer 100 units of insulin now.",
                quote="Administer insulin per sliding scale",
            )
        )
        result = await verifier.verify_memory_file(summary, ctx)
        assert result.action == VerificationAction.withheld
        assert result.claims[0].attribution_ok is True
        assert result.claims[0].value_match is False
        assert "100" in result.claims[0].reason

    async def test_number_in_text_present_in_chunk_still_served(self) -> None:
        # Honest case: the prose's number is stated in the cited chunk.
        content = "Target a mean arterial pressure of at least 65 mmHg in septic shock."
        ctx = _guideline_context(content)
        verifier = Verifier(rules=())
        summary = _summary(
            _guideline_claim("Target a MAP of 65 mmHg.", quote="at least 65 mmHg")
        )
        result = await verifier.verify_memory_file(summary, ctx)
        assert result.action == VerificationAction.served
        assert result.claims[0].value_match is True


# --- Glued-dose fabrication (P1) -------------------------------------------
#
# `_NUM_RE`'s trailing `\b` failed to match when a unit LETTER followed the
# digits, so a number glued to its unit was invisible to the number extractor:
# `extract_numbers('500mg')` -> [] (glued integer dropped entirely) and
# `extract_numbers('2.5mg')` -> ['2'] (decimal truncated to a different value).
# Only the SPACE-separated form ('500 mg') was seen. Effect: a claim that cites
# a drug honestly but fabricates a GLUED dose ("500mg" when the record says
# 50mg — a 10x error) passed all three fabrication gates (fhir
# `_numbers_not_in_resource`, document + guideline `_numbers_not_in_text`) and
# was served to the physician. The digit-look-around pattern stops treating a
# unit letter as a word boundary. These lock the fix at the pattern level and
# through the full Verifier on every path, and guard the space-separated form
# against regression.


class TestExtractNumbersGlued:
    def test_glued_integer_extracted(self) -> None:
        # Previously [] — the number was invisible when glued to its unit.
        assert extract_numbers("500mg") == ["500"]

    def test_glued_decimal_not_truncated(self) -> None:
        # Previously ['2'] — the '.5' was silently dropped, turning a 2.5 dose
        # into a 2 for matching purposes.
        assert extract_numbers("2.5mg") == ["2.5"]

    def test_date_still_splits(self) -> None:
        assert extract_numbers("2024-01-15") == ["2024", "01", "15"]

    def test_large_integer_intact(self) -> None:
        assert extract_numbers("1280") == ["1280"]

    def test_no_merge_across_decimal(self) -> None:
        assert extract_numbers("2.34 ng/mL") == ["2.34"]


def _med_resource(dose_text: str = "50mg PO daily", drug: str = "Metoprolol") -> dict:
    return {
        "resourceType": "MedicationRequest",
        "id": "med-1",
        "status": "active",
        "medicationCodeableConcept": {"text": drug},
        "dosageInstruction": [{"text": dose_text}],
    }


def _med_claim(text: str, drug: str = "Metoprolol") -> Claim:
    return Claim(
        text=text,
        source_ref=FhirReference(
            resource_type=ResourceType.MedicationRequest,
            resource_id="med-1",
            field="medicationCodeableConcept.text",
            value=drug,
        ),
    )


@pytest.mark.asyncio
class TestGluedDoseFabrication:
    async def test_fhir_glued_dose_fabrication_withheld(self) -> None:
        # Drug name cited honestly (value match passes); the prose fabricates a
        # dose glued to its unit — "500mg" — while the record's dose is 50mg.
        # Pre-fix: extract_numbers saw no number in "500mg", so the fabrication
        # gate had nothing to check and the claim was SERVED.
        ctx = build_context_from_resources([_med_resource(dose_text="50mg PO daily")])
        verifier = Verifier(rules=())
        summary = _summary(_med_claim("Metoprolol 500mg PO daily."))
        result = await verifier.verify_memory_file(summary, ctx)
        assert result.action == VerificationAction.withheld
        assert result.claims[0].attribution_ok is True
        assert result.claims[0].value_match is False
        assert "500" in result.claims[0].reason

    async def test_document_glued_dose_fabrication_withheld(self) -> None:
        # Stored fact "50mg" == cited quote (value match passes); prose says 500mg.
        ctx = _doc_context(value="50mg")
        verifier = Verifier(rules=())
        summary = _summary(_doc_claim("Metformin 500mg twice daily.", quote="50mg"))
        result = await verifier.verify_memory_file(summary, ctx)
        assert result.action == VerificationAction.withheld
        assert result.claims[0].attribution_ok is True
        assert result.claims[0].value_match is False
        assert "500" in result.claims[0].reason

    async def test_guideline_glued_dose_fabrication_withheld(self) -> None:
        # Quote appears verbatim in the chunk (value match passes); prose says 500mg.
        content = "Administer metoprolol 50mg orally once daily."
        ctx = _guideline_context(content)
        verifier = Verifier(rules=())
        summary = _summary(
            _guideline_claim("Give metoprolol 500mg now.", quote="metoprolol 50mg orally")
        )
        result = await verifier.verify_memory_file(summary, ctx)
        assert result.action == VerificationAction.withheld
        assert result.claims[0].attribution_ok is True
        assert result.claims[0].value_match is False
        assert "500" in result.claims[0].reason

    async def test_document_glued_decimal_truncation_withheld(self) -> None:
        # Decimal truncation fail-open: '2.5mg' -> '2' (pre-fix) matched the
        # record's real dose of 2 and SERVED a 2.5 fabrication. Post-fix the
        # extractor sees '2.5', which is absent from the record.
        ctx = _doc_context(value="2")
        verifier = Verifier(rules=())
        summary = _summary(_doc_claim("warfarin 2.5mg daily.", quote="2"))
        result = await verifier.verify_memory_file(summary, ctx)
        assert result.action == VerificationAction.withheld
        assert result.claims[0].value_match is False
        assert "2.5" in result.claims[0].reason

    async def test_space_separated_fabrication_still_withheld(self) -> None:
        # R1's F2 space-separated form was already caught; the glued-dose fix
        # must not regress it. '500 mg' fabricated over a '50 mg' record.
        ctx = _doc_context(value="50 mg")
        verifier = Verifier(rules=())
        summary = _summary(_doc_claim("Metformin 500 mg twice daily.", quote="50 mg"))
        result = await verifier.verify_memory_file(summary, ctx)
        assert result.action == VerificationAction.withheld
        assert result.claims[0].value_match is False
        assert "500" in result.claims[0].reason
