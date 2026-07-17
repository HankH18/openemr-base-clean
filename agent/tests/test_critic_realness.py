"""RealCritic keyed behaviour — genuine LLM safety pass over the citation gate.

The keyed critic runs an injectable LLM client (here a fake, so the suite stays
keyless and deterministic). These lock the four invariants that make the keyed
path safe:

(a) the deterministic citation gate is never loosened — an uncited claim is
    always rejected, no matter what the LLM says;
(b) the LLM pass can ADDITIONALLY reject a cited-but-flagged claim;
(c) the LLM pass never promotes a rejected claim to accepted;
(d) any LLM-client error falls back to the pure deterministic partition.
"""

from __future__ import annotations

from typing import Any

from copilot.config import Settings
from copilot.domain.contracts import Claim
from copilot.domain.primitives import FhirReference, ResourceType
from copilot.graph.contracts import CriticVerdict
from copilot.graph.critic import RealCritic, StubCritic

# --- fake Anthropic client (mirrors the sync .messages.create surface) -------


class _FakeBlock:
    def __init__(self, tool_input: dict[str, Any]) -> None:
        self.type = "tool_use"
        self.name = "flag_claims"
        self.input = tool_input


class _FakeResponse:
    def __init__(self, flagged: list[int]) -> None:
        self.content = [
            _FakeBlock({"flagged": [{"index": i, "reason": "unsafe_action"} for i in flagged]})
        ]


class _FakeMessages:
    def __init__(self, flagged: list[int], *, raises: bool) -> None:
        self._flagged = flagged
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        if self._raises:
            raise RuntimeError("simulated LLM outage")
        return _FakeResponse(self._flagged)


class _FakeClient:
    """Stands in for ``anthropic.Anthropic`` — only ``messages.create`` is used."""

    def __init__(self, *, flagged: list[int] | None = None, raises: bool = False) -> None:
        self.messages = _FakeMessages(flagged or [], raises=raises)


def _keyed() -> Settings:
    # A key marks the settings as keyed; the injected fake client means no real
    # Anthropic client is ever constructed.
    return Settings(anthropic_api_key="sk-live", voyage_api_key="", cohere_api_key="")


# --- reviewable claims -------------------------------------------------------

CITED_A = {
    "text": "CITED-A: continue the patient's home metformin per the medication list",
    "citation": {"source_type": "guideline", "source_id": "gd-1"},
}
CITED_B = {
    "text": "CITED-B: give a 20-unit IV insulin bolus now",
    "citation": {"source_type": "guideline", "source_id": "gd-2"},
}
UNCITED = {
    "text": "UNCITED: start empiric broad-spectrum antibiotics",
    "citation": None,
}


# --- (a) the citation gate is never loosened --------------------------------


def test_keyed_critic_still_rejects_every_uncited_claim() -> None:
    # Even when the LLM flags nothing (says "all fine"), the uncited claim is
    # rejected by the deterministic gate — the LLM cannot rescue it.
    critic = RealCritic(_keyed(), client=_FakeClient(flagged=[]))

    verdict = critic.review([CITED_A, UNCITED])

    assert isinstance(verdict, CriticVerdict)
    assert any("UNCITED" in t for t in verdict.rejected)
    assert not any("UNCITED" in t for t in verdict.accepted)
    assert any("CITED-A" in t for t in verdict.accepted)


def test_keyed_critic_rejects_uncited_even_if_llm_tries_to_accept_it() -> None:
    # A fake that flags out-of-range / bogus indices must not disturb the gate:
    # the sole cited claim (index 0) is untouched, the uncited claim stays out.
    critic = RealCritic(_keyed(), client=_FakeClient(flagged=[1, 99]))

    verdict = critic.review([CITED_A, UNCITED])

    assert verdict.accepted == [CITED_A["text"]]
    assert verdict.rejected == [UNCITED["text"]]


# --- (b) the LLM pass can additionally reject a cited-but-flagged claim ------


def test_keyed_critic_additionally_rejects_flagged_cited_claim() -> None:
    # Both claims are cited (so both pass the gate); the LLM flags index 1 as an
    # unsafe action, demoting CITED-B from accepted to rejected.
    fake = _FakeClient(flagged=[1])
    critic = RealCritic(_keyed(), client=fake)

    verdict = critic.review([CITED_A, CITED_B])

    assert verdict.accepted == [CITED_A["text"]]
    assert verdict.rejected == [CITED_B["text"]]
    # The LLM pass actually ran and used the cheap gating model, tool-forced.
    assert fake.messages.calls, "the LLM safety pass never called the client"
    call = fake.messages.calls[0]
    assert call["model"] == _keyed().anthropic_model_gating
    assert call["tool_choice"] == {"type": "tool", "name": "flag_claims"}


def test_keyed_critic_flags_domain_claim_by_source_ref() -> None:
    # A domain Claim is cited via source_ref; the LLM can flag it just the same.
    claim = Claim(
        text="administer 100 units of insulin now",
        source_ref=FhirReference(
            resource_type=ResourceType.Observation,
            resource_id="obs-1",
            field="valueQuantity.value",
            value="0.9",
        ),
    )
    critic = RealCritic(_keyed(), client=_FakeClient(flagged=[0]))

    verdict = critic.review([claim])

    assert verdict.accepted == []
    assert verdict.rejected == ["administer 100 units of insulin now"]


# --- (c) the LLM pass never promotes a rejected claim to accepted ------------


def test_keyed_critic_never_promotes_rejected_to_accepted() -> None:
    claims = [CITED_A, UNCITED, CITED_B]
    # Whatever the LLM returns (even flagging nothing), the accepted set can only
    # ever be a subset of the deterministic gate's accepted set.
    gate = StubCritic().review(claims)
    critic = RealCritic(_keyed(), client=_FakeClient(flagged=[]))

    verdict = critic.review(claims)

    assert set(verdict.accepted) <= set(gate.accepted)
    assert UNCITED["text"] not in verdict.accepted
    assert UNCITED["text"] in verdict.rejected
    # No claim is invented or moved out of the rejected pool by the LLM pass.
    assert set(gate.rejected) <= set(verdict.rejected)


# --- (d) an LLM-client exception falls back to the deterministic partition ---


def test_keyed_critic_fails_safe_to_deterministic_partition() -> None:
    claims = [CITED_A, UNCITED, CITED_B]
    deterministic = StubCritic().review(claims)
    critic = RealCritic(_keyed(), client=_FakeClient(raises=True))

    verdict = critic.review(claims)

    # A raising client must yield exactly the pure deterministic partition —
    # never a crash, never a loosened gate.
    assert verdict == deterministic
