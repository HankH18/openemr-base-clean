"""Choose the right ``ChatAgent`` given the current settings.

Mirrors ``build_observability``: with no Anthropic key, returns the
deterministic ``StubAgent`` so the whole chat path runs green in tests;
with a key set, returns the real ``ClaudeAgent``.  Callers never branch on
"do we have a key?" — they just take a ``ChatAgent``.
"""

from __future__ import annotations

from copilot.agent.base import ChatAgent
from copilot.config import Settings
from copilot.fhir.client import FhirClient


def build_agent(settings: Settings, fhir_client: FhirClient) -> ChatAgent:
    if not settings.anthropic_api_key:
        from copilot.agent.stub import StubAgent

        return StubAgent(fhir_client)

    from copilot.agent.claude import ClaudeAgent

    return ClaudeAgent(settings, fhir_client)
