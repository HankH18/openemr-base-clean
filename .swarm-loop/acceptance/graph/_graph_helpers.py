"""Shared helpers for the frozen feat_graph acceptance suite.

FROZEN GOAL HARNESS — do not edit to make a test pass.

The pinned public surface these tests target (from W2_ARCHITECTURE.md):

- Modules ``copilot/graph/supervisor.py``, ``intake_extractor.py``,
  ``evidence_retriever.py``, ``critic.py`` — Stub/Real behind Protocols.
- ``copilot.graph.factory.build_graph(settings, *, observability=None,
  max_iterations=None)`` (also accepted on ``copilot.graph``) — keyless
  settings must yield the full deterministic stub graph. ``max_iterations``
  caps supervisor routing decisions (one worker dispatch = one iteration);
  exhausting it must produce the safe "insufficient grounded information"
  withhold, never an ungrounded answer or an exception.
- ``copilot.graph.contracts`` — typed Pydantic contracts, including
  ``AgentTask(patient_id, question, document_ids)``, ``Handoff{from_agent,
  to_agent, reason, payload}``, ``CriticVerdict``.
- ``graph.run(task)`` (sync or async) returns a result carrying the Week-1
  ``VerificationResult`` (chat-service contract preserved).
- Handoffs are logged into the trace as ``worker.handoff`` events (the
  phi_check corpus expects that event family) with the Handoff fields as
  attributes.

Defensive-import rule: a missing feature module/attr becomes ``pytest.fail``
inside the test body (ran-and-failed), never a collection error.
"""

from __future__ import annotations

import importlib
import inspect
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

ACC_DIR = Path(__file__).resolve().parents[1]
AGENT_DIR = ACC_DIR.parents[1] / "agent"

PATIENT_ID = 1001  # NSTEMI + critical troponin in the fake cohort

DOC_QUESTION = "What changed in the attached outside lab report?"
GUIDELINE_QUESTION = "What do guidelines recommend for insulin therapy in diabetic ketoacidosis?"
BOTH_QUESTION = (
    "Summarize the attached outside lab report and cite guideline recommendations "
    "for diabetic ketoacidosis."
)
NEITHER_QUESTION = "What is this patient's current potassium?"


def fail(msg: str) -> None:
    pytest.fail(msg, pytrace=False)


# --- defensive imports (missing feature => ran-and-failed, never a crash) ----


def feature_module(*names: str):
    errors = []
    for name in names:
        try:
            return importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 — any import failure = feature absent
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    fail(
        "feat_graph surface missing — none of the expected modules import:\n  "
        + "\n  ".join(errors)
    )


def feature_attr(module_names: tuple[str, ...], attr_names: tuple[str, ...], what: str):
    for mod_name in module_names:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:  # noqa: BLE001
            continue
        for attr in attr_names:
            obj = getattr(mod, attr, None)
            if obj is not None:
                return obj
    fail(f"feat_graph: {what} not found — looked for {list(attr_names)} in {list(module_names)}")


def make_settings():
    from copilot.config import Settings

    return Settings()


# --- graph construction + execution --------------------------------------------


def build_graph(*, settings=None, observability=None, max_iterations=None):
    fn = feature_attr(
        ("copilot.graph.factory", "copilot.graph"),
        ("build_graph",),
        "the build_graph factory",
    )
    kwargs: dict[str, Any] = {}
    if observability is not None:
        kwargs["observability"] = observability
    if max_iterations is not None:
        kwargs["max_iterations"] = max_iterations
    try:
        return fn(settings or make_settings(), **kwargs)
    except TypeError as exc:
        fail(
            "pinned factory surface is build_graph(settings, *, observability=None, "
            f"max_iterations=None): {exc}"
        )


def make_task(question: str, document_ids: tuple[str, ...] | list[str] = ()):
    task_cls = feature_attr(
        ("copilot.graph.contracts", "copilot.graph"),
        ("AgentTask",),
        "the AgentTask contract",
    )
    try:
        return task_cls(
            patient_id=PATIENT_ID, question=question, document_ids=list(document_ids)
        )
    except Exception as exc:  # noqa: BLE001
        fail(
            "pinned task shape is AgentTask(patient_id: int, question: str, "
            f"document_ids: list[str] = []): {type(exc).__name__}: {exc}"
        )


