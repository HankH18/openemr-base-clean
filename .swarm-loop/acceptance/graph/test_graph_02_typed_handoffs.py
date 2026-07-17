"""feat_graph criterion 2 — typed, logged handoffs in order.

FROZEN GOALS. Every supervisor<->worker transition emits a typed
``Handoff{from_agent, to_agent, reason, payload}`` into the captured artifact
(``worker.handoff`` observability events — the family the phi_check corpus
expects), in emission order. The Handoff Pydantic contract itself must exist
with exactly those four fields present, and each captured handoff must
validate through it.
"""

from __future__ import annotations

import _graph_helpers as H

REQUIRED_FIELDS = {"from_agent", "to_agent", "reason", "payload"}


async def test_graph_02_typed_logged_handoffs_in_order():
    handoff_cls = H.feature_attr(
        ("copilot.graph.contracts", "copilot.graph"),
        ("Handoff",),
        "the Handoff contract",
    )
    model_fields = getattr(handoff_cls, "model_fields", None)
    assert model_fields is not None, "Handoff must be a Pydantic model (typed contract)"
    assert REQUIRED_FIELDS <= set(model_fields), (
        f"Handoff must declare {sorted(REQUIRED_FIELDS)}; has {sorted(model_fields)}"
    )

    doc_id = H.seed_document()
    recorder = H.RecordingObs()
    graph = H.build_graph(observability=recorder)
    result = await H.run_graph(graph, H.make_task(H.BOTH_QUESTION, [doc_id]))

    pairs = H.handoff_pairs(recorder, result)
    assert len(pairs) >= 2, (
        "a doc+guideline run must log at least the two supervisor->worker handoffs; "
        f"captured {pairs}"
    )
    assert "supervisor" in pairs[0]["from_agent"].lower(), (
        f"the first logged transition must originate from the supervisor; got {pairs[0]}"
    )
    assert H.routed(pairs, "intake") and (
        H.routed(pairs, "evidence") or H.routed(pairs, "retriev")
    ), f"both worker handoffs must appear in the captured sequence; got {pairs}"

    for pair in pairs:
        assert pair["from_agent"] and pair["to_agent"], f"handoff missing endpoints: {pair}"
        assert isinstance(pair["reason"], str) and pair["reason"].strip(), (
            f"every handoff must carry a non-empty reason: {pair}"
        )
        try:
            handoff_cls(
                from_agent=pair["from_agent"],
                to_agent=pair["to_agent"],
                reason=pair["reason"],
                payload=pair["payload"],
            )
        except Exception as exc:  # noqa: BLE001
            H.fail(f"captured handoff does not validate through the Handoff contract: {exc}")

    # In order: if the result also carries a typed handoff log, it must agree
    # with the event capture order.
    result_seq = getattr(result, "handoffs", None)
    if isinstance(result_seq, (list, tuple)) and result_seq:
        result_pairs = [
            (str(getattr(h, "from_agent", "")), str(getattr(h, "to_agent", "")))
            for h in result_seq
        ]
        event_pairs = [(p["from_agent"], p["to_agent"]) for p in pairs]
        assert result_pairs == event_pairs, (
            "the result's handoff log and the captured events disagree on order: "
            f"{result_pairs} vs {event_pairs}"
        )
