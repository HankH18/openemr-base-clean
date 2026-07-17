"""Graph telemetry must never ship the raw clinician question to the trace backend.

The defect these lock out: the supervisor put ``task.question`` — free clinical
prose, PHI-bearing — on the ``supervisor.route`` span, the
``evidence-retriever.retrieve`` span, and the ``worker.handoff`` event payload.
``copilot.observability.langfuse_backend`` is a pure passthrough
(``span(name=..., metadata=attributes)`` / ``update(metadata={key: value})``), so
every one of those reached a third-party SaaS verbatim on the deployed stack.

The invariant, already stated by ``copilot.rag.retriever.retrieve`` and now held
here too: *attributes are non-PHI signals only — never the query text, which may
carry PHI even before the de-identify choke point.* ``deidentify()`` is NOT a
choke point on this path — it has exactly one call site (the RAG leg), so nothing
scrubs what the graph hands to observability.

Two halves, and the second is what stops the fix from being "emit nothing":

1. The PHI marker reaches no span name, attribute, output, or event payload.
2. The spans still carry their useful non-PHI signals (route plan, hit counts,
   the router's matched vocabulary, the seven metrics fields).

The recorder deliberately captures exactly what the Langfuse backend would
serialize: whatever is handed to ``span()``/``event()``/``set_attribute()``/
``set_output()`` IS the egress payload.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from pydantic import BaseModel

from copilot.domain.documents import ExtractedFact
from copilot.graph.contracts import AgentTask
from copilot.graph.critic import StubCritic
from copilot.graph.intake_extractor import IntakeReport
from copilot.graph.supervisor import AgentGraph, StubSupervisor

# Reuse the in-memory FHIR double + synthetic cohort + worker doubles.
from tests.test_chat_routes import _COHORT, SICK, _FakeFhir
from tests.test_graph import _DKA_EVIDENCE, _FakeEvidenceRetriever, _FakeIntakeExtractor, _keyless

# A question shaped like the real thing: guideline intent (so it routes to the
# evidence worker) wrapped around free clinical prose naming a patient.
PHI_QUESTION = (
    "Is Mrs. Chen's troponin still rising after the heparin drip, "
    "and what do the guidelines recommend?"
)

# Distinctive fragments of that prose. None is a routing signal, so none has any
# business on a span: the vocabulary the router keys on is "guidelines" /
# "recommend", never the patient's name or her therapy.
PHI_MARKERS = ("Mrs. Chen", "Chen", "heparin drip", "still rising")

_INTAKE_REPORT = IntakeReport(
    document_ids=["5"],
    fact_count=1,
    extraction_confidence=0.91,
    facts=[
        ExtractedFact(
            field_path="labs.potassium.value", value="5.6", unit="mmol/L", supported=True
        )
    ],
)


# --- recording observability -------------------------------------------------


class _RecordingSpan:
    """Captures every byte the Langfuse adapter would forward for one span."""

    def __init__(self, name: str, attributes: dict[str, Any]) -> None:
        self.name = name
        self.attributes: dict[str, Any] = dict(attributes)
        self.outputs: list[Any] = []

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_output(self, value: Any) -> None:
        self.outputs.append(value)


class _RecordingObservability:
    """Observability double that records instead of shipping.

    Mirrors the real backend's contract exactly: the kwargs handed to
    ``span``/``event`` become span/event metadata verbatim, which is precisely
    what egresses.
    """

    def __init__(self) -> None:
        self.spans: list[_RecordingSpan] = []
        self.events: list[dict[str, Any]] = []

    @asynccontextmanager
    async def span(self, name: str, **attributes: Any) -> Any:
        recorded = _RecordingSpan(name, attributes)
        self.spans.append(recorded)
        yield recorded

    def event(self, name: str, **attributes: Any) -> None:
        self.events.append({"name": name, **attributes})

    def record_verification(self, *, passed: bool, action: str, patient_id: int) -> None:
        self.event("verification.result", passed=passed, action=action, patient_id=patient_id)

    def record_poller_staleness(self, *, patient_id: int, age_seconds: int) -> None:
        self.event("poller.staleness", patient_id=patient_id, age_seconds=age_seconds)

    async def flush(self) -> None:
        return None

    def named(self, name: str) -> _RecordingSpan:
        """The one recorded span called ``name`` — fails loudly if it vanished."""
        matches = [s for s in self.spans if s.name == name]
        assert matches, f"no {name!r} span was recorded; got {[s.name for s in self.spans]}"
        return matches[0]

    def event_named(self, name: str) -> dict[str, Any]:
        matches = [e for e in self.events if e["name"] == name]
        assert matches, f"no {name!r} event; got {[e['name'] for e in self.events]}"
        return matches[0]


# --- recursive text harvest --------------------------------------------------


def _strings(value: Any) -> Iterator[str]:
    """Every string reachable inside ``value``, however deeply nested.

    Walks mappings (keys AND values), sequences, and pydantic models, and falls
    back to ``str(value)`` for anything else — so parking a whole ``AgentTask``
    on a span (whose repr embeds the question) is caught too, not just a bare
    ``question=`` kwarg. Checking one known field instead would only prove the
    leak moved.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, BaseModel):
        yield from _strings(value.model_dump())
    elif value is not None:
        yield str(value)


