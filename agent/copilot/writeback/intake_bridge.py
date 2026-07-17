"""Intake-fact → propose bridge (F4b) — the categorized facts, sent to the gate.

Turns a document's categorized intake facts into PROPOSED write candidates by
composing the existing propose→confirm gate — it never reimplements it. Recent
ingestion tags each intake fact with an :class:`IntakeCategory`; the three
``lists``-backed ones (``allergy`` / ``medication`` / ``medical_problem``) are
exactly the kinds the write-back already knows how to write. This bridge maps
each such fact to the matching write and runs it through
:meth:`WriteService.propose` (parse → verify → echo-back + ``write_proposed``
audit, entry mode ``agent_proposed_physician_confirmed``). The physician then
confirms each one through the unchanged confirm route.

**Provenance — the source link, carried across the write boundary.** The agent
store makes provenance airtight internally (``extracted_fact.extraction_id →
extraction.source_document_id → source_document``, all NOT NULL), but that chain
stopped here: this bridge knew both the source document and the exact
``extracted_fact`` id and dropped them, so a confirmed intake-derived allergy
landed in OpenEMR as an untraceable record. Every proposal now carries a
:class:`WriteSource` naming both ids (plus page/bbox/confidence, enough to
rebuild the read side's ``DocumentCitation``), which rides the candidate through
the physician's confirm into the ``write_proposed`` / ``write_committed`` audit
rows — and, for allergies, into the chart record's own ``comments``.

**Safety invariant — the agent is structurally incapable of committing.** The
bridge holds a :class:`WriteService` and calls ONLY its ``propose`` path, which
builds no write client and performs no OpenEMR call (see
``writeback/service.py``). Commit stays the separate, physician-confirmed
transaction on ``POST /v1/writes/{key}/confirm``; the bridge never calls
``WriteService.commit``. So even a buggy or adversarial document can, at most,
draft typed, range-checked candidates for a human to accept or reject.
"""

from __future__ import annotations

from dataclasses import dataclass

from copilot.config import Settings
from copilot.domain.documents import IntakeCategory
from copilot.domain.primitives import ClinicianId, PatientId
from copilot.domain.writes import ProposedWrite, WriteEntryMode, WriteKind, WriteSource
from copilot.memory.db import session_scope
from copilot.memory.repository import MemoryRepository
from copilot.observability import Observability
from copilot.writeback.service import WriteService


class DocumentNotFoundError(Exception):
    """The requested source document is missing or not this patient's.

    Raised when ``document_id`` has no ``source_document`` row, or the row
    belongs to a different patient than the authorized ``patient_id``. Both cases
    collapse to one error so the route can answer **404** without revealing
    whether a document exists for another patient — a document outside the
    caller's authorization boundary must never be observable.
    """


@dataclass(frozen=True)
class _WritableFact:
    """One intake fact resolved to a write, with the provenance it came from.

    Replaces the earlier ``(kind, title)`` tuple: the fact's source is now
    carried alongside the value it produced, so the two cannot be separated on
    the way to ``propose``. That pairing is the whole point — the bridge is the
    last place that knows both, and it used to drop the provenance half.
    """

    kind: WriteKind
    title: str
    source: WriteSource


