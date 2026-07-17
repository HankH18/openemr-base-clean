"""Concurrency tests for the MemoryRepository upserts.

Every upsert in ``MemoryRepository`` used to be a read-then-write straddling an
``await``:

    row = await self.get_sync_state(patient_id)   # <- yields to the event loop
    if row is None:
        row = SyncStateRow(...); self._session.add(row)
    ...
    await self._session.flush()

On one event loop, ``POST /refresh`` and a poller tick (or simply two
concurrent ``POST /refresh`` calls for a shared patient) are separate tasks
holding separate sessions. Both can return ``None`` from that first ``await``,
both construct a row, both INSERT — and the loser takes an ``IntegrityError``.
These tests force that interleave deterministically rather than hoping a race
shows up under load.

**How the interleave is forced.** ``asyncio.gather`` alone does not reproduce
it reliably: the scheduler is free to run task A to completion before task B
starts. So each test wraps a *real* repository over a *real* session and gates
it on an ``asyncio.Barrier`` — both writers are held until both have passed the
point where the old code did its read, guaranteeing the exact overlap. The
repository code under test is untouched production code; only the scheduling is
forced.

**On the watermark not being monotonic** (see ``upsert_sync_state``): a
``GREATEST``-style guard is deliberately absent, and
``test_watermark_may_move_backward_and_that_is_intentional`` pins that. The
poller's error paths already pass ``watermark=None`` to preserve a good
watermark, so the only writers that move it are ones that successfully covered
their window. ``memory_file`` and ``sync_state`` are written in the *same*
session by ``pipeline._persist``, so they commit as a pair — a lower-coverage
writer that commits last leaves a lower watermark *and* its own card, which is
coherent, and the next tick re-polls the gap. Pinning the watermark high while
that writer's card won would strand the gap permanently instead.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from copilot.domain.contracts import Claim, MemoryFileSummary
from copilot.domain.primitives import ClinicianId, FhirReference, PatientId, ResourceType
from copilot.memory import Base, MemoryRepository
from copilot.memory.models import LastSeenRow, SyncStateRow

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def engine() -> AsyncEngine:
    """A file-backed SQLite DB shared by several concurrent sessions.

    Deliberately NOT ``:memory:``: each aiosqlite connection to ``:memory:``
    gets its own private database, so two sessions would never see each other's
    rows and the conflict under test could not physically occur. A temp file is
    one real database with real constraints — which is the whole point.
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "concurrency.sqlite"
        eng = create_async_engine(f"sqlite+aiosqlite:///{db}")
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield eng
        await eng.dispose()


def _summary(pid: int, *, content_hash: str, watermark: datetime) -> MemoryFileSummary:
    return MemoryFileSummary(
        patient_id=PatientId(value=pid),
        claims=[
            Claim(
                text=f"Troponin I from writer {content_hash[0]}.",
                source_ref=FhirReference(
                    resource_type=ResourceType.Observation,
                    resource_id="90045",
                    field="valueQuantity.value",
                    value=content_hash[0],
                    last_updated=watermark,
                    timestamp=watermark,
                ),
            ),
        ],
        acuity_score=8.5,
        rank_reason=f"writer {content_hash[0]}",
        synthesized_at=datetime(2026, 7, 8, 5, tzinfo=UTC),
        source_watermark=watermark,
        content_hash=content_hash,
    )


