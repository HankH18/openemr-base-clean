"""An `unsafe_action` flag must withhold the ANSWER, not just drop the claim.

Guards a residual found while wiring the critic's verdict into the serve path.
Dropping a rejected claim removes it from the served evidence and the access
trail — but the model's PROSE (`result.answer`) may still narrate it. For an
`unsafe_action` that is the whole danger: the physician still reads "give 10x the
insulin dose", now merely unfootnoted. Removing the citation while serving the
suggestion is worse than useless — it launders the recommendation.

`narrative_inconsistency` needs no escalation: dropping the unsupported claim
contains it.

Posture, stated plainly: the critic screens the machine-generated claim CHIPS,
not the free-text answer narrative. The narrative is trusted narration, not
hard-gated, and a dedicated prose-screening pass is Week-3 scope. What this test
pins is the whole-turn withhold that keeps a flagged claim from surviving as
unfootnoted prose — the safety comes from withholding the turn, not from
screening the sentence.
"""

from __future__ import annotations

from copilot.graph.contracts import CriticVerdict
from copilot.graph.critic import NARRATIVE_INCONSISTENCY, UNSAFE_ACTION, _flagged_reasons


def test_unsafe_subset_is_separated_from_ordinary_rejections() -> None:
    v = CriticVerdict(accepted=["safe"], rejected=["bad", "wrong"], unsafe=["bad"])
    assert v.unsafe == ["bad"]
    assert "bad" in v.rejected, "unsafe claims are still rejected, not a parallel channel"


def test_verdict_defaults_leave_the_keyless_path_untouched() -> None:
    # StubCritic and every deterministic partition must produce no unsafe subset.
    assert CriticVerdict(accepted=["a"], rejected=[]).unsafe == []


def test_reason_survives_the_tool_payload() -> None:
    payload = {"flagged": [{"index": 0, "reason": UNSAFE_ACTION},
                           {"index": 1, "reason": NARRATIVE_INCONSISTENCY}]}
    assert _flagged_reasons(payload, 2) == {0: UNSAFE_ACTION, 1: NARRATIVE_INCONSISTENCY}


def test_missing_or_unknown_reason_degrades_to_unsafe() -> None:
    # A flag we cannot classify is not a flag we may serve — degrade strict.
    assert _flagged_reasons({"flagged": [{"index": 0}]}, 1) == {0: UNSAFE_ACTION}
    assert _flagged_reasons({"flagged": [{"index": 0, "reason": 12}]}, 1) == {0: UNSAFE_ACTION}
    assert _flagged_reasons({"flagged": [0]}, 1) == {0: UNSAFE_ACTION}
    # The case the name promised and the body used to omit: an unknown STRING.
    # Non-strings (12, None, missing) are what an LLM never emits; a free-text
    # string is what it actually returns, and it must degrade strict too.
    assert _flagged_reasons({"flagged": [{"index": 0, "reason": "weird_reason"}]}, 1) == {
        0: UNSAFE_ACTION
    }
    assert _flagged_reasons({"flagged": [{"index": 0, "reason": None}]}, 1) == {0: UNSAFE_ACTION}
    assert _flagged_reasons({"flagged": [{"index": 0, "reason": ""}]}, 1) == {0: UNSAFE_ACTION}


def test_out_of_range_and_bool_indices_are_still_ignored() -> None:
    # Pre-existing hardening must survive the reason plumbing.
    assert _flagged_reasons({"flagged": [{"index": 9}]}, 1) == {}
    assert _flagged_reasons({"flagged": [{"index": True}]}, 2) == {}
    assert _flagged_reasons({"flagged": "nope"}, 1) == {}


