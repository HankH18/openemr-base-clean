"""Round-trip tests for the chat + rounds repository methods (in-memory SQLite)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from copilot.domain.primitives import ClinicianId, PatientId
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


class TestConversations:
    async def test_create_append_and_read_in_order(self, session: AsyncSession) -> None:
        repo = MemoryRepository(session)
        conv_id = await repo.create_conversation(
            ClinicianId(value=42), PatientId(value=1015), "corr-abc123"
        )
        assert conv_id > 0

        await repo.append_message(conv_id, "user", "What is the latest troponin?")
        await repo.append_message(conv_id, "assistant", "Troponin I 2.34 ng/mL.")

        messages = await repo.get_conversation_messages(conv_id)
        assert [(m.role, m.content) for m in messages] == [
            ("user", "What is the latest troponin?"),
            ("assistant", "Troponin I 2.34 ng/mL."),
        ]
        assert all(isinstance(m.created_at, datetime) for m in messages)

    async def test_messages_scoped_to_conversation(self, session: AsyncSession) -> None:
        repo = MemoryRepository(session)
        conv_a = await repo.create_conversation(
            ClinicianId(value=1), PatientId(value=2), "corr-aaa11111"
        )
        conv_b = await repo.create_conversation(
            ClinicianId(value=1), PatientId(value=3), "corr-bbb22222"
        )
        await repo.append_message(conv_a, "user", "a-only")
        await repo.append_message(conv_b, "user", "b-only")

        assert [m.content for m in await repo.get_conversation_messages(conv_a)] == ["a-only"]
        assert [m.content for m in await repo.get_conversation_messages(conv_b)] == ["b-only"]

    async def test_empty_conversation_returns_empty_list(self, session: AsyncSession) -> None:
        repo = MemoryRepository(session)
        conv_id = await repo.create_conversation(
            ClinicianId(value=7), PatientId(value=8), "corr-empty01"
        )
        assert await repo.get_conversation_messages(conv_id) == []


class TestRoundingCursor:
    async def test_get_missing_returns_none(self, session: AsyncSession) -> None:
        repo = MemoryRepository(session)
        assert await repo.get_rounding_cursor(ClinicianId(value=42)) is None

    async def test_upsert_then_get(self, session: AsyncSession) -> None:
        repo = MemoryRepository(session)
        cid = ClinicianId(value=42)
        await repo.upsert_rounding_cursor(cid, [1015, 1016, 1017], 1, [1015])

        cursor = await repo.get_rounding_cursor(cid)
        assert cursor is not None
        assert cursor.clinician_id == cid
        assert cursor.ordered_patient_ids == [1015, 1016, 1017]
        assert cursor.current_index == 1
        assert cursor.completed_ids == [1015]

    async def test_upsert_updates_existing(self, session: AsyncSession) -> None:
        repo = MemoryRepository(session)
        cid = ClinicianId(value=42)
        await repo.upsert_rounding_cursor(cid, [1015, 1016], 0, [])
        await repo.upsert_rounding_cursor(cid, [1015, 1016, 1017], 2, [1015, 1016])

        cursor = await repo.get_rounding_cursor(cid)
        assert cursor is not None
        assert cursor.ordered_patient_ids == [1015, 1016, 1017]
        assert cursor.current_index == 2
        assert cursor.completed_ids == [1015, 1016]


class TestLastSeen:
    async def test_get_missing_returns_none(self, session: AsyncSession) -> None:
        repo = MemoryRepository(session)
        assert await repo.get_last_seen(ClinicianId(value=42), PatientId(value=1015)) is None

    async def test_set_then_get_with_explicit_time(self, session: AsyncSession) -> None:
        repo = MemoryRepository(session)
        cid = ClinicianId(value=42)
        pid = PatientId(value=1015)
        seen = datetime(2026, 7, 8, 5, 30, tzinfo=UTC)
        await repo.set_last_seen(cid, pid, seen)

        out = await repo.get_last_seen(cid, pid)
        assert out is not None
        assert out == seen.replace(tzinfo=None)

    async def test_set_defaults_to_now(self, session: AsyncSession) -> None:
        repo = MemoryRepository(session)
        cid = ClinicianId(value=42)
        pid = PatientId(value=1015)
        await repo.set_last_seen(cid, pid)

        out = await repo.get_last_seen(cid, pid)
        assert out is not None
        assert isinstance(out, datetime)

    async def test_set_upserts_on_pair(self, session: AsyncSession) -> None:
        repo = MemoryRepository(session)
        cid = ClinicianId(value=42)
        pid = PatientId(value=1015)
        await repo.set_last_seen(cid, pid, datetime(2026, 7, 8, 5, tzinfo=UTC))
        await repo.set_last_seen(cid, pid, datetime(2026, 7, 8, 9, tzinfo=UTC))

        out = await repo.get_last_seen(cid, pid)
        assert out == datetime(2026, 7, 8, 9).replace(tzinfo=None)
