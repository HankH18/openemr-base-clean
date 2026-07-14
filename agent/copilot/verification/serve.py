"""Serve-time verification entry point.

``verify_memory_file`` (in ``core.py``) gates the Poller's write path: it
verifies claims against the context the poller already pulled.  At serve
time — when the chat handler is about to stream an answer — we cannot
trust that cached context; the record may have changed since synthesis.
So ``verify_answer`` re-fetches every cited resource **live, by ID** and
runs the identical deterministic ``Verifier`` over the answer's claims.

Fail-closed is the whole point: a claim whose cited resource cannot be
re-fetched (the read raises, or comes back empty) is treated as
unverifiable and dropped — never "assumed true on error".  Because the
gate keys attribution on resources present in the freshly-built context,
an un-fetchable citation simply fails attribution, and a set of claims
that all fail collapses to ``action == withheld`` per ``core.py``'s
policy.

This module is the missing serve-time *caller*; the gate itself lives in
``core.py`` and ``rules.py`` and is reused verbatim — no logic is
duplicated here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from copilot.domain.contracts import Claim, MemoryFileSummary, VerificationResult
from copilot.domain.primitives import FhirReference, PatientId, ResourceType, utcnow
from copilot.verification.core import Verifier, build_context_from_resources
from copilot.verification.rules import default_rules


class ResourceReader(Protocol):
    """The slice of ``FhirClient`` serve-time verification needs.

    Structural: the real ``FhirClient`` satisfies it, and tests can pass
    an in-memory fake with the same ``read`` shape.
    """

    async def read(self, resource_type: ResourceType, resource_id: str) -> dict[str, Any]: ...


class EntailmentChecker(Protocol):
    """Optional narrative-drift check, mirrored from ``LlmEntailment``."""

    async def entails(self, claim: Claim, resource: Mapping[str, Any]) -> bool: ...


async def verify_answer(
    claims: list[Claim],
    patient_id: PatientId,
    fhir_client: ResourceReader,
    entailment: EntailmentChecker | None = None,
) -> VerificationResult:
    """Re-verify a chat answer's claims against a live re-fetch.

    For each uniquely-cited ``(resource_type, resource_id)`` in ``claims``,
    fetch the resource fresh via ``fhir_client.read`` and build the same
    ``VerificationContext`` the ``Verifier`` consumes.  Then run the
    deterministic gate + domain rules over the claims and return the
    ``VerificationResult`` unchanged (``served`` / ``degraded`` /
    ``withheld`` as ``core.py`` defines them).

    Fail-closed: if a citation's resource cannot be fetched (the read
    raises or returns nothing), that resource is absent from the context,
    so its claim fails attribution rather than passing.
    """
    resources: list[Mapping[str, Any]] = []
    fetched: set[tuple[ResourceType, str]] = set()
    for claim in claims:
        ref = claim.source_ref
        # Only the fhir citation variant has a live resource to re-fetch. A
        # document/guideline citation is left out of the context, so the gate
        # marks it unverifiable and drops it (fail-closed) — see core.py. NB:
        # ref is statically the fhir variant (SkipValidation), so this guard reads
        # as unreachable to mypy but fires for real non-fhir citations at runtime.
        if not isinstance(ref, FhirReference):
            continue
        key = (ref.resource_type, ref.resource_id)
        if key in fetched:
            continue
        fetched.add(key)
        resource = await _safe_read(fhir_client, ref.resource_type, ref.resource_id)
        if resource is not None:
            resources.append(resource)

    context = build_context_from_resources(resources)
    verifier = Verifier(rules=default_rules(), entailment=entailment)
    # The Verifier's only public entry point takes a MemoryFileSummary; at
    # serve time we wrap the answer's claims in a transient one purely as a
    # carrier (verify_memory_file reads only `.claims`). This reuses the
    # gate verbatim without reimplementing it — see module docstring.
    summary = _as_summary(claims, patient_id)
    return await verifier.verify_memory_file(summary, context)


async def _safe_read(
    fhir_client: ResourceReader, resource_type: ResourceType, resource_id: str
) -> dict[str, Any] | None:
    """Fetch one resource, swallowing any failure into a fail-closed ``None``.

    A raised exception or an empty/falsy body both mean "unverifiable" —
    the caller leaves the resource out of the context so its claim fails.
    """
    try:
        resource = await fhir_client.read(resource_type, resource_id)
    except Exception:
        return None
    if not resource:
        return None
    return resource


def _as_summary(claims: list[Claim], patient_id: PatientId) -> MemoryFileSummary:
    """Wrap answer claims in a transient summary to feed the Verifier.

    Only ``claims`` is read by ``verify_memory_file``; the remaining fields
    are valid-but-inert placeholders.
    """
    now = utcnow()
    return MemoryFileSummary(
        patient_id=patient_id,
        claims=list(claims),
        acuity_score=0.0,
        rank_reason="serve-time answer verification",
        synthesized_at=now,
        source_watermark=now,
        content_hash="serve-time",
    )
