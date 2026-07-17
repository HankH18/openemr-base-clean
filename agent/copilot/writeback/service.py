"""Serve-time write-back orchestration — the read-side gate, run in reverse.

Keeps the route thin. One place parses a raw physician request into a typed,
closed-set ``WriteCandidate``, runs the deterministic write verifier, records the
``write_proposed`` audit and returns the structured echo-back (propose); then, on
an explicit second transaction, re-verifies the identical candidate, commits it
append-only through the ``OpenEmrWriteClient``, records ``write_committed`` /
``write_failed``, and closes the loop with a fail-open read-back (commit).

Mirrors ``chat/service.py`` in shape (settings + observability injected, its own
FHIR client seam, audit fail-open) but never touches the read-only ``FhirClient``
write-side — the write client is a separate, guarded transport built only here,
inside the interactive request path (``research/WRITEBACK_PHASE1_PLAN.md`` §2.4).

Fail-closed on the value, fail-open on the trail:

- A candidate that cannot be parsed (non-numeric / non-finite value, unknown
  metric) or that the verifier hard-blocks (wrong unit) raises
  ``WriteInputError`` — the route maps it to 400. No free text reaches OpenEMR.
- An out-of-range **human_direct** value is a soft, overridable warning carried
  on the echo-back — a genuine critical value stays recordable.
- A write whose success the client could not confirm raises ``OpenEmrWriteError``
  (audited ``write_failed``) — never assumed committed.
- Audit writes are fail-open: a broken audit row never turns a completed write
  into a 500, exactly as on the read side.
"""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
from collections.abc import Awaitable, Callable, Mapping
from functools import lru_cache
from typing import Any

from pydantic import ValidationError

from copilot.config import Settings
from copilot.domain.primitives import ClinicianId, PatientId, ResourceType, is_iso_date, utcnow
from copilot.domain.writes import (
    AllergyWrite,
    AnyWriteCandidate,
    CommittedWrite,
    IssueWriteCandidate,
    MedicalProblemWrite,
    MedicationWrite,
    ProposedWrite,
    VitalWrite,
    WritableMetric,
    WriteCandidate,
    WriteEntryMode,
    WriteKind,
    WriteSource,
    WriteVerdict,
)
from copilot.fhir.client import FhirClient
from copilot.fhir.provider import build_fhir_client, build_write_client
from copilot.fhir.write_client import OpenEmrWriteClient, OpenEmrWriteError
from copilot.memory.db import session_scope
from copilot.memory.repository import MemoryRepository
from copilot.observability import NoopObservability, Observability, current_correlation_id
from copilot.verification.writes import verify_write

_logger = logging.getLogger(__name__)