async def _race(engine: AsyncEngine, writer: Any, n: int = 2) -> list[Any]:
    """Run ``writer(repo, i)`` in ``n`` tasks, each on its own session.

    A barrier inside each task releases all writers only once every one of them
    holds an open session and is about to call into the repository, so they
    genuinely overlap inside the upsert's await points. Exceptions are returned,
    not raised, so a test can assert on what each writer actually got.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    barrier = asyncio.Barrier(n)

    async def one(i: int) -> Any:
        async with factory() as session:
            repo = MemoryRepository(session)
            await barrier.wait()
            result = await writer(repo, i)
            await session.commit()
            return result

    return await asyncio.gather(*(one(i) for i in range(n)), return_exceptions=True)


def _assert_no_failures(results: list[Any]) -> None:
    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, f"concurrent writers raised: {[repr(f) for f in failures]}"


class TestSyncStateConcurrency:
    async def test_concurrent_first_insert_same_patient_both_succeed(
        self, engine: AsyncEngine
    ) -> None:
        """Two writers, same never-before-seen patient. Neither may see IntegrityError.

        This is the exact failure the audit reproduced against the old code:
            poller : IntegrityError: UNIQUE constraint failed: sync_state.patient_id
            refresh: committed OK
        """
        pid = PatientId(value=1015)
        base = datetime(2026, 7, 8, 3)

        async def writer(repo: MemoryRepository, i: int) -> None:
            await repo.upsert_sync_state(
                pid,
                polled_at=base + timedelta(minutes=i),
                success_at=base + timedelta(minutes=i),
                watermark=base + timedelta(minutes=i),
                content_hash=("a" if i == 0 else "b") * 64,
                consecutive_failures=0,
            )

        _assert_no_failures(await _race(engine, writer))

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            rows = (await s.execute(select(SyncStateRow))).scalars().all()
        assert len(rows) == 1, "the upsert must converge on exactly one row"

    async def test_concurrent_insert_leaves_coherent_row_not_a_mix(
        self, engine: AsyncEngine
    ) -> None:
        """The surviving row's watermark and content_hash come from ONE writer.

        A torn pairing (writer A's hash next to writer B's watermark) is what
        makes the poller's change-gate skip a re-synthesis that was genuinely
        needed — the clinician then reads a stale card. That is the failure mode
        here that can actively mislead, so it gets its own assertion.
        """
        pid = PatientId(value=1016)
        # Each writer's watermark is uniquely tied to its hash, so any mix is visible.
        marks = {
            "a" * 64: datetime(2026, 7, 8, 3),
            "b" * 64: datetime(2026, 7, 8, 9),
        }

        async def writer(repo: MemoryRepository, i: int) -> None:
            h = ("a" if i == 0 else "b") * 64
            await repo.upsert_sync_state(
                pid,
                polled_at=marks[h],
                success_at=marks[h],
                watermark=marks[h],
                content_hash=h,
                consecutive_failures=0,
            )

        _assert_no_failures(await _race(engine, writer))

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            row = (await s.execute(select(SyncStateRow))).scalar_one()
        assert row.watermark == marks[row.content_hash], (
            f"torn write: content_hash={row.content_hash[0]!r} paired with "
            f"watermark={row.watermark} belonging to another writer"
        )

    async def test_concurrent_update_of_existing_row_both_succeed(
        self, engine: AsyncEngine
    ) -> None:
        """Once the row exists, both writers still complete and stay coherent."""
        pid = PatientId(value=1017)
        seed = datetime(2026, 7, 8, 1)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            await MemoryRepository(s).upsert_sync_state(
                pid,
                polled_at=seed,
                success_at=seed,
                watermark=seed,
                content_hash="seed" + "0" * 60,
                consecutive_failures=0,
            )
            await s.commit()

        marks = {"a" * 64: datetime(2026, 7, 8, 3), "b" * 64: datetime(2026, 7, 8, 9)}

        async def writer(repo: MemoryRepository, i: int) -> None:
            h = ("a" if i == 0 else "b") * 64
            await repo.upsert_sync_state(
                pid,
                polled_at=marks[h],
                success_at=marks[h],
                watermark=marks[h],
                content_hash=h,
                consecutive_failures=0,
            )

        _assert_no_failures(await _race(engine, writer))
        async with factory() as s:
            row = (await s.execute(select(SyncStateRow))).scalar_one()
        assert row.content_hash in marks
        assert row.watermark == marks[row.content_hash]

    async def test_error_path_writer_does_not_erase_a_good_watermark(
        self, engine: AsyncEngine
    ) -> None:
        """The conditional-update semantics survive the rewrite.

        The poller's error paths pass ``success_at=None, watermark=None``
        meaning "leave those alone". If the upsert wrote them as NULL, a single
        failed tick would reset the change-gate and force a full re-pull.
        """
        pid = PatientId(value=1018)
        good = datetime(2026, 7, 8, 3)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            repo = MemoryRepository(s)
            await repo.upsert_sync_state(
                pid,
                polled_at=good,
                success_at=good,
                watermark=good,
                content_hash="a" * 64,
                consecutive_failures=0,
            )
            # Now the error path: watermark/success_at None, failures incremented.
            await repo.upsert_sync_state(
                pid,
                polled_at=datetime(2026, 7, 8, 4),
                success_at=None,
                watermark=None,
                content_hash="a" * 64,
                consecutive_failures=1,
            )
            await s.commit()

        async with factory() as s:
            row = (await s.execute(select(SyncStateRow))).scalar_one()
        assert row.watermark == good, "a failed tick must not erase the watermark"
        assert row.last_success_at == good, "a failed tick must not erase last_success_at"
        assert row.last_polled_at == datetime(2026, 7, 8, 4)
        assert row.consecutive_failures == 1

    async def test_first_insert_stores_null_watermark_when_none(
        self, engine: AsyncEngine
    ) -> None:
        """None on a first-ever INSERT is a real NULL, not a skipped column."""
        pid = PatientId(value=1019)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            await MemoryRepository(s).upsert_sync_state(
                pid,
                polled_at=datetime(2026, 7, 8, 4),
                success_at=None,
                watermark=None,
                content_hash="",
                consecutive_failures=1,
            )
            await s.commit()
        async with factory() as s:
            row = (await s.execute(select(SyncStateRow))).scalar_one()
        assert row.watermark is None
        assert row.last_success_at is None

    async def test_returned_row_reflects_this_write_not_a_stale_cached_one(
        self, engine: AsyncEngine
    ) -> None:
        """The returned entity must be refreshed, not the identity map's old copy.

        The poller calls ``get_sync_state`` before ``upsert_sync_state``, so a
        pre-upsert ``SyncStateRow`` is already in the session's identity map.
        The upsert is Core-level and bypasses that map, so without
        ``populate_existing`` the returned object would carry stale attributes.
        """
        pid = PatientId(value=1020)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            repo = MemoryRepository(s)
            await repo.upsert_sync_state(
                pid,
                polled_at=datetime(2026, 7, 8, 1),
                success_at=datetime(2026, 7, 8, 1),
                watermark=datetime(2026, 7, 8, 1),
                content_hash="a" * 64,
                consecutive_failures=0,
            )
            await s.commit()

        async with factory() as s:
            repo = MemoryRepository(s)
            cached = await repo.get_sync_state(pid)  # loads into the identity map
            assert cached is not None
            returned = await repo.upsert_sync_state(
                pid,
                polled_at=datetime(2026, 7, 8, 2),
                success_at=datetime(2026, 7, 8, 2),
                watermark=datetime(2026, 7, 8, 2),
                content_hash="b" * 64,
                consecutive_failures=7,
            )
            assert returned.content_hash == "b" * 64
            assert returned.consecutive_failures == 7
            assert returned.watermark == datetime(2026, 7, 8, 2)

    async def test_watermark_may_move_backward_and_that_is_intentional(
        self, engine: AsyncEngine
    ) -> None:
        """No monotonic guard: a later writer with an older watermark wins.

        Pinned deliberately. ``sync_state`` and ``memory_file`` are written in
        one session by ``pipeline._persist``, so they commit as a coherent pair;
        letting the last committer's watermark stand keeps the stored watermark
        matched to the stored card. The next tick re-polls the gap (wasted work,
        self-healing). A GREATEST guard would instead keep the high watermark
        beside the low-coverage card and strand the gap forever — the stale-card
        outcome this whole change exists to prevent. If this test ever needs to
        change, that trade-off is what is being reversed.
        """
        pid = PatientId(value=1021)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            repo = MemoryRepository(s)
            await repo.upsert_sync_state(
                pid,
                polled_at=datetime(2026, 7, 8, 9),
                success_at=datetime(2026, 7, 8, 9),
                watermark=datetime(2026, 7, 8, 9),
                content_hash="b" * 64,
                consecutive_failures=0,
            )
            await repo.upsert_sync_state(
                pid,
                polled_at=datetime(2026, 7, 8, 3),
                success_at=datetime(2026, 7, 8, 3),
                watermark=datetime(2026, 7, 8, 3),  # older than what is stored
                content_hash="a" * 64,
                consecutive_failures=0,
            )
            await s.commit()
        async with factory() as s:
            row = (await s.execute(select(SyncStateRow))).scalar_one()
        assert row.watermark == datetime(2026, 7, 8, 3)
        assert row.content_hash == "a" * 64, "watermark and hash still move together"


class TestMemoryFileConcurrency:
    async def test_concurrent_first_save_same_patient_both_succeed(
        self, engine: AsyncEngine
    ) -> None:
        """``memory_file.patient_id`` is the PK — same race, same fix."""
        pid = 1030
        marks = {
            "a" * 64: datetime(2026, 7, 8, 3, tzinfo=UTC),
            "b" * 64: datetime(2026, 7, 8, 9, tzinfo=UTC),
        }

        async def writer(repo: MemoryRepository, i: int) -> None:
            h = ("a" if i == 0 else "b") * 64
            await repo.save_memory_file(_summary(pid, content_hash=h, watermark=marks[h]))

        _assert_no_failures(await _race(engine, writer))

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            out = await MemoryRepository(s).get_memory_file(PatientId(value=pid))
        assert out is not None
        # Coherent: the card, its watermark and its rank_reason are one writer's.
        assert out.source_watermark.replace(tzinfo=UTC) == marks[out.content_hash]
        assert out.rank_reason == f"writer {out.content_hash[0]}"
        assert out.claims[0].source_ref.value == out.content_hash[0]


class TestRoundingCursorConcurrency:
    async def test_concurrent_first_upsert_same_clinician_both_succeed(
        self, engine: AsyncEngine
    ) -> None:
        """``rounding_cursor.clinician_id`` is the PK — same race, same fix."""
        cid = ClinicianId(value=1)
        lists = {0: ([1015, 1016], 0, []), 1: ([1015, 1016, 1017], 2, [1015, 1016])}

        async def writer(repo: MemoryRepository, i: int) -> None:
            ordered, idx, done = lists[i]
            await repo.upsert_rounding_cursor(cid, ordered, idx, done)

        _assert_no_failures(await _race(engine, writer))

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            cursor = await MemoryRepository(s).get_rounding_cursor(cid)
        assert cursor is not None
        # Coherent: index/completed/ordered are all from the same writer.
        assert (cursor.ordered_patient_ids, cursor.current_index, cursor.completed_ids) in [
            (list(o), i, list(d)) for o, i, d in lists.values()
        ]


class TestLastSeenConcurrency:
    async def test_concurrent_first_set_same_pair_both_succeed(self, engine: AsyncEngine) -> None:
        """The conflict target is uq_last_seen_cln_pt, not the surrogate PK.

        ``last_seen.id`` autoincrements, so a duplicate insert would NOT violate
        the PK — it would silently create a SECOND row for the same
        (clinician, patient) if the unique constraint were not the conflict
        target, and ``get_last_seen``'s ``scalar_one_or_none`` would then start
        raising MultipleResultsFound. So this asserts row count, not just
        "no exception".
        """
        cid, pid = ClinicianId(value=1), PatientId(value=1015)
        stamps = {
            0: datetime(2026, 7, 8, 5, tzinfo=UTC),
            1: datetime(2026, 7, 8, 9, tzinfo=UTC),
        }

        async def writer(repo: MemoryRepository, i: int) -> None:
            await repo.set_last_seen(cid, pid, stamps[i])

        _assert_no_failures(await _race(engine, writer))

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            rows = (await s.execute(select(LastSeenRow))).scalars().all()
            assert len(rows) == 1, "the unique pair must converge on exactly one row"
            seen = await MemoryRepository(s).get_last_seen(cid, pid)
        assert seen is not None
        assert seen.replace(tzinfo=UTC) in stamps.values()

    async def test_distinct_pairs_still_get_distinct_rows(self, engine: AsyncEngine) -> None:
        """The conflict target must not over-match: different pairs, different rows."""
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            repo = MemoryRepository(s)
            await repo.set_last_seen(ClinicianId(value=1), PatientId(value=1015))
            await repo.set_last_seen(ClinicianId(value=1), PatientId(value=1016))
            await repo.set_last_seen(ClinicianId(value=2), PatientId(value=1015))
            await s.commit()
        async with factory() as s:
            rows = (await s.execute(select(LastSeenRow))).scalars().all()
        assert len(rows) == 3


# The exact URL the deployed agent runs on — docker-compose.deploy.yml:163
#   COPILOT_DATABASE_URL: postgresql+psycopg://copilot:${AGENT_POSTGRES_PASSWORD}@agent-postgres:5432/copilot
# Pinned here so that if prod ever moves to another driver or engine, the
# dialect-dispatch tests below fail loudly instead of quietly testing a branch
# production stopped taking.
PROD_DATABASE_URL = "postgresql+psycopg://copilot:pw@agent-postgres:5432/copilot"


class _StubBind:
    def __init__(self, dialect_name: str) -> None:
        self.dialect = SimpleNamespace(name=dialect_name)


class _StubSession:
    """Just enough AsyncSession for ``_upsert``, which only reads the bind's dialect.

    A real Postgres engine cannot be constructed here: ``create_async_engine``
    imports the DBAPI eagerly and ``psycopg`` is not installed in the test venv
    (it ships in the agent image). Stubbing the bind keeps the *dispatch* under
    test; ``test_production_url_resolves_to_the_postgres_branch`` independently
    proves the real production URL yields this same dialect name.
    """

    def __init__(self, dialect_name: str) -> None:
        self._bind = _StubBind(dialect_name)

    def get_bind(self, *args: Any, **kwargs: Any) -> Any:
        return self._bind


class TestProductionDialectBranch:
    """The suite runs SQLite; prod runs Postgres. Pin the Postgres branch too.

    ``_upsert`` dispatches on the bind's dialect, so every test above only ever
    compiles the **SQLite** branch. Without something here, the Postgres
    construct — the only one production ever builds — would ship having never
    been constructed once. These close that gap in three steps: the production
    URL resolves to ``postgresql``; the ``postgresql`` branch emits the ON
    CONFLICT SQL intended; an unexpected dialect raises.

    Honest limit: this proves the statement is *constructed* correctly for
    Postgres. It does not prove a live Postgres server executes it — no
    Postgres runs in this suite.
    """

    async def test_production_url_resolves_to_the_postgres_branch(self) -> None:
        """The deployed URL maps to the dialect name ``_upsert`` branches on.

        This is the link between "what the tests dispatch on" and "what prod
        dispatches on": ``get_dialect()`` reads SQLAlchemy's dialect registry,
        the same registry ``create_async_engine`` uses to build the real bind,
        and it does not need the DBAPI installed.
        """
        assert make_url(PROD_DATABASE_URL).get_dialect().name == "postgresql"

    async def test_postgres_branch_emits_on_conflict_do_update(self) -> None:
        from copilot.memory.repository import _upsert

        session = cast(AsyncSession, _StubSession("postgresql"))
        stmt = _upsert(session, SyncStateRow).values(patient_id=1, content_hash="a")
        sql = str(
            stmt.on_conflict_do_update(
                index_elements=["patient_id"],
                set_={"content_hash": stmt.excluded.content_hash},
            ).compile(dialect=postgresql.dialect())
        )
        assert "ON CONFLICT (patient_id) DO UPDATE" in sql
        assert "content_hash = excluded.content_hash" in sql

    async def test_postgres_last_seen_conflict_target_is_the_unique_pair(self) -> None:
        """Prod's last_seen upsert must target the constraint, not the surrogate PK."""
        from copilot.memory.repository import _upsert

        session = cast(AsyncSession, _StubSession("postgresql"))
        stmt = _upsert(session, LastSeenRow).values(clinician_id=1, patient_id=2)
        sql = str(
            stmt.on_conflict_do_update(
                index_elements=["clinician_id", "patient_id"],
                set_={"seen_at": stmt.excluded.seen_at},
            ).compile(dialect=postgresql.dialect())
        )
        assert "ON CONFLICT (clinician_id, patient_id) DO UPDATE" in sql

    async def test_sqlite_url_resolves_to_the_sqlite_branch(self) -> None:
        """The suite's own URL — the branch every other test in this file exercises."""
        assert make_url("sqlite+aiosqlite:///./copilot-local.db").get_dialect().name == "sqlite"

    async def test_unknown_dialect_fails_loudly(self) -> None:
        """No silent fallback to a racy read-then-write on an unexpected dialect."""
        from copilot.memory.repository import _upsert

        session = cast(AsyncSession, _StubSession("mysql"))
        with pytest.raises(RuntimeError, match="no ON CONFLICT upsert construct"):
            _upsert(session, SyncStateRow)


class TestSingleWriterUnchanged:
    """Existing single-writer behavior is preserved (the pre-fix contract)."""

    async def test_sync_state_insert_then_update(self, engine: AsyncEngine) -> None:
        pid = PatientId(value=1040)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            repo = MemoryRepository(s)
            assert await repo.get_sync_state(pid) is None
            polled = datetime(2026, 7, 8, 3)
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
            assert row.watermark == polled - timedelta(minutes=5)

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
