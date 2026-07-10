"""feat_tokens — LLM token usage + cost are captured to observability.

FROZEN GOALS. The doc requires answering "how many tokens, at what cost" from the
telemetry. This exercises the real chat path with a fake Anthropic client that
reports a known usage, injects a spy Observability, and asserts the chat records
`input_tokens`/`output_tokens` and a computed `cost_usd` onto it (via a span
attribute or an event). Baseline: nothing reads `response.usage`, so nothing is
recorded — these fail until token/cost capture is wired.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi.testclient import TestClient

CLIN = 8803
_ANSWER_JSON = (
    '{"answer": "Troponin I is 0.9 ng/mL.", '
    '"claims": [{"resource_type": "Observation", "resource_id": "obs-1001-trop"}]}'
)


class _Usage:
    input_tokens = 1200
    output_tokens = 340


class _Block:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Resp:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]
        self.stop_reason = "end_turn"
        self.usage = _Usage()


class _Messages:
    async def create(self, **_kw):
        return _Resp(_ANSWER_JSON)


class _FakeAnthropic:
    def __init__(self, *_a, **_k) -> None:
        self.messages = _Messages()


class _SpySpan:
    def __init__(self, attrs: dict) -> None:
        self._attrs = attrs

    def set_attribute(self, key: str, value) -> None:
        self._attrs[key] = value

    def set_output(self, value) -> None:
        return None


class _SpyObs:
    def __init__(self) -> None:
        self.attrs: dict = {}
        self.events: list[tuple[str, dict]] = []

    @asynccontextmanager
    async def span(self, name: str, **_attributes):
        yield _SpySpan(self.attrs)

    def event(self, name: str, **attributes) -> None:
        self.events.append((name, attributes))

    def record_verification(self, **_k) -> None:
        return None

    def record_poller_staleness(self, **_k) -> None:
        return None

    async def flush(self) -> None:
        return None

    def merged(self) -> dict:
        m = dict(self.attrs)
        for _name, attrs in self.events:
            m.update(attrs)
        return m


def _client_with_llm(monkeypatch) -> tuple[TestClient, _SpyObs]:
    monkeypatch.setenv("COPILOT_ANTHROPIC_API_KEY", "sk-ant-test")
    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _FakeAnthropic)
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    from copilot.api.app import create_app

    app = create_app(get_settings(), probe_factories=[])
    spy = _SpyObs()
    app.state.observability = spy
    return TestClient(app), spy


def _chat(client: TestClient):
    client.post("/v1/rounds/start", json={"clinician_id": CLIN, "patient_ids": [1001]})
    return client.post(
        "/v1/chat",
        json={"clinician_id": CLIN, "patient_id": 1001, "message": "latest troponin?"},
    )


def test_chat_records_token_usage(monkeypatch):
    client, spy = _client_with_llm(monkeypatch)
    assert _chat(client).status_code == 200
    m = spy.merged()
    assert m.get("input_tokens") == 1200 and m.get("output_tokens") == 340, (
        f"chat must record LLM token usage to observability; captured {m}"
    )


def test_chat_records_cost(monkeypatch):
    client, spy = _client_with_llm(monkeypatch)
    assert _chat(client).status_code == 200
    cost = spy.merged().get("cost_usd")
    assert isinstance(cost, (int, float)) and cost > 0, (
        f"chat must record a computed USD cost derived from tokens; captured {spy.merged()}"
    )
