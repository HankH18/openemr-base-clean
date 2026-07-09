"""The deterministic gate: attribution + numeric-value match.

For each claim in the memory-file summary:

1. **Attribution.** The claim's ``source_ref`` must point at a resource
   present in the verification context (memory-file synthesis) or
   fetchable by ID (serve-time).  If not → the claim fails.
2. **Value match.** Extract the value at ``source_ref.field`` from the
   source resource.  Compare to ``source_ref.value`` verbatim.  Then
   pull every numeric literal out of ``claim.text`` and require each to
   appear either in that resource's flattened text or in its numeric
   fields.  This catches "we said 2.34 but the record shows 0.23".

The gate is intentionally **not promptable**: a claim injected via a
free-text note field still has to point at a resource and match a
value.  Fabrications fail attribution or value match every time.

Domain rules are separate (`rules.py`) — they don't gate the claim,
they surface findings that must be shown to the user regardless.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from copilot.domain.contracts import (
    Claim,
    MemoryFileSummary,
    VerificationAction,
    VerificationClaimResult,
    VerificationDomainFlag,
    VerificationResult,
)
from copilot.domain.primitives import ResourceType

# Match integers or decimals: 42, 0.02, 2.34, 128, 3.375
_NUM_RE = re.compile(r"\b\d+(?:\.\d+)?\b")


@dataclass(frozen=True)
class VerificationContext:
    """The resources available for verification.

    Keyed by (ResourceType, id).  Built by ``build_context_from_resources``
    from what the poller already pulled — at serve time the API layer
    builds it from live fetches instead.
    """

    resources_by_key: Mapping[tuple[ResourceType, str], Mapping[str, Any]]


def build_context_from_resources(
    resources: Iterable[Mapping[str, Any]],
) -> VerificationContext:
    """Turn a list of raw FHIR resource dicts into a verification context."""
    indexed: dict[tuple[ResourceType, str], Mapping[str, Any]] = {}
    for res in resources:
        rtype = res.get("resourceType")
        rid = res.get("id")
        if not isinstance(rtype, str) or not isinstance(rid, str):
            continue
        try:
            key = (ResourceType(rtype), rid)
        except ValueError:
            continue
        indexed[key] = res
    return VerificationContext(resources_by_key=indexed)


def extract_field_value(resource: Mapping[str, Any], path: str) -> str | None:
    """Read a dotted FHIRPath-ish path out of a resource.

    Supports dotted access + numeric ``[N]`` indexing:
    ``code.coding[0].code``, ``valueQuantity.value``.  Returns the value
    stringified, or None if any segment misses.
    """
    node: Any = resource
    # Break "valueQuantity.value" into ["valueQuantity", "value"]
    segments = path.split(".")
    for seg in segments:
        # Peel [N] indices off the segment (may be chained: name[0].given[1]).
        match = re.match(r"^([^\[]+)(\[\d+\])*$", seg)
        if match is None:
            return None
        key = match.group(1)
        if isinstance(node, Mapping):
            if key not in node:
                return None
            node = node[key]
        else:
            return None
        for idx_str in re.findall(r"\[(\d+)\]", seg):
            idx = int(idx_str)
            if not isinstance(node, list) or idx >= len(node):
                return None
            node = node[idx]
    if node is None:
        return None
    return str(node)


def extract_numbers(text: str) -> list[str]:
    """Return every integer/decimal literal that appears in ``text``."""
    return _NUM_RE.findall(text)


class Verifier:
    """Runs the deterministic gate + domain rules over a memory file.

    Optional ``entailment`` runs a Claude entailment pass per claim
    (narrative-drift catch).  Absent an API key we skip it; deterministic
    checks are always applied.
    """

    def __init__(
        self,
        *,
        rules: Sequence[Any] = (),
        entailment: Any | None = None,
    ) -> None:
        # `rules` is a sequence of callables; typed loosely to avoid a
        # circular import with `rules.py`. Real type: Sequence[DomainRule].
        self._rules = tuple(rules)
        self._entailment = entailment

    async def verify_memory_file(
        self, summary: MemoryFileSummary, context: VerificationContext
    ) -> VerificationResult:
        """Run the gate over every claim in a proposed summary."""
        claim_results: list[VerificationClaimResult] = []
        for claim in summary.claims:
            claim_results.append(await self._verify_claim(claim, context))

        domain_flags: list[VerificationDomainFlag] = []
        for rule in self._rules:
            domain_flags.extend(rule(context))

        return _to_result(claim_results, domain_flags)

    async def _verify_claim(
        self, claim: Claim, context: VerificationContext
    ) -> VerificationClaimResult:
        ref = claim.source_ref
        key = (ref.resource_type, ref.resource_id)
        resource = context.resources_by_key.get(key)
        if resource is None:
            return VerificationClaimResult(
                text=claim.text,
                source_ref=ref,
                attribution_ok=False,
                value_match=False,
                reason=f"cited resource {ref.resource_type.value}/{ref.resource_id} not found",
            )

        extracted = extract_field_value(resource, ref.field)
        if extracted is None:
            return VerificationClaimResult(
                text=claim.text,
                source_ref=ref,
                attribution_ok=True,
                value_match=False,
                reason=f"field {ref.field!r} not found in cited resource",
            )
        if not _values_equal(extracted, ref.value):
            return VerificationClaimResult(
                text=claim.text,
                source_ref=ref,
                attribution_ok=True,
                value_match=False,
                reason=(f"value mismatch at {ref.field}: source={extracted!r} claim={ref.value!r}"),
            )

        # Numeric literals in the claim text must appear in the resource.
        missing = _numbers_not_in_resource(claim.text, resource)
        if missing:
            return VerificationClaimResult(
                text=claim.text,
                source_ref=ref,
                attribution_ok=True,
                value_match=False,
                reason=(
                    "numeric literal(s) in claim text absent from source: " + ", ".join(missing)
                ),
            )

        entailment_ok = await self._entailment_check(claim, resource)
        return VerificationClaimResult(
            text=claim.text,
            source_ref=ref,
            attribution_ok=True,
            value_match=True,
            entailment=entailment_ok,
        )

    async def _entailment_check(self, claim: Claim, resource: Mapping[str, Any]) -> bool | None:
        """Optional LLM entailment.  Returns None when not configured."""
        if self._entailment is None:
            return None
        return await self._entailment.entails(claim, resource)


# --- helpers ---------------------------------------------------------------


def _to_result(
    claim_results: list[VerificationClaimResult],
    domain_flags: list[VerificationDomainFlag],
) -> VerificationResult:
    """Compute the `action` per the fail-closed policy.

    - Every claim passes ⇒ ``served``.
    - No claims passed ⇒ ``withheld`` (nothing to say we can prove).
    - Mixed ⇒ ``degraded``: pass claims survive, fail claims dropped.
      Domain flags are surfaced regardless.
    """
    if not claim_results:
        # Special case: a memory file with no claims — treat as served
        # so the caller can still expose domain flags.
        return VerificationResult(
            passed=True,
            claims=[],
            domain_flags=domain_flags,
            action=VerificationAction.served,
        )
    passed_count = sum(1 for r in claim_results if r.attribution_ok and r.value_match)
    if passed_count == len(claim_results):
        return VerificationResult(
            passed=True,
            claims=claim_results,
            domain_flags=domain_flags,
            action=VerificationAction.served,
        )
    if passed_count == 0:
        return VerificationResult(
            passed=False,
            claims=claim_results,
            domain_flags=domain_flags,
            action=VerificationAction.withheld,
        )
    return VerificationResult(
        passed=False,
        claims=claim_results,
        domain_flags=domain_flags,
        action=VerificationAction.degraded,
    )


def _values_equal(source: str, claimed: str) -> bool:
    """Compare stringified values; when both parse as numbers, compare exactly."""
    if source == claimed:
        return True
    try:
        return float(source) == float(claimed)
    except (TypeError, ValueError):
        return False


def _numbers_not_in_resource(text: str, resource: Mapping[str, Any]) -> list[str]:
    numbers = extract_numbers(text)
    if not numbers:
        return []
    haystack = _flatten_to_text(resource)
    missing: list[str] = []
    for n in numbers:
        # 128 must appear as 128 (or 128.0), but not inside 1280 — use
        # word-boundary-ish check.
        if _number_appears(n, haystack):
            continue
        missing.append(n)
    return missing


def _flatten_to_text(node: Any, out: list[str] | None = None) -> str:
    """Flatten every string/number leaf to a single searchable string."""
    if out is None:
        out = []
    if isinstance(node, Mapping):
        for v in node.values():
            _flatten_to_text(v, out)
    elif isinstance(node, list):
        for v in node:
            _flatten_to_text(v, out)
    elif isinstance(node, bool) or node is not None:
        out.append(str(node))
    return " ".join(out) if out is not None else ""


def _number_appears(needle: str, haystack: str) -> bool:
    """Does ``needle`` (e.g. '2.34') appear as a standalone token in haystack?

    Numeric equality: '2.34' matches '2.34', '2.340'; also '128' matches
    '128' but not '1280'.  Uses `float()` compare on every numeric token.
    """
    try:
        want = float(needle)
    except ValueError:
        return False
    for tok in re.findall(r"\b\d+(?:\.\d+)?\b", haystack):
        try:
            if float(tok) == want:
                return True
        except ValueError:
            continue
    return False
