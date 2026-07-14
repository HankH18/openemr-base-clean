"""feat_graph criterion 6 — chat-service contract preserved (no hot-path migration).

FROZEN GOALS. The graph returns the same Week-1
``copilot.domain.contracts.VerificationResult`` to the chat service, and the
"no grounded claims -> withheld" override still LIVES in ``chat/service.py``
and still FIRES: an ungroundable question through ChatService (keyless stub
agent + fake OpenEMR) is withheld with zero claims and an honest answer.
"""

from __future__ import annotations

import inspect

import _graph_helpers as H

UNGROUNDABLE_QUESTION = "What did the patient's MRI brain show?"  # absent from the fixtures


async def test_graph_06_graph_returns_verification_result_and_withheld_override_fires():
    H.feature_module("copilot.graph")  # gate: the feature package must exist

    from copilot.domain.contracts import VerificationAction, VerificationResult

    # (a) The graph's result carries the unchanged Week-1 VerificationResult.
    doc_id = H.seed_document()
    graph = H.build_graph(observability=H.RecordingObs())
    result = await H.run_graph(graph, H.make_task(H.BOTH_QUESTION, [doc_id]))
    verification = H.find_verification_result(result)
    assert isinstance(verification, VerificationResult)
    assert isinstance(verification.action, VerificationAction)

    # (b) The override still LIVES in chat/service.py (no hot-path migration).
    import copilot.chat.service as chat_service

    source = inspect.getsource(chat_service)
    assert "withheld" in source, (
        "the 'no grounded claims -> withheld' override must remain in "
        "copilot/chat/service.py — it appears to have been migrated out"
    )

    # (c) ...and still FIRES through the chat service.
    from copilot.domain.primitives import ClinicianId, PatientId
    from copilot.observability import NoopObservability

    service = chat_service.ChatService(H.make_settings(), NoopObservability())
    reply = await service.chat(
        ClinicianId(value=42),
        PatientId(value=H.PATIENT_ID),
        UNGROUNDABLE_QUESTION,
        correlation_id="acc-graph-crit6-cid1",
    )
    assert reply.action == VerificationAction.withheld, (
        "an ungroundable question must be withheld through the chat path; got "
        f"{reply.action!r} with claims {[c.text for c in reply.claims]}"
    )
    assert reply.claims == [], "a withheld reply must expose zero claims"
    assert reply.answer and reply.answer.strip(), (
        "a withheld reply must still say something honest (surface uncertainty)"
    )
