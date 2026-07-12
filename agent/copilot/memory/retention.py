"""Retention sweep — floor-protected purge of the agent-owned Postgres.

Implements ``PRODUCTION_GRADE_PLAN.md`` §5 (Decision Set E). Two independent
sweeps, both operator-invoked (never touched by request handlers):

- ``sweep_audit_log`` — the HIPAA §164.312(b) audit trail. Retained **≥ 6
  years** and **never** deleted before then. The 6-year floor is a hard
  constant (``HIPAA_AUDIT_FLOOR_YEARS``), not a config value, so no environment
  can shorten it. Because there is no cold-storage archive target wired in yet,
  this sweep **deletes nothing** — it only reports how many rows *would* become
  eligible once a future "archive-then-delete" step exists. That is the
  fail-safe: enabling the sweep can never destroy the trail.

- ``sweep_chat`` — clinical conversation PHI (``conversation`` / ``message``).
  A separate, shorter clinical retention gated on ``chat_retention_days``:
  ``0`` (the default) means *never purge*; only a positive day count enables it.
  This is wholly independent of the audit floor.

Append-only invariant: ``MemoryRepository.record_audit`` remains insert-only.
This module adds a *bounded, floor-protected* purge path used only by the
operator sweep — it is the sole place a delete against these tables may live,
and for ``audit_log`` that path is deliberately inert.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from copilot.config import Settings
from copilot.memory.models import AuditLogRow, ConversationRow, MessageRow

_logger = logging.getLogger(__name__)

# HIPAA §164.312(b) requires audit records be retained for six years. This is a
# hard floor, NOT a tunable: the sweep never considers a row younger than this
# eligible, no matter how ``audit_retention_years`` is (mis)configured.
HIPAA_AUDIT_FLOOR_YEARS = 6


class RetentionPolicy(BaseModel):
    """Immutable retention window, derived from :class:`Settings`."""

    model_config = ConfigDict(frozen=True)

    audit_retention_years: int
    chat_retention_days: int

    @classmethod
    def from_settings(cls, settings: Settings) -> RetentionPolicy:
        return cls(
            audit_retention_years=settings.audit_retention_years,
            chat_retention_days=settings.chat_retention_days,
        )


class AuditSweepResult(BaseModel):
    """Outcome of an ``audit_log`` sweep — counts + the effective cutoff."""

    model_config = ConfigDict(frozen=True)

    scanned: int
    eligible: int
    deleted: int
    cutoff: datetime
    below_floor: bool
    dry_run: bool


class ChatSweepResult(BaseModel):
    """Outcome of a ``conversation``/``message`` sweep (counted by conversation)."""

    model_config = ConfigDict(frozen=True)

    scanned: int
    eligible: int
    deleted: int
    cutoff: datetime | None
    enabled: bool
    dry_run: bool


def _subtract_years(dt: datetime, years: int) -> datetime:
    """Subtract whole calendar years, collapsing Feb 29 to Feb 28 when needed."""
    try:
        return dt.replace(year=dt.year - years)
    except ValueError:
        # Only Feb 29 -> a non-leap target year can raise here.
        return dt.replace(year=dt.year - years, day=28)


def _to_naive_utc(dt: datetime) -> datetime:
    """Match how the timestamp columns store time: naive, but always UTC.

    ``audit_log.at`` / ``conversation.created_at`` are ``DateTime`` (no tz);
    the repository writes ``utcnow().replace(tzinfo=None)``. Comparisons must use
    the same shape or SQLite string-compares an offset-bearing value and breaks.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)
    return dt.replace(tzinfo=None)


