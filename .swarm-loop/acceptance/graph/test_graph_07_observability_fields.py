"""feat_graph criterion 7 — the captured artifact carries all 7 observability keys.

FROZEN GOALS. One stubbed end-to-end run (doc + guideline task, keyless) must
leave a captured artifact — the union of the injected recorder's events/spans
and the graph result — containing all seven observability concepts:

  1. tool/handoff sequence          (a key containing "handoff")
  2. latency                        (a numeric "latency"/"duration" key)
  3. tokens                         (a numeric or mapping "token" key)
  4. cost                           (a numeric "cost" key)
  5. retrieval hits                 (a key containing "hit")
  6. extraction confidence          (a numeric "confidence" key)
  7. eval outcome                   (a key containing "eval" — excluding
                                     "retriev*", which contains "eval" as a
                                     substring)
"""

from __future__ import annotations

from collections.abc import Mapping

import _graph_helpers as H


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


CONCEPTS = {
    "tool/handoff sequence": lambda k, v: "handoff" in k,
    "latency": lambda k, v: ("latency" in k or "duration" in k) and _is_number(v) and v >= 0,
    "tokens": lambda k, v: "token" in k and (_is_number(v) or isinstance(v, Mapping)),
    "cost": lambda k, v: "cost" in k and _is_number(v),
    "retrieval hits": lambda k, v: "hit" in k,
    "extraction confidence": lambda k, v: "confidence" in k and _is_number(v),
    "eval outcome": lambda k, v: "eval" in k and "retriev" not in k and v is not None,
}


async def test_graph_07_stubbed_e2e_artifact_contains_all_seven_keys():
    H.feature_module("copilot.graph")  # gate: the feature package must exist

    doc_id = H.seed_document()
    recorder = H.RecordingObs()
    graph = H.build_graph(observability=recorder)
    result = await H.run_graph(graph, H.make_task(H.BOTH_QUESTION, [doc_id]))

    pairs = H.artifact_pairs(recorder, result)
    assert pairs, "the stubbed run captured no artifact keys at all"

    missing = [
        concept
        for concept, predicate in CONCEPTS.items()
        if not any(predicate(key, value) for key, value in pairs)
    ]
    observed_keys = sorted({key for key, _ in pairs})
    assert not missing, (
        f"captured artifact is missing observability concepts {missing}; "
        f"observed keys (sample): {observed_keys[:60]}"
    )

    # The handoff sequence must reflect the actual multi-worker run.
    handoffs = H.handoff_pairs(recorder, result)
    assert len(handoffs) >= 2, (
        "the artifact's handoff sequence must contain the run's supervisor->worker "
        f"transitions in order; captured {handoffs}"
    )
