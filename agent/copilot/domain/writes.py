"""Typed contracts for physician write-back — the read-side model, reversed.

Reads flow ``source → grounding → verification → serve``; writes flow
``typed candidate → verification → echo-back → explicit confirm → commit``.
Everything a write touches is a frozen, typed object over a **closed** set of
writable metrics — no free text ever reaches the DB (see
``research/WRITEBACK_PHASE1_PLAN.md`` §3). These DTOs are the write-side
analogue of ``contracts.py``'s ``Claim`` / ``FhirReference``.

Phase 1a is foundation only: these types plus the deterministic verifier
(``verification/writes.py``) and the write client (``fhir/write_client.py``).
The route + service that orchestrate propose→confirm are Phase 1b.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from copilot.domain.primitives import ClinicianId, DocumentCitation, PatientId


class WritableMetric(StrEnum):
    """The closed set of vital-sign metrics a physician may write.

    Deliberately small: only OpenEMR's Standard-API *vitals* columns are
    directly writable as Observation-like data (labs have no create endpoint —
    see the feasibility brief). Extend only with a matching column mapping in
    ``fhir/write_client.py`` and a plausibility spec in ``verification/writes.py``
    — the exhaustive ``match`` in each will fail to compile until you do.
    """

    heart_rate = "heart_rate"
    spo2 = "spo2"
    systolic_bp = "systolic_bp"
    diastolic_bp = "diastolic_bp"
    respiratory_rate = "respiratory_rate"
    temperature = "temperature"
    weight = "weight"
    height = "height"


class WriteKind(StrEnum):
    """What kind of record a candidate creates.

    ``vital`` / ``medication`` are the physician-direct kinds (Phase 1,
    ``WriteCandidate``); ``medical_problem`` / ``allergy`` are the
    intake-derived, agent-proposed kinds (F4b, ``IssueWriteCandidate``) that
    flow through the same propose→confirm gate but require an explicit
    physician confirm to commit.
    """

    vital = "vital"
    medication = "medication"
    medical_problem = "medical_problem"
    allergy = "allergy"


# The kind subsets each candidate model can carry. Keeping these as ``Literal``
# subsets (not the full enum) is load-bearing: the exhaustive ``match`` in
# ``verification/writes.py`` covers exactly the direct kinds, and the issue
# dispatch in ``writeback/service.py`` covers exactly the issue kinds — mypy
# proves both closed.
DirectWriteKind = Literal[WriteKind.vital, WriteKind.medication]
IssueWriteKind = Literal[WriteKind.medical_problem, WriteKind.allergy]


class WriteEntryMode(StrEnum):
    """How the value reached the record — the physician-attribution surface.

    ``human_direct`` is Phase 1: the physician typed the value themselves, so an
    out-of-range value is a soft, overridable warning.
    ``agent_proposed_physician_confirmed`` is the live agent path (F4b): the
    agent drafts a typed candidate, a *separate* physician confirm transaction
    commits it, and the audit row attributes the write to this mode. The write
    verifier treats it as the strict mode that hard-blocks out-of-range values.
    """

    human_direct = "human_direct"
    agent_proposed_physician_confirmed = "agent_proposed_physician_confirmed"


class VitalWrite(BaseModel):
    """A single-metric vital reading, appended as a new vitals form.

    ``value`` is already parsed to a number at the system boundary (parse, don't
    validate): constructing a ``VitalWrite`` with a non-numeric value or an
    unknown metric fails here, which is the first deterministic gate. Semantic
    checks (unit-matches-metric, physiologic plausibility) are the verifier's job.
    """

    model_config = ConfigDict(frozen=True)

    metric: WritableMetric
    value: float
    unit: str = Field(min_length=1, description="Physician-supplied unit; verified against the metric.")


class MedicationWrite(BaseModel):
    """A medication-list entry, appended as a new list row (latest-wins).

    ``title`` is a picked/echoed drug string, never free prose. ``begdate`` is a
    ``YYYY-MM-DD`` string (the Standard API's format); its format is checked
    deterministically by the verifier, not coerced here.
    """

    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1)
    begdate: str = Field(min_length=1, description="Start date, YYYY-MM-DD.")
    enddate: str | None = Field(default=None, description="Optional end date, YYYY-MM-DD.")
    diagnosis: str | None = Field(
        default=None, description="Optional '<codetype>:<code>' association."
    )


class MedicalProblemWrite(BaseModel):
    """A medical-problem (issue) list entry, appended as a new list row.

    ``title`` is the intake-derived problem string the physician confirms —
    never free prose beyond it. ``begdate`` is a ``YYYY-MM-DD`` string (the
    Standard API's format); its format is checked deterministically by the
    write verification step, not coerced here.
    """

    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1)
    begdate: str = Field(min_length=1, description="Onset/entry date, YYYY-MM-DD.")
    diagnosis: str | None = Field(
        default=None, description="Optional '<codetype>:<code>' association."
    )


class AllergyWrite(BaseModel):
    """An allergy (issue) list entry, appended as a new list row.

    ``title`` is the intake-derived allergen/substance string the physician
    confirms. ``begdate`` is a ``YYYY-MM-DD`` string, format-checked by the
    write verification step.
    """

    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1)
    begdate: str = Field(min_length=1, description="Onset/entry date, YYYY-MM-DD.")
    reaction: str | None = Field(default=None, description="Optional reaction description.")


class WriteSource(BaseModel):
    """Where an agent-proposed write came from — its (document, fact) provenance.

    The write-side analogue of the read side's ``DocumentCitation``: the spec
    requires every derived fact to link back to its source document, and the
    agent store already guarantees that internally (a NOT NULL FK chain
    ``extracted_fact.extraction_id → extraction.source_document_id →
    source_document``). This type is what carries that guarantee **across the
    OpenEMR write boundary**, where it was previously dropped: without it a
    physician-confirmed intake-derived allergy lands in OpenEMR as an untraceable
    record, with nothing tying it to the scanned page it was read off.

    Deliberately *optional* everywhere it appears (``… | None = None``): the
    physician-direct path (``entry_mode=human_direct``) has no source document,
    and a typed-in vital must stay expressible with no provenance at all. A
    ``WriteSource`` therefore means "this write descends from a document"; its
    absence means "a human typed this", and the two are never conflated.

    The ids are the agent store's own row ids (``source_document.id`` /
    ``extracted_fact.id``), not OpenEMR ids — they are the durable end of the
    provenance chain, and they are what ``to_citation`` re-materializes.
    """

    model_config = ConfigDict(frozen=True)

    source_document_id: int = Field(gt=0, description="source_document row id.")
    extracted_fact_id: int = Field(gt=0, description="extracted_fact row id.")
    quote: str = Field(
        min_length=1, description="Verbatim extracted value the write was derived from."
    )
    page_no: int | None = Field(
        default=None, ge=1, description="1-based page the fact was found on, when reconciled."
    )
    bbox: list[float] | None = Field(
        default=None, description="Normalized [x, y, w, h] of the reconciled OCR span."
    )
    confidence: float | None = Field(
        default=None, description="OCR-reconciliation match confidence in [0, 1]."
    )

    def to_citation(self) -> DocumentCitation | None:
        """Re-materialize the read-side ``DocumentCitation`` this write descends from.

        Proves the "enough to reconstruct the citation" contract in code rather
        than in a comment: every field ``DocumentCitation`` requires is carried
        here. Returns ``None`` — never a fabricated page — when ``page_no`` is
        absent, because a fact whose OCR span was never reconciled has no page to
        cite and ``DocumentCitation.page_or_section`` is required (``ge=1``).
        Inventing a page number to satisfy the type would be exactly the
        no-invention violation the citation contract exists to prevent.
        """
        if self.page_no is None:
            return None
        return DocumentCitation(
            source_id=str(self.source_document_id),
            page_or_section=self.page_no,
            field_or_chunk_id=str(self.extracted_fact_id),
            quote_or_value=self.quote,
            bbox=self.bbox,
            confidence=self.confidence,
        )

    def provenance_note(self) -> str:
        """A short, human-readable provenance line for a chart record's comment.

        Written for the physician reading the row in OpenEMR months later: it
        names the source document, the page (when known), and the exact
        ``extracted_fact`` row, so the record can be traced to the scanned page
        it came from without leaving the chart. Deliberately terse and
        non-clinical — it is attribution, never a clinical assertion, and it
        never restates the value as though it were independently observed.
        """
        page = f", page {self.page_no}" if self.page_no is not None else ""
        return (
            f"Source: AgentForge intake document #{self.source_document_id}{page} "
            f"(extracted_fact #{self.extracted_fact_id}); "
            "agent-proposed, physician-confirmed."
        )


class WriteCandidate(BaseModel):
    """A parsed, typed write request over the closed writable surface.

    Carries exactly one payload (``vital`` xor ``medication``) matching ``kind``.
    ``kind`` is deliberately the *direct* subset — the vital/medication verifier
    in ``verification/writes.py`` matches exhaustively over it; the issue kinds
    live on ``IssueWriteCandidate``. ``idempotency_key`` is **server-generated**
    (``writeback/service.py`` mints a fresh ``secrets.token_urlsafe`` per propose
    and returns it on the echo-back); the client only re-sends that value on the
    confirm, so a retried/double-clicked confirm cannot create a duplicate record.
    ``patient_id`` / ``clinician_id`` scope the write; the route enforces
    ``is_authorized`` before a candidate is trusted.

    ``source`` is the optional (document, fact) provenance. It lives on the
    *candidate* rather than only on ``ProposedWrite`` because the candidate is
    the object that round-trips through the physician's confirm request — the
    confirm body echoes it back verbatim, so this is the only place provenance
    can survive propose → confirm and still reach the ``write_committed`` audit
    row. It defaults to ``None``, leaving the physician-direct path untouched.
    """

    model_config = ConfigDict(frozen=True)

    kind: DirectWriteKind
    patient_id: PatientId
    clinician_id: ClinicianId
    idempotency_key: str = Field(min_length=1, max_length=128)
    entry_mode: WriteEntryMode = WriteEntryMode.human_direct
    vital: VitalWrite | None = None
    medication: MedicationWrite | None = None
    source: WriteSource | None = None

    @model_validator(mode="after")
    def _exactly_one_payload(self) -> WriteCandidate:
        present = [p for p in (self.vital, self.medication) if p is not None]
        if len(present) != 1:
            raise ValueError("a WriteCandidate carries exactly one payload")
        if self.kind is WriteKind.vital and self.vital is None:
            raise ValueError("kind=vital requires a vital payload")
        if self.kind is WriteKind.medication and self.medication is None:
            raise ValueError("kind=medication requires a medication payload")
        return self


class IssueWriteCandidate(BaseModel):
    """A typed intake-derived issue write (medical problem / allergy) — F4b.

    The agent-proposed analogue of ``WriteCandidate``: same closed-surface
    discipline (exactly one payload matching ``kind``, server-generated
    ``idempotency_key`` minted by ``writeback/service.py`` at propose and only
    re-sent by the client on confirm, typed principal ids), but over the issue kinds and
    defaulting to the strict ``agent_proposed_physician_confirmed`` entry mode.
    The agent may only *construct and propose* one of these; committing it
    requires the separate physician confirm transaction in
    ``writeback/service.py`` — the agent structurally cannot self-commit.

    ``source`` is the (document, fact) provenance the intake bridge populates —
    the link the spec requires from every derived fact back to the scanned page
    it was read off. It stays optional here (rather than required on the
    agent-proposed type) so the confirm route can still parse a candidate minted
    before this field existed, and so an issue write proposed by some future
    non-document path is expressible; ``None`` honestly means "no source
    document", never a fabricated one.
    """

    model_config = ConfigDict(frozen=True)

    kind: IssueWriteKind
    patient_id: PatientId
    clinician_id: ClinicianId
    idempotency_key: str = Field(min_length=1, max_length=128)
    entry_mode: WriteEntryMode = WriteEntryMode.agent_proposed_physician_confirmed
    medical_problem: MedicalProblemWrite | None = None
    allergy: AllergyWrite | None = None
    source: WriteSource | None = None

    @model_validator(mode="after")
    def _exactly_one_payload(self) -> IssueWriteCandidate:
        present = [p for p in (self.medical_problem, self.allergy) if p is not None]
        if len(present) != 1:
            raise ValueError("an IssueWriteCandidate carries exactly one payload")
        if self.kind is WriteKind.medical_problem and self.medical_problem is None:
            raise ValueError("kind=medical_problem requires a medical_problem payload")
        if self.kind is WriteKind.allergy and self.allergy is None:
            raise ValueError("kind=allergy requires an allergy payload")
        return self


# Any candidate the propose→confirm gate can carry.
AnyWriteCandidate = WriteCandidate | IssueWriteCandidate


class WriteVerdict(BaseModel):
    """The deterministic verifier's decision for one candidate.

    ``blocked`` is the hard gate — a blocked candidate must never reach commit.
    ``warnings`` are soft and overridable (an out-of-range human-direct value);
    ``errors`` explain a block. Mirrors ``VerificationResult`` on the read side.
    """

    model_config = ConfigDict(frozen=True)

    kind: WriteKind
    metric: WritableMetric | None = None
    blocked: bool
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the candidate may proceed to an explicit physician confirm."""
        return not self.blocked


class ProposedWrite(BaseModel):
    """The structured echo-back a physician confirms — never agent prose.

    Returned by the propose step: the exact record to be written, its verdict,
    and the explicit "new record dated now" notice. The frontend renders this as
    a confirmation card; a fat-finger is caught here by a human.
    """

    model_config = ConfigDict(frozen=True)

    candidate: AnyWriteCandidate
    verdict: WriteVerdict
    effective_time: str = Field(default="now", description="Clinical time of the write; always 'now'.")
    notice: str = Field(
        default="This creates a NEW record dated now; it does not overwrite prior values.",
    )

    @property
    def source(self) -> WriteSource | None:
        """The provenance of the record being proposed, or ``None`` if typed by a human.

        A read-only delegation to ``candidate.source`` rather than a second
        field: the candidate is what the physician confirms and what the commit
        audits, so duplicating provenance here would create two copies that could
        disagree — and the echo-back is exactly where a disagreement would be
        invisible.
        """
        return self.candidate.source


class CommittedWrite(BaseModel):
    """Proof of a committed write — returned only on a confirmed 201/200.

    A write whose success could not be confirmed never produces one of these;
    it raises instead (see ``fhir/write_client.py``). ``encounter_id`` is set for
    vitals (which attach to an encounter) and ``None`` for medication and
    issue (medical problem / allergy) writes.

    ``unconfirmed`` records the *post-write read-back* outcome, which is a
    separate question from the write's own 201. The append landed (that is what
    produced this object); ``unconfirmed=True`` means the fail-open, non-gating
    read-back could not corroborate the value on a same-metric live re-fetch —
    either the value was not observed, or the read-back itself failed. It is
    carried here (and mirrored on a ``write_unconfirmed`` audit row) so a "201
    returned but the value was not observed" surfaces to the physician instead of
    being buried in a log line. It never blocks or rolls back the write: appends
    are append-only, and a read-back is advisory by design.
    """

    model_config = ConfigDict(frozen=True)

    resource_kind: WriteKind
    new_id: str = Field(min_length=1)
    encounter_id: str | None = None
    committed_at: datetime
    unconfirmed: bool = Field(
        default=False,
        description=(
            "True when the post-write read-back did not positively observe the "
            "written value on a same-metric re-fetch (value-not-observed or a "
            "failed read-back). Advisory and NON-gating — the append already "
            "landed and is never rolled back."
        ),
    )
