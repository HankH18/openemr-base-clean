"""``FhirReference.unit`` must survive persistence and be grounded on every path.

Commit b9834d7 added the unit to the verification gate so it compares a QUANTITY
and not a bare number — "2.34 ng/mL" against a record of 2.34 ng/L is a 1000x
error the value match alone cannot see. It wired the two chat agents but left two
holes, which these pin:

1. **The persist→rehydrate loss.** ``_citation_to_json`` serialized every
   ``FhirReference`` field except ``unit``, so a claim written to the memory file
   came back with ``unit=None``. Because a ``None`` unit *deliberately*
   short-circuits the gate (the documented policy for a unit-less claim), the
   loss was silent: the gate did not fail, it simply stopped checking. Every
   persisted claim was ungated on dimension.
2. **Ungrounded units on the rounds/synthesizer path.** ``StubSynthesizer`` never
   called ``extract_unit``, so a card claim read "Observation Troponin I: 2.34."
   — a lab value a clinician cannot judge — and its source_ref carried no unit
   for the gate to re-compare.

The round trip is proven against a **file-backed** SQLite DB, not the
``:memory:`` default: a per-command fresh empty database cannot witness a
persistence bug.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from copilot.domain.contracts import Claim, MemoryFileSummary, VerificationAction
from copilot.domain.primitives import FhirReference, PatientId, ResourceType
from copilot.memory import Base, MemoryRepository
from copilot.memory.models import MemoryFileRow
from copilot.rounds.summary import build_summary_claims
from copilot.verification.core import Verifier, build_context_from_resources
from copilot.worker.synthesizer import StubSynthesizer, SynthesisInput

# --- Helpers ---------------------------------------------------------------


@pytest_asyncio.fixture
async def session(tmp_path: Path) -> Any:
    """A FILE-BACKED SQLite DB with all tables created.

    Deliberately not ``:memory:``. This test exists to prove a value survives a
    write and a read; a database that is recreated empty per connection cannot
    distinguish "persisted correctly" from "never persisted at all".
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'round_trip.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


def _obs(unit: str | None = "ng/L", value: float = 2.34) -> dict[str, Any]:
    """A troponin Observation. ``unit=None`` omits ``valueQuantity.unit`` entirely."""
    quantity: dict[str, Any] = {"value": value}
    if unit is not None:
        quantity["unit"] = unit
    return {
        "resourceType": "Observation",
        "id": "trop-1",
        "status": "final",
        "code": {"text": "Troponin I"},
        "valueQuantity": quantity,
        "effectiveDateTime": "2026-07-08T03:00:00+00:00",
    }


def _claim(unit: str | None, *, text: str = "Observation Troponin I: 2.34 ng/L.") -> Claim:
    return Claim(
        text=text,
        source_ref=FhirReference(
            resource_type=ResourceType.Observation,
            resource_id="trop-1",
            field="valueQuantity.value",
            value="2.34",
            unit=unit,
            timestamp=datetime(2026, 7, 8, 3, tzinfo=UTC),
        ),
    )


def _summary(*claims: Claim, pid: int = 1015) -> MemoryFileSummary:
    return MemoryFileSummary(
        patient_id=PatientId(value=pid),
        claims=list(claims),
        acuity_score=8.5,
        rank_reason="Critical trop",
        synthesized_at=datetime(2026, 7, 8, 5, tzinfo=UTC),
        source_watermark=datetime(2026, 7, 8, 3, tzinfo=UTC),
        content_hash="a" * 64,
    )


async def _round_trip(session: AsyncSession, summary: MemoryFileSummary) -> MemoryFileSummary:
    """Persist a summary and read it back as a fresh object."""
    repo = MemoryRepository(session)
    await repo.save_memory_file(summary)
    await session.commit()
    session.expunge_all()  # never let the identity map answer the read
    loaded = await repo.get_memory_file(summary.patient_id)
    assert loaded is not None
    return loaded


# --- 1. The persist -> rehydrate loss --------------------------------------


