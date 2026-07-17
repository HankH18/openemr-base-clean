"""Poller telemetry: span nesting + the correlation-id join.

Two properties the dashboard depends on, neither of which the existing poller
tests cover (they inject ``NoopObservability``, which records nothing):

1. ``poller.result`` — the event carrying ``outcome``/``error``, the only
   signal separating a healthy tick from an errored one — must land INSIDE
   the ``poller.tick`` span, not as a root-level orphan.
2. A background tick must run under a NON-EMPTY correlation id, and the
   ``audit_log.correlation_id`` row it writes must point at the SAME id the
   Langfuse trace was opened with. That equality IS the documented
   "reconstruct the whole trace from the correlation id" join; if the two ids
   differ the audit row points at nothing.

These drive the REAL :class:`LangfuseObservability` against a recording
client rather than a hand-rolled observability double, so the actual nesting
mechanism (the ``_current_observation`` ContextVar) is what gets exercised —
a double would happily "nest" an event the real backend orphans.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy import select

from copilot.domain.primitives import PatientId, ResourceType
from copilot.memory import Base
from copilot.memory.models import AuditLogRow
from copilot.observability import correlation_id_var, current_correlation_id
from copilot.observability.langfuse_backend import LangfuseObservability
from copilot.worker.poller import Poller, PollerTickOutcome

pytestmark = pytest.mark.asyncio


# --- Recording Langfuse client ---------------------------------------------


class _Recorder:
    """Everything the backend sent, split by where it landed in the trace."""

    def __init__(self) -> None:
        self.root_traces: list[dict[str, Any]] = []
        self.root_events: list[dict[str, Any]] = []  # orphans — nothing should land here
        self.nested_events: list[dict[str, Any]] = []

    def event_names_under(self, parent: str) -> list[str]:
        return [e["name"] for e in self.nested_events if e["parent"] == parent]


class _FakeObservation:
    """A Langfuse trace root / span. Children record themselves as nested."""

    def __init__(self, name: str, trace_id: Any, rec: _Recorder) -> None:
        self.name = name
        self.trace_id = trace_id
        self._rec = rec
        self.ended = 0

    def span(self, name: str, metadata: Any = None) -> _FakeObservation:
        return _FakeObservation(name, self.trace_id, self._rec)

    def event(self, name: str, metadata: Any = None) -> None:
        self._rec.nested_events.append(
            {"name": name, "parent": self.name, "trace_id": self.trace_id, "metadata": metadata}
        )

    def update(self, **kwargs: Any) -> None:
        return

    def end(self) -> None:
        self.ended += 1


class _FakeClient:
    """Stands in for the Langfuse SDK client.

    ``trace()`` opens a root; ``event()`` at THIS level is the orphan path —
    the backend only calls it when no observation is open.
    """

    def __init__(self, rec: _Recorder) -> None:
        self._rec = rec

    def trace(self, name: str, id: Any = None, metadata: Any = None) -> _FakeObservation:
        self._rec.root_traces.append({"name": name, "id": id, "metadata": metadata})
        return _FakeObservation(name, id, self._rec)

    def event(self, name: str, trace_id: Any = None, metadata: Any = None) -> None:
        self._rec.root_events.append({"name": name, "trace_id": trace_id, "metadata": metadata})

    def flush(self) -> None:
        return


def _langfuse(rec: _Recorder) -> LangfuseObservability:
    return LangfuseObservability(
        host="https://x", public_key="pk", secret_key="sk", client=_FakeClient(rec)
    )


# --- Collaborator doubles ---------------------------------------------------


class _QuietFhir:
    """No changes for any resource type — the cheapest tick that still runs."""

    def __init__(self) -> None:
        self.seen_correlation_ids: list[str] = []

    async def count_since(self, rt: ResourceType, patient_id: Any, since: Any) -> int:
        # Observed from INSIDE the tick: what id is actually in context here?
        self.seen_correlation_ids.append(current_correlation_id())
        return 0

    async def search(self, rt: ResourceType, params: Any) -> dict[str, Any]:
        return {"resourceType": "Bundle", "entry": []}


class _QuietRepo:
    async def get_sync_state(self, patient_id: Any) -> None:
        return None

    async def upsert_sync_state(self, patient_id: Any, **kwargs: Any) -> None:
        return None


class _UnexpectedDbError(RuntimeError):
    """Stands in for a SQLAlchemy error out of an unguarded state write."""


class _ExplodingRepo:
    """`upsert_sync_state` blows up — the unguarded call the poller never catches."""

    async def get_sync_state(self, patient_id: Any) -> None:
        return None

    async def upsert_sync_state(self, patient_id: Any, **kwargs: Any) -> None:
        raise _UnexpectedDbError("connection reset")


def _poller(fhir: Any, repo: Any, obs: LangfuseObservability) -> Poller:
    return Poller(
        fhir=fhir,
        synthesizer=None,  # type: ignore[arg-type]
        repository=repo,
        observability=obs,
    )


# --- 1. the event nests inside the span ------------------------------------


class TestPollerResultNesting:
    async def test_result_event_is_nested_in_the_tick_span_not_root(self) -> None:
        """`poller.result` must attach to `poller.tick`, not orphan at root."""
        rec = _Recorder()
        poller = _poller(_QuietFhir(), _QuietRepo(), _langfuse(rec))

        await poller.tick(PatientId(value=1015))

        assert [t["name"] for t in rec.root_traces] == ["poller.tick"]
        # The whole point: nothing may be emitted at root level beside the trace.
        assert rec.root_events == [], (
            f"poller.result orphaned at root level: {rec.root_events!r} — it was emitted "
            "outside its span, so the backend saw no enclosing observation"
        )
        assert rec.event_names_under("poller.tick") == ["poller.result"]

    async def test_nested_result_event_carries_the_outcome(self) -> None:
        """Nesting is worthless if the payload that distinguishes ticks is lost."""
        rec = _Recorder()
        poller = _poller(_QuietFhir(), _QuietRepo(), _langfuse(rec))

        await poller.tick(PatientId(value=1015))

        [event] = rec.nested_events
        assert event["metadata"]["outcome"] == PollerTickOutcome.no_change.value
        assert event["metadata"]["patient_id"] == 1015


# --- 2. the correlation-id join --------------------------------------------


@pytest.fixture
def _db_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Temp-file SQLite DB with the schema created (shared across sessions)."""
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    db_file = tmp_path / "poller_telemetry.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    sync_engine = sa.create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    yield str(db_file)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


