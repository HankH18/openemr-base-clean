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

from collections.abc import Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from copilot.domain.contracts import Claim
from copilot.domain.documents import ExtractedFact
from copilot.domain.primitives import PatientId
from copilot.rag.retriever import GuidelineEvidence

# Caps on how much worker context an agent renders into one turn. Both worker
# outputs are ordered deterministically by their producers, so a cap truncates
# reproducibly rather than sampling.
_MAX_CONTEXT_FACTS = 8
_MAX_CONTEXT_EVIDENCE = 4
_SNIPPET_CHARS = 240


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


def _snippet(text: str) -> str:
    """Whitespace-collapsed ``text``, truncated to a fixed, deterministic width."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= _SNIPPET_CHARS:
        return collapsed
    return collapsed[:_SNIPPET_CHARS].rstrip() + "…"


def render_document_facts(facts: Sequence[ExtractedFact]) -> list[str]:
    """One readable line per fact the intake-extractor pulled from a document.

    These are *document* facts, not FHIR resources: they inform the prose but
    can never become a :class:`Claim`, whose ``source_ref`` the deterministic
    verifier grounds against a live FHIR re-fetch.
    """
    lines: list[str] = []
    for fact in facts[:_MAX_CONTEXT_FACTS]:
        unit = f" {fact.unit}" if fact.unit else ""
        lines.append(f"{fact.field_path} = {fact.value}{unit}")
    return lines


def render_guideline_evidence(evidence: Sequence[GuidelineEvidence]) -> list[str]:
    """One readable line per guideline chunk the evidence-retriever returned.

    Each line names the corpus document + section it came from, so guideline
    recommendations stay visibly distinct from this patient's record.
    """
    return [
        f"[{item.document_id} §{item.section}] {_snippet(item.content)}"
        for item in evidence[:_MAX_CONTEXT_EVIDENCE]
    ]


class ChatAgent(Protocol):
    """The interface chat endpoints depend on."""

    async def answer(
        self,
        patient_id: PatientId,
        message: str,
        history: list[ConversationTurn] | None = None,
        *,
        guideline_evidence: list[GuidelineEvidence] | None = None,
        document_facts: list[ExtractedFact] | None = None,
    ) -> AgentAnswer:
        """Answer ``message`` about ``patient_id``, grounded in the record.

        ``guideline_evidence`` and ``document_facts`` are the multi-agent
        graph's worker output — the chunks the evidence-retriever retrieved and
        the facts the intake-extractor read off the documents in scope. Both are
        keyword-only and default to ``None``, so the inline (flag-OFF) chat path,
        which passes neither, is unchanged. Neither may become a ``Claim``:
        claims stay FHIR-grounded so the deterministic verifier's re-fetch gate
        is untouched — worker output informs the prose only.
        """
        ...
