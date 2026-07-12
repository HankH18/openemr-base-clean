#!/usr/bin/env python3
"""Operator sweep for the agent's audit trail + chat-history retention.

Implements ``PRODUCTION_GRADE_PLAN.md`` §5 (Decision Set E). Runs two
independent sweeps against the agent-owned database:

- **audit_log** — retained ≥ 6 years (HIPAA §164.312(b)). This sweep NEVER
  deletes an audit row; with no cold-storage archive target configured it only
  reports how many rows *would* be eligible. Enabling the sweep can therefore
  never destroy the compliance trail.
- **conversation/message** — clinical PHI purged only when
  ``COPILOT_CHAT_RETENTION_DAYS`` is a positive day count (0 ⇒ never purge).

Defaults to ``--dry-run`` (report only). Pass ``--no-dry-run`` to actually
execute the chat purge (audit is inert either way).

Intended to be invoked from cron, e.g. a nightly report + weekly execute::

    # nightly dry-run report (logs/prints eligibility, deletes nothing)
    0 2 * * *  cd /app/agent && python scripts/audit_retention_sweep.py

    # weekly chat purge (audit remains inert; chat purge honours the config)
    0 3 * * 0  cd /app/agent && python scripts/audit_retention_sweep.py --no-dry-run

Configuration comes from the process environment / ``.env`` via ``Settings``
(``COPILOT_AUDIT_RETENTION_YEARS``, ``COPILOT_CHAT_RETENTION_DAYS``,
``COPILOT_DATABASE_URL``).
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from copilot.config import get_settings
from copilot.domain.primitives import utcnow
from copilot.memory.db import session_scope
from copilot.memory.retention import (
    HIPAA_AUDIT_FLOOR_YEARS,
    RetentionPolicy,
    sweep_audit_log,
    sweep_chat,
)


async def _run(*, dry_run: bool) -> None:
    settings = get_settings()
    policy = RetentionPolicy.from_settings(settings)
    now = utcnow()

    # session_scope commits on success (a no-op when nothing was deleted) and
    # rolls back on error, so a failed sweep never leaves a partial purge.
    async with session_scope() as session:
        audit = await sweep_audit_log(session, policy, now, dry_run=dry_run)
        chat = await sweep_chat(session, policy, now, dry_run=dry_run)

    mode = "DRY-RUN (report only)" if dry_run else "EXECUTE"
    print("=== AgentForge audit + chat retention sweep ===")
    print(f"mode                 : {mode}")
    print(f"run at (UTC)         : {now.isoformat()}")
    print()
    print(
        f"audit retention      : {policy.audit_retention_years}y "
        f"(HIPAA floor {HIPAA_AUDIT_FLOOR_YEARS}y)"
    )
    if audit.below_floor:
        print(
            "  WARNING            : configured retention is below the 6-year floor; "
            "clamped to the floor"
        )
    print(f"audit cutoff (UTC)   : {audit.cutoff.isoformat()}")
    print(f"audit rows scanned   : {audit.scanned}")
    print(
        f"audit eligible       : {audit.eligible}  "
        "(older than cutoff — NEVER deleted: no archive target)"
    )
    print(f"audit rows deleted   : {audit.deleted}")
    print()
    if chat.enabled:
        assert chat.cutoff is not None  # enabled ⇒ a cutoff was computed
        print(f"chat retention       : {policy.chat_retention_days}d")
        print(f"chat cutoff (UTC)    : {chat.cutoff.isoformat()}")
        print(f"conversations scanned: {chat.scanned}")
        print(f"conversations eligible: {chat.eligible}")
        print(f"conversations deleted: {chat.deleted}")
    else:
        print("chat retention       : disabled (chat_retention_days<=0) — no conversations purged")
        print(f"conversations scanned: {chat.scanned}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Report only, delete nothing (default). Use --no-dry-run to execute the chat purge.",
    )
    args = ap.parse_args()
    asyncio.run(_run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
