"""Multi-agent graph (F7): routing, typed handoffs, critic gate, span nesting.

DB-free unit coverage of the pure pieces + the flat→nested observability change.
The full ``graph.run`` end-to-end (DB + FHIR + verifier) is exercised by the
frozen acceptance suite; here we lock the deterministic logic and the
parent/child span export directly.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from copilot.config import Settings
from copilot.domain.contracts import Claim
from copilot.domain.primitives import FhirReference, ResourceType
from copilot.graph import (
    AgentTask,
    CriticVerdict,
    Handoff,
    build_critic,
    build_evidence_retriever,
    build_intake_extractor,
)
from copilot.graph.critic import RealCritic, StubCritic
from copilot.graph.evidence_retriever import GuidelineEvidenceRetriever
from copilot.graph.intake_extractor import DocumentIntakeExtractor
from copilot.graph.supervisor import StubSupervisor, build_supervisor
from copilot.observability.base import correlation_id_var
from copilot.observability.langfuse_backend import LangfuseObservability


def _keyless() -> Settings:
    return Settings(anthropic_api_key="", voyage_api_key="", cohere_api_key="")


# --- routing ----------------------------------------------------------------


class TestSupervisorRouting:
    def test_document_only_routes_to_intake(self) -> None:
        plan = StubSupervisor().route(
            AgentTask(patient_id=1, question="What changed in the attached lab report?", document_ids=["5"])
        )
        assert plan == ["intake"]

    def test_guideline_only_routes_to_evidence(self) -> None:
        plan = StubSupervisor().route(
            AgentTask(patient_id=1, question="What do guidelines recommend for DKA insulin?")
        )
        assert plan == ["evidence"]

    def test_both_signals_route_to_both_in_order(self) -> None:
        plan = StubSupervisor().route(
            AgentTask(
                patient_id=1,
                question="Summarize the attached lab and cite guideline recommendations.",
                document_ids=["5"],
            )
        )
        assert plan == ["intake", "evidence"]

    def test_plain_chart_question_routes_to_neither(self) -> None:
        plan = StubSupervisor().route(
            AgentTask(patient_id=1, question="What is this patient's current potassium?")
        )
        assert plan == []

    def test_build_supervisor_is_stub(self) -> None:
        assert isinstance(build_supervisor(_keyless()), StubSupervisor)


# --- typed handoffs ---------------------------------------------------------


class TestHandoffContract:
    def test_requires_four_fields(self) -> None:
        fields = set(Handoff.model_fields)
        assert {"from_agent", "to_agent", "reason", "payload"} <= fields

    def test_validates_and_defaults_payload(self) -> None:
        h = Handoff(from_agent="supervisor", to_agent="intake-extractor", reason="doc in scope")
        assert h.payload == {}

    def test_rejects_empty_endpoints(self) -> None:
        with pytest.raises(ValidationError):
            Handoff(from_agent="", to_agent="x", reason="r")


# --- critic gate ------------------------------------------------------------

_CITED = {
    "text": "CITED: start insulin per DKA guideline",
    "citation": {"source_type": "guideline", "source_id": "gd-1"},
}
_UNCITED = {"text": "UNCITED: empiric anticoagulation", "citation": None}


class TestCritic:
    def test_rejects_uncited_accepts_cited(self) -> None:
        verdict = StubCritic().review([_CITED, _UNCITED])
        assert isinstance(verdict, CriticVerdict)
        assert any("CITED" in t for t in verdict.accepted)
        assert any("UNCITED" in t for t in verdict.rejected)
        assert not any("UNCITED" in t for t in verdict.accepted)

    def test_is_deterministic(self) -> None:
        critic = build_critic(_keyless())
        assert critic.review([_CITED, _UNCITED]) == critic.review([_CITED, _UNCITED])

    def test_accepts_domain_claim_with_source_ref(self) -> None:
        claim = Claim(
            text="troponin 0.9",
            source_ref=FhirReference(
                resource_type=ResourceType.Observation,
                resource_id="obs-1",
                field="valueQuantity.value",
                value="0.9",
            ),
        )
        verdict = StubCritic().review([claim])
        assert verdict.accepted == ["troponin 0.9"]
        assert verdict.rejected == []

    def test_build_critic_keyed_is_real(self) -> None:
        assert isinstance(build_critic(Settings(anthropic_api_key="sk-live")), RealCritic)

    def test_build_critic_keyless_is_stub(self) -> None:
        assert isinstance(build_critic(_keyless()), StubCritic)


# --- worker factories -------------------------------------------------------


class TestWorkerFactories:
    def test_intake_builds_document_extractor(self) -> None:
        assert isinstance(build_intake_extractor(_keyless()), DocumentIntakeExtractor)

    def test_evidence_builds_guideline_retriever(self) -> None:
        assert isinstance(build_evidence_retriever(_keyless()), GuidelineEvidenceRetriever)


# --- flat -> nested span export ---------------------------------------------


class _FakeObs:
    """One recorded Langfuse observation with its parentage."""

    def __init__(self, client: _FakeClient, *, name: str | None, trace_id: str | None, parent: str | None):
        self.client = client
        self.name = name
        self.id = client.next_id()
        self.trace_id = trace_id
        self.parent_observation_id = parent
        self.kind = "span"
        client.observations.append(self)

    def span(self, **kwargs: Any) -> _FakeObs:
        return _FakeObs(self.client, name=kwargs.get("name"), trace_id=self.trace_id, parent=self.id)

    def event(self, **kwargs: Any) -> _FakeObs:
        obs = self.span(**kwargs)
        obs.kind = "event"
        return obs

    def update(self, **kwargs: Any) -> _FakeObs:
        return self

    def end(self, **kwargs: Any) -> _FakeObs:
        return self


class _FakeTrace:
    def __init__(self, client: _FakeClient, *, trace_id: str, name: str | None):
        self.client = client
        self.id = trace_id
        self.name = name

    def span(self, **kwargs: Any) -> _FakeObs:
        # Trace-level span: no parent observation.
        return _FakeObs(self.client, name=kwargs.get("name"), trace_id=self.id, parent=kwargs.get("parent_observation_id"))

    def event(self, **kwargs: Any) -> _FakeObs:
        obs = self.span(**kwargs)
        obs.kind = "event"
        return obs

    def update(self, **kwargs: Any) -> _FakeTrace:
        return self

    def end(self, **kwargs: Any) -> _FakeTrace:
        return self


class _FakeClient:
    def __init__(self) -> None:
        self.observations: list[_FakeObs] = []
        self.traces: list[_FakeTrace] = []
        self._n = 0

    def next_id(self) -> str:
        self._n += 1
        return f"obs-{self._n}"

    def trace(self, **kwargs: Any) -> _FakeTrace:
        trace = _FakeTrace(self, trace_id=kwargs.get("id") or f"trace-{len(self.traces)}", name=kwargs.get("name"))
        self.traces.append(trace)
        return trace

    def event(self, **kwargs: Any) -> _FakeObs:
        obs = _FakeObs(self, name=kwargs.get("name"), trace_id=kwargs.get("trace_id"), parent=None)
        obs.kind = "event"
        return obs


CID = "unit-graph-nesting-cid-0001"


@pytest.mark.asyncio
async def test_worker_spans_nest_under_supervisor_and_share_trace_id() -> None:
    fake = _FakeClient()
    obs = LangfuseObservability(host="http://x", public_key="pk", secret_key="sk", client=fake)

    token = correlation_id_var.set(CID)
    try:
        async with obs.span("graph.run"), obs.span("supervisor.route") as sup:
            sup.set_attribute("k", "v")
            async with obs.span("intake-extractor.run"):
                pass
            async with obs.span("evidence-retriever.retrieve"):
                pass
            obs.event("worker.handoff", from_agent="supervisor", to_agent="intake-extractor")
    finally:
        correlation_id_var.reset(token)

    supervisor = next(o for o in fake.observations if o.name == "supervisor.route")
    workers = [o for o in fake.observations if o.name in ("intake-extractor.run", "evidence-retriever.retrieve")]

    assert len(workers) == 2
    for worker in workers:
        assert worker.parent_observation_id == supervisor.id, worker.name

    # supervisor is a trace-level span (its parent is None), and every exported
    # observation carries the correlation id as its trace id.
    assert supervisor.parent_observation_id is None
    assert {o.trace_id for o in fake.observations} == {CID}
    # the one-off event landed inside the trace too (not an orphan).
    handoff = next(o for o in fake.observations if o.kind == "event")
    assert handoff.trace_id == CID
    assert handoff.parent_observation_id == supervisor.id
