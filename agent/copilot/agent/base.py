"""Chat-agent contracts.

The chat path (built next cycle) depends on this exact interface.  A
``ChatAgent`` turns a free-text question about one patient into an
``AgentAnswer`` — prose plus the grounded ``Claim`` list that backs it.
Every claim carries a ``source_ref`` so the verification layer can gate
it against a live FHIR re-fetch, exactly as it does for memory-file
summaries.

Two implementations live behind this Protocol (mirroring
``build_observability`` / ``LlmSynthesizer``):

- ``StubAgent`` — deterministic, no API key, honest by construction.
- ``ClaudeAgent`` — real Anthropic tool-use loop.

``build_agent`` in ``factory.py`` picks one based on the settings.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from copilot.domain.contracts import Claim
from copilot.domain.primitives import PatientId


class ConversationTurn(BaseModel):
    """One prior message in a chat thread — replayed as context."""

    model_config = ConfigDict(frozen=True)

    role: Literal["user", "assistant"]
    content: str


class AgentAnswer(BaseModel):
    """What a ``ChatAgent`` returns for a single question.

    ``answer`` is the prose shown to the clinician; ``claims`` is the
    grounded evidence behind it.  An empty ``claims`` list with an honest
    ``answer`` is the correct response when nothing in the record supports
    the question — never fabricate.

    ``input_tokens``/``output_tokens`` carry the LLM usage the agent spent
    producing this answer, and ``tool_calls`` how many tool invocations it
    made.  They are optional so a deterministic, keyless agent (``StubAgent``)
    can leave them unset: ``None`` counts mean "no LLM ran", which the chat
    service reads as "nothing to cost".
    """

    model_config = ConfigDict(frozen=True)

    answer: str
    claims: list[Claim]
    input_tokens: int | None = None
    output_tokens: int | None = None
    tool_calls: int = 0


class ChatAgent(Protocol):
    """The interface chat endpoints depend on."""

    async def answer(
        self,
        patient_id: PatientId,
        message: str,
        history: list[ConversationTurn] | None = None,
    ) -> AgentAnswer:
        """Answer ``message`` about ``patient_id``, grounded in the record."""
        ...
