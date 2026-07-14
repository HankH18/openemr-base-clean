"""feat_graph criterion 4 — deterministic critic gate; verifier stays in path.

FROZEN GOALS. The critic (keyless stub — but the citation check is
deterministic code in any variant) rejects a drafted claim that carries no
machine-readable citation and accepts an identical claim that does; two
identical reviews agree byte-for-byte. The critic AUGMENTS the deterministic
verifier: a stub graph run still carries the Week-1 VerificationResult (the
verifier was not replaced).
"""

from __future__ import annotations

import _graph_helpers as H

CITED_MARKER = "CITED-CLAIM-ACC-4A"
UNCITED_MARKER = "UNCITED-CLAIM-ACC-4B"

CITED = {
    "text": f"{CITED_MARKER}: begin a continuous intravenous insulin infusion per the DKA guideline",
    "citation": {
        "source_type": "guideline",
        "source_id": "gd-1",
        "page_or_section": "insulin-therapy",
        "field_or_chunk_id": "chunk-1",
        "quote_or_value": "begin a continuous intravenous insulin infusion",
    },
}
UNCITED = {
    "text": f"{UNCITED_MARKER}: the patient should receive empiric anticoagulation",
    "citation": None,
}


async def _review(critic, claims):
    fn = getattr(critic, "review", None) or getattr(critic, "critique", None)
    if fn is None and callable(critic):
        fn = critic
    if fn is None:
        H.fail("the critic must expose review(claims) (pinned surface)")
    try:
        verdict = fn(list(claims))
    except TypeError:
        verdict = fn(claims=list(claims))
    import inspect

    if inspect.isawaitable(verdict):
        verdict = await verdict
    return verdict


def _bucket(verdict, names: tuple[str, ...], what: str) -> str:
    for name in names:
        value = getattr(verdict, name, None)
        if value is not None:
            return str(value)
    H.fail(
        f"CriticVerdict must expose its {what} claims (one of {list(names)}); "
        f"got {type(verdict).__name__} with fields "
        f"{sorted(getattr(type(verdict), 'model_fields', {}) or vars(verdict))}"
    )


async def test_graph_04_critic_rejects_uncited_claims_deterministically():
    build_critic = H.feature_attr(
        ("copilot.graph.critic", "copilot.graph.factory", "copilot.graph"),
        ("build_critic",),
        "the build_critic factory",
    )
    verdict_cls = H.feature_attr(
        ("copilot.graph.contracts", "copilot.graph.critic", "copilot.graph"),
        ("CriticVerdict",),
        "the CriticVerdict contract",
    )
    critic = build_critic(H.make_settings())

    verdict = await _review(critic, [CITED, UNCITED])
    assert isinstance(verdict, verdict_cls), (
        f"review() must return a CriticVerdict, got {type(verdict).__name__}"
    )

    rejected = _bucket(verdict, ("rejected", "rejected_claims", "rejections"), "rejected")
    accepted = _bucket(verdict, ("accepted", "accepted_claims", "approved"), "accepted")

    assert UNCITED_MARKER in rejected, (
        "a drafted claim WITHOUT a machine-readable citation must be rejected — "
        f"rejected bucket was {rejected!r}"
    )
    assert UNCITED_MARKER not in accepted, "the uncited claim leaked into the accepted bucket"
    assert CITED_MARKER in accepted, (
        f"the properly cited claim must be accepted — accepted bucket was {accepted!r}"
    )
    assert CITED_MARKER not in rejected, "the cited claim was wrongly rejected"

    # Deterministic: an identical review returns the identical verdict.
    verdict_again = await _review(critic, [CITED, UNCITED])
    assert _bucket(verdict_again, ("rejected", "rejected_claims", "rejections"), "rejected") == rejected
    assert _bucket(verdict_again, ("accepted", "accepted_claims", "approved"), "accepted") == accepted

    # The critic AUGMENTS the verifier: a full stub run still carries the
    # deterministic VerificationResult in its result.
    doc_id = H.seed_document()
    graph = H.build_graph(observability=H.RecordingObs())
    result = await H.run_graph(graph, H.make_task(H.BOTH_QUESTION, [doc_id]))
    H.find_verification_result(result)  # fails the test if the verifier left the path