class TestUnitSurvivesPersistence:
    async def test_grounded_unit_survives_round_trip(self, session: AsyncSession) -> None:
        """THE HEADLINE: this returned unit=None before the fix."""
        loaded = await _round_trip(session, _summary(_claim(unit="ng/L")))

        ref = loaded.claims[0].source_ref
        assert isinstance(ref, FhirReference)
        assert ref.unit == "ng/L"

    async def test_unit_is_written_into_the_persisted_json(self, session: AsyncSession) -> None:
        """The unit reaches the column, not just the in-memory object."""
        repo = MemoryRepository(session)
        await repo.save_memory_file(_summary(_claim(unit="ng/L")))
        await session.commit()
        session.expunge_all()

        row = await session.get(MemoryFileRow, 1015)
        assert row is not None
        assert row.summary["claims"][0]["source_ref"]["unit"] == "ng/L"

    async def test_unit_survives_verbatim_and_is_never_case_folded(
        self, session: AsyncSession
    ) -> None:
        """``mg`` must not come back ``Mg`` — milligram vs megagram is 1e9."""
        loaded = await _round_trip(session, _summary(_claim(unit="mg")))

        ref = loaded.claims[0].source_ref
        assert isinstance(ref, FhirReference)
        assert ref.unit == "mg"

    async def test_changes_claims_also_carry_unit(self, session: AsyncSession) -> None:
        """``changes`` serializes through the same helper — pin it too."""
        summary = _summary().model_copy(update={"changes": [_claim(unit="ng/L")]})
        loaded = await _round_trip(session, summary)

        ref = loaded.changes[0].source_ref
        assert isinstance(ref, FhirReference)
        assert ref.unit == "ng/L"


# --- 2. Backward compatibility: legacy rows ---------------------------------


class TestLegacyRowsRehydrate:
    async def test_legacy_row_without_unit_key_rehydrates_to_none(
        self, session: AsyncSession
    ) -> None:
        """A row persisted before `unit` existed has no such key: None, no crash."""
        session.add(
            MemoryFileRow(
                patient_id=2020,
                summary={
                    "patient_id": 2020,
                    "claims": [
                        {
                            "text": "Observation Troponin I: 2.34.",
                            "source_ref": {
                                # Exactly the pre-b9834d7 shape — no "unit" key.
                                "source_type": "fhir",
                                "resource_type": "Observation",
                                "resource_id": "trop-1",
                                "field": "valueQuantity.value",
                                "value": "2.34",
                                "last_updated": None,
                                "timestamp": None,
                            },
                        }
                    ],
                },
                acuity_score=1.0,
                rank_reason="legacy",
                synthesized_at=datetime(2026, 7, 8, 5),
                source_watermark=datetime(2026, 7, 8, 3),
                content_hash="b" * 64,
            )
        )
        await session.commit()
        session.expunge_all()

        loaded = await MemoryRepository(session).get_memory_file(PatientId(value=2020))
        assert loaded is not None
        ref = loaded.claims[0].source_ref
        assert isinstance(ref, FhirReference)
        assert ref.unit is None

    async def test_week1_row_without_source_type_or_unit_still_rehydrates(
        self, session: AsyncSession
    ) -> None:
        """The oldest shape: no `source_type` discriminator AND no `unit`."""
        session.add(
            MemoryFileRow(
                patient_id=2021,
                summary={
                    "patient_id": 2021,
                    "claims": [
                        {
                            "text": "Observation Troponin I: 2.34.",
                            "source_ref": {
                                "resource_type": "Observation",
                                "resource_id": "trop-1",
                                "field": "valueQuantity.value",
                                "value": "2.34",
                            },
                        }
                    ],
                },
                acuity_score=1.0,
                rank_reason="week1",
                synthesized_at=datetime(2026, 7, 8, 5),
                source_watermark=datetime(2026, 7, 8, 3),
                content_hash="c" * 64,
            )
        )
        await session.commit()
        session.expunge_all()

        loaded = await MemoryRepository(session).get_memory_file(PatientId(value=2021))
        assert loaded is not None
        ref = loaded.claims[0].source_ref
        assert isinstance(ref, FhirReference)
        assert ref.unit is None

    async def test_legacy_unit_less_claim_is_not_gated_on_units(self) -> None:
        """The policy the back-compat default relies on: None short-circuits, never withholds."""
        result = await Verifier().verify_memory_file(
            _summary(_claim(unit=None, text="Observation Troponin I: 2.34.")),
            build_context_from_resources([_obs(unit="ng/L")]),
        )
        assert result.action is VerificationAction.served


