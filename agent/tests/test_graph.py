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

from copilot.agent.stub import StubAgent
from copilot.config import Settings
from copilot.domain.contracts import Claim, VerificationAction
from copilot.domain.documents import ExtractedFact
from copilot.domain.primitives import FhirReference, GuidelineCitation, PatientId, ResourceType
from copilot.graph import (
    AgentTask,
    CriticVerdict,
    Handoff,
    build_critic,
    build_evidence_retriever,
    build_intake_extractor,
)
from copilot.graph.critic import RealCritic, StubCritic
from copilot.graph.evidence_retriever import EvidenceReport, GuidelineEvidenceRetriever
from copilot.graph.intake_extractor import DocumentIntakeExtractor, IntakeReport
from copilot.graph.supervisor import AgentGraph, StubSupervisor, build_supervisor, evidence_signals
from copilot.observability import NoopObservability
from copilot.observability.base import correlation_id_var
from copilot.observability.langfuse_backend import LangfuseObservability
from copilot.rag import GuidelineEvidence

# Reuse the in-memory FHIR double + synthetic cohort from the chat-route tests.
from tests.test_chat_routes import _COHORT, SICK, _FakeFhir


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


# --- routing breadth --------------------------------------------------------
#
# The router used to be two substrings ("guideline", "recommend"), which missed
# obvious evidence needs. These lock the broader deterministic rule set without
# letting it swallow the chart-only path.


class TestRoutingBreadth:
    @pytest.mark.parametrize(
        "question",
        [
            # The regression that motivated the broadening: no "guideline", no
            # "recommend", but plainly an evidence need.
            "Is this dose appropriate per current standards of care?",
            "Is this dose appropriate?",  # tier 2: appraisal cue + decision noun
            "What is the first-line therapy here?",
            "Should we start empiric antibiotics?",
            "Is this insulin regimen safe?",
            "What is the transfusion threshold?",
            "Is anticoagulation contraindicated?",
            "What does the protocol say about titration?",
            "Is this evidence-based?",
            "What is the target for management?",
        ],
    )
    def test_guideline_intent_routes_to_evidence(self, question: str) -> None:
        assert StubSupervisor().route(AgentTask(patient_id=1, question=question)) == ["evidence"]

    @pytest.mark.parametrize(
        "question",
        [
            # A bare lookup must stay on the chart-only path: a decision noun
            # alone ("dose") is a lookup, an appraisal cue alone is not clinical.
            "What is this patient's current potassium?",
            "What is the latest troponin value?",
            "What is the current metoprolol dose?",
            "Is this patient's name spelled correctly?",
            "What changed in the attached outside lab report?",
        ],
    )
    def test_plain_chart_questions_still_route_to_neither(self, question: str) -> None:
        assert StubSupervisor().route(AgentTask(patient_id=1, question=question)) == []

    def test_signals_explain_the_routing_decision(self) -> None:
        """Routing stays inspectable: the signals are the words it keyed on."""
        assert "standards of care" in evidence_signals(
            "Is this dose appropriate per current standards of care?"
        )
        assert evidence_signals("What is this patient's current potassium?") == []

    def test_routing_is_deterministic(self) -> None:
        task = AgentTask(patient_id=1, question="Is this dose appropriate?")
        assert StubSupervisor().route(task) == StubSupervisor().route(task)


# --- worker output reaches the answer ---------------------------------------
#
# The defect these lock out: the graph dispatched workers, then threw their
# output away and answered from the chart alone — making a graph-on answer
# byte-identical to the flag-off inline path. DB-free: both workers are
# injected doubles, so no extraction rows are read.

GUIDELINE_Q = "What do the guidelines recommend for this troponin result?"

_DKA_EVIDENCE = GuidelineEvidence(
    chunk_id="chunk-1",
    document_id="dka-2024",
    section="Insulin therapy",
    content="Start a fixed-rate intravenous insulin infusion at 0.1 units/kg/hour.",
    score=0.91,
    citation=GuidelineCitation(
        source_id="dka-2024",
        page_or_section="Insulin therapy",
        field_or_chunk_id="chunk-1",
        quote_or_value="0.1 units/kg/hour",
    ),
)
_ACS_EVIDENCE = GuidelineEvidence(
    chunk_id="chunk-2",
    document_id="acs-2023",
    section="Antiplatelet therapy",
    content="Give aspirin 162-325 mg at first medical contact in suspected ACS.",
    score=0.88,
    citation=GuidelineCitation(
        source_id="acs-2023",
        page_or_section="Antiplatelet therapy",
        field_or_chunk_id="chunk-2",
        quote_or_value="aspirin 162-325 mg",
    ),
)

_EMPTY_INTAKE = IntakeReport(document_ids=[], fact_count=0, extraction_confidence=0.0)


class _FakeEvidenceRetriever:
    """Evidence-retriever double returning a fixed corpus hit set."""

    def __init__(self, evidence: list[GuidelineEvidence]) -> None:
        self._evidence = evidence

    async def run(self, task: AgentTask) -> EvidenceReport:
        return EvidenceReport(hits=len(self._evidence), evidence=list(self._evidence))