async def run_graph(graph, task):
    fn = getattr(graph, "run", None)
    if not callable(fn):
        fail("the built graph must expose run(task) (sync or async)")
    result = fn(task)
    if inspect.isawaitable(result):
        result = await result
    if result is None:
        fail("graph.run(task) returned None — expected a typed graph result")
    return result


# --- recording observability double (implements copilot.observability.base) ------


class _RecSpan:
    def __init__(self, name: str, attrs: Mapping[str, Any], span_id: str, parent_id: str | None):
        self.name = name
        self.attrs: dict[str, Any] = dict(attrs)
        self.id = span_id
        self.parent_id = parent_id
        self.output: Any = None

    def set_attribute(self, key: str, value: Any) -> None:
        self.attrs[key] = value

    def set_output(self, value: Any) -> None:
        self.output = value


class RecordingObs:
    """An ``Observability`` implementation that records spans + events in order."""

    def __init__(self) -> None:
        self.spans: list[_RecSpan] = []
        self.events: list[dict[str, Any]] = []
        self.verifications: list[dict[str, Any]] = []
        self._stack: list[_RecSpan] = []
        self._n = 0

    @asynccontextmanager
    async def span(self, name: str, **attributes: Any) -> AsyncIterator[_RecSpan]:
        self._n += 1
        rec = _RecSpan(
            name,
            attributes,
            f"span-{self._n}",
            self._stack[-1].id if self._stack else None,
        )
        self.spans.append(rec)
        self._stack.append(rec)
        try:
            yield rec
        finally:
            self._stack.pop()

    def event(self, name: str, **attributes: Any) -> None:
        self.events.append({"name": name, "attrs": dict(attributes)})

    def record_verification(self, *, passed: bool, action: str, patient_id: int) -> None:
        self.verifications.append(
            {"passed": passed, "action": action, "patient_id": patient_id}
        )

    def record_poller_staleness(self, *, patient_id: int, age_seconds: int) -> None:
        return

    async def flush(self) -> None:
        return


# --- fake Langfuse v2 client (captures the exported trace, ids included) ---------


class FakeLangfuseObservation:
    """One recorded observation (span/event/generation) with its parentage."""

    def __init__(
        self,
        client: "FakeLangfuseClient",
        *,
        kind: str,
        name: str | None,
        obs_id: str,
        trace_id: str | None,
        parent_observation_id: str | None,
        metadata: Any = None,
    ) -> None:
        self._client = client
        self.kind = kind
        self.name = name
        self.id = obs_id
        self.trace_id = trace_id
        self.parent_observation_id = parent_observation_id
        self.metadata: dict[str, Any] = dict(metadata or {})
        self.output: Any = None
        self.ended = False
        client.observations.append(self)

    # Nested creation — children inherit trace_id and parent onto this obs.
    def span(self, **kwargs: Any) -> "FakeLangfuseObservation":
        return FakeLangfuseObservation(
            self._client,
            kind="span",
            name=kwargs.get("name"),
            obs_id=kwargs.get("id") or self._client.next_id("span"),
            trace_id=kwargs.get("trace_id") or self.trace_id,
            parent_observation_id=kwargs.get("parent_observation_id") or self.id,
            metadata=kwargs.get("metadata"),
        )

    def generation(self, **kwargs: Any) -> "FakeLangfuseObservation":
        obs = self.span(**kwargs)
        obs.kind = "generation"
        return obs

    def event(self, **kwargs: Any) -> "FakeLangfuseObservation":
        obs = self.span(**kwargs)
        obs.kind = "event"
        return obs

    def update(self, **kwargs: Any) -> "FakeLangfuseObservation":
        meta = kwargs.pop("metadata", None)
        if isinstance(meta, Mapping):
            self.metadata.update(meta)
        if "output" in kwargs:
            self.output = kwargs.pop("output")
        self.metadata.update({k: v for k, v in kwargs.items() if k not in ("name",)})
        return self

    def end(self, **kwargs: Any) -> "FakeLangfuseObservation":
        self.ended = True
        return self.update(**kwargs)