# --- 3. The rounds / synthesizer path grounds units -------------------------


class TestRoundsPathGroundsUnit:
    async def test_stub_synthesizer_claim_text_carries_the_unit(self) -> None:
        """The card read "Observation Troponin I: 2.34." — a number with no dimension."""
        summary = await StubSynthesizer().synthesize(
            SynthesisInput(
                patient_id=PatientId(value=1015),
                resources=[_obs(unit="ng/L")],
                source_watermark=datetime(2026, 7, 8, tzinfo=UTC),
            )
        )
        assert summary.claims[0].text == "Observation Troponin I: 2.34 ng/L."

    async def test_stub_synthesizer_grounds_unit_in_source_ref(self) -> None:
        """Text alone is not enough — the gate reads source_ref."""
        summary = await StubSynthesizer().synthesize(
            SynthesisInput(
                patient_id=PatientId(value=1015),
                resources=[_obs(unit="ng/L")],
                source_watermark=datetime(2026, 7, 8, tzinfo=UTC),
            )
        )
        ref = summary.claims[0].source_ref
        assert isinstance(ref, FhirReference)
        assert ref.unit == "ng/L"

    async def test_synthesized_claim_survives_its_own_verification(self) -> None:
        """End-to-end: what the synthesizer emits must verify against its own source."""
        resource = _obs(unit="ng/L")
        summary = await StubSynthesizer().synthesize(
            SynthesisInput(
                patient_id=PatientId(value=1015),
                resources=[resource],
                source_watermark=datetime(2026, 7, 8, tzinfo=UTC),
            )
        )
        result = await Verifier().verify_memory_file(
            summary, build_context_from_resources([resource])
        )
        assert result.action is VerificationAction.served

    async def test_stub_synthesizer_unit_less_observation_renders_bare_value(self) -> None:
        """No unit in the record ⇒ value alone, never the string "None"."""
        summary = await StubSynthesizer().synthesize(
            SynthesisInput(
                patient_id=PatientId(value=1015),
                resources=[_obs(unit=None)],
                source_watermark=datetime(2026, 7, 8, tzinfo=UTC),
            )
        )
        assert summary.claims[0].text == "Observation Troponin I: 2.34."
        ref = summary.claims[0].source_ref
        assert isinstance(ref, FhirReference)
        assert ref.unit is None

    def test_rounds_summary_grounds_the_verbatim_unit_not_the_display_label(self) -> None:
        """The trap: `_unit()` maps Cel→°C for the CARD. source_ref must stay verbatim.

        Grounding the display label would write °C where a live re-fetch re-derives
        `Cel`, and the gate — which refuses to case-fold units — would withhold
        every temperature as a unit mismatch.
        """
        temp = {
            "resourceType": "Observation",
            "id": "temp-1",
            "status": "final",
            "code": {"text": "Body temperature"},
            "valueQuantity": {"value": 37.0, "unit": "Cel"},
            "effectiveDateTime": "2026-07-08T03:00:00+00:00",
        }
        claims = build_summary_claims([temp])

        ref = claims[0].source_ref
        assert isinstance(ref, FhirReference)
        assert ref.unit == "Cel"  # verbatim, NOT "°C"
        assert "°C" in claims[0].text  # the card still shows the friendly label

    def test_rounds_summary_claim_verifies_against_its_source(self) -> None:
        """The verbatim grounding must actually survive the gate."""
        temp = {
            "resourceType": "Observation",
            "id": "temp-1",
            "status": "final",
            "code": {"text": "Body temperature"},
            "valueQuantity": {"value": 37.0, "unit": "Cel"},
            "effectiveDateTime": "2026-07-08T03:00:00+00:00",
        }
        ref = build_summary_claims([temp])[0].source_ref
        assert isinstance(ref, FhirReference)
        from copilot.agent.grounding import extract_unit

        assert extract_unit(temp) == ref.unit


# --- 4. Non-quantity claims round-trip as unit-less -------------------------


