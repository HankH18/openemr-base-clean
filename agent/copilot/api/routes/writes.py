"""Physician write-back API — the propose→confirm gate (Phase 1b).

``POST /v1/writes`` parses a typed physician request over the closed writable
metric set, verifies it deterministically, and returns a structured echo-back to
confirm (never agent prose). ``POST /v1/writes/{idempotency_key}/confirm`` is the
distinct, human-initiated second transaction that commits the identical candidate
append-only through the guarded write client and returns proof of the write.

Both endpoints enforce the same rounding-list authorization boundary as chat and
observations (``is_authorized`` → **403**, no audit on the refusal) and both are
inert unless ``settings.writeback_enabled`` is true — when the flag is off they
return a clear **503**, never a 500, and no write client is ever built.

Mounted automatically by ``copilot.api.app.register_routers`` (module-level
``router``); no edit to ``app.py`` is required.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, Field

from copilot.api.deps import resolve_acting_clinician, resolve_acting_context
from copilot.auth import is_authorized
from copilot.config import get_settings
from copilot.domain.primitives import PatientId
from copilot.domain.writes import AnyWriteCandidate, CommittedWrite, ProposedWrite, WriteKind
from copilot.fhir.client import FhirClient
from copilot.fhir.provider import (
    WritebackDisabledError,
    build_fhir_client_for_session,
    build_write_client_for_session,
)
from copilot.fhir.write_client import OpenEmrWriteClient, OpenEmrWriteError
from copilot.observability import Observability
from copilot.writeback.intake_bridge import DocumentNotFoundError, IntakeWritebackBridge
from copilot.writeback.service import WriteInputError, WriteService

router = APIRouter(prefix="/v1", tags=["writes"])

_DISABLED_DETAIL = "Write-back is currently disabled"
_UNAUTHORIZED_DETAIL = "Patient is not on your rounding list"


class ProposeRequest(BaseModel):
    """A raw physician write request, parsed into a typed candidate server-side.

    ``metric``/``unit`` are required for a vital and ignored for a medication
    (whose ``raw_value`` is the picked/echoed drug title). No value is ever
    interpreted by a model — the deterministic parser owns it.

    ``clinician_id`` is optional: in ``disabled`` mode it identifies the acting
    clinician (as today); in ``smart`` mode the session cookie is authoritative
    and this field, if present, is only validated against it (mismatch → 403).
    """

    clinician_id: int | None = Field(default=None, gt=0)
    patient_id: int = Field(gt=0)
    kind: WriteKind
    raw_value: str = Field(min_length=1)
    metric: str | None = None
    unit: str | None = None


class ConfirmRequest(BaseModel):
    """The confirm transaction: the identical candidate echoed back verbatim.

    The candidate carries its own ``patient_id`` / ``clinician_id`` (parsed at the
    boundary by Pydantic) and ``idempotency_key``; the URL key must match it.

    ``candidate`` is the full ``AnyWriteCandidate`` union (``WriteCandidate`` for
    the physician-direct vital/medication kinds, ``IssueWriteCandidate`` for the
    F4b agent-proposed medical_problem/allergy kinds), so an issue write can be
    confirmed over HTTP — the two candidate shapes are distinguished by their
    disjoint ``kind`` literals. The service's ``commit`` already accepts the
    union.
    """

    candidate: AnyWriteCandidate


class ProposeFromDocumentRequest(BaseModel):
    """Identify the patient (and, in disabled mode, the acting clinician) for a
    document-driven batch proposal. The document itself is named in the path.

    ``clinician_id`` follows the same auth-mode contract as :class:`ProposeRequest`:
    it identifies the acting clinician in ``disabled`` mode and, in ``smart`` mode,
    is only validated against the session cookie (mismatch → 403).
    """

    clinician_id: int | None = Field(default=None, gt=0)
    patient_id: int = Field(gt=0)


def _service(
    observability: Observability,
    *,
    write_client_factory: Callable[[], OpenEmrWriteClient] | None = None,
    read_client_factory: Callable[[], FhirClient] | None = None,
) -> WriteService:
    return WriteService(
        get_settings(),
        observability,
        write_client_factory=write_client_factory,
        read_client_factory=read_client_factory,
    )


def _write_factory(session_id: str | None) -> Callable[[], OpenEmrWriteClient] | None:
    """A per-session write-client factory in smart mode; ``None`` (password path) otherwise."""
    if session_id is None:
        return None
    return lambda: build_write_client_for_session(get_settings(), session_id)


def _read_factory(session_id: str | None) -> Callable[[], FhirClient] | None:
    """A per-session read-back factory in smart mode; ``None`` (system path) otherwise."""
    if session_id is None:
        return None
    return lambda: build_fhir_client_for_session(get_settings(), session_id)


def _proposed_body(proposed: ProposedWrite, idempotency_key: str) -> dict[str, Any]:
    """The confirmation card: the exact record, its verdict, and the notice."""
    return {
        "idempotency_key": idempotency_key,
        "candidate": proposed.candidate.model_dump(mode="json"),
        "verdict": proposed.verdict.model_dump(mode="json"),
        "effective_time": proposed.effective_time,
        "notice": proposed.notice,
        "warnings": proposed.verdict.warnings,
    }


def _committed_body(committed: CommittedWrite) -> dict[str, Any]:
    return committed.model_dump(mode="json")


def _input_error_detail(exc: WriteInputError) -> Any:
    """Surface the (non-PHI, self-authored) violation to the caller."""
    if exc.details:
        return {"message": str(exc), "violations": exc.details}
    return str(exc)


@router.post("/writes", summary="Propose a physician write and get the echo-back to confirm")
async def propose_write(req: ProposeRequest, request: Request) -> dict[str, Any]:
    # Parse the raw ids into validated primitives at the boundary. Identity per
    # the auth-mode contract: disabled → the body clinician_id; smart → the
    # session cookie (401 if none, 403 if the body id disagrees).
    cid = await resolve_acting_clinician(get_settings(), request, req.clinician_id)
    pid = PatientId(value=req.patient_id)

    # Authorization boundary (UC-6), identical to chat/observations: refuse a
    # patient the clinician has not established on their rounding list. Checked
    # before the disabled gate so feature availability never leaks to an
    # unauthorized caller. No audit on this refusal (no PHI action taken).
    if not await is_authorized(cid, pid):
        raise HTTPException(status_code=403, detail=_UNAUTHORIZED_DETAIL)

    if not get_settings().writeback_enabled:
        raise HTTPException(status_code=503, detail=_DISABLED_DETAIL)

    try:
        proposed, idempotency_key = await _service(request.app.state.observability).propose(
            clinician_id=cid,
            patient_id=pid,
            kind=req.kind,
            raw_value=req.raw_value,
            metric=req.metric,
            unit=req.unit,
        )
    except WriteInputError as exc:
        raise HTTPException(status_code=400, detail=_input_error_detail(exc)) from exc

    return _proposed_body(proposed, idempotency_key)


@router.post(
    "/writes/propose-from-document/{document_id}",
    summary="Propose write candidates from a document's categorized intake facts",
)
async def propose_writes_from_document(
    document_id: Annotated[int, Path(gt=0)],
    req: ProposeFromDocumentRequest,
    request: Request,
) -> dict[str, Any]:
    # Same auth + authorization boundary as the other write routes. Identity per
    # the auth-mode contract (disabled → body clinician_id; smart → session
    # cookie); the rounding-list check refuses an unauthorized patient (403, no
    # audit) before the disabled gate so feature availability never leaks.
    acting = await resolve_acting_context(get_settings(), request, req.clinician_id)
    cid = acting.clinician_id
    pid = PatientId(value=req.patient_id)

    if not await is_authorized(cid, pid):
        raise HTTPException(status_code=403, detail=_UNAUTHORIZED_DETAIL)

    if not get_settings().writeback_enabled:
        raise HTTPException(status_code=503, detail=_DISABLED_DETAIL)

    # Propose-only: the bridge composes WriteService.propose (no write client is
    # ever built, no OpenEMR write occurs). Commit stays the physician's separate
    # confirm transaction — the agent structurally cannot self-commit.
    bridge = IntakeWritebackBridge(_service(request.app.state.observability))
    try:
        proposals = await bridge.propose_writes_from_document(
            document_id=document_id,
            acting_clinician=cid,
            patient_id=pid,
        )
    except WriteInputError as exc:
        raise HTTPException(status_code=400, detail=_input_error_detail(exc)) from exc
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Document not found") from exc

    return {
        "document_id": document_id,
        "count": len(proposals),
        "proposals": [_proposed_body(p, p.candidate.idempotency_key) for p in proposals],
    }


@router.post(
    "/writes/{idempotency_key}/confirm",
    summary="Confirm and commit a previously proposed write (append-only)",
)
async def confirm_write(
    idempotency_key: Annotated[str, Path(min_length=1, max_length=128)],
    req: ConfirmRequest,
    request: Request,
) -> dict[str, Any]:
    candidate = req.candidate
    # The candidate already carries typed ids (parsed at the boundary by Pydantic).
    # Identity per the auth-mode contract: disabled → the candidate's clinician_id;
    # smart → the session cookie (401 if none, 403 if the candidate id disagrees).
    # The session id (smart mode) selects the physician's delegated write token.
    acting = await resolve_acting_context(get_settings(), request, candidate.clinician_id.value)
    cid = acting.clinician_id
    pid = candidate.patient_id

    if not await is_authorized(cid, pid):
        raise HTTPException(status_code=403, detail=_UNAUTHORIZED_DETAIL)

    if not get_settings().writeback_enabled:
        raise HTTPException(status_code=503, detail=_DISABLED_DETAIL)

    try:
        committed = await _service(
            request.app.state.observability,
            write_client_factory=_write_factory(acting.session_id),
            read_client_factory=_read_factory(acting.session_id),
        ).commit(
            clinician_id=cid,
            patient_id=pid,
            candidate=candidate,
            idempotency_key=idempotency_key,
        )
    except WriteInputError as exc:
        raise HTTPException(status_code=400, detail=_input_error_detail(exc)) from exc
    except WritebackDisabledError as exc:
        # Belt-and-braces: the flag flipped off between propose and confirm.
        raise HTTPException(status_code=503, detail=_DISABLED_DETAIL) from exc
    except OpenEmrWriteError as exc:
        # The write could not be confirmed against OpenEMR — generic upstream
        # failure; the specific server detail is logged/audited, never surfaced.
        raise HTTPException(status_code=502, detail="The write could not be completed") from exc

    return _committed_body(committed)