def test_real_critic_marks_unsafe_but_not_inconsistent() -> None:
    # The end-to-end reason plumbing: only the unsafe_action flag lands in `unsafe`.
    from typing import Any

    from copilot.config import Settings
    from copilot.graph.critic import RealCritic

    class _Block:
        def __init__(self) -> None:
            self.type = "tool_use"
            self.name = "flag_claims"
            self.input: dict[str, Any] = {
                "flagged": [
                    {"index": 0, "reason": UNSAFE_ACTION},
                    {"index": 1, "reason": NARRATIVE_INCONSISTENCY},
                ]
            }

    class _Msgs:
        def create(self, **_: Any) -> Any:
            return type("R", (), {"content": [_Block()]})()

    class _Client:
        messages = _Msgs()

    cite = {"source_type": "fhir", "source_id": "1"}
    claims = [
        {"text": "give 10x insulin", "citation": cite},
        {"text": "lactate is 4.2", "citation": cite},
        {"text": "uncited thing", "citation": None},
    ]
    verdict = RealCritic(Settings(anthropic_api_key="sk-t"), client=_Client()).review(claims)

    assert verdict.unsafe == ["give 10x insulin"], "only the unsafe_action claim escalates"
    assert "lactate is 4.2" in verdict.rejected, "inconsistent claim is still rejected"
    assert "lactate is 4.2" not in verdict.unsafe, "but it must not force a withhold"
    assert "uncited thing" in verdict.rejected, "the citation gate is untouched"
    assert verdict.accepted == [], "both cited claims were flagged"


def test_a_reason_with_an_explanation_appended_still_withholds() -> None:
    """The exact live hazard: an LLM given a free-text field explains itself.

    Measured before the fix — the inversion is the whole finding::

        reason='unsafe_action'                  -> withholds=True
        reason='unsafe_action: dose is 10x max' -> withholds=False   <- the likely output
        reason='UNSAFE_ACTION'                  -> withholds=False
        reason='unsafe action'                  -> withholds=False
        reason=12                               -> withholds=True
        reason=None                             -> withholds=True

    ``12`` and ``None`` — which a model never emits — withheld correctly, while every
    plausible string variant failed open. The classifier only degraded NON-strings to
    strict, and a string is the only type that actually arrives. So the gating model
    would flag a dangerous claim, the claim's citation would be stripped, ``unsafe``
    would stay empty, and the physician would read "Give 10x the insulin dose" with
    its evidence quietly removed — what this file's own header calls laundering the
    recommendation.
    """
    for reason in (
        "unsafe_action: dose is 10x max",  # the model explains itself
        "UNSAFE_ACTION",  # wrong case
        "unsafe action",  # space, not underscore
        "  unsafe_action  ",  # stray whitespace
    ):
        assert _flagged_reasons({"flagged": [{"index": 0, "reason": reason}]}, 1) == {
            0: UNSAFE_ACTION
        }, f"a near-miss reason must fail CLOSED, got lenient for {reason!r}"


def test_the_one_lenient_case_is_still_lenient() -> None:
    # The regression guard. Without it, "withhold everything" passes every test above
    # for entirely the wrong reason — and withholding every narrative_inconsistency
    # would withhold ANSWERS that only needed a claim dropped.
    assert _flagged_reasons({"flagged": [{"index": 0, "reason": NARRATIVE_INCONSISTENCY}]}, 1) == {
        0: NARRATIVE_INCONSISTENCY
    }
    # ...and case/whitespace variants of it are still recognised as the mild case.
    assert _flagged_reasons({"flagged": [{"index": 0, "reason": " Narrative_Inconsistency "}]}, 1) == {
        0: NARRATIVE_INCONSISTENCY
    }


def test_narrative_inconsistency_with_an_explanation_degrades_strict() -> None:
    # Deliberate and worth stating: an explained narrative_inconsistency is NOT
    # matched leniently. That withholds an answer which only needed a claim dropped —
    # annoying, but it is the fail-CLOSED direction, and safe-but-annoying beats
    # unsafe-but-quiet. Pinned so a future change has to argue with it rather than
    # drift into leniency.
    assert _flagged_reasons(
        {"flagged": [{"index": 0, "reason": "narrative_inconsistency: claim 2 unsupported"}]}, 1
    ) == {0: UNSAFE_ACTION}
