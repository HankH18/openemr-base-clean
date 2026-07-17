"""The machine-readable citation contract every clinical claim must satisfy.

The spec requires each claim's citation to expose, by name,
``{source_type, source_id, page_or_section, field_or_chunk_id, quote_or_value}``.
Two properties are load-bearing and covered here:

1. **Every variant carries the five keys in its serialized output** — including
   ``FhirReference``, the variant live claims actually use, which derives them
   from its record-shaped fields (``resource_id`` / ``field`` / ``value``)
   rather than renaming them out from under the verifier.
2. **A non-fhir citation is expressible and round-trips** — ``Claim.source_ref``
   is the real ``Citation`` union, so a document-cited claim can be constructed,
   persisted, and rehydrated as itself.

Neither may come at the cost of the fail-closed gate, so the last class re-proves
that an unverifiable claim is still dropped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from copilot.domain.contracts import Claim, MemoryFileSummary, VerificationAction
from copilot.domain.primitives import (
    DocumentCitation,
    FhirReference,
    GuidelineCitation,
    PatientId,
    ResourceType,
)
from copilot.memory import Base, MemoryRepository
from copilot.memory.repository import _claim_from_json, _claim_to_json
from copilot.verification.core import DocumentFact, Verifier, build_context_from_resources

SPEC_KEYS = frozenset(
    {"source_type", "source_id", "page_or_section", "field_or_chunk_id", "quote_or_value"}
)
"""The five keys the spec mandates on every citation, by name."""


def _fhir_ref(value: str = "2.34") -> FhirReference:
    return FhirReference(
        resource_type=ResourceType.Observation,
        resource_id="trop-1",
        field="valueQuantity.value",
        value=value,
        timestamp=datetime(2026, 7, 8, 3, tzinfo=UTC),
    )


def _doc_citation(value: str = "6.9") -> DocumentCitation:
    return DocumentCitation(
        source_id="41",
        page_or_section=2,
        field_or_chunk_id="907",
        quote_or_value=value,
        bbox=[0.1, 0.2, 0.3, 0.05],
        confidence=0.94,
    )


def _guideline_citation(quote: str = "Target HbA1c below 7.0% for most adults.") -> GuidelineCitation:
    return GuidelineCitation(
        source_id="12",
        page_or_section="Section 6.2 — Glycemic Targets",
        field_or_chunk_id="338",
        quote_or_value=quote,
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


class TestFiveSpecKeys:
    """Every citation variant exposes the mandated keys in serialized output."""

    def test_fhir_cited_claim_serializes_all_five_spec_keys(self) -> None:
        """The variant every live claim uses — the defect this contract turned on."""
        dumped = Claim(text="Troponin I 2.34 ng/mL.", source_ref=_fhir_ref()).model_dump(
            mode="json"
        )
        ref = dumped["source_ref"]
        assert set(ref) >= SPEC_KEYS, f"missing spec keys: {SPEC_KEYS - set(ref)}"

    @pytest.mark.parametrize(
        "citation",
        [
            pytest.param(_fhir_ref(), id="fhir"),
            pytest.param(_doc_citation(), id="document"),
            pytest.param(_guideline_citation(), id="guideline"),
        ],
    )
    def test_every_variant_carries_the_five_keys(self, citation: Any) -> None:
        dumped = citation.model_dump(mode="json")
        assert set(dumped) >= SPEC_KEYS, f"missing spec keys: {SPEC_KEYS - set(dumped)}"

    def test_fhir_spec_keys_alias_their_source_fields_verbatim(self) -> None:
        """The derived keys mirror the record fields — they add nothing and
        can never disagree with the values the verifier re-checks."""
        ref = _fhir_ref()
        dumped = ref.model_dump(mode="json")
        assert dumped["source_id"] == ref.resource_id == "trop-1"
        assert dumped["field_or_chunk_id"] == ref.field == "valueQuantity.value"
        assert dumped["quote_or_value"] == ref.value == "2.34"
        assert dumped["source_type"] == "fhir"

    def test_fhir_page_or_section_is_the_field_path_not_a_fabricated_page(self) -> None:
        """A FHIR resource has no pages. The honest analogue is the
        resource-qualified field path — the exact spot the verifier re-reads."""
        dumped = _fhir_ref().model_dump(mode="json")
        assert dumped["page_or_section"] == "Observation.valueQuantity.value"

    def test_verifier_fields_survive_alongside_the_spec_keys(self) -> None:
        """Deriving rather than renaming: the gate's fields are still there."""
        dumped = _fhir_ref().model_dump(mode="json")
        assert {"resource_type", "resource_id", "field", "value"} <= set(dumped)
        assert dumped["value"] == "2.34"

    def test_source_type_remains_the_union_discriminator(self) -> None:
        """Raw model output still validates straight into the right concrete type."""
        payload = {
            "text": "HbA1c 6.9% on the outside lab report.",
            "source_ref": _doc_citation().model_dump(mode="json"),
        }
        claim = Claim.model_validate(payload)
        assert isinstance(claim.source_ref, DocumentCitation)


