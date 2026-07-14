"""Critic — the graph's deterministic citation gate.

The critic reviews the drafted claims and partitions them: a claim carrying a
machine-readable citation is accepted; a claim with none is rejected. The check
is pure, deterministic code in every variant (Stub or Real), so two identical
reviews return the identical :class:`~copilot.graph.contracts.CriticVerdict`.

The critic AUGMENTS the deterministic serve-time verifier — it never replaces
it. The authoritative served/withheld decision remains the
:class:`~copilot.domain.contracts.VerificationResult` the verifier produces; the
verdict is advisory telemetry recorded alongside it.

Stub/Real sit behind the :class:`Critic` Protocol; ``build_critic`` selects on
API-key presence (keyless → Stub).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from copilot.config import Settings
from copilot.domain.contracts import Claim
from copilot.graph.contracts import CriticVerdict

# A reviewable claim is either a domain Claim (its ``source_ref`` is the
# machine-readable citation) or a raw mapping with ``text`` + ``citation`` keys.
ReviewableClaim = Claim | Mapping[str, object]


class Critic(Protocol):
    """The swappable critic surface (Stub/Real behind this Protocol)."""

    def review(self, claims: Sequence[ReviewableClaim]) -> CriticVerdict: ...


def _claim_text(claim: ReviewableClaim) -> str:
    if isinstance(claim, Mapping):
        return str(claim.get("text", ""))
    return str(getattr(claim, "text", ""))


def _has_citation(claim: ReviewableClaim) -> bool:
    """True when the claim carries a machine-readable citation.

    A mapping is cited when its ``citation`` key is present and non-null; a
    domain :class:`Claim` is cited when it carries a ``source_ref`` (the fhir /
    document / guideline citation the verifier grounds).
    """
    if isinstance(claim, Mapping):
        return claim.get("citation") is not None
    if getattr(claim, "citation", None) is not None:
        return True
    return getattr(claim, "source_ref", None) is not None


def _partition(claims: Sequence[ReviewableClaim]) -> CriticVerdict:
    """Deterministic accept/reject partition, preserving input order."""
    accepted: list[str] = []
    rejected: list[str] = []
    for claim in claims:
        text = _claim_text(claim)
        if _has_citation(claim):
            accepted.append(text)
        else:
            rejected.append(text)
    return CriticVerdict(accepted=accepted, rejected=rejected)


class StubCritic:
    """Deterministic, keyless critic — the citation gate, nothing else."""

    def review(self, claims: Sequence[ReviewableClaim]) -> CriticVerdict:
        return _partition(claims)


class RealCritic:
    """Keyed critic — the same deterministic citation gate.

    The citation check is deterministic in any variant (a claim with no
    machine-readable citation is always rejected); the keyed path is where an
    LLM narrative-consistency judgment would augment the gate. It never loosens
    the citation requirement.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def review(self, claims: Sequence[ReviewableClaim]) -> CriticVerdict:
        return _partition(claims)


def build_critic(settings: Settings) -> Critic:
    """Keyless settings → the Stub critic; a key → the Real critic."""
    if not settings.anthropic_api_key:
        return StubCritic()
    return RealCritic(settings)
