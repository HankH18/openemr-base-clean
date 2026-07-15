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

from dataclasses import dataclass
from typing import Protocol

from copilot.config import Settings
from copilot.domain.primitives import PatientId
from copilot.graph.contracts import AgentTask
from copilot.memory.db import session_scope
from copilot.memory.repository import MemoryRepository


@dataclass(frozen=True)
class IntakeReport:
    """What the intake-extractor produced for one task."""

    document_ids: list[str]
    fact_count: int
    extraction_confidence: float


class IntakeExtractor(Protocol):
    """The intake-extractor surface (one implementation behind this Protocol)."""

    async def run(self, task: AgentTask) -> IntakeReport: ...


async def _read_extractions(document_ids: list[str]) -> IntakeReport:
    """Latest extraction confidence + supported-fact count for ingested docs.

    Read-only, deterministic: for each document id, the newest ``extraction``
    row's ``confidence_overall`` is averaged and its supported facts counted. An
    unparseable or unknown id is skipped (contributes nothing), so an empty /
    bogus id set yields ``0.0`` confidence and ``0`` facts — never an error.
    """
    confidences: list[float] = []
    fact_count = 0
    async with session_scope() as session:
        repo = MemoryRepository(session)
        for raw in document_ids:
            try:
                document_id = int(raw)
            except (TypeError, ValueError):
                continue
            extraction = await repo.get_latest_extraction(document_id)
            if extraction is None:
                continue
            if extraction.confidence_overall is not None:
                confidences.append(extraction.confidence_overall)
            supported = await repo.get_supported_extracted_facts(extraction.id)
            fact_count += len(supported)
    confidence = sum(confidences) / len(confidences) if confidences else 0.0
    return IntakeReport(
        document_ids=list(document_ids), fact_count=fact_count, extraction_confidence=confidence
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
        return await _read_extractions(task.document_ids)

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