class TestNonFhirClaimsAreExpressible:
    """`Claim.source_ref` is the real union — the second defect."""

    def test_claim_can_carry_a_document_citation(self) -> None:
        claim = Claim(text="HbA1c 6.9% on the outside lab report.", source_ref=_doc_citation())
        assert isinstance(claim.source_ref, DocumentCitation)
        assert claim.source_ref.quote_or_value == "6.9"

    def test_claim_can_carry_a_guideline_citation(self) -> None:
        claim = Claim(text="Guideline target is HbA1c < 7.0%.", source_ref=_guideline_citation())
        assert isinstance(claim.source_ref, GuidelineCitation)

    def test_document_claim_round_trips_through_the_repository_serializer(self) -> None:
        """Rehydrates as a DocumentCitation — not silently coerced to fhir."""
        claim = Claim(text="HbA1c 6.9% on the outside lab report.", source_ref=_doc_citation())
        back = _claim_from_json(_claim_to_json(claim))
        assert isinstance(back.source_ref, DocumentCitation)
        assert back.source_ref == claim.source_ref
        assert back.source_ref.page_or_section == 2
        assert back.source_ref.bbox == [0.1, 0.2, 0.3, 0.05]
        assert back.source_ref.confidence == 0.94

    def test_guideline_claim_round_trips_through_the_repository_serializer(self) -> None:
        claim = Claim(text="Guideline target is HbA1c < 7.0%.", source_ref=_guideline_citation())
        back = _claim_from_json(_claim_to_json(claim))
        assert isinstance(back.source_ref, GuidelineCitation)
        assert back.source_ref == claim.source_ref

    def test_fhir_claim_still_round_trips_unchanged(self) -> None:
        claim = Claim(text="Troponin I 2.34 ng/mL.", source_ref=_fhir_ref())
        back = _claim_from_json(_claim_to_json(claim))
        assert isinstance(back.source_ref, FhirReference)
        assert back.source_ref == claim.source_ref

    def test_legacy_row_without_source_type_rehydrates_as_fhir(self) -> None:
        """A Week-1 row predates the discriminator — still reads back as fhir."""
        legacy = {
            "text": "Troponin I 2.34 ng/mL.",
            "source_ref": {
                "resource_type": "Observation",
                "resource_id": "trop-1",
                "field": "valueQuantity.value",
                "value": "2.34",
            },
        }
        claim = _claim_from_json(legacy)
        assert isinstance(claim.source_ref, FhirReference)
        assert claim.source_ref.value == "2.34"

    def test_derived_spec_keys_are_not_persisted_back_as_fields(self) -> None:
        """The computed keys are output-only: a row carrying them rehydrates from
        its stored record fields, so a stale/oddball alias can never overwrite the
        values the verifier compares."""
        row = _claim_to_json(Claim(text="Troponin I 2.34 ng/mL.", source_ref=_fhir_ref()))
        row["source_ref"]["source_id"] = "tampered"
        row["source_ref"]["quote_or_value"] = "9.99"
        back = _claim_from_json(row)
        assert isinstance(back.source_ref, FhirReference)
        assert back.source_ref.resource_id == "trop-1"
        assert back.source_ref.value == "2.34"
        assert back.source_ref.source_id == "trop-1"


