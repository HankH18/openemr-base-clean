"""feat_graph criterion 3 — Stub/Real workers behind Protocol + factory, keyless.

FROZEN GOALS. The four pinned modules (supervisor, intake_extractor,
evidence_retriever, critic) exist under ``copilot/graph/``; at least one
``typing.Protocol`` defines the swappable worker surface; ``build_graph`` with
keyless settings (empty Anthropic/Voyage/Cohere keys — the conftest env) runs
the FULL stub graph end-to-end, and two identical runs are byte-identical in
routing and verification outcome (deterministic == LLM-free).
"""

from __future__ import annotations

import _graph_helpers as H

GRAPH_MODULES = ("supervisor", "intake_extractor", "evidence_retriever", "critic")


async def test_graph_03_full_stub_graph_runs_keyless_and_deterministic():
    # Pinned module layout (W2_ARCHITECTURE.md §Components).
    modules = [H.feature_module(f"copilot.graph.{name}") for name in GRAPH_MODULES]
    modules.append(H.feature_module("copilot.graph.contracts", "copilot.graph"))
    try:
        modules.append(H.feature_module("copilot.graph.factory"))
    except BaseException:  # noqa: BLE001 — factory may live on the package itself
        pass

    protocol_classes = [
        obj
        for mod in modules
        for obj in vars(mod).values()
        if isinstance(obj, type) and getattr(obj, "_is_protocol", False)
    ]
    assert protocol_classes, (
        "the supervisor/workers/critic must sit behind typing.Protocol interfaces "
        "(Stub/Real swap) — no Protocol classes found in copilot.graph.*"
    )

    # Keyless sanity: the env fixture blanked every provider key.
    settings = H.make_settings()
    assert getattr(settings, "anthropic_api_key", "") == "", "env fixture broken?"

    doc_id = H.seed_document()

    async def one_run():
        recorder = H.RecordingObs()
        graph = H.build_graph(settings=settings, observability=recorder)
        result = await H.run_graph(graph, H.make_task(H.BOTH_QUESTION, [doc_id]))
        verification = H.find_verification_result(result)
        pairs = [(p["from_agent"], p["to_agent"]) for p in H.handoff_pairs(recorder, result)]
        return result, verification, pairs

    result_a, verification_a, pairs_a = await one_run()
    result_b, verification_b, pairs_b = await one_run()

    assert pairs_a, "the keyless stub graph must actually route work (no handoffs seen)"
    assert pairs_a == pairs_b, (
        f"stub routing must be deterministic across runs: {pairs_a} vs {pairs_b}"
    )
    assert verification_a.action == verification_b.action, (
        "stub verification outcome must be deterministic across runs: "
        f"{verification_a.action} vs {verification_b.action}"
    )
    assert [c.text for c in verification_a.claims] == [c.text for c in verification_b.claims], (
        "stub claim set must be deterministic across runs"
    )

    # An answer surface must exist on the result (whatever its field name).
    for candidate in ("answer", "text", "message", "content"):
        value = getattr(result_a, candidate, None)
        if isinstance(value, str) and value.strip():
            answer_a = value
            answer_b = getattr(result_b, candidate, None)
            assert answer_a == answer_b, "stub answers must be deterministic across runs"
            break
    else:
        H.fail(
            "the graph result must expose the drafted answer text "
            "(answer/text/message/content) — none found"
        )
