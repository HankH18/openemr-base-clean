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

from copilot.config import get_settings
from copilot.domain.contracts import Claim, MemoryFileSummary, VerificationResult
from copilot.domain.primitives import (
    DocumentCitation,
    FhirReference,
    GuidelineCitation,
    PatientId,
    ResourceType,
    utcnow,
)
from copilot.memory.db import session_scope
from copilot.memory.repository import MemoryRepository
from copilot.verification.core import (
    DocumentFact,
    Verifier,
    build_context_from_resources,
)
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

    Fail-closed: if a citation's source cannot be re-materialized (a FHIR
    read raises or returns nothing; a document fact / guideline chunk is
    missing from the agent store), that source is absent from the context,
    so its claim fails attribution rather than passing.
    """
    resources: list[Mapping[str, Any]] = []
    fetched: set[tuple[ResourceType, str]] = set()
    doc_citations: list[DocumentCitation] = []
    guideline_citations: list[GuidelineCitation] = []
    for claim in claims:
        ref = claim.source_ref
        # The three citation variants re-materialize from different stores: the
        # fhir variant re-fetches a live FHIR resource by id; document/guideline
        # variants re-read the agent store (labs/guidelines are not FHIR-writable
        # — the agent DB is authoritative). Each is dropped fail-closed by the
        # gate when its source cannot be re-materialized. NB: ref is statically
        # the fhir variant (SkipValidation), so the non-fhir guards read as
        # unreachable to mypy but fire for real non-fhir citations at runtime.
        if isinstance(ref, DocumentCitation):
            doc_citations.append(ref)
            continue
        if isinstance(ref, GuidelineCitation):
            guideline_citations.append(ref)
            continue
        if not isinstance(ref, FhirReference):
            continue
        key = (ref.resource_type, ref.resource_id)
        if key in fetched:
            continue
        fetched.add(key)
        resource = await _safe_read(fhir_client, ref.resource_type, ref.resource_id)
        if resource is not None:
            resources.append(resource)

    settings = get_settings()
    context = build_context_from_resources(
        resources,
        document_facts=await _materialize_document_facts(doc_citations),
        guideline_chunks=await _materialize_guideline_chunks(guideline_citations),
        doc_confidence_threshold=settings.doc_extraction_confidence_threshold,
    )
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


async def _materialize_document_facts(
    citations: list[DocumentCitation],
) -> dict[str, DocumentFact]:
    """Re-fetch each cited ``extracted_fact`` from the agent store, keyed by fact id.

    Agent-store authoritative: a document-cited claim is re-checked against the
    stored, schema-validated fact, not a FHIR resource. A citation whose row is
    absent (or unreadable) is simply left out of the map, so the gate drops the
    claim fail-closed.
    """
    facts: dict[str, DocumentFact] = {}
    for citation in citations:
        if citation.field_or_chunk_id in facts:
            continue
        fact = await _read_document_fact(citation.source_id, citation.field_or_chunk_id)
        if fact is not None:
            facts[citation.field_or_chunk_id] = fact
    return facts


async def _read_document_fact(source_id: str, fact_id: str) -> DocumentFact | None:
    """One ``extracted_fact`` row re-fetched by (source_document id, fact id).

    Routed through ``MemoryRepository.get_extracted_fact_by_id``, whose join binds
    the fact to its cited source document, so a fact id pointing at a different
    document does not ground the claim. Fail-closed: a bad id or any DB error
    resolves to ``None`` (source absent → claim dropped).
    """
    ids = _as_int_pair(source_id, fact_id)
    if ids is None:
        return None
    source_int, fact_int = ids
    try:
        async with session_scope() as session:
            row = await MemoryRepository(session).get_extracted_fact_by_id(
                fact_int, source_int
            )
            if row is None:
                return None
            return DocumentFact(
                value=row.value,
                supported=row.supported,
                match_confidence=row.match_confidence,
            )
    except Exception:
        return None


async def _materialize_guideline_chunks(
    citations: list[GuidelineCitation],
) -> dict[str, str]:
    """Re-fetch each cited ``guideline_chunk``'s content, keyed by chunk id.

    A citation whose chunk is absent (or unreadable) is left out of the map, so
    the gate drops the claim fail-closed.
    """
    chunks: dict[str, str] = {}
    for citation in citations:
        if citation.field_or_chunk_id in chunks:
            continue
        content = await _read_guideline_chunk(citation.source_id, citation.field_or_chunk_id)
        if content is not None:
            chunks[citation.field_or_chunk_id] = content
    return chunks


async def _read_guideline_chunk(source_id: str, chunk_id: str) -> str | None:
    """One ``guideline_chunk``'s content re-fetched by (document id, chunk id).

    Routed through ``MemoryRepository.get_guideline_chunk_by_id``, which scopes the
    chunk to its cited guideline document. Fail-closed: a bad id or any DB error
    resolves to ``None``.
    """
    ids = _as_int_pair(source_id, chunk_id)
    if ids is None:
        return None
    source_int, chunk_int = ids
    try:
        async with session_scope() as session:
            row = await MemoryRepository(session).get_guideline_chunk_by_id(
                chunk_int, source_int
            )
            if row is None:
                return None
            return row.content
    except Exception:
        return None


def _as_int_pair(a: str, b: str) -> tuple[int, int] | None:
    """Parse two citation id strings to ints; ``None`` if either is non-numeric."""
    try:
        return int(a), int(b)
    except (TypeError, ValueError):
        return None


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