class WriteInputError(Exception):
    """A physician request that cannot become a trustworthy write.

    Covers the deterministic parse failures (non-numeric / non-finite value,
    unknown metric, empty title) and a verifier hard block (wrong unit). The
    route maps it to **400** and may surface ``details`` — these are our own
    crafted, non-PHI descriptions of the typed input, never an internal message.
    """

    def __init__(self, message: str, *, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.details: list[str] = details or []


class IdempotencyStore:
    """Single-flight registry of idempotency keys → ``CommittedWrite``, per process.

    Deduplicates a double-clicked / retried confirm: the first caller to claim a
    key owns the write, a *concurrent* second caller awaits that same outcome
    rather than issuing its own append, and a later caller replays the recorded
    result. The claim happens **before** the write, not after it — see
    ``run_once``, which is the only correct way in.

    **In-process only — this is not a distributed guard, and cannot become one
    by wishing.** It holds no lease that anything outside this interpreter can
    see, so it does *not* survive multiple uvicorn workers, multiple replicas,
    or a restart: two processes will each admit one write for the same key and
    the chart gets two identical rows. Nothing downstream saves us — OpenEMR
    does not implement idempotency keys (the ``Idempotency-Key`` header
    ``write_client`` sends is advisory; no OpenEMR route reads it), so this
    store is the *only* dedupe in the system.

    That is sufficient today **only** because the container serves on one
    uvicorn worker — and note it is one by *default*, not by explicit pin: the
    ``Dockerfile`` ``CMD`` simply passes no ``--workers`` flag. Adding
    ``--workers``/``--reload``, a second replica, or any external process that
    can confirm a write silently invalidates the guarantee below with no test
    turning red. Scaling past one worker requires a shared claim (a Redis key or
    a uniquely-indexed DB row on ``idempotency_key``) *first*.

    A restart forgetting keys is the mild case: the underlying OpenEMR write is
    append-only, so a re-confirm after a restart would at worst create one more
    record dated now, never overwrite a prior value. Since writes cannot be
    deleted (only end-dated), a duplicate is a chart-correctness problem, not a
    data-loss one.
    """

    def __init__(self) -> None:
        self._committed: dict[str, CommittedWrite] = {}
        self._in_flight: dict[str, asyncio.Future[CommittedWrite]] = {}

    def get(self, key: str) -> CommittedWrite | None:
        """Peek at a *settled* result — ``None`` while in flight or unknown.

        A read-only accessor. It is deliberately not a gate: checking this and
        then writing is the TOCTOU this class exists to prevent. Use
        ``run_once``.
        """
        return self._committed.get(key)

    def put(self, key: str, value: CommittedWrite) -> None:
        """Record a confirmed result for later replay."""
        self._committed[key] = value

    async def run_once(
        self, key: str, commit: Callable[[], Awaitable[CommittedWrite]]
    ) -> CommittedWrite:
        """Run ``commit`` at most once per key; concurrent callers share one outcome.

        The claim is atomic *across the awaits inside* ``commit``: the lookup and
        the in-flight reservation happen in one synchronous step with no
        ``await`` between them, so on asyncio's single thread no other confirm
        can interleave and claim the same key. The reservation is therefore in
        place **before** the HTTP POST begins rather than after it returns —
        which is precisely what a read-then-write-then-record cannot promise.

        Per key, in order:

        - **settled** → replay the recorded ``CommittedWrite``; ``commit`` is
          never called.
        - **in flight** → ``await`` the owner's outcome and return *that same*
          ``CommittedWrite``. A waiter never reports failure for a write that
          landed, and never issues a second append.
        - **free** → this caller owns the write.

        A **failure does not poison the key.** The reservation is dropped and the
        error is raised to the owner *and* to any concurrent waiter, so a later
        retry with the same key is admitted and genuinely re-attempts: one
        transient 500 must never brick a physician's confirm permanently. The
        trade-off is deliberate and worth naming — only a *confirmed* write is
        recorded, so an append OpenEMR performed but failed to acknowledge stays
        retryable, and retrying it can duplicate. We accept a duplicate risk on
        an already-ambiguous write over a key that can never be used again.

        Granularity is per key: a hung POST parks only its own key's waiters, and
        every other key proceeds concurrently. No lock is held across the await —
        waiters park on that key's future, not on a shared mutex — so a wedged
        write cannot deadlock the store.
        """
        recorded = self._committed.get(key)
        if recorded is not None:
            return recorded

        in_flight = self._in_flight.get(key)
        if in_flight is not None:
            # ``shield`` so a waiter that is itself cancelled (client hung up)
            # cancels only its own wait — never the shared future the owner is
            # still on its way to settling for everyone else.
            return await asyncio.shield(in_flight)

        # --- the claim. Synchronous from the lookups above to the insert below:
        # no await, so this whole sequence is one uninterruptible event-loop step.
        future: asyncio.Future[CommittedWrite] = asyncio.get_running_loop().create_future()
        future.add_done_callback(_consume_future_exception)
        self._in_flight[key] = future
        try:
            committed = await commit()
        except BaseException as exc:
            # Release the key first: a failed attempt must leave it retryable.
            self._in_flight.pop(key, None)
            if not future.done():  # a cancelled waiter may already have settled it
                future.set_exception(exc)
            raise
        self._in_flight.pop(key, None)
        self.put(key, committed)
        if not future.done():
            future.set_result(committed)
        return committed


@lru_cache(maxsize=1)
def get_idempotency_store() -> IdempotencyStore:
    """The shared per-process idempotency store.

    Cached so propose/confirm across separate requests see the same keys; tests
    reset it with ``get_idempotency_store.cache_clear()`` the same way the engine
    and settings singletons are reset.
    """
    return IdempotencyStore()


class WriteService:
    """Orchestrates one physician direct-edit through the propose→confirm gate."""

    def __init__(
        self,
        settings: Settings,
        observability: Observability | None = None,
        idempotency: IdempotencyStore | None = None,
        *,
        write_client_factory: Callable[[], OpenEmrWriteClient] | None = None,
        read_client_factory: Callable[[], FhirClient] | None = None,
    ) -> None:
        self._settings = settings
        self._obs: Observability = observability or NoopObservability()
        self._idempotency = idempotency or get_idempotency_store()
        # Optional per-request client factories. In ``smart`` mode the route
        # injects factories that build the physician's delegated per-session write
        # + read-back clients (the physician's SMART token carries the
        # ``api:oemr user/*.crus`` write scopes). When absent (disabled mode) both
        # fall back to today's guarded password-grant write / system read path.
        self._write_client_factory = write_client_factory
        self._read_client_factory = read_client_factory

    # --- propose ----------------------------------------------------------

    async def propose(
        self,
        *,
        clinician_id: ClinicianId,
        patient_id: PatientId,
        kind: WriteKind,
        raw_value: str,
        metric: str | None = None,
        unit: str | None = None,
        entry_mode: WriteEntryMode = WriteEntryMode.human_direct,
        source: WriteSource | None = None,
    ) -> tuple[ProposedWrite, str]:
        """Parse → verify → echo-back. Never commits; audits ``write_proposed``.

        Returns the structured echo-back a physician confirms and the
        server-generated ``idempotency_key`` (also carried on the candidate) that
        the confirm step re-sends. Raises ``WriteInputError`` (route → 400) for an
        unparseable value, an unknown metric, or a verifier hard block (wrong
        unit). An out-of-range human_direct value is *not* a block — it rides
        along as a soft warning on the verdict.

        ``entry_mode`` is the attribution surface: ``human_direct`` for a
        physician-typed value (default); ``agent_proposed_physician_confirmed``
        for the agent path (F4b), where the verifier runs strict and the write
        stays uncommitted until the separate physician confirm transaction.
        The propose step itself performs **no** OpenEMR call in either mode.

        ``source`` is the optional (document, fact) provenance — the intake bridge
        passes the document + extracted_fact the value was read off, so the
        candidate, its echo-back, and the ``write_proposed`` audit row all name
        the scanned page it came from. It defaults to ``None`` for the
        physician-direct path, which has no source document.
        """
        idempotency_key = _new_idempotency_key()
        candidate = self._parse_candidate(
            clinician_id=clinician_id,
            patient_id=patient_id,
            kind=kind,
            raw_value=raw_value,
            metric=metric,
            unit=unit,
            idempotency_key=idempotency_key,
            entry_mode=entry_mode,
            source=source,
        )

        async with self._obs.span(
            "writeback.propose", patient_id=patient_id.value, clinician_id=clinician_id.value
        ):
            verdict = _verify_candidate(candidate)
            if verdict.blocked:
                raise WriteInputError("write candidate failed verification", details=verdict.errors)
            proposed = ProposedWrite(candidate=candidate, verdict=verdict)

        # HIPAA §164.312(b): the proposal is a physician-attributed action on the
        # chart, so it leaves a trail. Fail-open — the echo-back is already built.
        # ``resource_id`` stays None: a proposal creates nothing, so the trail
        # never names a returned resource. Provenance rides the separate
        # ``source_ref`` field, which is what it honestly is — an input.
        await self._record_write_audit(
            "write_proposed",
            clinician_id,
            patient_id,
            resource_id=None,
            mode=candidate.entry_mode,
            source=candidate.source,
        )
        return proposed, idempotency_key

    # --- commit -----------------------------------------------------------

    async def commit(
        self,
        *,
        clinician_id: ClinicianId,
        patient_id: PatientId,
        candidate: AnyWriteCandidate,
        idempotency_key: str,
    ) -> CommittedWrite:
        """Re-verify the identical candidate, then commit it append-only.

        This is the explicit physician confirm — the only path that writes.
        Re-runs the same deterministic verification and refuses (``WriteInputError``
        → 400) if the candidate would now be blocked or its key does not match the
        confirm URL. Otherwise builds the guarded write client
        (``build_write_client`` raises ``WritebackDisabledError`` → route 503 when
        disabled), commits, audits ``write_committed`` (carrying the candidate's
        ``entry_mode`` — ``agent_proposed_physician_confirmed`` for the agent
        path), and read-backs (fail-open); any ``OpenEmrWriteError`` is audited
        ``write_failed`` and re-raised (→ 502).

        The whole append runs under ``IdempotencyStore.run_once``, so the key is
        claimed *before* the POST rather than recorded after it: a re-confirm
        replays the first ``CommittedWrite`` and a **concurrent** second confirm
        (a double-click — the two arrive interleaved, not one after the other)
        awaits the first's outcome instead of racing it into a duplicate append.
        Verification stays outside the claim: a blocked or mismatched candidate
        is rejected on its own merits and must never reserve a key. See
        ``run_once`` for the failure semantics — a failed write leaves the key
        retryable — and for why this guard is in-process only.
        """
        if candidate.idempotency_key != idempotency_key:
            raise WriteInputError("idempotency key does not match the confirmed candidate")

        # Re-verify: a candidate that would be blocked can never slip through the
        # second transaction, even if the client re-sends a tampered payload.
        verdict = _verify_candidate(candidate)
        if verdict.blocked:
            raise WriteInputError("write candidate failed re-verification", details=verdict.errors)

        async def _commit_once() -> CommittedWrite:
            async with self._obs.span(
                "writeback.commit", patient_id=patient_id.value, clinician_id=clinician_id.value
            ):
                try:
                    async with self._write_client() as writer:
                        committed = await self._perform_write(writer, patient_id, candidate)
                except OpenEmrWriteError:
                    await self._record_write_audit(
                        "write_failed",
                        clinician_id,
                        patient_id,
                        resource_id=None,
                        mode=candidate.entry_mode,
                        source=candidate.source,
                    )
                    raise

                await self._record_write_audit(
                    "write_committed",
                    clinician_id,
                    patient_id,
                    resource_id=committed.new_id,
                    mode=candidate.entry_mode,
                    source=candidate.source,
                )
                await self._read_back(patient_id, candidate, committed)
                return committed

        return await self._idempotency.run_once(idempotency_key, _commit_once)

    # --- parsing ----------------------------------------------------------

    def _parse_candidate(
        self,
        *,
        clinician_id: ClinicianId,
        patient_id: PatientId,
        kind: WriteKind,
        raw_value: str,
        metric: str | None,
        unit: str | None,
        idempotency_key: str,
        entry_mode: WriteEntryMode,
        source: WriteSource | None = None,
    ) -> AnyWriteCandidate:
        """Parse raw physician/agent input into a typed candidate over the closed set.

        Exhaustive ``match`` (no ``default``): a new ``WriteKind`` fails to compile
        until handled. All construction errors collapse to ``WriteInputError``.

        ``source`` is attached verbatim to whichever candidate shape ``kind``
        selects, so provenance is carried by the same object the physician
        confirms — never re-derived later from something that could have drifted.
        """
        try:
            match kind:
                case WriteKind.vital:
                    return WriteCandidate(
                        kind=kind,
                        patient_id=patient_id,
                        clinician_id=clinician_id,
                        idempotency_key=idempotency_key,
                        entry_mode=entry_mode,
                        vital=self._parse_vital(raw_value, metric, unit),
                        source=source,
                    )
                case WriteKind.medication:
                    return WriteCandidate(
                        kind=kind,
                        patient_id=patient_id,
                        clinician_id=clinician_id,
                        idempotency_key=idempotency_key,
                        entry_mode=entry_mode,
                        medication=self._parse_medication(raw_value),
                        source=source,
                    )
                case WriteKind.medical_problem:
                    return IssueWriteCandidate(
                        kind=kind,
                        patient_id=patient_id,
                        clinician_id=clinician_id,
                        idempotency_key=idempotency_key,
                        entry_mode=entry_mode,
                        medical_problem=self._parse_medical_problem(raw_value),
                        source=source,
                    )
                case WriteKind.allergy:
                    return IssueWriteCandidate(
                        kind=kind,
                        patient_id=patient_id,
                        clinician_id=clinician_id,
                        idempotency_key=idempotency_key,
                        entry_mode=entry_mode,
                        allergy=self._parse_allergy(raw_value),
                        source=source,
                    )
        except ValidationError as exc:
            raise WriteInputError(
                "invalid write candidate", details=_validation_details(exc)
            ) from exc

    def _parse_vital(self, raw_value: str, metric: str | None, unit: str | None) -> VitalWrite:
        if metric is None:
            raise WriteInputError("a vital write requires a metric")
        if unit is None:
            raise WriteInputError("a vital write requires a unit")
        try:
            return VitalWrite(
                metric=_parse_metric(metric), value=_parse_number(raw_value), unit=unit
            )
        except ValidationError as exc:
            raise WriteInputError("invalid vital", details=_validation_details(exc)) from exc

    def _parse_medication(self, raw_value: str) -> MedicationWrite:
        title = raw_value.strip()
        if not title:
            raise WriteInputError("a medication write requires a non-empty title")
        try:
            # begdate defaults to the write's clinical time ("now"); dose/schedule
            # live in a separate prescription endpoint (deferred, see the plan).
            return MedicationWrite(title=title, begdate=utcnow().date().isoformat())
        except ValidationError as exc:
            raise WriteInputError("invalid medication", details=_validation_details(exc)) from exc

    def _parse_medical_problem(self, raw_value: str) -> MedicalProblemWrite:
        title = raw_value.strip()
        if not title:
            raise WriteInputError("a medical problem write requires a non-empty title")
        try:
            return MedicalProblemWrite(title=title, begdate=utcnow().date().isoformat())
        except ValidationError as exc:
            raise WriteInputError(
                "invalid medical problem", details=_validation_details(exc)
            ) from exc

    def _parse_allergy(self, raw_value: str) -> AllergyWrite:
        title = raw_value.strip()
        if not title:
            raise WriteInputError("an allergy write requires a non-empty title")
        try:
            return AllergyWrite(title=title, begdate=utcnow().date().isoformat())
        except ValidationError as exc:
            raise WriteInputError("invalid allergy", details=_validation_details(exc)) from exc

    # --- write + read-back ------------------------------------------------

    async def _perform_write(
        self, writer: OpenEmrWriteClient, patient_id: PatientId, candidate: AnyWriteCandidate
    ) -> CommittedWrite:
        """Dispatch the append to the write client — exhaustive ``match``.

        Vitals resolve/create an encounter first (a vital attaches to one);
        medications and issues (medical problems / allergies) post a new list
        row directly.
        """
        if isinstance(candidate, IssueWriteCandidate):
            return await self._perform_issue_write(writer, patient_id, candidate)
        match candidate.kind:
            case WriteKind.vital:
                vital = candidate.vital
                if vital is None:  # unreachable given the candidate validator.
                    raise WriteInputError("vital candidate is missing its payload")
                eid = await writer.resolve_or_create_encounter(patient_id)
                return await writer.create_vital(
                    patient_id, eid, vital, idempotency_key=candidate.idempotency_key
                )
            case WriteKind.medication:
                med = candidate.medication
                if med is None:  # unreachable given the candidate validator.
                    raise WriteInputError("medication candidate is missing its payload")
                return await writer.create_medication(
                    patient_id, med, idempotency_key=candidate.idempotency_key
                )

    async def _perform_issue_write(
        self, writer: OpenEmrWriteClient, patient_id: PatientId, candidate: IssueWriteCandidate
    ) -> CommittedWrite:
        """Commit one physician-confirmed issue write — exhaustive ``match``."""
        match candidate.kind:
            case WriteKind.medical_problem:
                problem = candidate.medical_problem
                if problem is None:  # unreachable given the candidate validator.
                    raise WriteInputError("medical problem candidate is missing its payload")
                return await writer.create_medical_problem(
                    patient_id, problem, idempotency_key=candidate.idempotency_key
                )
            case WriteKind.allergy:
                allergy = candidate.allergy
                if allergy is None:  # unreachable given the candidate validator.
                    raise WriteInputError("allergy candidate is missing its payload")
                # ``source`` reaches OpenEMR only here: the allergy route is the
                # single Standard-API list write with an honest home for it (a
                # whitelisted ``comments`` column). See create_allergy.
                return await writer.create_allergy(
                    patient_id,
                    allergy,
                    idempotency_key=candidate.idempotency_key,
                    source=candidate.source,
                )

    async def _read_back(
        self, patient_id: PatientId, candidate: AnyWriteCandidate, committed: CommittedWrite
    ) -> None:
        """Close the loop: re-read through the read client, log any mismatch.

        Fail-open and log-only — a write that landed is append-only, so a failed
        or mismatched read-back is *surfaced*, never rolled back (there is no
        destructive delete in Phase 1). Any error here is swallowed after logging.
        """
        try:
            async with self._read_client() as reader:
                confirmed = await self._value_round_trips(reader, patient_id, candidate)
            if not confirmed:
                _logger.warning(
                    "post-write read-back did not observe the committed value",
                    extra={
                        "patient_id": patient_id.value,
                        "resource_kind": committed.resource_kind.value,
                        "new_id": committed.new_id,
                    },
                )
        except Exception:
            _logger.exception(
                "post-write read-back failed",
                extra={"patient_id": patient_id.value, "new_id": committed.new_id},
            )

    async def _value_round_trips(
        self, reader: FhirClient, patient_id: PatientId, candidate: AnyWriteCandidate
    ) -> bool:
        """Best-effort: does the written value/title appear in a live re-fetch?

        Lightweight heuristic (the write returns an OpenEMR form/list id, not a
        FHIR id): search the patient's resources of the matching type and look for
        the value. Only used to log a round-trip warning, never to gate the write.
        """
        if isinstance(candidate, IssueWriteCandidate):
            return await self._issue_round_trips(reader, patient_id, candidate)
        match candidate.kind:
            case WriteKind.vital:
                vital = candidate.vital
                if vital is None:
                    return False
                bundle = await reader.search(ResourceType.Observation, {"patient": str(patient_id)})
                return any(math.isclose(v, vital.value) for v in _observation_values(bundle))
            case WriteKind.medication:
                med = candidate.medication
                if med is None:
                    return False
                bundle = await reader.search(
                    ResourceType.MedicationRequest, {"patient": str(patient_id)}
                )
                needle = med.title.strip().lower()
                return any(needle in title.lower() for title in _medication_titles(bundle))

    async def _issue_round_trips(
        self, reader: FhirClient, patient_id: PatientId, candidate: IssueWriteCandidate
    ) -> bool:
        """Issue-kind read-back: look for the title on the matching FHIR type."""
        match candidate.kind:
            case WriteKind.medical_problem:
                issue: MedicalProblemWrite | AllergyWrite | None = candidate.medical_problem
                resource_type = ResourceType.Condition
            case WriteKind.allergy:
                issue = candidate.allergy
                resource_type = ResourceType.AllergyIntolerance
        if issue is None:
            return False
        bundle = await reader.search(resource_type, {"patient": str(patient_id)})
        needle = issue.title.strip().lower()
        return any(needle in text.lower() for text in _code_texts(bundle))

    # --- audit ------------------------------------------------------------

    async def _record_write_audit(
        self,
        action: str,
        clinician_id: ClinicianId,
        patient_id: PatientId,
        *,
        resource_id: str | None,
        mode: WriteEntryMode,
        source: WriteSource | None = None,
    ) -> None:
        """Append the physician-attributed write-trail row (fail-open).

        ``entry_mode`` carries the attribution surface (``human_direct`` for a
        physician-typed edit; ``agent_proposed_physician_confirmed`` for an
        agent-proposed, physician-confirmed write); ``resources_returned`` names
        the created resource on a commit, empty on a proposal or a failure. A
        broken audit write is logged and swallowed so it can never turn a
        completed write into a 500.

        ``source`` is the (document, fact) provenance of a derived write, recorded
        in its own ``source_ref`` column — never folded into
        ``resources_returned``, which means "resources this action returned or
        created". Together the two answer the spec's traceability question on
        every row: *what did this write create* (``resources_returned``) and
        *what was it derived from* (``source_ref``). ``None`` for a
        physician-direct write, which has no source document.
        """
        try:
            async with session_scope() as session:
                await MemoryRepository(session).record_audit(
                    correlation_id=current_correlation_id(),
                    action=action,
                    patient_id=patient_id,
                    clinician_id=clinician_id.value,
                    resources_returned=[resource_id] if resource_id else [],
                    entry_mode=mode.value,
                    source_ref=source,
                )
        except Exception:
            _logger.exception(
                "failed to write %s audit row",
                action,
                extra={"patient_id": patient_id.value, "clinician_id": clinician_id.value},
            )

    # --- collaborators (seams for tests) ----------------------------------

    def _write_client(self) -> OpenEmrWriteClient:
        """The guarded Standard-API write client for this request.

        Built here — inside the interactive request path — never at import or in
        the poller lifespan. Smart mode: the route-injected factory builds the
        physician's delegated per-session write client (their SMART token carries
        the write scopes), so OpenEMR attributes the write to that physician; it
        stays guarded on ``writeback_enabled``. Otherwise (disabled mode):
        ``build_write_client`` — the dedicated password-grant client — which
        raises ``WritebackDisabledError`` unless write-back is enabled and
        configured.
        """
        if self._write_client_factory is not None:
            return self._write_client_factory()
        return build_write_client(self._settings)

    def _read_client(self) -> FhirClient:
        """The read-only FHIR client for the post-write read-back.

        Smart mode uses the route-injected per-session factory (physician's
        delegated token); disabled mode uses the system-token client.
        """
        if self._read_client_factory is not None:
            return self._read_client_factory()
        return build_fhir_client(self._settings)


# --- module helpers ---------------------------------------------------------


def _new_idempotency_key() -> str:
    """A fresh client-facing idempotency key (URL-safe, well under 128 chars)."""
    return secrets.token_urlsafe(24)


def _consume_future_exception(future: asyncio.Future[CommittedWrite]) -> None:
    """Mark a settled claim-future's exception retrieved, silencing asyncio noise.

    The shared future is only a hand-off to concurrent waiters: the owner always
    raises the failure itself, and each waiter re-raises it. When a claim fails
    with nobody waiting — the common case — nothing would ever call
    ``.exception()``, and asyncio would log a spurious "exception was never
    retrieved" at GC. Retrieving it here marks it consumed. It is never
    swallowed: the owner is raising this very exception up its own stack.
    """
    if not future.cancelled():
        future.exception()


def _verify_candidate(candidate: AnyWriteCandidate) -> WriteVerdict:
    """The single deterministic verification gate for propose AND commit.

    Direct kinds (vital/medication) go through ``verification/writes.py``'s
    ``verify_write`` under the candidate's own ``entry_mode`` — so an
    agent-proposed vital would be held to the strict (hard-block) range rules.
    Issue kinds (medical problem / allergy) are verified here: their
    deterministic gate is a non-empty title (already enforced by the type) plus
    a well-formed ``YYYY-MM-DD`` ``begdate``, mirroring the medication gate.
    """
    if isinstance(candidate, WriteCandidate):
        return verify_write(candidate, mode=candidate.entry_mode)
    return _verify_issue(candidate)


def _verify_issue(candidate: IssueWriteCandidate) -> WriteVerdict:
    """Deterministically verify one issue candidate. Never raises."""
    match candidate.kind:
        case WriteKind.medical_problem:
            issue: MedicalProblemWrite | AllergyWrite | None = candidate.medical_problem
        case WriteKind.allergy:
            issue = candidate.allergy
    if issue is None:  # unreachable given the candidate validator; belt-and-braces.
        return WriteVerdict(
            kind=candidate.kind, blocked=True, errors=[f"missing {candidate.kind.value} payload"]
        )

    errors: list[str] = []
    if not issue.title.strip():
        errors.append(f"{candidate.kind.value} title is empty")
    if not is_iso_date(issue.begdate):
        errors.append(f"begdate {issue.begdate!r} is not a valid YYYY-MM-DD date")

    return WriteVerdict(kind=candidate.kind, blocked=bool(errors), errors=errors)


def _parse_metric(metric: str) -> WritableMetric:
    try:
        return WritableMetric(metric)
    except ValueError as exc:
        raise WriteInputError(f"unknown metric {metric!r}") from exc


def _parse_number(raw_value: str) -> float:
    """Parse a physician-typed number, rejecting non-numeric and non-finite input.

    ``nan``/``inf`` are rejected here rather than left to the range check: ``nan``
    compares false against both bounds, so it would otherwise slip through as an
    in-range value.
    """
    try:
        value = float(raw_value.strip())
    except ValueError as exc:
        raise WriteInputError(f"value {raw_value!r} is not a number") from exc
    if not math.isfinite(value):
        raise WriteInputError(f"value {raw_value!r} is not a finite number")
    return value


def _validation_details(exc: ValidationError) -> list[str]:
    return [f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}" for e in exc.errors()]


def _bundle_resources(bundle: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    for entry in bundle.get("entry") or []:
        if isinstance(entry, Mapping):
            res = entry.get("resource")
            if isinstance(res, Mapping):
                out.append(res)
    return out


def _observation_values(bundle: Mapping[str, Any]) -> list[float]:
    """Numeric ``valueQuantity.value`` of each Observation in a search Bundle."""
    values: list[float] = []
    for res in _bundle_resources(bundle):
        vq = res.get("valueQuantity")
        if isinstance(vq, Mapping):
            v = vq.get("value")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                values.append(float(v))
    return values


def _medication_titles(bundle: Mapping[str, Any]) -> list[str]:
    """Display titles of each MedicationRequest in a search Bundle."""
    titles: list[str] = []
    for res in _bundle_resources(bundle):
        concept = res.get("medicationCodeableConcept")
        if isinstance(concept, Mapping):
            text = concept.get("text")
            if isinstance(text, str) and text:
                titles.append(text)
    return titles


def _code_texts(bundle: Mapping[str, Any]) -> list[str]:
    """``code.text`` of each resource in a search Bundle (Condition / Allergy)."""
    texts: list[str] = []
    for res in _bundle_resources(bundle):
        code = res.get("code")
        if isinstance(code, Mapping):
            text = code.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
    return texts