class _FakeFhirCtx:
    """Async-CM shim matching the `async with pipeline._fhir_client()` seam."""

    def __init__(self, fhir: Any) -> None:
        self._fhir = fhir

    async def __aenter__(self) -> Any:
        return self._fhir

    async def __aexit__(self, *exc: Any) -> bool:
        return False


@pytest_asyncio.fixture
async def _runtime_tick(
    _db_file: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Any, _Recorder, _QuietFhir]]:
    """A `_RuntimePoller` wired to a live DB + a recording Langfuse client."""
    from copilot.config import get_settings
    from copilot.worker.pipeline import RefreshPipeline
    from copilot.worker.runtime import _RuntimePoller

    fhir = _QuietFhir()
    monkeypatch.setattr(RefreshPipeline, "_fhir_client", lambda self: _FakeFhirCtx(fhir))

    rec = _Recorder()
    poller = _RuntimePoller(get_settings(), _langfuse(rec))
    yield poller, rec, fhir


async def _audit_rows() -> list[AuditLogRow]:
    from copilot.memory.db import session_scope

    async with session_scope() as session:
        result = await session.execute(select(AuditLogRow))
        return list(result.scalars().all())


class TestCorrelationIdJoin:
    async def test_background_tick_runs_with_a_non_empty_correlation_id(
        self, _runtime_tick: tuple[Any, _Recorder, _QuietFhir]
    ) -> None:
        """No middleware runs behind APScheduler — the tick must mint its own."""
        poller, _rec, fhir = _runtime_tick

        await poller.tick(PatientId(value=1015))

        assert fhir.seen_correlation_ids, "the tick never reached the FHIR client"
        assert all(cid for cid in fhir.seen_correlation_ids), (
            f"tick ran with an EMPTY correlation id: {fhir.seen_correlation_ids!r}"
        )

    async def test_audit_row_id_matches_the_trace_id(
        self, _runtime_tick: tuple[Any, _Recorder, _QuietFhir]
    ) -> None:
        """The join: audit row id == trace id, or the row points at nothing."""
        poller, rec, _fhir = _runtime_tick

        await poller.tick(PatientId(value=1015))

        [trace] = rec.root_traces
        assert trace["name"] == "poller.tick"
        # id=None ⇒ Langfuse assigns a random trace id the audit row can't name.
        assert trace["id"], f"trace opened with no id: {trace['id']!r}"

        rows = await _audit_rows()
        [row] = [r for r in rows if r.action == "poller.read"]
        assert row.correlation_id, "audit row written with an empty correlation id"
        assert row.correlation_id == trace["id"], (
            f"broken join: audit row {row.correlation_id!r} != trace {trace['id']!r} — "
            "the id was minted after the span had already opened"
        )

    async def test_tick_does_not_clobber_an_inherited_correlation_id(
        self, _runtime_tick: tuple[Any, _Recorder, _QuietFhir]
    ) -> None:
        """A tick driven from a request context stays on that request's trace."""
        poller, rec, fhir = _runtime_tick

        token = correlation_id_var.set("inherited-corr-1")
        try:
            await poller.tick(PatientId(value=1015))
            # Checked INSIDE the set/reset window: the tick must hand the
            # context back exactly as it found it, for whatever runs next.
            assert current_correlation_id() == "inherited-corr-1"
        finally:
            correlation_id_var.reset(token)

        assert fhir.seen_correlation_ids == ["inherited-corr-1"] * len(fhir.seen_correlation_ids)
        [trace] = rec.root_traces
        assert trace["id"] == "inherited-corr-1"
        rows = await _audit_rows()
        [row] = [r for r in rows if r.action == "poller.read"]
        assert row.correlation_id == "inherited-corr-1"


