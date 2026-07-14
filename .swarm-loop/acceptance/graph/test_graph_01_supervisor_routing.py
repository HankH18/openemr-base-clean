"""feat_graph criterion 1 — supervisor routing + iteration-cap safe withhold.

FROZEN GOALS. The (stub, keyless) supervisor routes: a document in scope to
the intake-extractor; explicit guideline need to the evidence-retriever; both
signals to both workers; neither signal to neither worker. Routing decisions
are observed black-box through the logged handoffs. A hard iteration cap
(``build_graph(..., max_iterations=1)``, one worker dispatch = one iteration)
on a task that needs two dispatches must produce the safe "insufficient
grounded information" withhold — never an ungrounded answer, never a crash.
"""

from __future__ import annotations

import _graph_helpers as H


async def _routes_for(question: str, document_ids: list[str]) -> list[dict]:
    recorder = H.RecordingObs()
    graph = H.build_graph(observability=recorder)
    result = await H.run_graph(graph, H.make_task(question, document_ids))
    return H.handoff_pairs(recorder, result)


async def test_graph_01_supervisor_routes_doc_guideline_both_neither_and_caps():
    H.feature_module("copilot.graph")  # gate: the feature package must exist
    doc_id = H.seed_document()

    # (a) Document in scope -> intake-extractor, not the evidence-retriever.
    pairs = await _routes_for(H.DOC_QUESTION, [doc_id])
    assert pairs, "a routed task must emit logged handoffs (worker.handoff events)"
    assert H.routed(pairs, "intake"), (
        f"doc-in-scope task must hand off to the intake-extractor; got {pairs}"
    )
    assert not H.routed(pairs, "evidence") and not H.routed(pairs, "retriev"), (
        f"a pure document task must not invoke the evidence-retriever; got {pairs}"
    )

    # (b) Guideline need -> evidence-retriever, not the intake-extractor.
    pairs = await _routes_for(H.GUIDELINE_QUESTION, [])
    assert H.routed(pairs, "evidence") or H.routed(pairs, "retriev"), (
        f"guideline-need task must hand off to the evidence-retriever; got {pairs}"
    )
    assert not H.routed(pairs, "intake"), (
        f"a no-document task must not invoke the intake-extractor; got {pairs}"
    )

    # (c) Both signals -> both workers.
    pairs = await _routes_for(H.BOTH_QUESTION, [doc_id])
    assert H.routed(pairs, "intake") and (
        H.routed(pairs, "evidence") or H.routed(pairs, "retriev")
    ), f"doc+guideline task must reach both workers; got {pairs}"

    # (d) Neither signal -> neither worker (chart-only answer path).
    pairs = await _routes_for(H.NEITHER_QUESTION, [])
    assert not H.routed(pairs, "intake"), (
        f"a plain chart question must not invoke the intake-extractor; got {pairs}"
    )
    assert not H.routed(pairs, "evidence") and not H.routed(pairs, "retriev"), (
        f"a plain chart question must not invoke the evidence-retriever; got {pairs}"
    )

    # (e) Iteration cap: a both-workers task under max_iterations=1 cannot
    # finish grounding -> the safe withhold, not an ungrounded answer.
    recorder = H.RecordingObs()
    capped = H.build_graph(observability=recorder, max_iterations=1)
    try:
        result = await H.run_graph(capped, H.make_task(H.BOTH_QUESTION, [doc_id]))
    except Exception as exc:  # noqa: BLE001 — the cap is a safe stop, not a crash
        H.fail(f"hitting the iteration cap must withhold safely, not raise: {exc!r}")
    verification = H.find_verification_result(result)
    from copilot.domain.contracts import VerificationAction

    assert verification.action == VerificationAction.withheld, (
        "exhausting the iteration cap must withhold "
        f"(got action={verification.action!r})"
    )
    assert "insufficient" in H.result_blob(result).lower(), (
        'the capped run must surface the safe "insufficient grounded information" message'
    )
