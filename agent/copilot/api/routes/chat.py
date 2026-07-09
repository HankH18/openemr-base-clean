"""Grounded conversational chat API — drill-down on one patient.

``POST /v1/chat`` answers a free-text question about a patient; every claim is
gated against a live FHIR re-fetch, and an ungroundable question is withheld
(fail-closed) rather than guessed.  ``GET /v1/conversations/{id}`` reads the
persisted multi-turn history back in order.

Mounted automatically by ``copilot.api.app.register_routers`` (it exposes a
module-level ``router``); no edit to ``app.py`` is required.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from copilot.api.middleware import resolve_correlation_id
from copilot.chat.service import ChatReply, ChatService
from copilot.config import get_settings
from copilot.domain.primitives import ClinicianId, PatientId
from copilot.memory.db import session_scope
from copilot.memory.repository import MemoryRepository

router = APIRouter(prefix="/v1", tags=["chat"])


class ChatRequest(BaseModel):
    """One grounded question about a patient, optionally continuing a thread."""

    clinician_id: int = Field(gt=0)
    patient_id: int = Field(gt=0)
    message: str = Field(min_length=1)
    conversation_id: int | None = Field(default=None, gt=0)
    correlation_id: str | None = None


def _service() -> ChatService:
    return ChatService(get_settings())


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


@router.post("/chat", summary="Answer a grounded question about a patient")
async def chat(req: ChatRequest) -> dict[str, Any]:
    # Resolve at the boundary: a valid supplied id is honoured, anything else
    # (missing / malformed) yields a freshly generated one.
    correlation_id = resolve_correlation_id(req.correlation_id)
    reply = await _service().chat(
        clinician_id=ClinicianId(value=req.clinician_id),
        patient_id=PatientId(value=req.patient_id),
        message=req.message,
        correlation_id=correlation_id,
        conversation_id=req.conversation_id,
    )
    return _reply_body(reply)


@router.get("/conversations/{conversation_id}", summary="Read a conversation's turns in order")
async def get_conversation(conversation_id: int) -> dict[str, Any]:
    async with session_scope() as session:
        repo = MemoryRepository(session)
        messages = await repo.get_conversation_messages(conversation_id)
    return {"messages": [{"role": m.role, "content": m.content} for m in messages]}
