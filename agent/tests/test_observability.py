"""Observability: noop vs Langfuse, correlation-id propagation, factory."""

from __future__ import annotations

import asyncio

import pytest

from copilot.config import Settings
from copilot.observability import (
    NoopObservability,
    build_observability,
    correlation_id_var,
    current_correlation_id,
    generate_correlation_id,
)
from copilot.observability.langfuse_backend import LangfuseObservability


class TestCorrelationId:
    def test_generate_returns_unique_ids(self) -> None:
        a, b = generate_correlation_id(), generate_correlation_id()
        assert a != b
        assert len(a) >= 12

    def test_default_is_empty_string(self) -> None:
        # New context has no correlation id.
        assert current_correlation_id() == ""

    def test_set_and_read_via_contextvar(self) -> None:
        token = correlation_id_var.set("abc-123")
        try:
            assert current_correlation_id() == "abc-123"
        finally:
            correlation_id_var.reset(token)


class TestNoopObservability:
    def test_noop_event_is_zero_cost(self) -> None:
        obs = NoopObservability()
        # These must not raise, must not throw, must return None.
        assert obs.event("some.thing", extra="x") is None
        assert obs.record_verification(passed=True, action="served", patient_id=1015) is None
        assert obs.record_poller_staleness(patient_id=1015, age_seconds=600) is None

    @pytest.mark.asyncio
    async def test_noop_span_yields_a_span_with_setters(self) -> None:
        obs = NoopObservability()
        async with obs.span("test") as span:
            span.set_attribute("k", "v")
            span.set_output({"ok": True})
        # Nothing to assert about output — it must not raise.

    @pytest.mark.asyncio
    async def test_noop_flush_is_noop(self) -> None:
        obs = NoopObservability()
        await obs.flush()


class TestFactory:
    def test_factory_returns_noop_when_creds_missing(self) -> None:
        s = Settings(langfuse_host="", langfuse_public_key="", langfuse_secret_key="")
        obs = build_observability(s)
        assert isinstance(obs, NoopObservability)

    def test_factory_returns_langfuse_when_all_creds_set(self) -> None:
        s = Settings(
            langfuse_host="https://cloud.langfuse.com",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        )
        obs = build_observability(s)
        assert isinstance(obs, LangfuseObservability)

    def test_partial_creds_still_noop(self) -> None:
        s = Settings(langfuse_host="https://cloud.langfuse.com", langfuse_public_key="pk-test")
        obs = build_observability(s)
        assert isinstance(obs, NoopObservability)


class TestLangfuseBackendWithFakeClient:
    """Exercise the LangfuseObservability wiring without importing the SDK."""

    def test_construct_refuses_missing_creds(self) -> None:
        with pytest.raises(RuntimeError):
            LangfuseObservability(host="", public_key="pk", secret_key="sk")

    @pytest.mark.asyncio
    async def test_span_calls_client_trace_and_end(self) -> None:
        calls = {"traces": [], "events": [], "ended": 0}

        class FakeTraceObj:
            def update(self, **kwargs):
                calls.setdefault("updates", []).append(kwargs)

            def end(self) -> None:
                calls["ended"] += 1

        class FakeClient:
            def trace(self, **kwargs):
                calls["traces"].append(kwargs)
                return FakeTraceObj()

            def event(self, **kwargs):
                calls["events"].append(kwargs)

            def flush(self) -> None:
                calls["flushed"] = True

        obs = LangfuseObservability(
            host="https://x", public_key="pk", secret_key="sk", client=FakeClient()
        )
        token = correlation_id_var.set("corr-1")
        try:
            async with obs.span("poller.tick", patient_id=1015) as span:
                span.set_attribute("outcome", "synthesized")
                span.set_output({"claims": 3})
        finally:
            correlation_id_var.reset(token)

        assert len(calls["traces"]) == 1
        assert calls["traces"][0]["name"] == "poller.tick"
        assert calls["traces"][0]["id"] == "corr-1"
        assert calls["ended"] == 1
        # attribute + output propagated through update calls
        assert any("metadata" in u for u in calls["updates"])

    def test_record_verification_emits_event_with_correlation(self) -> None:
        emitted: list[dict] = []

        class FakeClient:
            def event(self, **kwargs):
                emitted.append(kwargs)

        obs = LangfuseObservability(
            host="https://x", public_key="pk", secret_key="sk", client=FakeClient()
        )
        token = correlation_id_var.set("corr-veri")
        try:
            obs.record_verification(passed=False, action="withheld", patient_id=1015)
            obs.record_poller_staleness(patient_id=1015, age_seconds=1800)
        finally:
            correlation_id_var.reset(token)
        assert len(emitted) == 2
        assert emitted[0]["name"] == "verification.result"
        assert emitted[0]["metadata"]["passed"] is False
        assert emitted[0]["metadata"]["correlation_id"] == "corr-veri"
        assert emitted[1]["name"] == "poller.staleness"
        assert emitted[1]["metadata"]["age_seconds"] == 1800

    @pytest.mark.asyncio
    async def test_span_swallows_client_exception_yields_noop_span(self) -> None:
        """Telemetry MUST NOT break the caller."""

        class BadClient:
            def trace(self, **_):
                raise RuntimeError("langfuse down")

            def event(self, **_):
                pass

            def flush(self) -> None:
                pass

        obs = LangfuseObservability(
            host="https://x", public_key="pk", secret_key="sk", client=BadClient()
        )
        # Must NOT raise — even if the SDK does.
        async with obs.span("poller.tick") as span:
            span.set_attribute("k", "v")


class TestCorrelationIdInAsyncTask:
    """Confirm ContextVar propagates into asyncio.create_task."""

    @pytest.mark.asyncio
    async def test_context_var_propagates_to_child_task(self) -> None:
        seen: list[str] = []
        token = correlation_id_var.set("parent-corr")

        async def child() -> None:
            seen.append(current_correlation_id())

        try:
            await asyncio.create_task(child())
        finally:
            correlation_id_var.reset(token)
        assert seen == ["parent-corr"]
