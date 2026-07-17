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

Why batch rather than refuse
----------------------------
``ClaudeVision.extract`` used to append EVERY page of a document as an image
into one ``messages.create`` call, with nothing bounding the count
(``_MAX_TOKENS`` bounds the model's OUTPUT; there was no input bound). Past the
API's per-request image limit that call simply fails — and it fails *after* the
rasterize + OCR work, so the whole ingest is wasted.

The bound is therefore per CALL, and a document longer than it is extracted in
batches and merged. It is deliberately NOT a refusal: a 60-page discharge
summary is ordinary clinical paperwork, and making it un-ingestible would be a
worse defect than the one being fixed. The two questions are genuinely
different — "how big a document do we accept?" belongs to the raster layer;
"how many images can one call carry?" is this one, and refusing here would
answer the wrong one.

**This is not a spend cap, and must not be sold as one.** Batching sends every
page exactly once, so cost still scales with page count — slightly worse, in
fact, since each batch re-sends the prompt and produces its own output tokens.
What it buys is a call that is *correct on its own terms* and an ingest that
fails at the document boundary rather than at the API. Bounding spend requires
bounding the DOCUMENT, which is the raster layer's page-count cap, not this.

Merging honestly costs something, and the cost is paid explicitly:

- Each call sees only part of the document, so a fact's ``page_no`` must survive
  batching. Every image is labelled with its TRUE page number and the model is
  told to copy that label; ``_check_page_numbers`` then refuses any fact naming
  a page the call did not carry. See its docstring for why a wrong ``page_no``
  is a safety problem and not a cosmetic one.
- Per-document header fields reach only the batch holding the page that printed
  them. ``_merge_headers`` lets present beat absent, and RAISES on a genuine
  conflict rather than picking a winner.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Protocol, get_args

from pydantic import BaseModel

from copilot.config import Settings
from copilot.documents.fixtures import STUB_INTAKE_FACTS, STUB_LAB_FACTS, STUB_MEDLIST_FACTS
from copilot.documents.raster import RasterizedPage
from copilot.domain.documents import (
    ExtractedFact,
    IntakeFact,
    IntakeForm,
    LabReport,
    MedicationListDocument,
)
from copilot.resilience import VISION_MAX_RETRIES, VISION_TIMEOUT

_logger = logging.getLogger(__name__)

# The schema version stamped on every extraction row this extractor produces.
SCHEMA_VERSION = "w2-v1"

ExtractionResult = LabReport | IntakeForm | MedicationListDocument

_MAX_TOKENS = 4096

_EXTRACTION_PROMPT = (
    "You are extracting structured clinical facts from the attached scanned "
    "document page images. Each image is preceded by a label giving its page "
    "number in the source document; these may not start at page 1, because a "
    "long document is extracted a few pages at a time. Set each fact's page_no "
    "to the labelled page number of the image you read it from — copy the "
    "label, never renumber the images from 1. "
    "Call the record_extraction tool exactly once with the "
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

#: Per-document schema fields (everything that is not ``facts``) — the header.
#: Merged across batches by :func:`_merge_headers`, which refuses a conflict.
_FACTS_KEY = "facts"


class DocumentType(StrEnum):
    """Closed set of ingestible document kinds — selects the extraction schema."""

    lab_pdf = "lab_pdf"
    intake_form = "intake_form"
    medication_list = "medication_list"


class UnknownDocumentTypeError(ValueError):
    """The requested document type is not an ingestible kind."""


def parse_doc_type(raw: str) -> DocumentType:
    """Parse a raw doc-type string into the enum; raise on an unknown kind.

    Parse, don't validate — and *fail loud at the source*. This used to default a
    unknown string to ``lab_pdf``, so a mistyped/renamed kind was silently
    extracted with the WRONG schema (an intake form parsed as a lab report) with
    no error anywhere. The HTTP route rejects unknown types too, but that is the
    sink; every caller of the service (CLI, background jobs, the graph's
    intake-extractor) reaches this parser instead, so the guarantee belongs here.
    """
    try:
        return DocumentType(raw)
    except ValueError as exc:
        expected = sorted(kind.value for kind in DocumentType)
        raise UnknownDocumentTypeError(
            f"unsupported doc_type {raw!r}; expected one of {expected}"
        ) from exc


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
                    facts=[IntakeFact.model_validate(f) for f in STUB_MEDLIST_FACTS]
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
        self._max_pages_per_call = settings.vision_max_pages_per_call
        if client is not None:
            self._client: Any = client
        else:
            from anthropic import AsyncAnthropic  # local import keeps the stub path light

            # Explicit, not inherited — the SDK default read timeout is 600s.
            # VISION_TIMEOUT is the loosest budget in the service (page images
            # are a large upload and extraction genuinely takes seconds); see
            # copilot.resilience for the ingestion SLO it is anchored to.
            self._client = AsyncAnthropic(
                api_key=settings.anthropic_api_key,
                timeout=VISION_TIMEOUT,
                max_retries=VISION_MAX_RETRIES,
            )

    async def extract(
        self, pages: Sequence[RasterizedPage], doc_type: DocumentType
    ) -> ExtractionResult:
        """Extract the document, in batches of at most ``vision_max_pages_per_call``.

        A document within the bound is ONE call, validated exactly as it always
        was. Past the bound it is extracted in batches and merged — refusing a
        60-page discharge summary would be a worse bug than the one being fixed
        (see the module docstring's "Why batch rather than refuse").

        Batches run sequentially, not concurrently: a long document trades
        latency for correctness rather than opening N simultaneous calls against
        the same rate limit. Every page is still sent exactly once, so this
        bounds the CALL, not the spend.
        """
        schema = schema_for(doc_type)
        batches = _batches(pages, self._max_pages_per_call)
        payloads = [await self._extract_batch(batch, schema) for batch in batches]
        if len(payloads) == 1:
            # The single-call path, unchanged: this is the pre-batching line.
            return schema.model_validate(payloads[0])
        # Strict validation of the MERGED document — deliberately once, at the
        # end. The floors it enforces (``facts`` min_length=1, and a medication
        # list's all-non-medication check) are statements about the DOCUMENT, not
        # about a page range: "pages 41-60 are the signature and disclaimer pages
        # and carry no facts" is an honest batch, and per-batch validation would
        # fail the whole ingest over it. Nothing is loosened — the merged payload
        # meets the same schema, so a real empty extraction still raises.
        return schema.model_validate(_merge(payloads, schema))

    async def _extract_batch(
        self,
        batch: Sequence[RasterizedPage],
        schema: type[LabReport] | type[IntakeForm] | type[MedicationListDocument],
    ) -> dict[str, Any]:
        """One model call over ``batch``; returns its cleaned, UNvalidated payload.

        Cleaning is exactly the single-call path's (``_destringify`` then
        ``_drop_extras``) — both are per-response behaviours and stay per-batch.
        Validation is the caller's, because the schema describes the document.
        """
        content: list[dict[str, Any]] = [{"type": "text", "text": _EXTRACTION_PROMPT}]
        for page in batch:
            # The page's TRUE number, stated to the model. Without it a second
            # batch has no way to know its images are pages 21-40 rather than
            # 1-20, and every fact in it would come back mis-numbered — silently
            # mis-verifying downstream, since reconciliation searches the page
            # ``fact.page_no`` names. Labelling images is also what Anthropic's
            # own multiple-image guidance recommends.
            content.append({"type": "text", "text": f"Page {page.page_no}:"})
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
        # Recover a JSON-stringified payload/facts before strict validation — dense
        # real documents occasionally make the model return ``facts`` (or the whole
        # object) as a JSON string instead of structured JSON.
        payload = _destringify(payload)
        # Drop keys the model invented (see ``_drop_extras``).
        payload = _drop_extras(payload, schema)
        # Never trust the echo: verify the model used the page labels.
        _check_page_numbers(payload, batch)
        return payload


def _batches(
    pages: Sequence[RasterizedPage], size: int
) -> list[Sequence[RasterizedPage]]:
    """Split ``pages`` into runs of at most ``size``, in page order.

    An empty page list yields ONE empty batch, not zero: ``extract([], ...)`` is
    a degenerate but supported call (it is how the tool contract is asserted),
    and it must still make exactly one model call rather than silently returning
    nothing.
    """
    if not pages:
        return [[]]
    return [pages[start : start + size] for start in range(0, len(pages), size)]


def _check_page_numbers(payload: dict[str, Any], batch: Sequence[RasterizedPage]) -> None:
    """Refuse a fact claiming a page this call did not carry.

    The model cannot have read a page that was not in front of it, so a
    ``page_no`` outside the batch is *provably* fabricated provenance — not a
    decoration like the extras ``_drop_extras`` forgives. It matters because
    ``page_no`` is load-bearing: ``pipeline._reconcile_facts`` searches the OCR
    tokens of the page a fact names (falling back to page 1), so a wrong number
    either loses a real match or, worse, blesses a value against the WRONG
    page's tokens and stores that page's bbox under this page's number — a
    citation pointing somewhere the value is not. Fail closed instead.

    Facts without a ``page_no`` are left alone: "I did not record which page"
    is honest, and is exactly what the single-call path has always allowed. A
    wrong-TYPED ``page_no`` is left to strict validation, which rejects it.
    """
    if not batch:
        return  # extract([]) — no pages, so no ground truth to check against
    allowed = {page.page_no for page in batch}
    facts = payload.get(_FACTS_KEY)
    if not isinstance(facts, list):
        return  # malformed — the schema is the authority on that, not this check
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        page_no = fact.get("page_no")
        # bool is an int in Python; strict validation rejects it, so skip it here.
        if page_no is None or isinstance(page_no, bool) or not isinstance(page_no, int):
            continue
        if page_no not in allowed:
            raise VisionExtractionError(
                f"vision model reported page_no={page_no} for a fact, but this call "
                f"carried only pages {sorted(allowed)}; the model is not following "
                "the page labels, so every fact in this batch has suspect provenance"
            )


def _merge(
    payloads: Sequence[dict[str, Any]],
    schema: type[LabReport] | type[IntakeForm] | type[MedicationListDocument],
) -> dict[str, Any]:
    """Combine per-batch payloads into one document payload.

    Facts concatenate in page order (each already carries its true ``page_no``,
    checked by :func:`_check_page_numbers`). Header fields are merged by
    :func:`_merge_headers`, which refuses to invent a winner for a conflict.
    """
    facts: list[Any] = []
    for index, payload in enumerate(payloads, start=1):
        got = payload.get(_FACTS_KEY)
        if not isinstance(got, list):
            raise VisionExtractionError(
                f"vision model returned a malformed {_FACTS_KEY!r} payload for page "
                f"batch {index} of {len(payloads)}: expected a list, got "
                f"{type(got).__name__}"
            )
        facts.extend(got)
    return {**_merge_headers(payloads), _FACTS_KEY: facts}


def _merge_headers(payloads: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Merge the per-document fields across batches; a conflict RAISES.

    A header field (``patient_name``, ``date_of_birth``, ``collected_at``, ...)
    is printed once, so it reaches only the batch holding that page; every other
    batch honestly reports ``None``. Absent therefore yields to present — that is
    not invention, it is the one batch that could read it doing so.

    Two batches reporting DIFFERENT values is the case that must never be
    silently resolved. Picking the first would be a coin flip with a clinical
    blast radius: the benign reading is that the model misread one, but the
    malignant one is that the upload is two patients' documents concatenated, and
    "first wins" would then file patient B's medications under patient A's name —
    the exact fabrication this pipeline's whole reconciliation layer exists to
    prevent. There is no evidence here for choosing, so this refuses to choose.

    The message names the field but NEVER the values: they are PHI, and this
    exception's text travels into logs and traces.
    """
    header: dict[str, Any] = {}
    for payload in payloads:
        for key, value in payload.items():
            if key == _FACTS_KEY or value is None:
                continue
            if key not in header:
                header[key] = value
            elif header[key] != value:
                raise VisionExtractionError(
                    f"vision model reported conflicting values for the per-document "
                    f"field {key!r} across page batches; a document has one {key!r}, "
                    "so this is either a misread or two documents in one upload — "
                    "refusing to guess which value is real (values omitted: PHI)"
                )
    return header


def _fact_model(
    schema: type[LabReport] | type[IntakeForm] | type[MedicationListDocument],
) -> type[BaseModel] | None:
    """The per-fact model behind a document schema's ``facts: list[X]`` field."""
    field = schema.model_fields.get("facts")
    if field is None:
        return None
    for arg in get_args(field.annotation):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return arg
    return None


def _without_extras(data: dict[str, Any], model: type[BaseModel], where: str) -> dict[str, Any]:
    """Drop keys ``model`` does not declare, logging any that carried a value."""
    declared = set(model.model_fields)
    kept = {k: v for k, v in data.items() if k in declared}
    valued = {k: v for k, v in data.items() if k not in declared and v is not None}
    if valued:
        # Never silent: a model that starts routing real content into an
        # undeclared key must be visible here, in the logs and the trace.
        _logger.warning(
            "vision model emitted undeclared keys; dropped before validation",
            extra={"where": where, "keys": sorted(valued), "model": model.__name__},
        )
    return kept


def _drop_extras(
    payload: dict[str, Any],
    schema: type[LabReport] | type[IntakeForm] | type[MedicationListDocument],
) -> dict[str, Any]:
    """Drop keys the vision model invented, before strict validation.

    Measured against real Claude vision on the real demo intake form, 4 runs::

        run 0: 46 facts, 0 invented keys
        run 1: 39 facts, 0 invented keys
        run 2: 47 facts, 0 invented keys
        run 3: 46 facts, 2 invented keys   field_path_note='Dose', value_confidence=None

    The model decorates. It intermittently adds keys that exist in NO schema —
    sometimes null, sometimes carrying a value — and ``extra="forbid"`` then threw
    away the ENTIRE 46-fact extraction over one of them. Roughly one run in four,
    so a green run proves nothing, and every keyless test missed it because
    ``StubVision`` replays a recording that never invents a key.

    Dropping *valued* extras too is a deliberate reversal: the first cut of this
    kept them and raised, on the theory that a valued extra is real data we want
    loud. Four live runs refuted that — the valued extras are decorative
    annotations (``field_path_note='Dose'`` next to a well-formed fact), and
    destroying a whole good extraction over one is indefensible.

    This does not weaken the boundary, because ``extra="forbid"`` was not what
    protected it:

    - ``field_path``, ``value`` and ``category`` are REQUIRED, so a model that
      renamed or moved real content out of a declared field still fails loudly on
      the missing field. That is the guard against genuine schema drift.
    - The no-invention gate is ``documents/reconcile.py`` — a value is trusted only
      when it is located in the page's OCR tokens. Extras never reach it.
    - Declared fields are still strictly validated: nothing is coerced, and a
      wrong-typed value still raises.

    So the residual risk of dropping is losing a decoration, and it is logged
    rather than silent (see ``_without_extras``). The risk of NOT dropping is
    losing every fact on the page. That trade is not close.
    """
    fact_model = _fact_model(schema)
    cleaned = _without_extras(payload, schema, "document")
    facts = cleaned.get("facts")
    if fact_model is None or not isinstance(facts, list):
        return cleaned
    cleaned["facts"] = [
        _without_extras(f, fact_model, "fact") if isinstance(f, dict) else f for f in facts
    ]
    return cleaned


def _tool_input(response: Any, name: str) -> dict[str, Any] | None:
    """The arguments of the named forced tool call, or ``None``."""
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == name:
            data = getattr(block, "input", None)
            if isinstance(data, dict):
                return data
    return None


def _destringify(payload: dict[str, Any]) -> dict[str, Any]:
    """Undo a model that JSON-encoded its structured output as a string.

    Tool-forced JSON is meant to arrive structured, but on dense documents a
    vision model sometimes returns ``facts`` — or the entire object — as a JSON
    *string*. That would fail strict validation (``facts`` is not a ``list``), so
    recover it here rather than lose a real extraction. Only touches a string
    ``facts`` value; a well-formed structured payload passes through untouched.
    A string that does not parse is left as-is (strict validation then rejects it
    loudly, never silently).
    """
    facts = payload.get("facts")
    if not isinstance(facts, str):
        return payload
    try:
        parsed = json.loads(facts)
    except (json.JSONDecodeError, TypeError):
        return payload
    if isinstance(parsed, list):
        return {**payload, "facts": parsed}
    # The model stringified the whole object under "facts" — use the parsed object.
    if isinstance(parsed, dict) and isinstance(parsed.get("facts"), list):
        return parsed
    return payload


def build_vision(settings: Settings) -> VisionExtractor:
    """Real Claude-vision extractor when an API key is set, else the keyless stub."""
    if not settings.anthropic_api_key:
        return StubVision()
    return ClaudeVision(settings)