class FakeLangfuseTrace:
    """The v2 stateful trace client: root of one exported trace."""

    def __init__(self, client: "FakeLangfuseClient", *, trace_id: str, name: str | None, metadata: Any):
        self._client = client
        self.id = trace_id
        self.name = name
        self.metadata: dict[str, Any] = dict(metadata or {})

    def span(self, **kwargs: Any) -> FakeLangfuseObservation:
        return FakeLangfuseObservation(
            self._client,
            kind="span",
            name=kwargs.get("name"),
            obs_id=kwargs.get("id") or self._client.next_id("span"),
            trace_id=self.id,
            # Trace-level spans have no parent observation.
            parent_observation_id=kwargs.get("parent_observation_id"),
            metadata=kwargs.get("metadata"),
        )

    def generation(self, **kwargs: Any) -> FakeLangfuseObservation:
        obs = self.span(**kwargs)
        obs.kind = "generation"
        return obs

    def event(self, **kwargs: Any) -> FakeLangfuseObservation:
        obs = self.span(**kwargs)
        obs.kind = "event"
        return obs

    def update(self, **kwargs: Any) -> "FakeLangfuseTrace":
        meta = kwargs.pop("metadata", None)
        if isinstance(meta, Mapping):
            self.metadata.update(meta)
        return self

    def end(self, **kwargs: Any) -> "FakeLangfuseTrace":
        return self.update(**kwargs)


class FakeLangfuseClient:
    """Recording double for the Langfuse v2 SDK client (pinned ``<3``)."""

    def __init__(self) -> None:
        self.traces: list[FakeLangfuseTrace] = []
        self.observations: list[FakeLangfuseObservation] = []
        self.flushed = False
        self._n = 0

    def next_id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n}"

    def trace(self, **kwargs: Any) -> FakeLangfuseTrace:
        trace = FakeLangfuseTrace(
            self,
            trace_id=kwargs.get("id") or self.next_id("trace"),
            name=kwargs.get("name"),
            metadata=kwargs.get("metadata"),
        )
        self.traces.append(trace)
        return trace

    def span(self, **kwargs: Any) -> FakeLangfuseObservation:
        # Low-level creation with explicit trace_id/parent_observation_id.
        return FakeLangfuseObservation(
            self,
            kind="span",
            name=kwargs.get("name"),
            obs_id=kwargs.get("id") or self.next_id("span"),
            trace_id=kwargs.get("trace_id"),
            parent_observation_id=kwargs.get("parent_observation_id"),
            metadata=kwargs.get("metadata"),
        )

    def event(self, **kwargs: Any) -> FakeLangfuseObservation:
        obs = self.span(**kwargs)
        obs.kind = "event"
        return obs

    def generation(self, **kwargs: Any) -> FakeLangfuseObservation:
        obs = self.span(**kwargs)
        obs.kind = "generation"
        return obs

    def flush(self) -> None:
        self.flushed = True


# --- handoff + artifact extraction ------------------------------------------------


def _dig_handoff(attrs: Any) -> dict[str, Any] | None:
    if not isinstance(attrs, Mapping):
        return None
    if "from_agent" in attrs and "to_agent" in attrs:
        return {
            "from_agent": str(attrs["from_agent"]),
            "to_agent": str(attrs["to_agent"]),
            "reason": attrs.get("reason"),
            "payload": attrs.get("payload"),
        }
    for value in attrs.values():
        found = _dig_handoff(value)
        if found is not None:
            return found
    return None


def handoff_pairs(recorder: RecordingObs, result: Any = None) -> list[dict[str, Any]]:
    """Ordered handoffs from the captured artifact (events first, result second)."""
    out: list[dict[str, Any]] = []
    for ev in recorder.events:
        if "handoff" not in str(ev["name"]).lower():
            continue
        found = _dig_handoff(ev["attrs"])
        if found is not None:
            out.append(found)
    if not out and result is not None:
        for attr in ("handoffs", "handoff_log"):
            seq = getattr(result, attr, None)
            if isinstance(seq, (list, tuple)):
                for h in seq:
                    from_agent = getattr(h, "from_agent", None)
                    to_agent = getattr(h, "to_agent", None)
                    if from_agent and to_agent:
                        out.append(
                            {
                                "from_agent": str(from_agent),
                                "to_agent": str(to_agent),
                                "reason": getattr(h, "reason", None),
                                "payload": getattr(h, "payload", None),
                            }
                        )
    return out


