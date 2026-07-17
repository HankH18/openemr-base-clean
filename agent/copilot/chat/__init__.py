"""Grounded conversational chat feature — drill-down on one patient.

A clinician asks a free-text question about a patient; the agent answers
grounded in the record, every claim gated at serve time against a live FHIR
re-fetch.  Fail-closed: a question nothing in the record can ground is
withheld with an honest "I can't confirm that" rather than guessed.  Multi-turn
history is persisted so a thread survives across requests.

The serve-time orchestration lives in ``service`` (``ChatService``); the HTTP
surface is ``copilot.api.routes.chat``.
"""

from copilot.chat.service import ChatReply, ChatService

__all__ = [
    "ChatReply",
    "ChatService",
]
