"""An `unsafe_action` flag must withhold the ANSWER, not just drop the claim.

Guards a residual found while wiring the critic's verdict into the serve path.
Dropping a rejected claim removes it from the served evidence and the access
trail — but the model's PROSE (`result.answer`) may still narrate it. For an
`unsafe_action` that is the whole danger: the physician still reads "give 10x the
insulin dose", now merely unfootnoted. Removing the citation while serving the
suggestion is worse than useless — it launders the recommendation.

`narrative_inconsistency` needs no escalation: dropping the unsupported claim
contains it.
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
