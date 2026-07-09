"""MemoryRepository round-trip tests against an in-memory SQLite DB."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from copilot.domain.contracts import Claim, MemoryFileSummary
from copilot.domain.primitives import FhirReference, PatientId, ResourceType
from copilot.memory import Base, MemoryRepository

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    """Fresh in-memory DB with all tables created."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


def _summary(pid: int = 1015) -> MemoryFileSummary:
    return MemoryFileSummary(
        patient_id=PatientId(value=pid),
        claims=[
            Claim(
                text="Troponin I 2.34 ng/mL (critical high).",
                source_ref=FhirReference(
                    resource_type=ResourceType.Observation,
                    resource_id="90045",
                    field="valueQuantity.value",
                    value="2.34",
                    last_updated=datetime(2026, 7, 8, 3, tzinfo=UTC),
                ),
            ),
        ],
        acuity_score=8.5,
        rank_reason="Critical trop rise",
        synthesized_at=datetime(2026, 7, 8, 5, tzinfo=UTC),
        source_watermark=datetime(2026, 7, 8, 3, tzinfo=UTC),
        content_hash="a" * 64,
    )


class TestSyncState:
    async def test_first_write_inserts_then_read_returns_row(self, session: AsyncSession) -> None:
        repo = MemoryRepository(session)
        pid = PatientId(value=1015)
        assert await repo.get_sync_state(pid) is None

        polled = datetime(2026, 7, 8, 3, tzinfo=UTC).replace(tzinfo=None)
        await repo.upsert_sync_state(
            pid,
            polled_at=polled,
            success_at=polled,
            watermark=polled - timedelta(minutes=5),
            content_hash="deadbeef",
            consecutive_failures=0,
        )
        row = await repo.get_sync_state(pid)
        assert row is not None
        assert row.content_hash == "deadbeef"

    async def test_upsert_updates_existing_row(self, session: AsyncSession) -> None:
        repo = MemoryRepository(session)
        pid = PatientId(value=1015)
        polled = datetime(2026, 7, 8, tzinfo=UTC).replace(tzinfo=None)
        await repo.upsert_sync_state(
            pid,
            polled_at=polled,
            success_at=polled,
            watermark=polled,
            content_hash="a",
            consecutive_failures=0,
        )
        await repo.upsert_sync_state(
            pid,
            polled_at=polled,
            success_at=None,
            watermark=None,
            content_hash="b",
            consecutive_failures=3,
        )
        row = await repo.get_sync_state(pid)
        assert row is not None
        assert row.content_hash == "b"
        assert row.consecutive_failures == 3


class TestMemoryFile:
    async def test_save_and_read_roundtrip(self, session: AsyncSession) -> None:
        repo = MemoryRepository(session)
        s_in = _summary()
        await repo.save_memory_file(s_in)
        s_out = await repo.get_memory_file(s_in.patient_id)
        assert s_out is not None
        assert s_out.patient_id == s_in.patient_id
        assert s_out.acuity_score == s_in.acuity_score
        assert s_out.content_hash == s_in.content_hash
        assert len(s_out.claims) == 1
        assert s_out.claims[0].source_ref.value == "2.34"

    async def test_second_save_overwrites(self, session: AsyncSession) -> None:
        repo = MemoryRepository(session)
        pid = PatientId(value=1015)
        await repo.save_memory_file(_summary(pid.value))
        newer = MemoryFileSummary(
            patient_id=pid,
            claims=[],
            acuity_score=1.0,
            rank_reason="updated",
            synthesized_at=datetime(2026, 7, 8, 6, tzinfo=UTC),
            source_watermark=datetime(2026, 7, 8, 6, tzinfo=UTC),
            content_hash="b" * 64,
        )
        await repo.save_memory_file(newer)
        out = await repo.get_memory_file(pid)
        assert out is not None
        assert out.rank_reason == "updated"
        assert out.claims == []


class TestAudit:
    async def test_audit_record_inserts_row(self, session: AsyncSession) -> None:
        repo = MemoryRepository(session)
        await repo.record_audit(
            correlation_id="corr-abc",
            action="poller.tick",
            patient_id=PatientId(value=1015),
            resources_returned=["Observation/90045"],
        )
        from sqlalchemy import select

        from copilot.memory import AuditLogRow

        result = await session.execute(select(AuditLogRow))
        rows = result.scalars().all()
        assert len(rows) == 1
        assert rows[0].action == "poller.tick"
        assert rows[0].resources_returned == ["Observation/90045"]
