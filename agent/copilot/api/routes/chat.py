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
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field

from copilot.api.deps import resolve_acting_context
from copilot.api.middleware import resolve_correlation_id
from copilot.auth import is_authorized
from copilot.chat.service import ChatReply, ChatService, ConversationAccessError
from copilot.config import get_settings
from copilot.domain.primitives import PatientId
from copilot.fhir.client import FhirClient
from copilot.fhir.provider import build_fhir_client_for_session
from copilot.graph.contracts import Handoff
from copilot.memory.db import session_scope
from copilot.memory.repository import MemoryRepository
from copilot.observability import Observability
from copilot.rag import build_retriever

router = APIRouter(prefix="/v1", tags=["chat"])

_logger = logging.getLogger(__name__)

# One detail string for BOTH "no such conversation" and "not yours" — the two are
# deliberately indistinguishable to an unauthorized caller (see get_conversation).
_CONVERSATION_NOT_FOUND_DETAIL = "Conversation not found"


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
    document_ids: list[str] = Field(default_factory=list)


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
        "handoffs": [_handoff_view(h) for h in reply.handoffs],
    }


def _handoff_view(handoff: Handoff) -> dict[str, Any]:
    """One graph handoff, projected to a PHI-free view safe to serve.

    Makes the multi-agent routing observable to a caller (which agents ran, in
    what order, and why) instead of a claim only a doc makes. Deliberately a
    PROJECTION, not a ``model_dump``: the typed ``Handoff.payload`` carries
    routing context that may be patient-derived — document ids and routing
    signals (it also carried the raw question until that leak was closed) —
    and none of it belongs in a response block whose whole purpose is to explain
    routing. Only the agent names, the static reason string, and the router's
    matched vocabulary terms (drawn from a fixed word list in
    ``copilot.graph.supervisor``, never from the record) cross this boundary.
    """
    signals = handoff.payload.get("signals")
    return {
        "from_agent": handoff.from_agent,
        "to_agent": handoff.to_agent,
        "reason": handoff.reason,
        "signals": [str(s) for s in signals] if isinstance(signals, list) else [],
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

    observability = request.app.state.observability
    try:
        reply = await _service(observability, _reader_factory(acting.session_id)).chat(
            clinician_id=clinician_id,
            patient_id=patient_id,
            message=req.message,
            correlation_id=correlation_id,
            conversation_id=req.conversation_id,
            document_ids=req.document_ids,
        )
    except ConversationAccessError:
        # A supplied conversation_id that is not this patient's thread — foreign OR
        # nonexistent — is the SAME 404 with the SAME detail as the sibling read
        # route (GET /v1/conversations/{id}, which unified both on 404 above). A
        # 403-vs-404 split would let a caller enumerate which conversation ids
        # exist; existence is withheld from anyone not already entitled to the
        # contents. Raised here in the route (not the service) so the HTTP shape is
        # owned in the same layer the GET route raises its refusal.
        raise HTTPException(
            status_code=404, detail=_CONVERSATION_NOT_FOUND_DETAIL
        ) from None
    body = _reply_body(reply)
    # Evidence separation: guideline backing rides as a distinct top-level block,
    # never inside a patient-fact claim's citation.
    #
    # ONE retrieval per turn. In graph mode the evidence-retriever worker has
    # already retrieved under the supervisor's routing decision, so we display
    # exactly that (`reply.guideline_evidence` is not None) — retrieving again
    # here would both double the cost and decouple what the clinician sees from
    # what the supervisor actually decided. The inline path has no worker, so it
    # is the one mode that retrieves here, unchanged.
    if reply.guideline_evidence is None:
        evidence = await _guideline_evidence(req.message)
        # Req 7: the inline path's retrieval happens here, so its hit count is
        # only knowable here. Correlated to the rest of the turn's telemetry by
        # correlation id. (Graph mode records its own hits inside graph.run.)
        observability.event(
            "chat.retrieval",
            retrieval_hits=len(evidence),
            correlation_id=reply.correlation_id,
        )
    else:
        evidence = [e.model_dump(mode="json") for e in reply.guideline_evidence]
    body["guideline_evidence"] = evidence
    # Discriminates the two states an empty `guideline_evidence` block conflates:
    # True + [] is a routed-but-zero-hit turn (the evidence-retriever ran and the
    # corpus returned nothing), False + [] is a never-routed turn (no guideline
    # need) or the inline path. Without this a lost/degraded corpus reads as "no
    # guidelines apply". Graph-only signal; the inline path's own retrieval hits
    # ride the `chat.retrieval` event above.
    body["evidence_retrieved"] = reply.evidence_retrieved
    return body


@router.get("/conversations/{conversation_id}", summary="Read a conversation's turns in order")
async def get_conversation(
    conversation_id: Annotated[int, Path(gt=0)],
    request: Request,
    clinician_id: Annotated[int | None, Query(gt=0)] = None,
) -> dict[str, Any]:
    # Identity FIRST, before any read: a conversation is free-text clinical Q&A
    # about a named patient, at a guessable autoincrement id. This handler used to
    # take no Request at all, so no auth could run and the session cookie was never
    # touched — meaning auth_mode=smart could not mask it either. Same auth-mode
    # contract as the chat/document/observations routes (smart → session cookie,
    # 401 if none; disabled → an asserted clinician_id, 400 if absent).
    acting = await resolve_acting_context(get_settings(), request, clinician_id)
    cid = acting.clinician_id

    async with session_scope() as session:
        repo = MemoryRepository(session)
        conversation = await repo.get_conversation(conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail=_CONVERSATION_NOT_FOUND_DETAIL)
        messages = await repo.get_conversation_messages(conversation_id)

    # Authorization boundary (UC-6), identical to the document reads: the
    # conversation's patient must be on the acting clinician's rounding list.
    #
    # 404 — NOT 403 — and deliberately the *same* 404 as the unknown-id branch
    # above. An unauthorized caller must not be able to tell "this thread exists
    # but is not yours" from "no such thread": distinct codes would turn the
    # autoincrement id space into a clean enumeration oracle (walk 1..N, count the
    # 403s, learn exactly how many conversations exist and which ids are live).
    # Existence is itself PHI-adjacent here, so it is withheld from anyone not
    # already entitled to the contents. The owner still gets a true 200.
    if not await is_authorized(cid, PatientId(value=conversation.patient_id)):
        raise HTTPException(status_code=404, detail=_CONVERSATION_NOT_FOUND_DETAIL)

    return {"messages": [{"role": m.role, "content": m.content} for m in messages]}