async def sweep_audit_log(
    session: AsyncSession,
    policy: RetentionPolicy,
    now: datetime,
    *,
    dry_run: bool = False,
) -> AuditSweepResult:
    """Report (never delete) ``audit_log`` rows past the retention window.

    The eligibility cutoff is the EARLIER of ``now - audit_retention_years`` and
    ``now - HIPAA_AUDIT_FLOOR_YEARS`` — so a row younger than six years is never
    even *reported* as eligible, let alone deleted, regardless of config. And
    because no cold-storage archive target is wired in, ``deleted`` is always 0:
    there is no DELETE statement against ``audit_log`` anywhere in this codebase.
    """
    retention_cutoff = _subtract_years(now, policy.audit_retention_years)
    floor_cutoff = _subtract_years(now, HIPAA_AUDIT_FLOOR_YEARS)
    # min() picks the further-in-past cutoff: the floor always wins if retention
    # is (mis)configured below six years.
    protected_cutoff = min(retention_cutoff, floor_cutoff)
    below_floor = policy.audit_retention_years < HIPAA_AUDIT_FLOOR_YEARS
    if below_floor:
        _logger.warning(
            "audit_retention_years is below the HIPAA six-year floor; clamping to the floor",
            extra={
                "audit_retention_years": policy.audit_retention_years,
                "floor_years": HIPAA_AUDIT_FLOOR_YEARS,
            },
        )

    cutoff_naive = _to_naive_utc(protected_cutoff)
    scanned_result = await session.scalar(select(func.count()).select_from(AuditLogRow))
    scanned = int(scanned_result or 0)
    eligible_result = await session.scalar(
        select(func.count()).select_from(AuditLogRow).where(AuditLogRow.at < cutoff_naive)
    )
    eligible = int(eligible_result or 0)

    # Fail-safe: deleting an audit row would require (a) it to be older than the
    # floor-protected cutoff AND (b) a configured cold-storage archive to export
    # it to first. No archive target exists yet, so nothing is deleted. A future
    # "archive-then-delete" step would slot in HERE, still gated on eligibility
    # against ``cutoff_naive`` (never a row younger than six years).
    deleted = 0

    _logger.info(
        "audit_log retention sweep complete",
        extra={
            "scanned": scanned,
            "eligible": eligible,
            "deleted": deleted,
            "cutoff": protected_cutoff.isoformat(),
            "dry_run": dry_run,
        },
    )
    return AuditSweepResult(
        scanned=scanned,
        eligible=eligible,
        deleted=deleted,
        cutoff=protected_cutoff,
        below_floor=below_floor,
        dry_run=dry_run,
    )


async def sweep_chat(
    session: AsyncSession,
    policy: RetentionPolicy,
    now: datetime,
    *,
    dry_run: bool = False,
) -> ChatSweepResult:
    """Purge conversations (and their messages) older than ``chat_retention_days``.

    ``chat_retention_days <= 0`` (default 0) disables purging entirely — a pure
    no-op that reports zero eligible. Only a positive day count deletes, and
    then only conversations strictly older than the cutoff; newer ones survive.
    """
    scanned_result = await session.scalar(select(func.count()).select_from(ConversationRow))
    scanned = int(scanned_result or 0)

    if policy.chat_retention_days <= 0:
        _logger.info(
            "chat retention disabled (chat_retention_days<=0); no conversations purged",
            extra={"scanned": scanned, "chat_retention_days": policy.chat_retention_days},
        )
        return ChatSweepResult(
            scanned=scanned,
            eligible=0,
            deleted=0,
            cutoff=None,
            enabled=False,
            dry_run=dry_run,
        )

    cutoff = now - timedelta(days=policy.chat_retention_days)
    cutoff_naive = _to_naive_utc(cutoff)

    ids_result = await session.execute(
        select(ConversationRow.id).where(ConversationRow.created_at < cutoff_naive)
    )
    eligible_ids = list(ids_result.scalars().all())
    eligible = len(eligible_ids)

    deleted = 0
    if eligible_ids and not dry_run:
        # Delete messages first so the purge is correct even without SQLite FK
        # cascade (PRAGMA foreign_keys), then the parent conversations.
        await session.execute(
            delete(MessageRow).where(MessageRow.conversation_id.in_(eligible_ids))
        )
        await session.execute(delete(ConversationRow).where(ConversationRow.id.in_(eligible_ids)))
        await session.flush()
        deleted = eligible

    _logger.info(
        "chat retention sweep complete",
        extra={
            "scanned": scanned,
            "eligible": eligible,
            "deleted": deleted,
            "cutoff": cutoff.isoformat(),
            "chat_retention_days": policy.chat_retention_days,
            "dry_run": dry_run,
        },
    )
    return ChatSweepResult(
        scanned=scanned,
        eligible=eligible,
        deleted=deleted,
        cutoff=cutoff,
        enabled=True,
        dry_run=dry_run,
    )
