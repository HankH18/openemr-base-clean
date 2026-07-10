"""Seed a throwaway SQLite DB so the load test exercises real serve paths.

Creates the schema, seeds a per-patient memory file for patients 101/102/103,
and establishes a rounding cursor for clinician 1 over those patients. With this
in place the load-tested agent returns genuine 200s on the DB-backed serve paths
(``GET /v1/rounds/current`` reads a card; ``POST /v1/chat`` passes the
rounding-list authorization boundary and runs the full grounded/verify/persist
path) instead of 500-ing on missing tables.

Run against the SAME database the agent boots with:

    export COPILOT_DATABASE_URL="sqlite+aiosqlite:////tmp/copilot_loadtest.db"
    agent/.venv/bin/python loadtest/seed_data.py

This is a load-test harness helper — it lives under ``loadtest/`` (outside the
agent pytest ``testpaths``) and is not named ``test_*``; the agent test suite
never collects or imports it. It imports the agent's own modules, so run it with
the agent venv and from the ``agent/`` directory (or with ``agent`` on
PYTHONPATH).
"""

from __future__ import annotations

import asyncio

from copilot.domain.contracts import Claim, MemoryFileSummary
from copilot.domain.primitives import (
    ClinicianId,
    FhirReference,
    PatientId,
    ResourceType,
    utcnow,
)
from copilot.memory import models as _models  # noqa: F401 — register tables on Base.metadata
from copilot.memory.db import Base, get_engine, session_scope
from copilot.memory.repository import MemoryRepository

PATIENT_IDS = [101, 102, 103]
CLINICIAN_ID = 1


async def main() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    now = utcnow()
    async with session_scope() as session:
        repo = MemoryRepository(session)
        for pid in PATIENT_IDS:
            summary = MemoryFileSummary(
                patient_id=PatientId(value=pid),
                claims=[
                    Claim(
                        text=f"Observation Potassium: 5.{pid % 10}",
                        source_ref=FhirReference(
                            resource_type=ResourceType.Observation,
                            resource_id=f"obs-{pid}",
                            field="valueQuantity.value",
                            value=f"5.{pid % 10}",
                        ),
                    )
                ],
                changes=[],
                acuity_score=float(pid % 10),
                rank_reason="seeded for load test",
                synthesized_at=now,
                source_watermark=now,
                content_hash=f"seed-{pid}",
            )
            await repo.save_memory_file(summary)
        await repo.upsert_rounding_cursor(
            ClinicianId(value=CLINICIAN_ID), PATIENT_IDS, 0, []
        )

    await engine.dispose()
    print(f"seeded {len(PATIENT_IDS)} memory files + rounding cursor for clinician {CLINICIAN_ID}")


if __name__ == "__main__":
    asyncio.run(main())