class TestStaticCitationContract:
    """The half of the contract only a type checker can see.

    ``Claim.source_ref`` was annotated ``SkipValidation[FhirReference]``: pydantic
    tolerated a document citation at *runtime*, so no runtime test could catch it
    — but the declared type said fhir, so no producer could construct one and the
    spec's document/guideline variants were dead. These assertions run mypy for
    real, because the failure mode they guard is invisible to the interpreter.
    """

    def _mypy(self, source: str, tmp_path: Any) -> str:
        api = pytest.importorskip("mypy.api", reason="mypy is a dev-only dependency")
        snippet = tmp_path / "snippet.py"
        snippet.write_text(source)
        out, _err, _code = api.run(["--strict", "--no-error-summary", str(snippet)])
        return str(out)

    def test_source_ref_is_declared_as_the_full_citation_union(self, tmp_path: Any) -> None:
        """The defect, asserted at the exact seam: the *declared* type.

        Asserted via `assert_type` on the attribute rather than by constructing a
        document claim — pydantic's mypy plugin leaves `__init__` args untyped by
        default, so a constructor call type-checks either way and would prove
        nothing.
        """
        out = self._mypy(
            "from typing import assert_type\n"
            "from copilot.domain.contracts import Claim\n"
            "from copilot.domain.primitives import (\n"
            "    DocumentCitation, FhirReference, GuidelineCitation)\n"
            "def f(c: Claim) -> None:\n"
            "    assert_type(\n"
            "        c.source_ref, FhirReference | DocumentCitation | GuidelineCitation)\n",
            tmp_path,
        )
        assert "error:" not in out, (
            "Claim.source_ref must be declared as the full citation union — a "
            f"document-cited claim is otherwise inexpressible:\n{out}"
        )

    def test_verification_result_source_ref_is_also_the_union(self, tmp_path: Any) -> None:
        """The gate's output carries the claim's citation back verbatim."""
        out = self._mypy(
            "from typing import assert_type\n"
            "from copilot.domain.contracts import VerificationClaimResult\n"
            "from copilot.domain.primitives import (\n"
            "    DocumentCitation, FhirReference, GuidelineCitation)\n"
            "def f(r: VerificationClaimResult) -> None:\n"
            "    assert_type(\n"
            "        r.source_ref, FhirReference | DocumentCitation | GuidelineCitation)\n",
            tmp_path,
        )
        assert "error:" not in out, f"VerificationClaimResult.source_ref must be the union:\n{out}"

    def test_unguarded_variant_read_is_a_type_error(self, tmp_path: Any) -> None:
        """The hazard: widening the union is only safe while readers must narrow.

        If this ever passes, an unguarded `source_ref.value` reads clean under
        mypy and crashes at runtime on a document claim — exactly the failure the
        isinstance guards exist to prevent.
        """
        out = self._mypy(
            "from copilot.domain.contracts import Claim\n"
            "def f(c: Claim) -> str:\n"
            "    return c.source_ref.value\n",
            tmp_path,
        )
        assert "union-attr" in out, (
            "reading a fhir-only field off the citation union must be a type error "
            f"— readers would go unguarded:\n{out}"
        )


class TestDocumentClaimDbRoundTrip:
    """The document-cited claim survives a real memory-file write + read."""

    pytestmark = pytest.mark.asyncio

    @pytest_asyncio.fixture
    async def session(self) -> AsyncSession:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            yield s
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_mixed_citation_memory_file_survives_save_and_read(
        self, session: AsyncSession
    ) -> None:
        repo = MemoryRepository(session)
        s_in = _summary(
            Claim(text="Troponin I 2.34 ng/mL.", source_ref=_fhir_ref()),
            Claim(text="HbA1c 6.9% on the outside lab report.", source_ref=_doc_citation()),
            Claim(text="Guideline target is HbA1c < 7.0%.", source_ref=_guideline_citation()),
        )
        await repo.save_memory_file(s_in)
        s_out = await repo.get_memory_file(s_in.patient_id)

        assert s_out is not None
        assert [type(c.source_ref) for c in s_out.claims] == [
            FhirReference,
            DocumentCitation,
            GuidelineCitation,
        ]
        assert s_out.claims == s_in.claims