class _FakeIntakeExtractor:
    """Intake-extractor double returning a fixed report (no DB read)."""

    def __init__(self, report: IntakeReport) -> None:
        self._report = report

    async def run(self, task: AgentTask) -> IntakeReport:
        return self._report


def _graph(
    *,
    evidence: list[GuidelineEvidence],
    intake: IntakeReport = _EMPTY_INTAKE,
) -> AgentGraph:
    """A keyless graph over the in-memory cohort with both workers injected."""
    return AgentGraph(
        settings=_keyless(),
        supervisor=StubSupervisor(),
        intake_extractor=_FakeIntakeExtractor(intake),
        evidence_retriever=_FakeEvidenceRetriever(evidence),
        critic=StubCritic(),
        observability=NoopObservability(),
        fhir_client_factory=lambda: _FakeFhir(_COHORT),
    )


class TestWorkerOutputReachesAnswer:
    async def test_retrieved_evidence_reaches_the_answer(self) -> None:
        """The retriever's chunks must show up in the reply, not just in a count."""
        result = await _graph(evidence=[_DKA_EVIDENCE]).run(
            AgentTask(patient_id=SICK, question=GUIDELINE_Q)
        )

        assert result.metrics.retrieval_hits == 1
        assert "0.1 units/kg/hour" in result.answer, (
            "the retrieved guideline content must reach the answer; the graph "
            f"answered {result.answer!r}"
        )
        assert "Insulin therapy" in result.answer  # cited to its corpus section

        # ...and the answer is still a served, FHIR-grounded one: guideline text
        # informs the prose, it never becomes a claim.
        assert result.verification.action == VerificationAction.served
        assert result.verification.claims
        assert all(isinstance(c.source_ref, FhirReference) for c in result.verification.claims)

    async def test_answer_tracks_which_evidence_was_retrieved(self) -> None:
        """Different retrieved evidence => different answer. The property the
        byte-identical-answer defect violated."""
        task = AgentTask(patient_id=SICK, question=GUIDELINE_Q)
        dka = await _graph(evidence=[_DKA_EVIDENCE]).run(task)
        acs = await _graph(evidence=[_ACS_EVIDENCE]).run(task)

        assert dka.answer != acs.answer
        assert "0.1 units/kg/hour" in dka.answer
        assert "aspirin 162-325 mg" in acs.answer

    async def test_extracted_document_facts_reach_the_answer(self) -> None:
        """The intake worker's facts inform the answer — not just a confidence float."""
        report = IntakeReport(
            document_ids=["5"],
            fact_count=1,
            extraction_confidence=0.91,
            facts=[
                ExtractedFact(
                    field_path="labs.potassium.value",
                    value="5.6",
                    unit="mmol/L",
                    supported=True,
                )
            ],
        )
        result = await _graph(evidence=[], intake=report).run(
            AgentTask(
                patient_id=SICK,
                question="What is the latest troponin value?",
                document_ids=["5"],
            )
        )

        assert "labs.potassium.value = 5.6 mmol/L" in result.answer
        assert result.metrics.extraction_confidence == 0.91

    async def test_no_worker_output_leaves_the_inline_answer_untouched(self) -> None:
        """A run that dispatched no worker answers byte-for-byte what the inline
        (flag-OFF) path answers — proving the channel is additive."""
        result = await _graph(evidence=[]).run(
            AgentTask(patient_id=SICK, question=GUIDELINE_Q)
        )
        baseline = await StubAgent(_FakeFhir(_COHORT)).answer(
            PatientId(value=SICK), GUIDELINE_Q
        )

        assert result.answer == baseline.answer
        assert "Guideline context" not in result.answer


class TestStubAgentWorkerContextIsAdditive:
    """The flag-off path calls ``answer`` with no worker kwargs — that call must
    behave exactly as it did before the kwargs existed."""

    async def test_absent_none_and_empty_worker_output_are_identical(self) -> None:
        agent = StubAgent(_FakeFhir(_COHORT))
        pid = PatientId(value=SICK)

        absent = await agent.answer(pid, GUIDELINE_Q)
        explicit_none = await agent.answer(
            pid, GUIDELINE_Q, None, guideline_evidence=None, document_facts=None
        )
        empty = await agent.answer(pid, GUIDELINE_Q, None, guideline_evidence=[], document_facts=[])

        assert absent.answer == explicit_none.answer == empty.answer
        assert [c.text for c in absent.claims] == [c.text for c in empty.claims]

    async def test_evidence_extends_prose_without_touching_claims(self) -> None:
        agent = StubAgent(_FakeFhir(_COHORT))
        pid = PatientId(value=SICK)

        plain = await agent.answer(pid, GUIDELINE_Q)
        informed = await agent.answer(pid, GUIDELINE_Q, guideline_evidence=[_DKA_EVIDENCE])

        # Purely additive: the grounded prose is a prefix of the informed prose.
        assert informed.answer.startswith(plain.answer)
        assert len(informed.answer) > len(plain.answer)
        # The audited evidence is unchanged — a guideline is never a claim.
        assert [c.text for c in informed.claims] == [c.text for c in plain.claims]
        assert all(isinstance(c.source_ref, FhirReference) for c in informed.claims)
