"""Tests for the runtime-selected chat agent.

Covers the factory's key-gated selection and the ``StubAgent``'s
question-scoped grounding / honesty / determinism.  A fake in-memory FHIR
client returns canned bundles — no network.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from copilot.agent import AgentAnswer, ClaudeAgent, StubAgent, build_agent
from copilot.agent.base import ConversationTurn
from copilot.config import Settings
from copilot.domain.primitives import PatientId, ResourceType

pytestmark = pytest.mark.asyncio


# --- canned FHIR data ------------------------------------------------------


def _bundle(*resources: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceType": "Bundle",
        "entry": [{"resource": r} for r in resources],
    }


_TROPONIN = {
    "resourceType": "Observation",
    "id": "obs-1",
    "code": {"text": "Troponin I"},
    "valueQuantity": {"value": 2.34, "unit": "ng/mL"},
}
_LISINOPRIL = {
    "resourceType": "MedicationRequest",
    "id": "med-1",
    "medicationCodeableConcept": {"text": "Lisinopril 10 mg"},
}
_DIABETES = {
    "resourceType": "Condition",
    "id": "cond-1",
    "code": {"text": "Type 2 diabetes mellitus"},
}


class FakeFhirClient:
    """In-memory FHIR client — returns canned bundles keyed by resource type."""

    def __init__(self, bundles: Mapping[ResourceType, dict[str, Any]]) -> None:
        self._bundles = dict(bundles)
        self.calls: list[tuple[ResourceType, dict[str, str]]] = []

    async def search(
        self, resource_type: ResourceType, params: Mapping[str, str]
    ) -> dict[str, Any]:
        self.calls.append((resource_type, dict(params)))
        return self._bundles.get(resource_type, {"resourceType": "Bundle", "entry": []})


def _full_client() -> FakeFhirClient:
    return FakeFhirClient(
        {
            ResourceType.Observation: _bundle(_TROPONIN),
            ResourceType.MedicationRequest: _bundle(_LISINOPRIL),
            ResourceType.Condition: _bundle(_DIABETES),
        }
    )


_PID = PatientId(value=1015)


# --- factory ---------------------------------------------------------------


class TestBuildAgent:
    async def test_selects_stub_without_key(self) -> None:
        settings = Settings(anthropic_api_key="")
        agent = build_agent(settings, _full_client())  # type: ignore[arg-type]
        assert isinstance(agent, StubAgent)

    async def test_selects_claude_with_key(self) -> None:
        settings = Settings(anthropic_api_key="sk-testing")
        agent = build_agent(settings, _full_client())  # type: ignore[arg-type]
        assert isinstance(agent, ClaudeAgent)


# --- StubAgent -------------------------------------------------------------


class TestStubAgent:
    async def test_emits_grounded_claim_for_matching_question(self) -> None:
        agent = StubAgent(_full_client())  # type: ignore[arg-type]
        result = await agent.answer(_PID, "What is the troponin trend?")

        assert isinstance(result, AgentAnswer)
        assert len(result.claims) == 1
        claim = result.claims[0]
        assert claim.source_ref.resource_type == ResourceType.Observation
        assert claim.source_ref.resource_id == "obs-1"
        assert claim.source_ref.field == "valueQuantity.value"
        assert claim.source_ref.value == "2.34"
        # The verbatim value is carried into the human-facing text too.
        assert "2.34" in claim.text
        assert result.answer != ""

    async def test_summary_request_includes_all_resources(self) -> None:
        agent = StubAgent(_full_client())  # type: ignore[arg-type]
        result = await agent.answer(_PID, "Give me a summary of this patient")

        types = {c.source_ref.resource_type for c in result.claims}
        assert types == {
            ResourceType.Observation,
            ResourceType.MedicationRequest,
            ResourceType.Condition,
        }

    async def test_medication_match_uses_verbatim_value(self) -> None:
        agent = StubAgent(_full_client())  # type: ignore[arg-type]
        result = await agent.answer(_PID, "Are they on lisinopril?")

        assert len(result.claims) == 1
        claim = result.claims[0]
        assert claim.source_ref.resource_type == ResourceType.MedicationRequest
        assert claim.source_ref.field == "medicationCodeableConcept.text"
        assert claim.source_ref.value == "Lisinopril 10 mg"

    async def test_no_match_returns_honest_empty_answer(self) -> None:
        agent = StubAgent(_full_client())  # type: ignore[arg-type]
        result = await agent.answer(_PID, "Did they have an MRI?")

        assert result.claims == []
        assert "can't confirm" in result.answer.lower()

    async def test_skips_resource_type_that_errors(self) -> None:
        class ExplodingClient(FakeFhirClient):
            async def search(
                self, resource_type: ResourceType, params: Mapping[str, str]
            ) -> dict[str, Any]:
                if resource_type == ResourceType.Observation:
                    raise RuntimeError("boom")
                return await super().search(resource_type, params)

        client = ExplodingClient({ResourceType.MedicationRequest: _bundle(_LISINOPRIL)})
        agent = StubAgent(client)  # type: ignore[arg-type]
        # Observation search raises but the answer still grounds on meds.
        result = await agent.answer(_PID, "Summarize everything")
        assert [c.source_ref.resource_type for c in result.claims] == [
            ResourceType.MedicationRequest
        ]

    async def test_deterministic_same_input_same_output(self) -> None:
        agent = StubAgent(_full_client())  # type: ignore[arg-type]
        first = await agent.answer(_PID, "Give me the full overview")
        second = await agent.answer(_PID, "Give me the full overview")
        assert first == second

    async def test_history_argument_is_accepted(self) -> None:
        agent = StubAgent(_full_client())  # type: ignore[arg-type]
        history = [ConversationTurn(role="user", content="hello")]
        result = await agent.answer(_PID, "troponin?", history=history)
        assert len(result.claims) == 1


# --- ClaudeAgent (fake Anthropic client — no network, no key) --------------


class _Block:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _Resp:
    def __init__(self, content: list[Any], stop_reason: str) -> None:
        self.content = content
        self.stop_reason = stop_reason


class _FakeAnthropic:
    def __init__(self, responses: list[_Resp]) -> None:
        self._responses = responses
        self._i = 0
        self.messages = self

    async def create(self, **_kw: Any) -> _Resp:
        resp = self._responses[self._i]
        self._i += 1
        return resp


class TestClaudeAgent:
    async def test_builds_grounded_claims_from_fetched_resource(self) -> None:
        # Round 1: Claude calls get_labs. Round 2: returns JSON citing obs-1
        # (real) + a ghost id that was never fetched (must be dropped).
        tool_resp = _Resp([_Block(type="tool_use", id="t1", name="get_labs", input={})], "tool_use")
        final = json.dumps(
            {
                "answer": "Troponin is critically elevated at 2.34 ng/mL.",
                "claims": [
                    {"resource_type": "Observation", "resource_id": "obs-1"},
                    {"resource_type": "Observation", "resource_id": "ghost"},
                ],
            }
        )
        final_resp = _Resp([_Block(type="text", text=final)], "end_turn")
        agent = ClaudeAgent(
            Settings(anthropic_api_key="sk-testing"),
            _full_client(),  # type: ignore[arg-type]
            client=_FakeAnthropic([tool_resp, final_resp]),
        )

        result = await agent.answer(_PID, "What is the troponin?")

        assert result.answer == "Troponin is critically elevated at 2.34 ng/mL."
        # Ghost dropped; obs-1 grounded from the FETCHED resource, not the model.
        assert len(result.claims) == 1
        claim = result.claims[0]
        assert claim.source_ref.resource_id == "obs-1"
        assert claim.source_ref.field == "valueQuantity.value"
        assert claim.source_ref.value == "2.34"

    async def test_unsupported_question_yields_no_claims(self) -> None:
        final = json.dumps({"answer": "I can't confirm that from the record.", "claims": []})
        agent = ClaudeAgent(
            Settings(anthropic_api_key="sk-testing"),
            _full_client(),  # type: ignore[arg-type]
            client=_FakeAnthropic([_Resp([_Block(type="text", text=final)], "end_turn")]),
        )
        result = await agent.answer(_PID, "What did the MRI show?")
        assert result.claims == []