class TestNonQuantityClaims:
    async def test_medication_name_round_trips_with_unit_none(
        self, session: AsyncSession
    ) -> None:
        """A drug NAME has no dimension — None must mean None, not "" or "None"."""
        med = Claim(
            text="Medication: Hydromorphone.",
            source_ref=FhirReference(
                resource_type=ResourceType.MedicationRequest,
                resource_id="med-1",
                field="medicationCodeableConcept.text",
                value="Hydromorphone",
                timestamp=datetime(2026, 7, 8, 3, tzinfo=UTC),
            ),
        )
        loaded = await _round_trip(session, _summary(med))

        ref = loaded.claims[0].source_ref
        assert isinstance(ref, FhirReference)
        assert ref.unit is None
        assert ref.value == "Hydromorphone"

    async def test_date_valued_claim_round_trips_with_unit_none(
        self, session: AsyncSession
    ) -> None:
        """A date is not a quantity either."""
        dated = Claim(
            text="Encounter: Inpatient admission.",
            source_ref=FhirReference(
                resource_type=ResourceType.Encounter,
                resource_id="enc-1",
                field="period.start",
                value="2026-07-08",
            ),
        )
        loaded = await _round_trip(session, _summary(dated))

        ref = loaded.claims[0].source_ref
        assert isinstance(ref, FhirReference)
        assert ref.unit is None

    async def test_synthesizer_medication_claim_has_no_unit(self) -> None:
        """The synthesizer must not invent a dimension for a name."""
        summary = await StubSynthesizer().synthesize(
            SynthesisInput(
                patient_id=PatientId(value=1015),
                resources=[
                    {
                        "resourceType": "MedicationRequest",
                        "id": "med-1",
                        "status": "active",
                        "medicationCodeableConcept": {"text": "Hydromorphone"},
                    }
                ],
                source_watermark=datetime(2026, 7, 8, tzinfo=UTC),
            )
        )
        assert summary.claims[0].text == "Medication: Hydromorphone."
        ref = summary.claims[0].source_ref
        assert isinstance(ref, FhirReference)
        assert ref.unit is None


# --- 5. Verification behavior is unchanged ----------------------------------


class TestVerificationUnchangedAcrossRoundTrip:
    """The point of persisting the unit: the gate still works on a REHYDRATED claim."""

    async def test_matching_unit_serves_after_round_trip(self, session: AsyncSession) -> None:
        loaded = await _round_trip(session, _summary(_claim(unit="ng/L")))
        result = await Verifier().verify_memory_file(
            loaded, build_context_from_resources([_obs(unit="ng/L")])
        )
        assert result.action is VerificationAction.served
        assert result.claims[0].value_match is True

    async def test_mismatched_unit_withheld_after_round_trip(self, session: AsyncSession) -> None:
        """THE SAFETY CASE: claim ng/mL, record ng/L — 1000x. The rehydrated claim
        must still be caught. Before the fix the unit rehydrated to None and the
        gate short-circuited, serving this."""
        claim = _claim(unit="ng/mL", text="Observation Troponin I: 2.34 ng/mL.")
        loaded = await _round_trip(session, _summary(claim))

        result = await Verifier().verify_memory_file(
            loaded, build_context_from_resources([_obs(unit="ng/L")])
        )
        assert result.action is VerificationAction.withheld
        assert result.claims[0].value_match is False
        assert result.claims[0].attribution_ok is True
        assert "unit mismatch" in (result.claims[0].reason or "")

    @pytest.mark.parametrize(
        ("claimed", "record", "served"),
        [
            ("ng/L", "ng/L", True),
            ("ng/mL", "ng/L", False),
            ("mg", "Mg", False),  # milligram vs megagram must not fold together
            ("mg/dL ", "mg/dL", True),  # padding is not a dimension
        ],
    )
    async def test_gate_outcomes_preserved_through_persistence(
        self, session: AsyncSession, claimed: str, record: str, served: bool
    ) -> None:
        loaded = await _round_trip(session, _summary(_claim(unit=claimed)))
        result = await Verifier().verify_memory_file(
            loaded, build_context_from_resources([_obs(unit=record)])
        )
        assert (result.action is VerificationAction.served) is served
