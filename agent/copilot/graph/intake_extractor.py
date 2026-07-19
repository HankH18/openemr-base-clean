"""Intake-extractor worker — the graph's document-ingestion node.

Wraps the F3 document pipeline (:func:`copilot.documents.attach_and_extract` /
:class:`~copilot.documents.DocumentIngestionService`). Given an
:class:`~copilot.graph.contracts.AgentTask` whose ``document_ids`` name
already-ingested source documents, the worker surfaces each document's stored
extraction confidence + supported-fact count so the finalize step can ground on
them. :meth:`~DocumentIntakeExtractor.ingest_content` is the ``attach_and_extract``
entry point for ingesting fresh bytes with the configured vision model.

There is ONE worker class behind the :class:`IntakeExtractor` Protocol — the
keyed vs keyless distinction lives entirely in the wrapped F3 vision pipeline
(the real Claude-vision extractor when keyed, the deterministic stub extractor
otherwise, selected inside ``attach_and_extract`` / ``build_vision``). Reading
stored extractions is deterministic regardless of key, so there is nothing for a
Real/Stub split at this layer to differentiate. ``build_intake_extractor`` builds
the worker; the whole read path runs with no key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from copilot.config import Settings
from copilot.domain.documents import ExtractedFact
from copilot.domain.primitives import PatientId
from copilot.graph.contracts import AgentTask
from copilot.memory.db import session_scope
from copilot.memory.models import ExtractedFactRow
from copilot.memory.repository import MemoryRepository


@dataclass(frozen=True)
class IntakeReport:
    """What the intake-extractor produced for one task.

    ``facts`` are the supported facts themselves — the worker's actual output,
    which finalize hands to the answering agent. ``fact_count`` stays the count
    of supported facts (``len(facts)`` may be smaller: a stored fact with no
    value cannot be materialized into an :class:`ExtractedFact`), so the metric
    keeps reporting what the extractor found rather than what survived
    rendering.
    """

    document_ids: list[str]
    fact_count: int
    extraction_confidence: float
    facts: list[ExtractedFact] = field(default_factory=list)


class IntakeExtractor(Protocol):
    """The intake-extractor surface (one implementation behind this Protocol)."""

    async def run(self, task: AgentTask) -> IntakeReport: ...


def _to_fact(row: ExtractedFactRow) -> ExtractedFact | None:
    """Materialize a stored fact row as the typed domain DTO (``None`` if it can't).

    ``extracted_fact.value`` is nullable in the schema but required on
    :class:`ExtractedFact` — a fact with no value carries nothing an answer could
    use, so it is dropped rather than coerced into an empty string.
    """
    if row.value is None:
        return None
    return ExtractedFact(
        field_path=row.field_path,
        value=row.value,
        unit=row.unit,
        reference_range=row.reference_range,
        abnormal=row.abnormal_flag,
        collection_date=row.collection_date,
        page_no=row.page_no,
        bbox=list(row.bbox) if row.bbox is not None else None,
        match_confidence=row.match_confidence,
        supported=row.supported,
    )


async def _read_extractions(document_ids: list[str], patient_id: int) -> IntakeReport:
    """Latest extraction confidence + the supported facts for ingested docs.

    Read-only, deterministic: for each document id, the newest ``extraction``
    row's ``confidence_overall`` is averaged and its supported facts collected.
    An unparseable or unknown id is skipped (contributes nothing), so an empty /
    bogus id set yields ``0.0`` confidence and no facts — never an error.

    PATIENT-SCOPED. The chat route authorizes only the turn's own patient
    (``req.patient_id``); the ``document_ids`` ride in UNAUTHORIZED, so a
    document naming any OTHER patient must contribute nothing. Each id is
    resolved to its source document and skipped unless it belongs to
    ``patient_id`` — the SAME boundary the document read route enforces with
    ``is_authorized(cid, doc.patient_id)``. Without this a clinician on patient
    A's turn could pass patient B's document id and read B's facts with no
    authorization for B (the facts would egress as A's turn and be absent from
    B's audit).

    The facts are sorted by ``(field_path, value)`` because the repository query
    imposes no ordering: the graph feeds them to the answering agent, so a
    stable order is what makes the same task produce the same answer.
    """
    confidences: list[float] = []
    fact_count = 0
    facts: list[ExtractedFact] = []
    async with session_scope() as session:
        repo = MemoryRepository(session)
        for raw in document_ids:
            try:
                document_id = int(raw)
            except (TypeError, ValueError):
                continue
            # Patient-scope the read before any fact is touched: an id belonging
            # to a different patient (or to no document at all) contributes
            # nothing. Mirrors the document route's authorization boundary.
            document = await repo.get_source_document(document_id)
            if document is None or document.patient_id != patient_id:
                continue
            extraction = await repo.get_latest_extraction(document_id)
            if extraction is None:
                continue
            if extraction.confidence_overall is not None:
                confidences.append(extraction.confidence_overall)
            supported = await repo.get_supported_extracted_facts(extraction.id)
            fact_count += len(supported)
            facts.extend(fact for row in supported if (fact := _to_fact(row)) is not None)
    confidence = sum(confidences) / len(confidences) if confidences else 0.0
    facts.sort(key=lambda f: (f.field_path, f.value))
    return IntakeReport(
        document_ids=list(document_ids),
        fact_count=fact_count,
        extraction_confidence=confidence,
        facts=facts,
    )


class DocumentIntakeExtractor:
    """Surfaces stored extractions and can ingest fresh bytes via the F3 pipeline.

    :meth:`run` reads the stored extraction for a task's referenced documents
    (deterministic, no model call). :meth:`ingest_content` ingests fresh document
    bytes through ``attach_and_extract`` — whose vision step is the real
    Claude-vision extractor when keyed and the deterministic stub otherwise, so
    the keyed/keyless distinction is resolved in the wrapped pipeline, not here.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def run(self, task: AgentTask) -> IntakeReport:
        return await _read_extractions(task.document_ids, task.patient_id)

    async def ingest_content(
        self,
        *,
        patient_id: PatientId,
        content: bytes,
        doc_type: str = "lab_pdf",
        filename: str | None = None,
        correlation_id: str = "",
    ) -> str:
        """Ingest fresh document bytes via the F3 pipeline; return the row id."""
        from copilot.documents import attach_and_extract

        result = await attach_and_extract(
            patient_id=patient_id,
            content=content,
            doc_type=doc_type,
            filename=filename,
            correlation_id=correlation_id,
            settings=self._settings,
        )
        return str(result.source_document_id)


def build_intake_extractor(settings: Settings) -> IntakeExtractor:
    """Build the intake-extractor; the keyed/keyless split lives in the pipeline."""
    return DocumentIntakeExtractor(settings)
