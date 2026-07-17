"""Critic — the deterministic citation gate, with a keyed LLM safety pass on top.

The critic reviews the drafted claims and partitions them: a claim carrying a
machine-readable citation is accepted; a claim with none is rejected. That
citation check is pure, deterministic code and is authoritative in every variant
— it is *never* loosened. An uncited claim is always rejected, and no pass can
move a claim from rejected to accepted.

The keyed :class:`RealCritic` AUGMENTS that gate with a genuine LLM pass: it asks
the cheap gating model to flag any already-accepted (cited) claim that recommends
an unsafe action or is narratively inconsistent with its own citation, and
demotes only those from accepted to rejected. The pass is fail-safe — any LLM
error, timeout, or parse failure falls back to the pure deterministic partition,
so the graph never crashes and the gate is never relaxed. The keyless
:class:`StubCritic` is the deterministic gate alone.

The critic also AUGMENTS the deterministic serve-time verifier — it never
replaces it. The authoritative served/withheld decision remains the
:class:`~copilot.domain.contracts.VerificationResult` the verifier produces; the
verdict is advisory telemetry recorded alongside it.

Stub/Real sit behind the :class:`Critic` Protocol; ``build_critic`` selects on
API-key presence (keyless → Stub, keyed → Real).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from copilot.config import Settings
from copilot.domain.contracts import Claim
from copilot.graph.contracts import CriticVerdict

# A reviewable claim is either a domain Claim (its ``source_ref`` is the
# machine-readable citation) or a raw mapping with ``text`` + ``citation`` keys.
ReviewableClaim = Claim | Mapping[str, object]

_GATING_MAX_TOKENS = 1024
#: The two reasons the safety pass may give. ``unsafe_action`` is the strict one:
#: the claim's PROSE recommends something dangerous, so removing the claim is not
#: enough — the sentence must not reach a physician at all.
UNSAFE_ACTION = "unsafe_action"
NARRATIVE_INCONSISTENCY = "narrative_inconsistency"

_FLAG_TOOL_NAME = "flag_claims"

_CRITIC_SYSTEM = (
    "You are a clinical safety reviewer auditing drafted claims from a clinical "
    "assistant. Every claim you are shown ALREADY carries a machine-readable "
    "citation — do NOT judge whether a citation exists; that check has already "
    "passed and is not yours to relax.\n\n"
    "Flag a claim ONLY when one of these clearly holds:\n"
    "- unsafe_action: the claim recommends or implies a clinical action that "
    "would be unsafe, or that its citation does not actually support (e.g. a "
    "dose or therapy the cited source never states).\n"
    "- narrative_inconsistency: the claim's prose contradicts or misstates what "
    "its own citation says.\n\n"
    "When in doubt, do NOT flag — a cited claim is presumed acceptable. Call the "
    "flag_claims tool exactly once with the indices of the claims to flag (an "
    "empty list when none warrant flagging)."
)

_FLAG_TOOL: dict[str, Any] = {
    "name": _FLAG_TOOL_NAME,
    "description": (
        "Record the indices of cited claims that are unsafe or narratively "
        "inconsistent with their citation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "flagged": {
                "type": "array",
                "description": "Indices of claims to reject; empty when none.",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {
                            "type": "integer",
                            "description": "Zero-based index of the claim to flag.",
                        },
                        "reason": {
                            "type": "string",
                            "description": "unsafe_action or narrative_inconsistency.",
                        },
                    },
                    "required": ["index"],
                },
            }
        },
        "required": ["flagged"],
    },
}


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


def _citation_repr(claim: ReviewableClaim) -> str:
    """A stable, JSON-ish rendering of a claim's citation for the LLM prompt."""
    if isinstance(claim, Mapping):
        return json.dumps(claim.get("citation"), default=str, sort_keys=True)
    ref = getattr(claim, "source_ref", None)
    if ref is None:
        ref = getattr(claim, "citation", None)
    dump = getattr(ref, "model_dump", None)
    if callable(dump):
        try:
            return json.dumps(dump(mode="json"), default=str, sort_keys=True)
        except Exception:
            return str(ref)
    return json.dumps(ref, default=str, sort_keys=True)


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
    """Keyed critic — the deterministic citation gate plus an LLM safety pass.

    The citation gate is authoritative and unchanged: a claim with no
    machine-readable citation is always rejected, and the LLM pass can only
    demote an already-accepted (cited) claim, never rescue a rejected one. The
    LLM pass flags cited claims that recommend an unsafe action or contradict
    their own citation. It is fail-safe: any client error/timeout/parse failure
    falls back to the pure deterministic partition.

    ``client`` is injectable (constructor param) so tests run keyless and
    deterministic with a fake; when omitted, the synchronous Anthropic client is
    built from settings. The synchronous client mirrors the synchronous
    :meth:`review` contract the supervisor calls without awaiting.
    """

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self._settings = settings
        self._model = settings.anthropic_model_gating
        if client is not None:
            self._client: Any = client
        else:
            from anthropic import Anthropic  # local import keeps the stub path light

            self._client = Anthropic(api_key=settings.anthropic_api_key)

    def review(self, claims: Sequence[ReviewableClaim]) -> CriticVerdict:
        # 1. Deterministic citation gate — authoritative, never loosened.
        base = _partition(claims)
        if not base.accepted:
            # Nothing cited to scrutinize; the gate result stands as-is.
            return base
        # 2. LLM safety pass — may ADDITIONALLY reject an accepted (cited) claim.
        try:
            flagged = self._flag(claims)
        except Exception:
            # Fail-safe: any LLM error/timeout/parse failure keeps the gate.
            return base
        if not flagged:
            return base
        accepted = [text for index, text in enumerate(base.accepted) if index not in flagged]
        demoted = [text for index, text in enumerate(base.accepted) if index in flagged]
        # An unsafe_action flag condemns the PROSE, not just the citation, so the
        # chat service withholds the whole answer rather than serving a dangerous
        # sentence with its evidence quietly removed. Carry the subset that earned it.
        unsafe = [
            text
            for index, text in enumerate(base.accepted)
            if flagged.get(index) == UNSAFE_ACTION
        ]
        return CriticVerdict(
            accepted=accepted, rejected=[*base.rejected, *demoted], unsafe=unsafe
        )

    def _flag(self, claims: Sequence[ReviewableClaim]) -> dict[int, str]:
        """Indices into the accepted (cited) claims the LLM flagged for rejection.

        The cited-claim order is identical to ``_partition``'s accepted order
        (both select ``_has_citation`` claims in input order), so an index here
        maps directly onto ``CriticVerdict.accepted``.
        """
        cited = [
            (_claim_text(claim), _citation_repr(claim))
            for claim in claims
            if _has_citation(claim)
        ]
        response = self._client.messages.create(
            model=self._model,
            max_tokens=_GATING_MAX_TOKENS,
            system=_CRITIC_SYSTEM,
            tools=[_FLAG_TOOL],
            tool_choice={"type": "tool", "name": _FLAG_TOOL_NAME},
            messages=[{"role": "user", "content": _render_cited(cited)}],
        )
        payload = _flag_tool_input(response)
        if payload is None:
            return {}
        return _flagged_reasons(payload, len(cited))