# --- 3. telemetry on the failure branch ------------------------------------


class TestUnexpectedFailureStillReports:
    async def test_unexpected_exception_emits_error_result_and_propagates(self) -> None:
        """The hardest failures must not be the silent ones.

        `_tick_inner` handles FhirClientError/SynthesisError itself. Anything
        else (a DB error out of the unguarded state writes) escapes the
        `async with` — and must still report an errored tick on the way out.
        """
        rec = _Recorder()
        poller = _poller(_QuietFhir(), _ExplodingRepo(), _langfuse(rec))

        with pytest.raises(_UnexpectedDbError):  # must NOT be swallowed
            await poller.tick(PatientId(value=1015))

        assert rec.root_events == [], f"error result orphaned at root: {rec.root_events!r}"
        assert rec.event_names_under("poller.tick") == ["poller.result"], (
            "an unexpected exception emitted NO poller.result — the tick fails "
            "invisibly and the dashboard shows only healthy ticks"
        )
        [event] = rec.nested_events
        assert event["metadata"]["outcome"] == PollerTickOutcome.error.value
        assert "_UnexpectedDbError" in event["metadata"]["error"]

    async def test_span_is_still_closed_when_the_tick_explodes(self) -> None:
        """Telemetry on the failure path must not leak an unclosed span."""
        rec = _Recorder()
        poller = _poller(_QuietFhir(), _ExplodingRepo(), _langfuse(rec))

        with pytest.raises(_UnexpectedDbError):
            await poller.tick(PatientId(value=1015))

        assert [t["name"] for t in rec.root_traces] == ["poller.tick"]
