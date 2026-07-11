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

import logging
import math
import secrets
from collections.abc import Mapping
from functools import lru_cache
from typing import Any

from pydantic import ValidationError

from copilot.config import Settings
from copilot.domain.primitives import ClinicianId, PatientId, ResourceType, utcnow
from copilot.domain.writes import (
    CommittedWrite,
    MedicationWrite,
    ProposedWrite,
    VitalWrite,
    WritableMetric,
    WriteCandidate,
    WriteEntryMode,
    WriteKind,
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
    """Process-local registry of committed idempotency keys → ``CommittedWrite``.

    Guards against a double-clicked / retried confirm creating a duplicate
    append: the second commit of a key replays the first ``CommittedWrite``
    instead of writing again. Phase 1 keeps this in-process (a bedside demo runs
    one worker); a restart forgets keys, which is safe because the underlying
    OpenEMR write is append-only — a re-confirm after a restart would at worst
    create one more record dated now, never overwrite a prior value. A durable
    store is a Phase-2+ concern.
    """

    def __init__(self) -> None:
        self._committed: dict[str, CommittedWrite] = {}

    def get(self, key: str) -> CommittedWrite | None:
        return self._committed.get(key)

    def put(self, key: str, value: CommittedWrite) -> None:
        self._committed[key] = value


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
    ) -> None:
        self._settings = settings
        self._obs: Observability = observability or NoopObservability()
        self._idempotency = idempotency or get_idempotency_store()

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
    ) -> tuple[ProposedWrite, str]:
        """Parse → verify → echo-back. Never commits; audits ``write_proposed``.

        Returns the structured echo-back a physician confirms and the
        server-generated ``idempotency_key`` (also carried on the candidate) that
        the confirm step re-sends. Raises ``WriteInputError`` (route → 400) for an
        unparseable value, an unknown metric, or a verifier hard block (wrong
        unit). An out-of-range human_direct value is *not* a block — it rides
        along as a soft warning on the verdict.
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
        )

        async with self._obs.span(
            "writeback.propose", patient_id=patient_id.value, clinician_id=clinician_id.value
        ):
            verdict = verify_write(candidate, mode=WriteEntryMode.human_direct)
            if verdict.blocked:
                raise WriteInputError(
                    "write candidate failed verification", details=verdict.errors
                )
            proposed = ProposedWrite(candidate=candidate, verdict=verdict)

        # HIPAA §164.312(b): the proposal is a physician-attributed action on the
        # chart, so it leaves a trail. Fail-open — the echo-back is already built.
        await self._record_write_audit(
            "write_proposed", clinician_id, patient_id, resource_id=None, mode=candidate.entry_mode
        )
        return proposed, idempotency_key

    # --- commit -----------------------------------------------------------

    async def commit(
        self,
        *,
        clinician_id: ClinicianId,
        patient_id: PatientId,
        candidate: WriteCandidate,
        idempotency_key: str,
    ) -> CommittedWrite:
        """Re-verify the identical candidate, then commit it append-only.

        Re-runs the same deterministic verification and refuses (``WriteInputError``
        → 400) if the candidate would now be blocked or its key does not match the
        confirm URL. A key already committed replays its ``CommittedWrite`` with no
        second write (idempotent). Otherwise builds the guarded write client
        (``build_write_client`` raises ``WritebackDisabledError`` → route 503 when
        disabled), commits, audits ``write_committed``, and read-backs (fail-open);
        any ``OpenEmrWriteError`` is audited ``write_failed`` and re-raised (→ 502).
        """
        if candidate.idempotency_key != idempotency_key:
            raise WriteInputError("idempotency key does not match the confirmed candidate")

        # Re-verify: a candidate that would be blocked can never slip through the
        # second transaction, even if the client re-sends a tampered payload.
        verdict = verify_write(candidate, mode=WriteEntryMode.human_direct)
        if verdict.blocked:
            raise WriteInputError("write candidate failed re-verification", details=verdict.errors)

        existing = self._idempotency.get(idempotency_key)
        if existing is not None:
            return existing  # idempotent replay — no second write

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
                )
                raise

            await self._record_write_audit(
                "write_committed",
                clinician_id,
                patient_id,
                resource_id=committed.new_id,
                mode=candidate.entry_mode,
            )
            await self._read_back(patient_id, candidate, committed)

        self._idempotency.put(idempotency_key, committed)
        return committed

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
    ) -> WriteCandidate:
        """Parse raw physician input into a typed candidate over the closed set.

        Exhaustive ``match`` (no ``default``): a new ``WriteKind`` fails to compile
        until handled. All construction errors collapse to ``WriteInputError``.
        """
        match kind:
            case WriteKind.vital:
                payload: VitalWrite | MedicationWrite = self._parse_vital(raw_value, metric, unit)
                field = "vital"
            case WriteKind.medication:
                payload = self._parse_medication(raw_value)
                field = "medication"
        try:
            return WriteCandidate(
                kind=kind,
                patient_id=patient_id,
                clinician_id=clinician_id,
                idempotency_key=idempotency_key,
                entry_mode=WriteEntryMode.human_direct,
                **{field: payload},
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

    # --- write + read-back ------------------------------------------------

    async def _perform_write(
        self, writer: OpenEmrWriteClient, patient_id: PatientId, candidate: WriteCandidate
    ) -> CommittedWrite:
        """Dispatch the append to the write client — exhaustive ``match``.

        Vitals resolve/create an encounter first (a vital attaches to one);
        medications post a new list row directly.
        """
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

    async def _read_back(
        self, patient_id: PatientId, candidate: WriteCandidate, committed: CommittedWrite
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
        self, reader: FhirClient, patient_id: PatientId, candidate: WriteCandidate
    ) -> bool:
        """Best-effort: does the written value/title appear in a live re-fetch?

        Lightweight heuristic (the write returns an OpenEMR form/list id, not a
        FHIR id): search the patient's resources of the matching type and look for
        the value. Only used to log a round-trip warning, never to gate the write.
        """
        match candidate.kind:
            case WriteKind.vital:
                vital = candidate.vital
                if vital is None:
                    return False
                bundle = await reader.search(
                    ResourceType.Observation, {"patient": str(patient_id)}
                )
                return any(
                    math.isclose(v, vital.value) for v in _observation_values(bundle)
                )
            case WriteKind.medication:
                med = candidate.medication
                if med is None:
                    return False
                bundle = await reader.search(
                    ResourceType.MedicationRequest, {"patient": str(patient_id)}
                )
                needle = med.title.strip().lower()
                return any(needle in title.lower() for title in _medication_titles(bundle))

    # --- audit ------------------------------------------------------------

    async def _record_write_audit(
        self,
        action: str,
        clinician_id: ClinicianId,
        patient_id: PatientId,
        *,
        resource_id: str | None,
        mode: WriteEntryMode,
    ) -> None:
        """Append the physician-attributed write-trail row (fail-open).

        ``entry_mode`` carries the attribution surface (``human_direct`` in
        Phase 1); ``resources_returned`` names the created resource on a commit,
        empty on a proposal or a failure. A broken audit write is logged and
        swallowed so it can never turn a completed write into a 500.
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
        the poller lifespan. ``build_write_client`` raises ``WritebackDisabledError``
        unless write-back is enabled and configured.
        """
        return build_write_client(self._settings)

    def _read_client(self) -> FhirClient:
        """The read-only FHIR client for the post-write read-back."""
        return build_fhir_client(self._settings)


# --- module helpers ---------------------------------------------------------


def _new_idempotency_key() -> str:
    """A fresh client-facing idempotency key (URL-safe, well under 128 chars)."""
    return secrets.token_urlsafe(24)


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