def _telemetry_text(obs: _RecordingObservability) -> list[tuple[str, str]]:
    """Every (site, text) pair that would leave the process, labelled by origin."""
    harvested: list[tuple[str, str]] = []
    for span in obs.spans:
        harvested.append((f"span name {span.name!r}", span.name))
        for key, value in span.attributes.items():
            harvested += [(f"span {span.name!r} attribute {key!r}", t) for t in _strings(value)]
        for output in span.outputs:
            harvested += [(f"span {span.name!r} output", t) for t in _strings(output)]
    for event in obs.events:
        for key, value in event.items():
            harvested += [(f"event {event['name']!r} field {key!r}", t) for t in _strings(value)]
    return harvested


async def _run(obs: _RecordingObservability) -> Any:
    """The REAL graph — real supervisor, real routing, real span plumbing."""
    graph = AgentGraph(
        settings=_keyless(),
        supervisor=StubSupervisor(),
        intake_extractor=_FakeIntakeExtractor(_INTAKE_REPORT),
        evidence_retriever=_FakeEvidenceRetriever([_DKA_EVIDENCE]),
        critic=StubCritic(),
        observability=obs,
        fhir_client_factory=lambda: _FakeFhir(_COHORT),
    )
    return await graph.run(
        AgentTask(patient_id=SICK, question=PHI_QUESTION, document_ids=["5"])
    )