class IntakeWritebackBridge:
    """Draft intake-derived write candidates through the existing propose gate.

    Propose-only by construction: it composes a :class:`WriteService` and touches
    only :meth:`WriteService.propose`. It has no code path to ``commit``, so it
    cannot self-commit a write — the confirm step is the physician's, on a
    separate transaction.
    """

    def __init__(self, write_service: WriteService) -> None:
        self._writes = write_service

    async def propose_writes_from_document(
        self,
        *,
        document_id: int,
        acting_clinician: ClinicianId,
        patient_id: PatientId,
    ) -> list[ProposedWrite]:
        """Propose one write candidate per write-relevant intake fact. Never commits.

        Loads the document's latest extraction (extractions are append-only), keeps
        the facts whose :class:`IntakeCategory` maps to a write kind
        (``allergy`` / ``medication`` / ``medical_problem``), and runs each through
        the existing propose path with entry mode
        ``agent_proposed_physician_confirmed``. Facts with a non-writable category
        (demographic / chief_complaint / family_history) or an empty value are
        ignored — they have no ``lists`` home. Returns the structured echo-backs;
        each carries its own ``idempotency_key`` for the physician to confirm.

        Raises :class:`DocumentNotFoundError` when the document is missing or not
        this patient's. Propagates ``WriteInputError`` from the propose path — but
        title-only issue/medication writes fail only on an empty title, which is
        already filtered out here.

        Each proposal carries the ``WriteSource`` of the fact it came from, so
        the spec's "link every derived fact back to the source" survives past the
        agent store: onto the candidate, through the physician's confirm, and
        into the audit trail.
        """
        writable = await self._load_writable_facts(document_id, patient_id)
        proposals: list[ProposedWrite] = []
        for fact in writable:
            proposed, _key = await self._writes.propose(
                clinician_id=acting_clinician,
                patient_id=patient_id,
                kind=fact.kind,
                raw_value=fact.title,
                entry_mode=WriteEntryMode.agent_proposed_physician_confirmed,
                source=fact.source,
            )
            proposals.append(proposed)
        return proposals

    async def _load_writable_facts(
        self, document_id: int, patient_id: PatientId
    ) -> list[_WritableFact]:
        """Read the latest extraction's write-relevant facts, each with its source.

        One read pass. Scopes the document to ``patient_id`` (cross-patient guard)
        before any fact is returned, so a fact from another patient's document can
        never become a write on this patient's chart. The guard is unchanged; it
        also means every ``WriteSource`` minted here is already known to belong to
        the authorized patient.
        """
        async with session_scope() as session:
            repo = MemoryRepository(session)
            document = await repo.get_source_document(document_id)
            if document is None or document.patient_id != patient_id.value:
                raise DocumentNotFoundError
            extraction = await repo.get_latest_extraction(document_id)
            if extraction is None:
                return []
            rows = await repo.get_extracted_facts(extraction.id)

        writable: list[_WritableFact] = []
        for row in rows:
            kind = _writable_kind(row.category)
            if kind is None:
                continue
            # title = the fact's verbatim value; begdate defaults to today in the
            # propose path's _parse_* step. An empty/absent value has no write.
            title = (row.value or "").strip()
            if not title:
                continue
            # The provenance this bridge used to throw away. ``document.id`` and
            # ``row.id`` are exactly the two ends of the store's FK chain; page /
            # bbox / confidence ride along so the read-side DocumentCitation can be
            # rebuilt from the write alone. Each is passed as-is — a fact whose OCR
            # span was never reconciled keeps its honest NULLs rather than
            # acquiring an invented page.
            writable.append(
                _WritableFact(
                    kind=kind,
                    title=title,
                    source=WriteSource(
                        source_document_id=document.id,
                        extracted_fact_id=row.id,
                        quote=title,
                        page_no=row.page_no,
                        bbox=row.bbox,
                        confidence=row.match_confidence,
                    ),
                )
            )
        return writable


def build_intake_bridge(
    settings: Settings, observability: Observability | None = None
) -> IntakeWritebackBridge:
    """Construct the bridge over a fresh, propose-only :class:`WriteService`.

    No write/read client factories are wired: the bridge only proposes, and the
    propose path never builds a client, so the smart-mode delegation seams the
    confirm route needs are irrelevant here.
    """
    return IntakeWritebackBridge(WriteService(settings, observability))


def _writable_kind(category: str | None) -> WriteKind | None:
    """Map an intake fact's stored category to its write kind, or ``None`` to skip.

    Only the three ``lists``-backed intake categories are writable through the
    propose→confirm gate. The exhaustive ``match`` (no ``default``) means a new
    :class:`IntakeCategory` fails type-checking here until its write disposition
    is decided: ``demographic`` / ``chief_complaint`` / ``family_history`` are
    explicitly ignored (they land in ``patient_data`` / ``form_encounter`` /
    ``history_data``, not ``lists``). A ``None`` category (a lab fact) or a
    string outside the enum (defensive) is also skipped.
    """
    if category is None:
        return None
    try:
        intake_category = IntakeCategory(category)
    except ValueError:
        return None
    match intake_category:
        case IntakeCategory.allergy:
            return WriteKind.allergy
        case IntakeCategory.medication:
            return WriteKind.medication
        case IntakeCategory.medical_problem:
            return WriteKind.medical_problem
        case (
            IntakeCategory.demographic
            | IntakeCategory.chief_complaint
            | IntakeCategory.family_history
        ):
            return None
