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
3. **Unit match.** A number is not a fact: "2.34 ng/mL" against a record
   of 2.34 ng/L matches on value and is a thousand-fold error.  So when
   the claim grounded a ``source_ref.unit``, the unit is re-extracted from
   the live resource and must match too (see ``_units_equal`` for why no
   equivalence table is applied), or the claim fails.

   **Policy — a claim with NO unit is not gated on units.**  A ``None``
   unit short-circuits the check entirely, exactly as a ``None``
   ``timestamp`` short-circuits the temporal gate: nothing was grounded,
   so there is nothing to re-verify.  The alternative — failing every
   unit-less claim against a record that has a unit — would withhold every
   lab answer the product gives, including every claim persisted before
   ``unit`` existed, which rehydrates with ``unit=None``.  The cost is
   real and stated plainly: this gate constrains claims that DO carry a
   unit, and cannot catch a unit error in a claim that asserts no unit.
   Closing that half is the grounding layer's job, not the gate's — every
   quantity claim must ground its unit (``grounding.extract_unit``) so
   that ``None`` means "no unit in the record", never "unit dropped on the
   way here".

The gate is intentionally **not promptable**: a claim injected via a
free-text note field still has to point at a resource and match a
value.  Fabrications fail attribution or value match every time.

Domain rules are separate (`rules.py`) — they don't gate the claim,
they surface findings that must be shown to the user regardless.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from copilot.domain.contracts import (
    Claim,
    MemoryFileSummary,
    VerificationAction,
    VerificationClaimResult,
    VerificationDomainFlag,
    VerificationResult,
)
from copilot.domain.primitives import (
    DocumentCitation,
    FhirReference,
    GuidelineCitation,
    ResourceType,
)

# Match integers or decimals: 42, 0.02, 2.34, 128, 3.375
_NUM_RE = re.compile(r"\b\d+(?:\.\d+)?\b")


@dataclass(frozen=True)
class DocumentFact:
    """A re-materialized ``extracted_fact`` row, the unit of document grounding.

    The agent store — not FHIR — is authoritative for extracted document facts
    (a scanned lab is not a FHIR-writable resource). The serve-time caller
    re-fetches the stored row by (source_document id, extracted_fact id) and
    hands the checker exactly the three fields document grounding turns on:
    the verbatim ``value``, the no-invention ``supported`` gate, and the
    reconciled ``match_confidence`` compared against the configured floor.
    """

    value: str | None
    supported: bool
    match_confidence: float | None


@dataclass(frozen=True)
class VerificationContext:
    """The resources available for verification.

    ``resources_by_key`` holds live FHIR resources keyed by (ResourceType, id).
    Built by ``build_context_from_resources`` from what the poller already
    pulled — at serve time the API layer builds it from live fetches instead.

    ``document_facts`` (keyed by ``extracted_fact`` id) and ``guideline_chunks``
    (chunk id → chunk content) carry the Week-2 non-fhir grounding sources the
    serve-time caller re-materialized from the agent store. ``doc_confidence_-
    threshold`` is the reconciliation-confidence floor a document fact must meet
    to ground a claim. All three default empty/zero so the fhir-only paths that
    build a context are unchanged.
    """

    resources_by_key: Mapping[tuple[ResourceType, str], Mapping[str, Any]]
    document_facts: Mapping[str, DocumentFact] = field(default_factory=dict)
    guideline_chunks: Mapping[str, str] = field(default_factory=dict)
    doc_confidence_threshold: float = 0.0