def _render_cited(cited: list[tuple[str, str]]) -> str:
    """Render the cited claims as an indexed prompt block for the safety pass."""
    lines = [
        "Review these already-cited clinical claims. For each, decide whether it "
        "is unsafe or narratively inconsistent with its citation, then call "
        "flag_claims once with the indices to flag (empty list if none):",
        "",
    ]
    for index, (text, citation) in enumerate(cited):
        lines.append(f"[{index}] claim: {text}")
        lines.append(f"     citation: {citation}")
    return "\n".join(lines)


def _flag_tool_input(response: Any) -> dict[str, Any] | None:
    """The arguments of the forced ``flag_claims`` tool call, or ``None``."""
    for block in getattr(response, "content", []) or []:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == _FLAG_TOOL_NAME
        ):
            data = getattr(block, "input", None)
            if isinstance(data, dict):
                return data
    return None


def _flagged_indices(payload: dict[str, Any], count: int) -> set[int]:
    """Valid in-range indices from the tool payload; anything else is ignored."""
    return set(_flagged_reasons(payload, count))


def _flagged_reasons(payload: dict[str, Any], count: int) -> dict[int, str]:
    """Valid in-range indices mapped to the reason the model gave, if any.

    The reason is load-bearing, not commentary: ``unsafe_action`` means the claim's
    *prose* recommends something dangerous, so dropping the claim is not enough —
    the sentence itself must not reach a physician (see
    ``copilot.chat.service``'s unsafe-withhold). ``narrative_inconsistency`` is
    contained by dropping the claim. An unrecognised or missing reason degrades to
    the stricter reading (treated as unsafe): a flag we cannot classify is not a
    flag we may serve.
    """
    reasons: dict[int, str] = {}
    raw = payload.get("flagged")
    if not isinstance(raw, list):
        return reasons
    for item in raw:
        index: object = item.get("index") if isinstance(item, Mapping) else item
        # bool is an int subclass — exclude it so True/False are never indices.
        if isinstance(index, bool) or not isinstance(index, int):
            continue
        if not (0 <= index < count):
            continue
        reason = item.get("reason") if isinstance(item, Mapping) else None
        reasons[index] = reason if isinstance(reason, str) else UNSAFE_ACTION
    return reasons


def build_critic(settings: Settings) -> Critic:
    """Keyless settings → the Stub critic; a key → the Real critic."""
    if not settings.anthropic_api_key:
        return StubCritic()
    return RealCritic(settings)
