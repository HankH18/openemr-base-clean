"""feat_graph criterion 5 — worker spans nest under the supervisor span.

FROZEN GOALS. The observability backend must move from flat traces to
parent/child spans: in the trace EXPORTED through the (fake, recording)
Langfuse v2 client, each worker span is a descendant of the supervisor span
(parent-observation ids asserted), and every observation carries the ambient
correlation id as its trace id — i.e. the full multi-agent trace reconstructs
from the correlation_id alone.

Captured at the vendor-SDK boundary: ``LangfuseObservability(..., client=fake)``
is the existing injection point, so the assertion sees exactly what a real
Langfuse deployment would ingest.
"""

from __future__ import annotations

import _graph_helpers as H

CORRELATION_ID = "acc-graph-span-cid-0001"


def _spans_named(observations, *needles: str):
    return [
        o
        for o in observations
        if o.kind in ("span", "generation")
        and any(n in (o.name or "").lower() for n in needles)
    ]


async def test_graph_05_worker_spans_are_children_of_supervisor_span():
    H.feature_module("copilot.graph")  # gate: the feature package must exist

    from copilot.observability.base import correlation_id_var
    from copilot.observability.langfuse_backend import LangfuseObservability

    fake = H.FakeLangfuseClient()
    try:
        observability = LangfuseObservability(
            host="http://langfuse.acceptance.test",
            public_key="pk-acceptance",
            secret_key="sk-acceptance",
            client=fake,
        )
    except TypeError as exc:
        H.fail(f"LangfuseObservability must keep its client injection point: {exc}")

    doc_id = H.seed_document()
    graph = H.build_graph(observability=observability)
    token = correlation_id_var.set(CORRELATION_ID)
    try:
        await H.run_graph(graph, H.make_task(H.BOTH_QUESTION, [doc_id]))
    finally:
        correlation_id_var.reset(token)

    observations = fake.observations
    assert observations, (
        "the run exported no observations through the injected Langfuse client — "
        "the graph must emit its spans through the injected Observability"
    )

    supervisor_spans = _spans_named(observations, "supervisor")
    assert supervisor_spans, (
        "no supervisor span exported — the supervisor must open the parent span "
        f"(exported span names: {[o.name for o in observations]})"
    )
    supervisor = supervisor_spans[0]

    intake_spans = _spans_named(observations, "intake")
    evidence_spans = _spans_named(observations, "evidence", "retriev")
    assert intake_spans and evidence_spans, (
        "both worker spans (intake-extractor, evidence-retriever) must be exported; "
        f"got names {[o.name for o in observations]}"
    )

    by_id = {o.id: o for o in observations}

    def ancestor_ids(obs) -> set[str]:
        seen: set[str] = set()
        parent_id = obs.parent_observation_id
        while parent_id is not None and parent_id not in seen:
            seen.add(parent_id)
            parent = by_id.get(parent_id)
            parent_id = parent.parent_observation_id if parent is not None else None
        return seen

    for worker in (*intake_spans, *evidence_spans):
        assert worker.parent_observation_id is not None, (
            f"worker span {worker.name!r} exported FLAT (no parent observation) — "
            "worker spans must be children of the supervisor span"
        )
        assert supervisor.id in ancestor_ids(worker), (
            f"worker span {worker.name!r} (parent {worker.parent_observation_id!r}) is not "
            f"a descendant of the supervisor span {supervisor.id!r}"
        )
        assert worker.id != supervisor.id

    # The whole trace reconstructs from the correlation id alone.
    trace_ids = {o.trace_id for o in observations}
    assert trace_ids == {CORRELATION_ID}, (
        "every exported observation must carry the ambient correlation id as its "
        f"trace id; saw {trace_ids}"
    )
