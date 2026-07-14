"""Intake-extractor worker — the graph's document-ingestion node.

Wraps the F3 document pipeline (:func:`copilot.documents.attach_and_extract` /
:class:`~copilot.documents.DocumentIngestionService`). Given an
:class:`~copilot.graph.contracts.AgentTask` whose ``document_ids`` name
already-ingested source documents, the worker surfaces each document's stored
extraction confidence + supported-fact count so the finalize step can ground on
them. When the deployment is keyed for a real vision model, the Real variant can
additionally ingest fresh bytes through ``attach_and_extract``; keyless
deployments select the deterministic Stub.

Stub/Real live behind the :class:`IntakeExtractor` Protocol; ``build_intake_extractor``
picks one on API-key presence (keyless → Stub, so the graph runs with no key).
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
    """The swappable intake-extractor surface (Stub/Real behind this Protocol)."""

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


class StubIntakeExtractor:
    """Deterministic, keyless intake-extractor.

    Surfaces the stored extraction confidence for the task's referenced
    documents; runs no model and makes no outbound call.
    """

    async def run(self, task: AgentTask) -> IntakeReport:
        return await _read_extractions(task.document_ids)


class RealIntakeExtractor:
    """Keyed intake-extractor — wraps the real vision-model document pipeline.

    For a task that references already-ingested documents it surfaces their
    stored extraction (identical to the Stub); :meth:`ingest_content` is the
    ``attach_and_extract`` entry point for ingesting fresh bytes with the
    configured vision model.
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
    """Keyless settings → the deterministic Stub; a key → the Real extractor."""
    if not settings.anthropic_api_key:
        return StubIntakeExtractor()
    return RealIntakeExtractor(settings)
