"""Runtime wiring for the deployed background poller.

The unit tests exercise :class:`PollerScheduler`, :class:`Poller`, and
:class:`RefreshPipeline` in isolation with hand-built collaborators. This
module is the *deployment* seam that assembles them against the process's
real configuration so the loop can actually run inside the FastAPI app
lifespan (see :func:`copilot.api.app.create_app`).

Three collaborators feed :class:`PollerScheduler`:

* ``active_patients`` — the distinct patient ids across every clinician's
  ``rounding_cursor``. Read-only: it opens a session, reads the cursor rows,
  and flattens them into a de-duplicated list of :class:`PatientId`.
* ``poller`` — a :class:`Poller` whose DB session and FHIR client are bound
  *per tick* (:class:`_RuntimePoller`), so every tick is its own short
  transaction rather than one process-lifetime session that never commits.
* ``on_result`` — verifies + persists a freshly-synthesized summary by
  **reusing** :class:`RefreshPipeline`'s existing verify-then-persist steps.
  No verification or persistence logic is reimplemented here.

Nothing in this module is imported unless the poller is switched on, and the
switch (:attr:`Settings.poller_enabled`) defaults OFF.
"""

from __future__ import annotations

from sqlalchemy import select

from copilot.config import Settings
from copilot.domain.primitives import PatientId
from copilot.memory.db import session_scope
from copilot.memory.models import RoundingCursorRow
from copilot.memory.repository import MemoryRepository
from copilot.observability import (
    NoopObservability,
    Observability,
    current_correlation_id,
    generate_correlation_id,
)
from copilot.verification.core import Verifier
from copilot.verification.rules import default_rules
from copilot.worker.pipeline import RefreshPipeline
from copilot.worker.poller import Poller, PollerResult, PollerTickOutcome
from copilot.worker.scheduler import PollerScheduler
from copilot.worker.synthesizer import StubSynthesizer


class _RuntimePoller(Poller):
    """A :class:`Poller` that binds a fresh session + FHIR client each tick.

    :class:`PollerScheduler` keeps one poller for its whole lifetime, but a
    background loop wants each tick to own its transaction and HTTP client
    rather than share a process-lifetime session that would never commit its
    ``sync_state`` writes. This subclass preserves the scheduler's
    single-poller contract while delegating every tick to a freshly-wired
    :class:`Poller`, so the real tick logic runs unchanged.
    """

    def __init__(self, settings: Settings, observability: Observability | None = None) -> None:
        self._settings = settings
        self._pipeline = RefreshPipeline(settings)
        self._obs: Observability = observability or NoopObservability()

    async def tick(self, patient_id: PatientId) -> PollerResult:
        async with session_scope() as session, self._pipeline._fhir_client() as fhir:
            repo = MemoryRepository(session)
            inner = Poller(
                fhir=fhir,
                synthesizer=StubSynthesizer(),
                repository=repo,
                observability=self._obs,
            )
            result = await inner.tick(patient_id)
            # HIPAA §164.312(b): the tick read this patient's chart from
            # OpenEMR. Trail it atomically with the tick's own state write.
            # Background ticks carry no request correlation id, so mint one.
            await repo.record_audit(
                correlation_id=current_correlation_id() or generate_correlation_id(),
                action="poller.read",
                patient_id=patient_id,
            )
            return result


def build_poller_scheduler(
    settings: Settings, observability: Observability | None = None
) -> PollerScheduler:
    """Assemble the deployable :class:`PollerScheduler` from ``settings``.

    Wires the three collaborators described in the module docstring. The
    returned scheduler is inert until ``start()`` is called (done by the app
    lifespan only when :attr:`Settings.poller_enabled` is true).
    """
    pipeline = RefreshPipeline(settings)

    async def active_patients() -> list[PatientId]:
        """Distinct patient ids across every clinician's rounding cursor."""
        async with session_scope() as session:
            result = await session.execute(select(RoundingCursorRow.ordered_patient_ids))
            seen: set[int] = set()
            patients: list[PatientId] = []
            for ordered_ids in result.scalars().all():
                for pid in ordered_ids:
                    if pid not in seen:
                        seen.add(pid)
                        patients.append(PatientId(value=pid))
            return patients

    async def on_result(result: PollerResult) -> None:
        """Verify + persist a new synthesis, reusing the refresh pipeline.

        Only a ``synthesized`` tick has an unverified summary to ground; the
        poller already advanced ``sync_state`` for every other outcome, so
        those are no-ops here.
        """
        if result.outcome is not PollerTickOutcome.synthesized or result.memory_file is None:
            return
        async with session_scope() as session, pipeline._fhir_client() as fhir:
            repo = MemoryRepository(session)
            verifier = Verifier(rules=default_rules())
            grounded = await pipeline._ground_and_score(
                result.patient_id, result.memory_file, fhir, verifier
            )
            await pipeline._persist(result.patient_id, grounded, repo)

    return PollerScheduler(
        poller=_RuntimePoller(settings, observability),
        active_patients=active_patients,
        on_result=on_result,
        interval_seconds=settings.poll_interval_seconds,
    )
