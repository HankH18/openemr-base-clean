"""LLM token usage + USD cost capture.

Three layers, all offline:

- Unit tests for the pure pricing helper (``copilot.observability.pricing``):
  the rate table, the deterministic cost formula, the unknown-model fallback,
  and negative-count clamping.
- ``ClaudeAgent.answer`` threads Anthropic ``response.usage`` out of the
  tool-use loop, summing across iterations and counting tool calls.
- ``ChatService._record_token_usage`` records the counts + a computed
  ``cost_usd`` onto the chat span and emits a matching ``llm.usage`` event on
  the LLM path — and records nothing on the deterministic stub path.

The frozen acceptance spec (``.swarm-loop/acceptance/tokens``) already drives
the full ``/v1/chat`` HTTP path with a respx-faked OpenEMR; these grey-box
tests exercise the individual seams without a network or DB.
"""

from __future__ import annotations

from typing import Any

from copilot.agent.claude import ClaudeAgent
from copilot.chat.service import ChatService
from copilot.config import Settings
from copilot.domain.primitives import PatientId
from copilot.observability.pricing import cost_usd, rates_for

_ANSWER_JSON = (
    '{"answer": "Troponin I is 0.9 ng/mL.", '
    '"claims": [{"resource_type": "Observation", "resource_id": "obs-1001-trop"}]}'
)


# --- pricing helper --------------------------------------------------------


def test_cost_usd_sonnet() -> None:
    # (1200 * $3 + 340 * $15) / 1e6 = (3600 + 5100) / 1e6 = 0.0087
    assert cost_usd("claude-sonnet-5", 1200, 340) == 0.0087


def test_cost_usd_haiku() -> None:
    # (1200 * $1 + 340 * $5) / 1e6 = (1200 + 1700) / 1e6 = 0.0029
    assert cost_usd("claude-haiku-4-5-20251001", 1200, 340) == 0.0029


def test_cost_is_zero_for_zero_tokens() -> None:
    assert cost_usd("claude-sonnet-5", 0, 0) == 0.0


def test_unknown_model_uses_default_rates() -> None:
    # An unrecognised model is costed at the default (Sonnet-tier) rate, never free.
    assert rates_for("some-future-model") == rates_for("claude-sonnet-5")
    assert cost_usd("some-future-model", 1200, 340) > 0


def test_negative_counts_clamped_to_zero() -> None:
    assert cost_usd("claude-sonnet-5", -1000, -1000) == 0.0
    # Only the negative side is clamped; the positive side still costs.
    assert cost_usd("claude-sonnet-5", 1200, -50) == cost_usd("claude-sonnet-5", 1200, 0)


def test_output_tokens_cost_more_than_input() -> None:
    input_rate, output_rate = rates_for("claude-sonnet-5")
    assert output_rate > input_rate
    assert cost_usd("claude-sonnet-5", 0, 1000) > cost_usd("claude-sonnet-5", 1000, 0)


# --- fakes for the Anthropic + FHIR seams ----------------------------------


class _Usage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _TextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _ToolUseBlock:
    type = "tool_use"
    name = "get_labs"
    id = "tu-1"


class _Resp:
    def __init__(self, content: list[Any], stop_reason: str, usage: _Usage) -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage


class _SeqMessages:
    """Return a pre-scripted response per ``create`` call, in order."""

    def __init__(self, responses: list[_Resp]) -> None:
        self._responses = responses
        self._i = 0

    async def create(self, **_kw: Any) -> _Resp:
        resp = self._responses[self._i]
        self._i += 1
        return resp


class _FakeClient:
    def __init__(self, responses: list[_Resp]) -> None:
        self.messages = _SeqMessages(responses)


class _FakeFhir:
    async def search(self, _resource_type: Any, _params: Any) -> dict[str, Any]:
        return {"entry": []}


def _llm_settings() -> Settings:
    return Settings(anthropic_api_key="sk-ant-test")


# --- ClaudeAgent usage threading -------------------------------------------


async def test_agent_reports_single_turn_usage() -> None:
    resp = _Resp([_TextBlock(_ANSWER_JSON)], "end_turn", _Usage(1200, 340))
    agent = ClaudeAgent(_llm_settings(), _FakeFhir(), client=_FakeClient([resp]))

    answer = await agent.answer(PatientId(value=1001), "latest troponin?")

    assert answer.input_tokens == 1200
    assert answer.output_tokens == 340
    assert answer.tool_calls == 0


async def test_agent_sums_usage_across_tool_loop() -> None:
    responses = [
        _Resp([_ToolUseBlock()], "tool_use", _Usage(1000, 200)),
        _Resp([_TextBlock(_ANSWER_JSON)], "end_turn", _Usage(1200, 340)),
    ]
    agent = ClaudeAgent(_llm_settings(), _FakeFhir(), client=_FakeClient(responses))

    answer = await agent.answer(PatientId(value=1001), "latest troponin?")

    assert answer.input_tokens == 2200
    assert answer.output_tokens == 540
    assert answer.tool_calls == 1


# --- ChatService span/event recording --------------------------------------


class _SpySpan:
    def __init__(self) -> None:
        self.attrs: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attrs[key] = value

    def set_output(self, value: Any) -> None:
        return None


class _SpyObs:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def event(self, name: str, **attributes: Any) -> None:
        self.events.append((name, attributes))


def _service() -> tuple[ChatService, _SpyObs]:
    obs = _SpyObs()
    svc = ChatService(_llm_settings(), obs)
    return svc, obs


def test_service_records_usage_on_llm_path() -> None:
    from copilot.agent.base import AgentAnswer

    svc, obs = _service()
    span = _SpySpan()
    answer = AgentAnswer(answer="a", claims=[], input_tokens=1200, output_tokens=340, tool_calls=2)

    svc._record_token_usage(span, answer)

    assert span.attrs["input_tokens"] == 1200
    assert span.attrs["output_tokens"] == 340
    assert span.attrs["cost_usd"] == cost_usd("claude-sonnet-5", 1200, 340)
    assert span.attrs["cost_usd"] > 0
    assert span.attrs["tool_calls"] == 2

    usage_events = [attrs for name, attrs in obs.events if name == "llm.usage"]
    assert len(usage_events) == 1
    assert usage_events[0]["input_tokens"] == 1200
    assert usage_events[0]["cost_usd"] == cost_usd("claude-sonnet-5", 1200, 340)


def test_service_records_nothing_on_stub_path() -> None:
    from copilot.agent.base import AgentAnswer

    svc, obs = _service()
    span = _SpySpan()
    # Stub agent leaves usage unset — there is nothing to cost.
    answer = AgentAnswer(answer="a", claims=[])

    svc._record_token_usage(span, answer)

    assert span.attrs == {}
    assert obs.events == []
