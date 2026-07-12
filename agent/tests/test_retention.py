"""Retention-sweep tests — the audit trail can NEVER be destroyed.

DB-backed against an in-memory SQLite (same fixture shape as
``test_repository.py``). Covers the floor-protected audit sweep (deletes
nothing under any config), the chat purge (disabled at 0 days, bounded at a
positive day count), and dry-run reporting.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from copilot.config import Settings
from copilot.domain.primitives import utcnow
from copilot.memory import AuditLogRow, Base, ConversationRow, MessageRow
from copilot.memory.retention import (
    HIPAA_AUDIT_FLOOR_YEARS,
    RetentionPolicy,
    _subtract_years,
    sweep_audit_log,
    sweep_chat,
)

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


def _naive_utc(dt: datetime) -> datetime:
    """Store timestamps the way the repository does: naive, UTC."""
    return dt.replace(tzinfo=None)


async def _add_audit(session: AsyncSession, at: datetime, tag: str) -> None:
    session.add(
        AuditLogRow(
            correlation_id=f"corr-{tag}",
            clinician_id=42,
            patient_id=1001,
            action="chat",
            resources_returned=[],
            at=_naive_utc(at),
        )
    )
    await session.flush()


async def _add_conversation(session: AsyncSession, created_at: datetime, tag: str) -> int:
    conv = ConversationRow(
        clinician_id=42,
        patient_id=1001,
        correlation_id=f"corr-{tag}",
        created_at=_naive_utc(created_at),
    )
    session.add(conv)
    await session.flush()
    session.add(
        MessageRow(
            conversation_id=conv.id,
            role="user",
            content=f"message-{tag}",
            created_at=_naive_utc(created_at),
        )
    )
    await session.flush()
    return conv.id


async def _audit_count(session: AsyncSession) -> int:
    return int((await session.scalar(select(func.count()).select_from(AuditLogRow))) or 0)


async def _conversation_count(session: AsyncSession) -> int:
    return int((await session.scalar(select(func.count()).select_from(ConversationRow))) or 0)


async def _message_count(session: AsyncSession) -> int:
    return int((await session.scalar(select(func.count()).select_from(MessageRow))) or 0)


class TestPolicy:
    async def test_from_settings_maps_defaults(self) -> None:
        policy = RetentionPolicy.from_settings(Settings())
        assert policy.audit_retention_years == HIPAA_AUDIT_FLOOR_YEARS  # default 6
        assert policy.chat_retention_days == 0  # default: chat purge disabled


class TestAuditFloor:
    async def test_sweep_never_deletes_rows_younger_than_floor(self, session: AsyncSession) -> None:
        now = utcnow()
        await _add_audit(session, now, "now")
        await _add_audit(session, _subtract_years(now, 1), "1y")
        await _add_audit(session, _subtract_years(now, 5), "5y")
        await _add_audit(session, _subtract_years(now, 6) - timedelta(days=1), "6y1d")
        assert await _audit_count(session) == 4

        policy = RetentionPolicy(audit_retention_years=6, chat_retention_days=0)
        result = await sweep_audit_log(session, policy, now)

        # Nothing is ever deleted, and every seeded row survives.
        assert result.deleted == 0
        assert await _audit_count(session) == 4
        assert result.scanned == 4
        # Only the >6-year row is even *reported* as eligible; the 5y/1y/now
        # rows are protected by the floor.
        assert result.eligible == 1
        assert result.below_floor is False

    async def test_below_floor_config_cannot_expose_recent_rows(
        self, session: AsyncSession
    ) -> None:
        """Even a misconfigured 3-year retention never marks <6y rows eligible."""
        now = utcnow()
        await _add_audit(session, _subtract_years(now, 4), "4y")  # <6y, must stay protected
        await _add_audit(session, _subtract_years(now, 7), "7y")  # >6y

        policy = RetentionPolicy(audit_retention_years=3, chat_retention_days=0)
        result = await sweep_audit_log(session, policy, now)

        assert result.below_floor is True
        assert result.deleted == 0
        assert await _audit_count(session) == 2
        # The 4-year row is younger than the 6-year floor ⇒ not eligible.
        assert result.eligible == 1


class TestChatPurge:
    async def test_zero_days_is_a_noop(self, session: AsyncSession) -> None:
        now = utcnow()
        await _add_conversation(session, now - timedelta(days=400), "old")

        policy = RetentionPolicy(audit_retention_years=6, chat_retention_days=0)
        result = await sweep_chat(session, policy, now)

        assert result.enabled is False
        assert result.deleted == 0
        assert result.cutoff is None
        assert await _conversation_count(session) == 1
        assert await _message_count(session) == 1

    async def test_positive_days_purges_only_old_conversations(self, session: AsyncSession) -> None:
        now = utcnow()
        await _add_conversation(session, now - timedelta(days=10), "recent")
        await _add_conversation(session, now - timedelta(days=60), "old")

        policy = RetentionPolicy(audit_retention_years=6, chat_retention_days=30)
        result = await sweep_chat(session, policy, now)

        assert result.enabled is True
        assert result.eligible == 1
        assert result.deleted == 1
        # Only the recent conversation (and its message) survive.
        assert await _conversation_count(session) == 1
        assert await _message_count(session) == 1
        surviving = (await session.execute(select(ConversationRow.correlation_id))).scalars().all()
        assert list(surviving) == ["corr-recent"]


class TestDryRun:
    async def test_dry_run_reports_without_deleting(self, session: AsyncSession) -> None:
        now = utcnow()
        await _add_conversation(session, now - timedelta(days=90), "old")
        await _add_audit(session, _subtract_years(now, 7), "7y")

        policy = RetentionPolicy(audit_retention_years=6, chat_retention_days=30)
        chat = await sweep_chat(session, policy, now, dry_run=True)
        audit = await sweep_audit_log(session, policy, now, dry_run=True)

        # Eligibility is reported...
        assert chat.eligible == 1
        assert audit.eligible == 1
        # ...but nothing is removed.
        assert chat.deleted == 0
        assert audit.deleted == 0
        assert await _conversation_count(session) == 1
        assert await _message_count(session) == 1
        assert await _audit_count(session) == 1