def build_context_from_resources(
    resources: Iterable[Mapping[str, Any]],
    *,
    document_facts: Mapping[str, DocumentFact] | None = None,
    guideline_chunks: Mapping[str, str] | None = None,
    doc_confidence_threshold: float = 0.0,
) -> VerificationContext:
    """Turn a list of raw FHIR resource dicts into a verification context.

    ``document_facts`` / ``guideline_chunks`` (both optional) supply the Week-2
    non-fhir grounding sources; fhir-only callers omit them and get the same
    context they always did.
    """
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
    return VerificationContext(
        resources_by_key=indexed,
        document_facts=document_facts or {},
        guideline_chunks=guideline_chunks or {},
        doc_confidence_threshold=doc_confidence_threshold,
    )


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
        # Week-2 grounding for the two non-fhir citation variants. Each is
        # re-materialized from the AGENT store (never FHIR) by the serve-time
        # caller and re-checked here against that stored row; a source that could
        # not be re-materialized is absent from the context, so the claim fails
        # attribution and is dropped (fail-closed) — never served as if proven.
        if isinstance(ref, DocumentCitation):
            return _verify_document_claim(claim, ref, context)
        if isinstance(ref, GuidelineCitation):
            return _verify_guideline_claim(claim, ref, context)
        if not isinstance(ref, FhirReference):
            # An unknown citation variant cannot be grounded — fail closed.
            source_type = getattr(ref, "source_type", None)
            kind = str(getattr(source_type, "value", source_type) or "non-fhir")
            return VerificationClaimResult(
                text=claim.text,
                source_ref=ref,
                attribution_ok=False,
                value_match=False,
                reason=f"unverifiable: unknown {kind} citation variant",
            )

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

        # Unit match — the value gate above compares NUMBERS; a number is not a
        # fact. "2.34 ng/mL" against a record of 2.34 ng/L matches on value and is
        # a thousand-fold error, so the dimension must be re-checked too. Same
        # extractor the grounding used, so an honest claim agrees byte-for-byte.
        # Imported locally to break the grounding<->core module cycle.
        if ref.unit is not None:
            from copilot.agent.grounding import extract_unit

            refetched_unit = extract_unit(resource)
            if refetched_unit is None or not _units_equal(refetched_unit, ref.unit):
                return VerificationClaimResult(
                    text=claim.text,
                    source_ref=ref,
                    attribution_ok=True,
                    value_match=False,
                    reason=(
                        f"unit mismatch at {ref.field}: source={refetched_unit!r} "
                        f"claim={ref.unit!r}"
                    ),
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

        # Temporal grounding — only when the claim carried a clinical timestamp.
        # A None timestamp short-circuits entirely, so every timestamp-less claim
        # is untouched (nothing was grounded, so there is nothing to re-verify).
        # When present, the SAME extractor grounding used must re-derive an equal
        # instant from the live re-fetch, or we fail closed. Import locally to
        # break the grounding<->core module cycle (grounding reads via this
        # module's extract_field_value).
        if ref.timestamp is not None:
            from copilot.agent.grounding import extract_temporal

            refetched = extract_temporal(resource)
            parsed = _parse_temporal(refetched) if refetched is not None else None
            if parsed is None or parsed != _to_utc(ref.timestamp):
                return VerificationClaimResult(
                    text=claim.text,
                    source_ref=ref,
                    attribution_ok=True,
                    value_match=False,
                    reason=(
                        f"temporal drift at {ref.field}: source={refetched!r} "
                        f"claim={ref.timestamp.isoformat()!r}"
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
        # `_entailment` is typed `Any` (loose, to keep the collaborator optional);
        # its `entails` contract returns bool. Cast the parsed result precisely.
        return cast("bool", await self._entailment.entails(claim, resource))


# --- non-fhir grounding -----------------------------------------------------


def _verify_document_claim(
    claim: Claim, citation: DocumentCitation, context: VerificationContext
) -> VerificationClaimResult:
    """Ground a document-cited claim against its stored ``extracted_fact``.

    Agent-store authoritative (a scanned lab is not FHIR-writable): the claim
    passes only when the serve-time caller re-materialized the fact row, that
    fact is ``supported``, its reconciled ``match_confidence`` is at/above the
    configured floor, and the claimed value equals the stored value. Any miss
    yields ``value_match=False`` so the fail-closed policy drops the claim.

    ``source_ref`` is passed as ``claim.source_ref`` so the result carries the
    citation verbatim — see ``VerificationClaimResult``.
    """
    fact = context.document_facts.get(citation.field_or_chunk_id)
    if fact is None:
        return VerificationClaimResult(
            text=claim.text,
            source_ref=claim.source_ref,
            attribution_ok=False,
            value_match=False,
            reason=(
                f"document fact {citation.field_or_chunk_id} could not be "
                "re-materialized from the agent store"
            ),
        )
    if not fact.supported:
        return VerificationClaimResult(
            text=claim.text,
            source_ref=claim.source_ref,
            attribution_ok=True,
            value_match=False,
            reason="stored extracted_fact is unsupported (value not located on the page)",
        )
    if fact.match_confidence is None or fact.match_confidence < context.doc_confidence_threshold:
        return VerificationClaimResult(
            text=claim.text,
            source_ref=claim.source_ref,
            attribution_ok=True,
            value_match=False,
            reason=(
                f"reconciliation confidence {fact.match_confidence} below floor "
                f"{context.doc_confidence_threshold}"
            ),
        )
    if fact.value is None or not _values_equal(fact.value, citation.quote_or_value):
        return VerificationClaimResult(
            text=claim.text,
            source_ref=claim.source_ref,
            attribution_ok=True,
            value_match=False,
            reason=f"value mismatch: stored={fact.value!r} claim={citation.quote_or_value!r}",
        )
    return VerificationClaimResult(
        text=claim.text,
        source_ref=claim.source_ref,
        attribution_ok=True,
        value_match=True,
    )


def _verify_guideline_claim(
    claim: Claim, citation: GuidelineCitation, context: VerificationContext
) -> VerificationClaimResult:
    """Ground a guideline-cited claim: the quote must appear verbatim in the chunk.

    The claim passes only when the serve-time caller re-materialized the cited
    ``guideline_chunk`` and the citation's ``quote_or_value`` occurs verbatim in
    that chunk's content. A missing chunk or an absent quote drops the claim
    fail-closed.
    """
    content = context.guideline_chunks.get(citation.field_or_chunk_id)
    if content is None:
        return VerificationClaimResult(
            text=claim.text,
            source_ref=claim.source_ref,
            attribution_ok=False,
            value_match=False,
            reason=(
                f"guideline chunk {citation.field_or_chunk_id} could not be "
                "re-materialized from the agent store"
            ),
        )
    if citation.quote_or_value not in content:
        return VerificationClaimResult(
            text=claim.text,
            source_ref=claim.source_ref,
            attribution_ok=True,
            value_match=False,
            reason="quoted text does not appear verbatim in the cited guideline chunk",
        )
    return VerificationClaimResult(
        text=claim.text,
        source_ref=claim.source_ref,
        attribution_ok=True,
        value_match=True,
    )


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


def _to_utc(dt: datetime) -> datetime:
    """Treat a naive datetime as UTC so aware/naive instants compare safely."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _parse_temporal(raw: str) -> datetime | None:
    """Parse an ISO timestamp (tolerating a trailing ``Z``) to an aware UTC datetime.

    Comparing instants (not raw strings) is deliberate: the claim's ``timestamp``
    was coerced to a ``datetime`` at grounding time, so ``Z`` vs ``+00:00`` or a
    differing precision must not read as drift and withhold an honest claim.
    """
    try:
        return _to_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


def _values_equal(source: str, claimed: str) -> bool:
    """Compare stringified values; when both parse as numbers, compare exactly."""
    if source == claimed:
        return True
    try:
        return float(source) == float(claimed)
    except (TypeError, ValueError):
        return False


def _units_equal(source: str, claimed: str) -> bool:
    """Compare unit strings: exact match after trimming surrounding whitespace.

    **Deliberately NOT case-folded, and deliberately without an equivalence
    table.** UCUM — the unit code system FHIR's ``Quantity`` uses — is
    case-sensitive by design: ``m`` is milli- but ``M`` is mega-, so ``mg`` is a
    milligram and ``Mg`` a megagram, a 1e9 difference. Case-folding units would
    therefore manufacture exactly the class of silent magnitude error this gate
    exists to catch.

    Trimming surrounding whitespace is the only normalization applied: it is
    lossless (padding carries no unit semantics). Nothing else is normalized.
    Recognizing ``ng/mL`` ≡ ``nanogram per milliliter``, or converting ``ng/L``
    to ``ng/mL``, needs a real UCUM parser and conversion table; hand-rolling one
    here would put invented equivalences inside the product's central safety
    mechanism, and a WRONG equivalence is strictly worse than the bug — it
    would launder a unit error into a "verified" claim. Until such a table is
    sourced from a real UCUM library, an unrecognized-but-equivalent spelling
    fails closed: the claim is withheld, which is safe, not served wrongly.

    Both sides are grounded by the same extractor from the same field, so an
    honest claim matches exactly; a mismatch means the claimed unit did not come
    from this record.
    """
    return source.strip() == claimed.strip()


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
