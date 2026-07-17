"""Poller.tick end-to-end: change gate → hash confirm → synthesize."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from copilot.domain.contracts import MemoryFileSummary
from copilot.domain.primitives import PatientId, ResourceType, utcnow
from copilot.fhir.client import FhirClientError
from copilot.memory import Base, MemoryRepository
from copilot.worker.poller import (
    Poller,
    PollerTickOutcome,
)
from copilot.worker.synthesizer import (
    StubSynthesizer,
    SynthesisError,
    SynthesisInput,
)

pytestmark = pytest.mark.asyncio


# --- Test doubles ----------------------------------------------------------


class FakeFhir:
    """In-memory FhirClient double.

    ``counts`` maps ResourceType.value → int.  ``bundles`` maps
    ResourceType.value → list of resource dicts to return from search().
    Both default to empty (no changes) for any type not explicitly set.
    """

    def __init__(
        self,
        counts: dict[str, int] | None = None,
        bundles: dict[str, list[dict]] | None = None,
        *,
        count_raises: Exception | None = None,
        search_raises: Exception | None = None,
    ) -> None:
        self.counts = counts or {}
        self.bundles = bundles or {}
        self.count_raises = count_raises
        self.search_raises = search_raises
        self.searched: list[str] = []

    async def count_since(self, rt: ResourceType, patient_id, since) -> int:
        if self.count_raises is not None:
            raise self.count_raises
        return self.counts.get(rt.value, 0)

    async def search(self, rt: ResourceType, params) -> dict:
        if self.search_raises is not None:
            raise self.search_raises
        self.searched.append(rt.value)
        entries = [{"resource": r} for r in self.bundles.get(rt.value, [])]
        return {"resourceType": "Bundle", "entry": entries, "total": len(entries)}


class RecordingSynth:
    """Wraps StubSynthesizer to record whether synthesize() was called."""

    def __init__(self) -> None:
        self.calls: list[SynthesisInput] = []
        self._inner = StubSynthesizer()

    async def synthesize(self, inputs: SynthesisInput) -> MemoryFileSummary:
        self.calls.append(inputs)
        return await self._inner.synthesize(inputs)


class RaisingSynth:
    async def synthesize(self, inputs: SynthesisInput) -> MemoryFileSummary:
        raise SynthesisError("boom")


# --- Fixtures --------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


def _obs(
    id: str = "90045", value: float = 2.34, last_updated: str = "2026-07-08T03:00:00Z"
) -> dict:
    return {
        "resourceType": "Observation",
        "id": id,
        "status": "final",
        "valueQuantity": {"value": value, "unit": "ng/mL"},
        "meta": {"lastUpdated": last_updated},
    }


# --- Tests ----------------------------------------------------------------


class TestPollerTick:
    async def test_no_change_skips_pull_and_synthesis(self, session: AsyncSession) -> None:
        fhir = FakeFhir(counts={})  # all zero
        synth = RecordingSynth()
        repo = MemoryRepository(session)
        poller = Poller(fhir=fhir, synthesizer=synth, repository=repo)  # type: ignore[arg-type]

        result = await poller.tick(PatientId(value=1015))

        assert result.outcome == PollerTickOutcome.no_change
        assert result.memory_file is None
        assert synth.calls == []
        assert fhir.searched == []  # never pulled
        # sync_state was still updated (polled_at)
        row = await repo.get_sync_state(PatientId(value=1015))
        assert row is not None
        assert row.consecutive_failures == 0

    async def test_change_triggers_pull_and_synthesis(self, session: AsyncSession) -> None:
        fhir = FakeFhir(
            counts={ResourceType.Observation.value: 1},
            bundles={ResourceType.Observation.value: [_obs()]},
        )
        synth = RecordingSynth()
        repo = MemoryRepository(session)
        poller = Poller(fhir=fhir, synthesizer=synth, repository=repo)  # type: ignore[arg-type]

        result = await poller.tick(PatientId(value=1015))

        assert result.outcome == PollerTickOutcome.synthesized
        assert result.memory_file is not None
        assert len(result.memory_file.claims) == 1
        assert result.memory_file.claims[0].source_ref.value == "2.34"
        assert len(synth.calls) == 1

    async def test_hash_unchanged_skips_synthesis_but_bumps_watermark(
        self, session: AsyncSession
    ) -> None:
        """
        Second poll returns the SAME resource (same hash) but with a
        moved lastUpdated timestamp — the poller should skip Claude and
        bump the watermark, cost-scales-with-change proof.
        """
        obs = _obs(last_updated="2026-07-08T03:00:00Z")
        fhir = FakeFhir(
            counts={ResourceType.Observation.value: 1},
            bundles={ResourceType.Observation.value: [obs]},
        )
        synth = RecordingSynth()
        repo = MemoryRepository(session)
        poller = Poller(fhir=fhir, synthesizer=synth, repository=repo)  # type: ignore[arg-type]

        # First tick: synthesizes.
        first = await poller.tick(PatientId(value=1015))
        assert first.outcome == PollerTickOutcome.synthesized

        # Persist the sync_state with the hash the poller returned so
        # the next tick sees prior_hash == new_hash.
        assert first.memory_file is not None
        await repo.upsert_sync_state(
            PatientId(value=1015),
            polled_at=utcnow().replace(tzinfo=None),
            success_at=utcnow().replace(tzinfo=None),
            watermark=first.memory_file.source_watermark.replace(tzinfo=None),
            content_hash=first.memory_file.content_hash,
            consecutive_failures=0,
        )

        # Second tick: same resource, changed only lastUpdated → hash equal.
        obs2 = _obs(last_updated="2026-07-08T04:00:00Z")
        fhir.bundles = {ResourceType.Observation.value: [obs2]}
        second = await poller.tick(PatientId(value=1015))
        assert second.outcome == PollerTickOutcome.hash_unchanged
        assert len(synth.calls) == 1  # unchanged from first tick

    async def test_fhir_count_error_records_failure(self, session: AsyncSession) -> None:
        fhir = FakeFhir(count_raises=FhirClientError("upstream 500"))
        synth = RecordingSynth()
        repo = MemoryRepository(session)
        poller = Poller(fhir=fhir, synthesizer=synth, repository=repo)  # type: ignore[arg-type]

        result = await poller.tick(PatientId(value=1015))
        assert result.outcome == PollerTickOutcome.error
        assert result.error is not None and "count query failed" in result.error

        row = await repo.get_sync_state(PatientId(value=1015))
        assert row is not None
        assert row.consecutive_failures == 1

    async def test_synthesis_error_records_failure_no_persist(self, session: AsyncSession) -> None:
        fhir = FakeFhir(
            counts={ResourceType.Observation.value: 1},
            bundles={ResourceType.Observation.value: [_obs()]},
        )
        repo = MemoryRepository(session)
        poller = Poller(fhir=fhir, synthesizer=RaisingSynth(), repository=repo)  # type: ignore[arg-type]

        result = await poller.tick(PatientId(value=1015))
        assert result.outcome == PollerTickOutcome.error
        assert result.memory_file is None
        row = await repo.get_sync_state(PatientId(value=1015))
        assert row is not None
        assert row.consecutive_failures == 1

    async def test_returns_result_but_does_not_persist_memory_file(
        self, session: AsyncSession
    ) -> None:
        """The Poller returns a summary; the Scheduler (with
        verification in between) persists.  Poller.tick must never
        write to memory_file itself."""
        fhir = FakeFhir(
            counts={ResourceType.Observation.value: 1},
            bundles={ResourceType.Observation.value: [_obs()]},
        )
        repo = MemoryRepository(session)
        poller = Poller(fhir=fhir, synthesizer=StubSynthesizer(), repository=repo)  # type: ignore[arg-type]

        result = await poller.tick(PatientId(value=1015))
        assert result.outcome == PollerTickOutcome.synthesized
        assert await repo.get_memory_file(PatientId(value=1015)) is None
