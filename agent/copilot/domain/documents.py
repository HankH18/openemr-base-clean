"""Strict extraction schemas for Week-2 document ingestion.

These are the *parse-don't-validate* boundary between a vision model's raw
output and the agent's typed world. The schema is the source of truth: a
malformed or partial payload raises a ``pydantic.ValidationError`` and is never
silently coerced into a valid-looking object. In particular every model runs in
Pydantic **strict** mode with ``extra="forbid"`` — a string is never coerced to
a bool/float/list, and an unknown key is a hard error — so a VLM that omits a
required field or emits a wrong-typed one fails loudly instead of producing a
confident-but-wrong extraction. Every document model additionally requires at
least one fact (``min_length=1``), because "found nothing" is the one failure a
type check alone will not catch: an empty extraction is well-typed.

The extracted values stay verbatim strings (``ExtractedFact.value``): the
reconciliation + verification layers compare against the source, so coercing
``"13.5"`` to a float here would throw away formatting we need to match.

Fields that a real document genuinely may not carry (``unit``,
``reference_range``, ``abnormal``, ``collection_date``) stay optional rather
than rejecting an honest partial extraction — but their absence is reported,
never silent: see :meth:`ExtractedFact.missing_lab_fields`,
:attr:`LabReport.incomplete_facts`, and :attr:`IntakeForm.categories_present`.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

#: Date formats a US clinical document actually prints, tried after ISO 8601.
_DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
    "%m-%d-%Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %b %Y",
)


def _lenient_date(v: object) -> datetime | None:
    """Parse a vision-extracted date string leniently; NEVER raise.

    Vision models print whatever format the document uses — ISO, US ``MM/DD/YYYY``,
    with or without a time. Pydantic's ``datetime`` parser accepts only ISO
    (``strict=False`` does NOT help), so a US-format header date raised a
    ``ValidationError`` and crashed the ENTIRE extraction — one unreadable date
    discarding every fact on the page (a live 500). Here an unparseable value
    degrades to ``None`` (a recorded absence), exactly like the intake form's
    ``str`` dates. Ambiguous ``DD/MM`` vs ``MM/DD`` is read US-style — acceptable
    for the specimen-collection metadata this guards; the clinical
    ``date_of_birth`` stays a verbatim ``str`` precisely to avoid that guess.
    """
    if v is None or isinstance(v, datetime):
        return v
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


#: A ``datetime`` field that tolerates any vision-extracted date string, degrading
#: an unparseable one to ``None`` rather than crashing the whole extraction.
LenientDate = Annotated[datetime | None, BeforeValidator(_lenient_date)]


class LabField(StrEnum):
    """The lab fields the spec (req 2, p4) names as required, as a closed set.

    Naming them makes the spec's list machine-checkable rather than prose: each
    member is one slot the extraction contract must *account for*. Four of them
    (:attr:`unit`, :attr:`reference_range`, :attr:`abnormal`,
    :attr:`collection_date`) are deliberately optional *fields* on
    :class:`ExtractedFact` — see :meth:`ExtractedFact.missing_lab_fields` for
    why, and for how their absence is kept visible instead of silent.
    """

    test_name = "test_name"  # -> ExtractedFact.field_path
    value = "value"  # -> ExtractedFact.value
    unit = "unit"
    reference_range = "reference_range"
    collection_date = "collection_date"  # per-fact, or LabReport.collected_at
    abnormal = "abnormal"
    source_citation = "source_citation"  # -> supported + bbox (set by reconciliation)


class ExtractedFact(BaseModel):
    """One schema-validated fact pulled from a document, with provenance.

    Mirrors the frozen ``extracted_fact`` Phase-0 columns. ``field_path`` and
    ``value`` are required — a fact with neither is not a fact — and ``value``
    is kept as a verbatim string (never coerced numeric).

    ``supported`` is the no-invention gate, and it is *derived downstream*: the
    reconciliation layer (``documents/reconcile.py``) sets ``supported=True``
    together with ``bbox`` + ``match_confidence`` when it locates the value in
    the page's OCR tokens, and ``supported=False`` when the value is nowhere on
    the page. A vision model never sets it — at this extraction boundary the
    field is simply unset. **This schema does not enforce that pairing**; the
    invariant lives in the layer that derives it, so do not read ``supported``
    on an un-reconciled fact as evidence of anything.

    The four optional lab fields (``unit``, ``reference_range``, ``abnormal``,
    ``collection_date``) record *absence*, and absence is reported — not
    silent — via :meth:`missing_lab_fields`.
    """

    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", hide_input_in_errors=True
    )

    field_path: str = Field(min_length=1)
    value: str = Field(description="Verbatim extracted value, as a string — never coerced numeric.")
    unit: str | None = None
    reference_range: str | None = None
    abnormal: str | None = None
    # Vision models print dates in whatever format the document uses (ISO or US
    # MM/DD/YYYY). LenientDate parses both and degrades an unparseable one to None
    # — a bare `datetime` (even strict=False) raised on a US date and crashed the
    # whole extraction (see _lenient_date).
    collection_date: LenientDate = Field(default=None)
    page_no: int | None = Field(default=None, ge=1)
    bbox: list[float] | None = Field(
        default=None, description="Normalized [x, y, w, h] of the reconciled OCR span."
    )
    match_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    supported: bool = False

    def missing_lab_fields(
        self, *, collected_at_fallback: datetime | None = None
    ) -> frozenset[LabField]:
        """Which spec-required lab fields this fact does not carry.

        The completeness signal that keeps optional-ness honest. The spec calls
        all seven fields required, but a schema that *rejected* a fact lacking a
        reference range would reject real laboratory documents: unitless
        analytes (INR, pH, ratios) have no unit, qualitative and narrative
        results have no reference range, and most report formats print an
        ``abnormal`` flag only when the result *is* abnormal. Worse, a required
        field on a model-extraction schema is not a data-quality guarantee — it
        is pressure to invent, because the model must emit *something* to
        validate. That would trade a visible gap (``None``) for an invisible
        fabrication, which is precisely what this pipeline's OCR reconciliation
        exists to prevent.

        So the fields stay optional and the gap becomes *data*: callers can
        count, audit, and gate on it. Absence is recorded, never assumed away.

        ``collected_at_fallback`` resolves :attr:`LabField.collection_date`
        against the report-level date (see :attr:`LabReport.collected_at`),
        because a real lab prints the collection date once in the specimen
        header rather than on every analyte row.

        :attr:`LabField.source_citation` counts as present only once
        reconciliation has attached a bbox — so a freshly-extracted,
        un-reconciled fact always reports it missing. That is intended: the
        citation is earned from the page, not claimed by the model.
        """
        missing: set[LabField] = set()
        if not self.unit:
            missing.add(LabField.unit)
        if not self.reference_range:
            missing.add(LabField.reference_range)
        if not self.abnormal:
            missing.add(LabField.abnormal)
        if self.collection_date is None and collected_at_fallback is None:
            missing.add(LabField.collection_date)
        if not (self.supported and self.bbox is not None):
            missing.add(LabField.source_citation)
        return frozenset(missing)


class LabReport(BaseModel):
    """Strict schema for a parsed laboratory report (VLM extraction target).

    ``facts`` is required *and* non-empty (``min_length=1``): a payload that
    omits the key fails, and so does ``{"facts": []}``. "I read a lab report and
    found nothing" is never an honest extraction — without the length floor a
    vision model that read nothing produces a clean, confident, empty report
    that every downstream consumer treats as success. The floor also rides into
    the tool schema the model is handed (``minItems: 1`` in
    ``model_json_schema()``), so the constraint is stated to the extractor, not
    only enforced after it.
    """

    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", hide_input_in_errors=True
    )

    facts: list[ExtractedFact] = Field(min_length=1)
    ordering_provider: str | None = None
    specimen: str | None = None
    collected_at: LenientDate = Field(default=None)

    @property
    def incomplete_facts(self) -> tuple[tuple[ExtractedFact, frozenset[LabField]], ...]:
        """Each fact that lacks a spec-required lab field, paired with the gaps.

        Makes partial extraction auditable: the fields stay optional (see
        :meth:`ExtractedFact.missing_lab_fields` for the defense), but no gap is
        silent. ``collection_date`` is resolved against :attr:`collected_at`
        first, so a report carrying one specimen-header date does not report
        every analyte as missing it.
        """
        gaps = (
            (fact, fact.missing_lab_fields(collected_at_fallback=self.collected_at))
            for fact in self.facts
        )
        return tuple((fact, missing) for fact, missing in gaps if missing)


class IntakeCategory(StrEnum):
    """Where an intake fact belongs in OpenEMR — one value per record type.

    This is what makes the intake extraction *match OpenEMR's schema*: every
    intake fact is tagged with the OpenEMR record it round-trips to, so the
    ``allergy``/``medication``/``medical_problem`` facts map 1:1 into the
    existing write-back path (all three are ``lists`` rows keyed by ``type``).
    """

    demographic = "demographic"  # -> patient_data (fname/lname/DOB/sex/...)
    chief_complaint = "chief_complaint"  # -> form_encounter.reason
    medication = "medication"  # -> lists type='medication'
    allergy = "allergy"  # -> lists type='allergy'
    medical_problem = "medical_problem"  # -> lists type='medical_problem'
    family_history = "family_history"  # -> history_data


class IntakeFact(ExtractedFact):
    """An :class:`ExtractedFact` tagged with its OpenEMR record type.

    Intake facts additionally carry a required :class:`IntakeCategory` so the
    downstream mapping to an OpenEMR record is typed, not a ``field_path`` string
    heuristic. ``strict=False`` on the enum so it parses the plain string value a
    vision model returns (``"allergy"``) — the model emits JSON, not enum members.
    """

    category: IntakeCategory = Field(strict=False)


class IntakeForm(BaseModel):
    """Strict schema for a parsed patient-intake form (VLM extraction target).

    Like :class:`LabReport`, ``facts`` is required *and* non-empty
    (``min_length=1``) — a blank extraction does not validate. Each fact is an
    :class:`IntakeFact` (carries its OpenEMR ``category``).

    The floor is on the *list*, not on the category coverage: the spec names
    demographics, chief concern, medications, allergies, and family history as
    required intake fields, but a real form legitimately has no allergies
    ("NKDA") or no family history, so requiring one fact per
    :class:`IntakeCategory` would reject honest paperwork. Which categories a
    form actually yielded is reported by :attr:`categories_present`.
    """

    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", hide_input_in_errors=True
    )

    facts: list[IntakeFact] = Field(min_length=1)
    patient_name: str | None = None
    date_of_birth: str | None = None
    #: Verbatim, as printed — a string for the same reason ``date_of_birth`` is one.
    #: See :class:`MedicationListDocument.completed_at` for the argument.
    completed_at: str | None = None

    @property
    def categories_present(self) -> frozenset[IntakeCategory]:
        """The OpenEMR record types this form actually yielded facts for.

        Coverage as data rather than as a rejection: absence of a category means
        "this form did not state it", which a reviewer can act on — it is never
        asserted to mean "the patient has none".
        """
        return frozenset(fact.category for fact in self.facts)


class MedicationListDocument(BaseModel):
    """Strict schema for a parsed medication list (VLM extraction target).

    Uses the same ``list[IntakeFact]`` shape as :class:`IntakeForm` — the shape
    the real vision path validates reliably — rather than a deeper
    ``MedicationFact`` subclass: the extra subclass depth made some vision models
    return ``facts`` as a JSON *string* instead of a list. A medication list is
    homogeneous, so a ``model_validator`` keeps only ``medication``-category
    facts: a stray non-medication line (e.g. a scanned patient/header line the
    VLM picks up) is dropped rather than failing the whole extraction, while the
    persisted list is still guaranteed to be all medications. Each surviving
    fact round-trips to ``lists type='medication'`` unchanged.

    ``facts`` is required and non-empty, enforced in *two* places, because this
    model has two distinct ways to end up empty:

    1. ``min_length=1`` rejects an omitted key and a literal ``{"facts": []}``.
    2. :meth:`_keep_only_medications` runs ``mode="after"`` — i.e. *after* the
       field constraint — so filtering could still hand back an empty list from
       a payload that passed step 1 (e.g. facts that are all ``demographic``).
       It therefore re-checks the floor itself. Dropping a stray line is
       tolerated; dropping *every* line is not an extraction, it is a
       misclassified document, and it fails loudly.
    """

    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", hide_input_in_errors=True
    )

    facts: list[IntakeFact] = Field(min_length=1)
    patient_name: str | None = None
    #: Verbatim page text, NOT a parsed instant — deliberately a ``str``.
    #:
    #: This was a ``datetime``, and it destroyed real extractions. Observed live
    #: against real Claude vision on the real demo intake form, which prints
    #: ``07/13/2026``::
    #:
    #:     completed_at: Input should be a valid datetime or date,
    #:       invalid character in year [input_value='07/13/2026']
    #:
    #: One unreadable header date discarded all 46 facts on the page. The extraction
    #: prompt orders the model to "copy each value character-for-character — never
    #: normalize", so a ``datetime`` here asked it to do the one thing it was told
    #: not to: the prompt and the type were in direct conflict, and the type lost
    #: every time a form printed a US-format date.
    #:
    #: A parser would be worse, not better. ``07/13/2026`` is unambiguous only by
    #: luck (13 > 12); the same form's ``date_of_birth: 03/11/1958`` is NOT — March
    #: 11 and 11 March are both readable, and nothing on the page says which. That
    #: is precisely why ``date_of_birth`` is already a ``str``. Guessing a date on a
    #: clinical record is the silent coercion this codebase exists to refuse, so the
    #: honest type for "what the page says" is the text the page says.
    #:
    #: Nothing reads this field — no consumer in ``copilot/``, the tests, the web
    #: client, or the acceptance harness. A caller that one day needs an instant can
    #: parse it where the document's locale is known, and fail loudly on ambiguity.
    completed_at: str | None = None

    @model_validator(mode="after")
    def _keep_only_medications(self) -> MedicationListDocument:
        """Drop any non-``medication`` fact so a medication list stays homogeneous.

        Raises when filtering would empty the list: ``min_length=1`` cannot see
        this case, because an after-validator runs once the field constraint has
        already passed.
        """
        meds = [fact for fact in self.facts if fact.category is IntakeCategory.medication]
        if len(meds) == len(self.facts):
            return self
        if not meds:
            raise ValueError(
                "medication list contains no medication-category facts: "
                f"{len(self.facts)} fact(s) extracted, all non-medication"
            )
        return self.model_copy(update={"facts": meds})