def routed(pairs: list[dict[str, Any]], needle: str) -> bool:
    return any(needle in p["to_agent"].lower() for p in pairs)


def find_verification_result(result: Any):
    """Locate the Week-1 VerificationResult carried by the graph result."""
    from copilot.domain.contracts import VerificationResult

    if isinstance(result, VerificationResult):
        return result
    candidates: list[Any] = []
    fields = getattr(type(result), "model_fields", None)
    if fields:
        candidates = [getattr(result, k, None) for k in fields]
    elif hasattr(result, "__dict__"):
        candidates = list(vars(result).values())
    for value in candidates:
        if isinstance(value, VerificationResult):
            return value
    for value in candidates:  # one more level (e.g. result.answer.verification)
        inner_fields = getattr(type(value), "model_fields", None)
        if inner_fields:
            for k in inner_fields:
                inner = getattr(value, k, None)
                if isinstance(inner, VerificationResult):
                    return inner
    fail(
        "the graph result must carry the Week-1 copilot.domain.contracts.VerificationResult "
        f"(chat-service contract preserved); got {type(result).__name__} without one"
    )


def result_blob(result: Any) -> str:
    """A searchable string dump of the graph result."""
    if hasattr(result, "model_dump_json"):
        try:
            return result.model_dump_json()
        except Exception:  # noqa: BLE001
            pass
    if hasattr(result, "__dict__"):
        return repr(vars(result))
    return repr(result)


def artifact_pairs(recorder: RecordingObs, result: Any) -> list[tuple[str, Any]]:
    """Every (lowercased key, value) captured across the run's artifact surfaces."""
    pairs: list[tuple[str, Any]] = []

    def add(key: Any, value: Any) -> None:
        pairs.append((str(key).lower(), value))

    def walk(obj: Any) -> None:
        if isinstance(obj, Mapping):
            for k, v in obj.items():
                add(k, v)
                walk(v)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                walk(v)

    for ev in recorder.events:
        add(ev["name"], ev["attrs"])
        walk(ev["attrs"])
    for span in recorder.spans:
        add(span.name, span.attrs)
        walk(span.attrs)
        walk(span.output)
    if hasattr(result, "model_dump"):
        try:
            walk(result.model_dump())
        except Exception:  # noqa: BLE001
            pass
    for attr in ("artifact", "observability", "metrics", "telemetry"):
        value = getattr(result, attr, None)
        if isinstance(value, Mapping):
            walk(value)
    return pairs


# --- DB seeding --------------------------------------------------------------------


def sync_db_url() -> str:
    url = os.environ.get("COPILOT_DATABASE_URL", "")
    assert url.startswith("sqlite+aiosqlite:///"), f"unexpected test DB url: {url!r}"
    return url.replace("sqlite+aiosqlite", "sqlite", 1)


def seed_document(patient_id: int = PATIENT_ID) -> str:
    """Insert an extracted source document (+1 supported fact); returns its id."""
    from copilot.memory.models import ExtractedFactRow, ExtractionRow, SourceDocumentRow

    engine = sa.create_engine(sync_db_url())
    try:
        with Session(engine) as session:
            doc = SourceDocumentRow(
                patient_id=patient_id,
                openemr_document_id="5001",
                doc_type="lab_pdf",
                filename="outside_labs.pdf",
                content_hash="acc-fixture-hash",
                page_count=1,
                status="extracted",
                correlation_id="acc-seed-doc-0001",
            )
            session.add(doc)
            session.flush()
            extraction = ExtractionRow(
                source_document_id=doc.id,
                schema_version="v1",
                model="stub",
                confidence_overall=0.93,
                status="ok",
                correlation_id="acc-seed-doc-0001",
            )
            session.add(extraction)
            session.flush()
            session.add(
                ExtractedFactRow(
                    extraction_id=extraction.id,
                    field_path="labs.potassium.value",
                    value="5.6",
                    unit="mmol/L",
                    page_no=1,
                    bbox=[0.12, 0.34, 0.2, 0.04],
                    match_confidence=0.97,
                    supported=True,
                )
            )
            session.commit()
            doc_id = doc.id
    finally:
        engine.dispose()
    return str(doc_id)