class TestNoPhiInGraphTelemetry:
    async def test_the_question_reaches_no_telemetry_surface(self) -> None:
        """No span name, attribute, output, or event payload carries the prose."""
        obs = _RecordingObservability()
        await _run(obs)

        assert obs.spans, "the graph recorded no spans at all — the test proves nothing"
        leaks = [
            (site, marker, text)
            for site, text in _telemetry_text(obs)
            for marker in PHI_MARKERS
            if marker.lower() in text.lower()
        ]
        assert not leaks, (
            "the clinician's question reached the trace backend — every entry below "
            "is PHI egressing to a third-party SaaS:\n"
            + "\n".join(f"  - {site}: {marker!r} found in {text!r}" for site, marker, text in leaks)
        )

    async def test_the_routing_span_still_explains_itself(self) -> None:
        """Dropping the question must not mean emitting nothing."""
        obs = _RecordingObservability()
        await _run(obs)

        route = obs.named("supervisor.route")
        assert route.attributes["route_plan"] == ["intake", "evidence"], (
            f"the routing decision must stay on the span: {route.attributes}"
        )
        assert route.attributes["patient_id"] == SICK

    async def test_the_evidence_span_still_carries_signals_and_hits(self) -> None:
        """The router's matched vocabulary is the non-PHI stand-in for the
        question: it explains WHY the worker was dispatched, and it is drawn from
        a fixed word list rather than from the record."""
        obs = _RecordingObservability()
        await _run(obs)

        evidence = obs.named("evidence-retriever.retrieve")
        assert set(evidence.attributes["signals"]) >= {"guidelines", "recommend"}, (
            f"the matched routing vocabulary must survive: {evidence.attributes}"
        )
        assert evidence.attributes["retrieval_hits"] == 1
        assert evidence.outputs == [{"retrieval_hits": 1}]

    async def test_the_intake_span_still_carries_its_extraction_signals(self) -> None:
        obs = _RecordingObservability()
        await _run(obs)

        intake = obs.named("intake-extractor.run")
        assert intake.attributes["document_ids"] == ["5"]
        assert intake.attributes["fact_count"] == 1
        assert intake.attributes["extraction_confidence"] == 0.91

    async def test_the_handoff_event_still_names_the_route_and_signals(self) -> None:
        """The handoff event is emitted verbatim to the backend, so it is an
        egress surface exactly like a span attribute — and it must still explain
        the routing after the question is dropped from its payload."""
        obs = _RecordingObservability()
        await _run(obs)

        handoffs = [e for e in obs.events if e["name"] == "worker.handoff"]
        targets = [e["to_agent"] for e in handoffs]
        assert "evidence-retriever" in targets, f"evidence worker must hand off: {targets}"

        evidence_handoff = next(e for e in handoffs if e["to_agent"] == "evidence-retriever")
        assert "question" not in evidence_handoff["payload"], (
            f"the raw question must not ride in the handoff payload: {evidence_handoff['payload']}"
        )
        assert set(evidence_handoff["payload"]["signals"]) >= {"guidelines", "recommend"}
        assert evidence_handoff["from_agent"] == "supervisor"
        assert evidence_handoff["reason"]

    async def test_the_seven_metrics_fields_still_ship(self) -> None:
        """The observability contract the trace is FOR must be undamaged."""
        obs = _RecordingObservability()
        await _run(obs)

        telemetry = obs.event_named("graph.telemetry")
        assert {
            "handoff_sequence",
            "latency_ms",
            "total_tokens",
            "cost_usd",
            "retrieval_hits",
            "extraction_confidence",
            "eval_outcome",
        } <= set(telemetry)
        assert telemetry["retrieval_hits"] == 1
        assert telemetry["extraction_confidence"] == 0.91
        assert "supervisor->evidence-retriever" in telemetry["handoff_sequence"]

        # ...and the verification event the safety dashboard reads.
        assert obs.event_named("verification.result")["action"] == "served"


class TestRecorderActuallyCatchesALeak:
    """The recorder's own smoke test.

    A no-PHI-found assertion is only worth what the detector behind it is worth:
    if ``_strings`` silently missed nested structures, every test above would
    pass while the leak shipped. So prove the walk finds a marker buried in the
    exact shapes the graph really uses — a nested event payload and a span
    attribute — rather than trusting it.
    """

    def test_recursive_walk_finds_a_marker_nested_in_a_payload(self) -> None:
        obs = _RecordingObservability()
        obs.event("worker.handoff", payload={"question": PHI_QUESTION, "signals": ["guidelines"]})

        found = [text for _site, text in _telemetry_text(obs) if "Mrs. Chen" in text]
        assert found == [PHI_QUESTION], (
            "the recursive walk must find a marker nested inside an event payload"
        )

    async def test_recursive_walk_finds_a_marker_on_a_span_attribute(self) -> None:
        obs = _RecordingObservability()
        async with obs.span("x", question=PHI_QUESTION) as span:
            span.set_output({"echo": [{"deep": PHI_QUESTION}]})

        assert any("Mrs. Chen" in text for _site, text in _telemetry_text(obs))
