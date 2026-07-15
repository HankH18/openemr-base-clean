"""Claude-vision structured extraction behind a Protocol, stub-first.

Two implementations satisfy :class:`VisionExtractor`:

- :class:`StubVision` — deterministic, keyless; replays a recorded extraction
  that is consistent with the recorded OCR page (so it reconciles).
- :class:`ClaudeVision` — a real Anthropic vision call that forces a single tool
  whose input schema IS the strict extraction schema, then validates the tool
  arguments back through that schema.

Both return a strict :class:`LabReport` / :class:`IntakeForm`: extraction is a
*parse-don't-validate* boundary, so a malformed or partial payload raises a
``pydantic.ValidationError`` and is never coerced into a confident-but-wrong
extraction. ``build_vision`` returns the stub whenever there is no Anthropic key.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Protocol

from copilot.config import Settings
from copilot.documents.fixtures import STUB_INTAKE_FACTS, STUB_LAB_FACTS, STUB_MEDLIST_FACTS
from copilot.documents.raster import RasterizedPage
from copilot.domain.documents import (
    ExtractedFact,
    IntakeFact,
    IntakeForm,
    LabReport,
    MedicationFact,
    MedicationListDocument,
)

# The schema version stamped on every extraction row this extractor produces.
SCHEMA_VERSION = "w2-v1"

ExtractionResult = LabReport | IntakeForm | MedicationListDocument

_MAX_TOKENS = 4096

_EXTRACTION_PROMPT = (
    "You are extracting structured clinical facts from the attached scanned "
    "document page images. Call the record_extraction tool exactly once with the "
    "facts you can read verbatim from the page. Copy each value character-for-"
    "character from the page — never normalize, infer, or invent a value that is "
    "not printed. Omit anything you cannot read. For a patient-intake form, set "
    "each fact's category to the OpenEMR record type it belongs to: demographic "
    "(name/DOB/sex/contact), chief_complaint, medication, allergy, "
    "medical_problem, or family_history. For a medication list, record one fact "
    "per medication: copy the drug name verbatim as the value and its dose and "
    "frequency exactly as printed — never infer a medication, dose, or schedule "
    "that is not written on the page."
)


class DocumentType(StrEnum):
    """Closed set of ingestible document kinds — selects the extraction schema."""

    lab_pdf = "lab_pdf"
    intake_form = "intake_form"
    medication_list = "medication_list"


def parse_doc_type(raw: str) -> DocumentType:
    """Parse a raw doc-type string into the enum, defaulting to ``lab_pdf``."""
    try:
        return DocumentType(raw)
    except ValueError:
        return DocumentType.lab_pdf


def schema_for(
    doc_type: DocumentType,
) -> type[LabReport] | type[IntakeForm] | type[MedicationListDocument]:
    """The strict extraction schema for a document type — exhaustive ``match``."""
    match doc_type:
        case DocumentType.lab_pdf:
            return LabReport
        case DocumentType.intake_form:
            return IntakeForm
        case DocumentType.medication_list:
            return MedicationListDocument


class VisionExtractionError(RuntimeError):
    """The vision model produced no usable structured extraction."""


class VisionExtractor(Protocol):
    """Contract the ingestion pipeline depends on for structured extraction."""

    model_name: str

    async def extract(
        self, pages: Sequence[RasterizedPage], doc_type: DocumentType
    ) -> ExtractionResult:
        """Return a strict, schema-validated extraction for the page images."""
        ...


class StubVision:
    """Deterministic, keyless extractor — replays a recorded extraction.

    Ignores the page pixels (the recording *is* the output) so it is fully
    offline and reproducible; the recorded values are present in the recorded OCR
    page, so every fact reconciles to a bbox downstream.
    """

    model_name = "stub-vision"

    async def extract(
        self, pages: Sequence[RasterizedPage], doc_type: DocumentType
    ) -> ExtractionResult:
        match doc_type:
            case DocumentType.lab_pdf:
                return LabReport(facts=[ExtractedFact.model_validate(f) for f in STUB_LAB_FACTS])
            case DocumentType.intake_form:
                return IntakeForm(
                    facts=[IntakeFact.model_validate(f) for f in STUB_INTAKE_FACTS]
                )
            case DocumentType.medication_list:
                return MedicationListDocument(
                    facts=[MedicationFact.model_validate(f) for f in STUB_MEDLIST_FACTS]
                )


class ClaudeVision:
    """Anthropic vision extractor — tool-forced JSON validated through the schema.

    Refuses to construct without an API key. Not exercised on the keyless test
    path (``StubVision`` carries correctness there) but imports + type-checks
    cleanly. The tool's ``input_schema`` is the strict extraction schema, and the
    tool arguments are validated back through it, so a malformed extraction raises
    rather than coercing.
    """

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        if not settings.anthropic_api_key:
            raise VisionExtractionError("ANTHROPIC_API_KEY not set — ClaudeVision refuses to run.")
        self.model_name = settings.anthropic_model_vision
        if client is not None:
            self._client: Any = client
        else:
            from anthropic import AsyncAnthropic  # local import keeps the stub path light

            self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def extract(
        self, pages: Sequence[RasterizedPage], doc_type: DocumentType
    ) -> ExtractionResult:
        schema = schema_for(doc_type)
        content: list[dict[str, Any]] = [{"type": "text", "text": _EXTRACTION_PROMPT}]
        for page in pages:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(page.image).decode("ascii"),
                    },
                }
            )
        response = await self._client.messages.create(
            model=self.model_name,
            max_tokens=_MAX_TOKENS,
            tools=[
                {
                    "name": "record_extraction",
                    "description": "Record the facts read verbatim from the document.",
                    "input_schema": schema.model_json_schema(),
                }
            ],
            tool_choice={"type": "tool", "name": "record_extraction"},
            messages=[{"role": "user", "content": content}],
        )
        payload = _tool_input(response, "record_extraction")
        if payload is None:
            raise VisionExtractionError("vision model returned no structured extraction")
        # Strict validation — a wrong-typed or partial payload raises here.
        return schema.model_validate(payload)


def _tool_input(response: Any, name: str) -> dict[str, Any] | None:
    """The arguments of the named forced tool call, or ``None``."""
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == name:
            data = getattr(block, "input", None)
            if isinstance(data, dict):
                return data
    return None


def build_vision(settings: Settings) -> VisionExtractor:
    """Real Claude-vision extractor when an API key is set, else the keyless stub."""
    if not settings.anthropic_api_key:
        return StubVision()
    return ClaudeVision(settings)
