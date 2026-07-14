"""Grounded conversational chat API — drill-down on one patient.

``POST /v1/chat`` answers a free-text question about a patient; every claim is
gated against a live FHIR re-fetch, and an ungroundable question is withheld
(fail-closed) rather than guessed.  ``GET /v1/conversations/{id}`` reads the
persisted multi-turn history back in order.

Mounted automatically by ``copilot.api.app.register_routers`` (it exposes a
module-level ``router``); no edit to ``app.py`` is required.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from copilot.api.deps import resolve_acting_context
from copilot.api.middleware import resolve_correlation_id
from copilot.auth import is_authorized
from copilot.chat.service import ChatReply, ChatService
from copilot.config import get_settings
from copilot.domain.primitives import PatientId
from copilot.fhir.client import FhirClient
from copilot.fhir.provider import build_fhir_client_for_session
from copilot.memory.db import session_scope
from copilot.memory.repository import MemoryRepository
from copilot.observability import Observability
from copilot.rag import build_retriever

router = APIRouter(prefix="/v1", tags=["chat"])

_logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    """One grounded question about a patient, optionally continuing a thread.

    ``clinician_id`` is optional: in ``disabled`` mode it identifies the acting
    clinician (as today); in ``smart`` mode the session cookie is authoritative
    and this field, if present, is only validated against it (mismatch → 403).
    """

    clinician_id: int | None = Field(default=None, gt=0)
    patient_id: int = Field(gt=0)
    message: str = Field(min_length=1)
    conversation_id: int | None = Field(default=None, gt=0)
    correlation_id: str | None = None


def _service(
    observability: Observability,
    fhir_client_factory: Callable[[], FhirClient] | None = None,
) -> ChatService:
    return ChatService(get_settings(), observability, fhir_client_factory=fhir_client_factory)


def _reader_factory(session_id: str | None) -> Callable[[], FhirClient] | None:
    """A per-session reader factory in smart mode; ``None`` (system path) otherwise."""
    if session_id is None:
        return None
    return lambda: build_fhir_client_for_session(get_settings(), session_id)


def _reply_body(reply: ChatReply) -> dict[str, Any]:
    return {
        "answer": reply.answer,
        "claims": [
            {"text": c.text, "source_ref": c.source_ref.model_dump(mode="json")}
            for c in reply.claims
        ],
        "verification": {"action": reply.action.value, "passed": reply.passed},
        "conversation_id": reply.conversation_id,
        "correlation_id": reply.correlation_id,
    }


async def _guideline_evidence(message: str) -> list[dict[str, Any]]:
    """Retrieve guideline chunks as a SEPARATE, labeled evidence block.

    Guideline backing is kept strictly out of the patient-fact ``claims`` — the
    two grounding surfaces never mix (a guideline recommendation is not a
    grounded patient observation). Each entry is typed guideline evidence
    (``source_type='guideline'`` + ``chunk_id``/``section`` + a
    ``GuidelineCitation``). The retriever de-identifies the query at its own
    choke point before any embedder/reranker egress. Best-effort: an empty
    corpus yields ``[]``, and a retrieval failure never withholds the grounded
    answer — it degrades to no evidence, logged.
    """
    try:
        evidence = await build_retriever(get_settings()).retrieve(message)
    except Exception:
        _logger.exception("guideline evidence retrieval failed; serving answer without evidence")
        return []
    return [e.model_dump(mode="json") for e in evidence]


@router.post("/chat", summary="Answer a grounded question about a patient")
async def chat(req: ChatRequest, request: Request) -> dict[str, Any]:
    # Resolve at the boundary: a valid supplied id is honoured, anything else
    # (missing / malformed) yields a freshly generated one.
    correlation_id = resolve_correlation_id(req.correlation_id)
    # Identity per the auth-mode contract: disabled → the request's clinician_id;
    # smart → the session cookie (401 if none, 403 if the body id disagrees). The
    # session id (smart mode) selects the physician's delegated read token.
    acting = await resolve_acting_context(get_settings(), request, req.clinician_id)
    clinician_id = acting.clinician_id
    patient_id = PatientId(value=req.patient_id)

    # Authorization boundary (UC-6): refuse a patient the clinician has not
    # established on their rounding list — never answer, never leak.  Generic
    # reason: no internal detail about who is (or is not) authorized.
    if not await is_authorized(clinician_id, patient_id):
        raise HTTPException(status_code=403, detail="Patient is not on your rounding list")

    reply = await _service(
        request.app.state.observability, _reader_factory(acting.session_id)
    ).chat(
        clinician_id=clinician_id,
        patient_id=patient_id,
        message=req.message,
        correlation_id=correlation_id,
        conversation_id=req.conversation_id,
    )
    body = _reply_body(reply)
    # Evidence separation: guideline backing rides as a distinct top-level block,
    # never inside a patient-fact claim's citation.
    body["guideline_evidence"] = await _guideline_evidence(req.message)
    return body


@router.get("/conversations/{conversation_id}", summary="Read a conversation's turns in order")
async def get_conversation(conversation_id: int) -> dict[str, Any]:
    async with session_scope() as session:
        repo = MemoryRepository(session)
        messages = await repo.get_conversation_messages(conversation_id)
    return {"messages": [{"role": m.role, "content": m.content} for m in messages]}