class TestFailClosedGateStillHolds:
    """Widening the union must not soften the verifier — unverifiable is dropped."""

    pytestmark = pytest.mark.asyncio

    @pytest.mark.asyncio
    async def test_document_claim_whose_fact_is_absent_is_withheld(self) -> None:
        """The agent-store row could not be re-materialized → attribution fails."""
        context = build_context_from_resources([], document_facts={}, doc_confidence_threshold=0.5)
        result = await Verifier().verify_memory_file(
            _summary(Claim(text="HbA1c 6.9%.", source_ref=_doc_citation())), context
        )
        assert result.action is VerificationAction.withheld
        assert result.passed is False
        assert result.claims[0].attribution_ok is False

    @pytest.mark.asyncio
    async def test_document_claim_with_unsupported_fact_is_withheld(self) -> None:
        """No-invention gate: the value was never located on the page."""
        context = build_context_from_resources(
            [],
            document_facts={"907": DocumentFact(value="6.9", supported=False, match_confidence=1.0)},
            doc_confidence_threshold=0.5,
        )
        result = await Verifier().verify_memory_file(
            _summary(Claim(text="HbA1c 6.9%.", source_ref=_doc_citation())), context
        )
        assert result.action is VerificationAction.withheld

    @pytest.mark.asyncio
    async def test_document_claim_below_confidence_floor_is_withheld(self) -> None:
        context = build_context_from_resources(
            [],
            document_facts={"907": DocumentFact(value="6.9", supported=True, match_confidence=0.2)},
            doc_confidence_threshold=0.5,
        )
        result = await Verifier().verify_memory_file(
            _summary(Claim(text="HbA1c 6.9%.", source_ref=_doc_citation())), context
        )
        assert result.action is VerificationAction.withheld

    @pytest.mark.asyncio
    async def test_document_claim_contradicting_the_stored_fact_is_withheld(self) -> None:
        """The claim says 9.9; the stored fact says 6.9 — dropped, not served."""
        context = build_context_from_resources(
            [],
            document_facts={"907": DocumentFact(value="6.9", supported=True, match_confidence=1.0)},
            doc_confidence_threshold=0.5,
        )
        result = await Verifier().verify_memory_file(
            _summary(Claim(text="HbA1c 9.9%.", source_ref=_doc_citation(value="9.9"))), context
        )
        assert result.action is VerificationAction.withheld
        assert result.claims[0].value_match is False

    @pytest.mark.asyncio
    async def test_fully_grounded_document_claim_is_served(self) -> None:
        """The other half of fail-closed: an honest, re-checkable claim survives."""
        context = build_context_from_resources(
            [],
            document_facts={"907": DocumentFact(value="6.9", supported=True, match_confidence=0.94)},
            doc_confidence_threshold=0.5,
        )
        result = await Verifier().verify_memory_file(
            _summary(Claim(text="HbA1c 6.9%.", source_ref=_doc_citation())), context
        )
        assert result.action is VerificationAction.served
        assert result.claims[0].attribution_ok is True
        assert result.claims[0].value_match is True

    @pytest.mark.asyncio
    async def test_guideline_claim_without_its_chunk_is_withheld(self) -> None:
        result = await Verifier().verify_memory_file(
            _summary(Claim(text="Target HbA1c < 7.0%.", source_ref=_guideline_citation())),
            build_context_from_resources([], guideline_chunks={}),
        )
        assert result.action is VerificationAction.withheld

    @pytest.mark.asyncio
    async def test_guideline_claim_quoting_text_absent_from_the_chunk_is_withheld(self) -> None:
        """The quote must appear verbatim — a paraphrase is not grounded."""
        result = await Verifier().verify_memory_file(
            _summary(Claim(text="Target HbA1c < 7.0%.", source_ref=_guideline_citation())),
            build_context_from_resources(
                [], guideline_chunks={"338": "Screen annually for retinopathy."}
            ),
        )
        assert result.action is VerificationAction.withheld

    @pytest.mark.asyncio
    async def test_mixed_batch_degrades_dropping_only_the_unverifiable_claim(self) -> None:
        """A provable fhir claim survives while an unprovable document claim is
        dropped — the union did not let an ungrounded claim ride along."""
        resource = {
            "resourceType": "Observation",
            "id": "trop-1",
            "valueQuantity": {"value": 2.34, "unit": "ng/mL"},
            "effectiveDateTime": "2026-07-08T03:00:00Z",
        }
        context = build_context_from_resources(
            [resource], document_facts={}, doc_confidence_threshold=0.5
        )
        result = await Verifier().verify_memory_file(
            _summary(
                Claim(text="Troponin I 2.34 ng/mL.", source_ref=_fhir_ref()),
                Claim(text="HbA1c 6.9%.", source_ref=_doc_citation()),
            ),
            context,
        )
        assert result.action is VerificationAction.degraded
        passed = [c for c in result.claims if c.attribution_ok and c.value_match]
        assert len(passed) == 1
        assert isinstance(passed[0].source_ref, FhirReference)
