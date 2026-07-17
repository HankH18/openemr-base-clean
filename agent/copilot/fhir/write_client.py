"""Async write client for OpenEMR's **Standard** REST API (``…/apis/default/api``).

Deliberately separate from the read-only ``FhirClient`` (different base, content
type ``application/json`` not ``application/fhir+json``, and a user-context write
token rather than the system read token). Keeping ``FhirClient`` minimal is
load-bearing for verification re-fetch reuse — the write path never touches it.

Fail-closed on write (see ``research/WRITEBACK_PHASE1_PLAN.md`` §1.2):

- Success is **only** an explicit ``201`` (create) / ``200`` (update) whose body
  carries a parseable id. Anything else — non-2xx, ambiguous body, unparseable
  id, transport error/timeout — raises ``OpenEmrWriteError``. **A write whose
  success cannot be confirmed is treated as FAILED, never assumed committed.**
- One ``401`` → one forced-refresh retry, exactly like ``FhirClient``.
- Idempotency-key aware: methods forward a client-generated key as an
  ``Idempotency-Key`` header so a retried/double-clicked confirm cannot dupe.

Append-only is enforced server-side (``insertVital`` strips any id and always
creates a new form; the medication list controller always inserts a new row).

No logging here — the client raises with a status code; callers log with PSR-3
context and never surface raw server messages to users. Tokens are never logged.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from copilot.domain.primitives import PatientId, utcnow
from copilot.domain.writes import (
    AllergyWrite,
    CommittedWrite,
    MedicalProblemWrite,
    MedicationWrite,
    VitalWrite,
    WritableMetric,
    WriteKind,
    WriteSource,
)
from copilot.fhir.auth import TokenAcquisitionError, TokenProvider

_ENCOUNTER_REASON = "AgentForge Co-Pilot bedside entry"

#: Returned by :meth:`OpenEmrWriteClient.upload_document` on a confirmed upload
#: when OpenEMR hands back no id. Its document-create genuinely returns a bare
#: ``true`` (``DocumentService::insertAtPath``), so there is nothing to parse:
#: inventing an id would fabricate provenance, and returning ``""`` would be
#: indistinguishable from :class:`~copilot.documents.pipeline.DerivedOnlyUploader`'s
#: "no upload happened". This sentinel says exactly what is true — OpenEMR holds
#: the document, and its API gave us no handle for it.
OPENEMR_NO_HANDLE = "openemr:uploaded-no-handle"


class OpenEmrWriteError(Exception):
    """A write that did not confirm success — never assume the record landed.

    ``status_code`` and ``validation`` carry the mapped detail (validation
    messages for ``400/422``) for the caller to audit and surface generically.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        validation: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.validation = validation


def _vital_column(metric: WritableMetric) -> str:
    """Map a metric to its OpenEMR vitals-form column — exhaustive, no ``default``.

    Adding a ``WritableMetric`` without a case here fails type-checking.
    """
    match metric:
        case WritableMetric.heart_rate:
            return "pulse"
        case WritableMetric.spo2:
            return "oxygen_saturation"
        case WritableMetric.systolic_bp:
            return "bps"
        case WritableMetric.diastolic_bp:
            return "bpd"
        case WritableMetric.respiratory_rate:
            return "respiration"
        case WritableMetric.temperature:
            return "temperature"
        case WritableMetric.weight:
            return "weight"
        case WritableMetric.height:
            return "height"


def _fmt_num(value: float) -> str:
    """Stringify a numeric vital for the all-strings vitals schema.

    Whole numbers lose the trailing ``.0`` ("72", not "72.0"); decimals stay.
    """
    return str(int(value)) if value == int(value) else str(value)


class OpenEmrWriteClient:
    """Small async writer for vitals, medications, issues (medical problems /
    allergies) and documents on the Standard REST API."""

    def __init__(
        self,
        base_url: str,
        token_provider: TokenProvider,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        verify: bool = True,
        patient_id_template: str = "",
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token_provider = token_provider
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=timeout, verify=verify)
        self._pid_template = patient_id_template
        self._now = now

    def _patient_uuid(self, pid: PatientId) -> str:
        """Map the agent's integer pid to the OpenEMR patient **UUID**.

        Only the encounter routes need this. ``GET``/``POST
        /api/patient/:puuid/encounter`` (``_rest_routes_standard.inc.php``
        :105/:112) are keyed by the patient UUID —
        ``EncounterRestController::getAll`` documents "Route parameter is always
        a UUID string", and ``EncounterService::insertEncounter`` feeds it to
        ``UuidRegistry::uuidToBytes`` → ``Uuid::fromString``, which throws on an
        integer. The sibling vital (:140) and medication (:305) routes really are
        pid-keyed and must keep sending the raw pid.

        Mirrors ``_PatientMappedFhirClient`` (``copilot/fhir/provider.py``:88-94):
        same ``settings.fhir_patient_id_template``, same ``{pid}`` format.

        Fails LOUDLY when unmapped or when the template yields a non-UUID rather
        than putting a value on the wire that OpenEMR can only reject — a write
        that cannot be confirmed is FAILED, and an unconfigurable one should say
        so in its own words, not as an opaque 400/500 from the server.
        """
        if not self._pid_template:
            raise OpenEmrWriteError(
                "cannot resolve an encounter: the OpenEMR encounter route is keyed by "
                "patient UUID, but no patient-id mapping is configured. Set "
                "COPILOT_FHIR_PATIENT_ID_TEMPLATE (e.g. "
                "'a1000000-0000-0000-0000-{pid:012d}'). Refusing to send the integer "
                f"pid {pid} to a UUID-keyed route."
            )
        try:
            candidate = self._pid_template.format(pid=pid.value)
        except (IndexError, KeyError, ValueError) as exc:
            raise OpenEmrWriteError(
                f"patient-id template {self._pid_template!r} is not a valid '{{pid}}' "
                "format string"
            ) from exc
        try:
            return str(UUID(candidate))
        except ValueError as exc:
            raise OpenEmrWriteError(
                f"patient-id template produced {candidate!r}, which is not a UUID; "
                "the OpenEMR encounter route would reject it"
            ) from exc

    async def __aenter__(self) -> OpenEmrWriteClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --- public write surface --------------------------------------------

    async def resolve_or_create_encounter(self, pid: PatientId) -> str:
        """Reuse today's most-recent encounter, else create a minimal one.

        A vital attaches to an encounter, so the write path needs an ``eid``.
        Reusing today's encounter keeps a bedside session's readings together;
        absent one, a minimal "Co-Pilot bedside entry" encounter is created.

        Both calls are keyed by patient **UUID**, not pid — see ``_patient_uuid``.
        """
        puuid = self._patient_uuid(pid)
        resp = await self._send("GET", f"/patient/{puuid}/encounter")
        if resp.status_code == 200:
            today = self._now().date().isoformat()
            existing = _most_recent_today(_envelope_list(self._json(resp)), today)
            if existing is not None:
                return existing
        elif resp.status_code != 404:
            raise OpenEmrWriteError(
                f"encounter lookup returned status={resp.status_code}",
                status_code=resp.status_code,
            )

        payload = {"date": self._now().date().isoformat(), "reason": _ENCOUNTER_REASON}
        created = await self._send("POST", f"/patient/{puuid}/encounter", json_body=payload)
        self._require(created, 201)
        data = self._json(created).get("data")
        if not isinstance(data, Mapping):
            raise OpenEmrWriteError("encounter create returned no data object")
        return _require_id(data, "id")

    async def create_vital(
        self,
        pid: PatientId,
        eid: str,
        vital: VitalWrite,
        *,
        idempotency_key: str | None = None,
    ) -> CommittedWrite:
        """POST a new single-column vitals form. Returns proof or raises."""
        payload = {_vital_column(vital.metric): _fmt_num(vital.value)}
        resp = await self._send(
            "POST",
            f"/patient/{pid}/encounter/{eid}/vital",
            json_body=payload,
            idempotency_key=idempotency_key,
        )
        self._require(resp, 201)
        new_id = _require_id(self._json(resp), "vid")
        return CommittedWrite(
            resource_kind=WriteKind.vital,
            new_id=new_id,
            encounter_id=str(eid),
            committed_at=self._now(),
        )

    async def create_medication(
        self,
        pid: PatientId,
        med: MedicationWrite,
        *,
        idempotency_key: str | None = None,
    ) -> CommittedWrite:
        """POST a new medication-list row. Returns proof or raises.

        **No provenance in the record itself, deliberately** — same finding as
        ``create_medical_problem``, one layer deeper. This route reaches
        ``ListService::insert``, whose INSERT names a fixed column list
        (``pid, type, title, begdate, enddate, diagnosis``); ``comments`` is
        never bound, so it would be silently discarded no matter what we send.
        Medication provenance therefore lives in ``audit_log.source_ref``.
        """
        payload: dict[str, str] = {"title": med.title, "begdate": med.begdate}
        if med.enddate:
            payload["enddate"] = med.enddate
        if med.diagnosis:
            payload["diagnosis"] = med.diagnosis
        resp = await self._send(
            "POST",
            f"/patient/{pid}/medication",
            json_body=payload,
            idempotency_key=idempotency_key,
        )
        self._require(resp, 201)
        new_id = _require_id(self._json(resp), "id")
        return CommittedWrite(
            resource_kind=WriteKind.medication,
            new_id=new_id,
            encounter_id=None,
            committed_at=self._now(),
        )

    async def retract_medication(
        self,
        pid: PatientId,
        mid: str,
        *,
        idempotency_key: str | None = None,
    ) -> CommittedWrite:
        """End-date a medication as a *compensating* append (never a delete).

        Reversibility path (§6): a bad write is undone by marking the med ended,
        not by destroying the record. Present on the client but not surfaced in
        the Phase-1 UI.
        """
        payload = {"enddate": self._now().date().isoformat()}
        resp = await self._send(
            "PUT",
            f"/patient/{pid}/medication/{mid}",
            json_body=payload,
            idempotency_key=idempotency_key,
        )
        self._require(resp, 200)
        new_id = _require_id(self._json(resp), "id")
        return CommittedWrite(
            resource_kind=WriteKind.medication,
            new_id=new_id,
            encounter_id=None,
            committed_at=self._now(),
        )

    # --- issue writes (F4b: intake-derived, physician-confirmed) ----------

    async def create_medical_problem(
        self,
        pid: PatientId,
        problem: MedicalProblemWrite,
        *,
        idempotency_key: str | None = None,
    ) -> CommittedWrite:
        """POST a new medical_problem list row. Returns proof or raises.

        Standard-API route ``POST /patient/{pid}/medical_problem`` — the
        physician-confirmed commit of an intake-derived problem. Fail-closed
        like every write: only an explicit ``201`` whose body carries a
        parseable id is success.

        **No provenance in the record itself, deliberately.** Unlike
        ``create_allergy``, this route has no honest field for it:
        ``ConditionRestController::WHITELISTED_FIELDS`` is exactly
        ``['title', 'begdate', 'enddate', 'diagnosis']``, and ``filterData``
        ``array_filter``s every other key out *silently* — a ``comments`` field
        would be accepted with a 201 and then dropped on the floor, producing a
        record that looks traceable in our code and is not in the chart. Worse
        would be smuggling provenance into ``title`` or ``diagnosis``, which are
        clinical fields that feed problem-list display and coding. So the
        provenance for a medical_problem lives where it can be trusted: the
        ``audit_log.source_ref`` trail and the agent store's FK chain. Restore
        this only if the upstream whitelist gains ``comments``.
        """
        payload: dict[str, str] = {"title": problem.title, "begdate": problem.begdate}
        if problem.diagnosis:
            payload["diagnosis"] = problem.diagnosis
        resp = await self._send(
            "POST",
            f"/patient/{pid}/medical_problem",
            json_body=payload,
            idempotency_key=idempotency_key,
        )
        self._require(resp, 201)
        new_id = _require_id(self._json(resp), "id")
        return CommittedWrite(
            resource_kind=WriteKind.medical_problem,
            new_id=new_id,
            encounter_id=None,
            committed_at=self._now(),
        )

    async def create_allergy(
        self,
        pid: PatientId,
        allergy: AllergyWrite,
        *,
        idempotency_key: str | None = None,
        source: WriteSource | None = None,
    ) -> CommittedWrite:
        """POST a new allergy list row. Returns proof or raises.

        Standard-API route ``POST /patient/{pid}/allergy`` — the
        physician-confirmed commit of an intake-derived allergy. Fail-closed
        like every write: only an explicit ``201`` whose body carries a
        parseable id is success.

        ``source``, when present, is rendered into ``comments`` so the record
        lands in OpenEMR **traceable to the page it came from**, readable by a
        physician who never opens the agent. This is the ONLY write kind that
        gets provenance in the record itself, and only because the allergy route
        genuinely accepts it: ``AllergyIntoleranceRestController`` whitelists
        ``comments`` (``WHITELISTED_FIELDS``), ``AllergyIntoleranceService``
        passes it through ``buildInsertColumns``, and ``lists.comments`` is a
        real ``longtext`` column. Verified end-to-end, not assumed — the sibling
        routes are checked in ``create_medical_problem`` / ``create_medication``
        and honestly cannot carry it.
        """
        payload: dict[str, str] = {"title": allergy.title, "begdate": allergy.begdate}
        # `reaction` is NOT accepted by OpenEMR: AllergyIntoleranceRestController's
        # WHITELISTED_FIELDS is {title, begdate, enddate, diagnosis, comments} and
        # filterData() SILENTLY drops everything else — so sending it returned 201
        # while the reaction never reached the chart. A physician confirming
        # "Penicillin — rash and hives" lost the clinically important half with no
        # error anywhere. Both the reaction and the provenance therefore ride
        # `comments`, the only field OpenEMR actually persists for them.
        comment = _allergy_comment(allergy.reaction, source)
        if comment:
            payload["comments"] = comment
        resp = await self._send(
            "POST",
            f"/patient/{pid}/allergy",
            json_body=payload,
            idempotency_key=idempotency_key,
        )
        self._require(resp, 201)
        new_id = _require_id(self._json(resp), "id")
        return CommittedWrite(
            resource_kind=WriteKind.allergy,
            new_id=new_id,
            encounter_id=None,
            committed_at=self._now(),
        )

    # --- document upload (Week-2 ingestion) -------------------------------

    async def upload_document(
        self,
        pid: PatientId,
        content: bytes,
        *,
        filename: str = "document.pdf",
        doc_type: str = "lab_pdf",
        category: str | None = None,
        mime_type: str = "application/pdf",
        idempotency_key: str | None = None,
    ) -> str:
        """Multipart-POST a source document to the Standard API; return its handle.

        The Week-2 document-ingestion pipeline stores the source bytes in OpenEMR
        (which owns the document) before deriving pages/facts from them.

        **This call is written against OpenEMR's ACTUAL contract, which is not the
        one the rest of this client uses** — verified in OpenEMR's own source, and
        every difference used to be a silent failure here:

        - ``DocumentRestController::postWithPath`` returns
          ``responseHandler($serviceResult, null, 200)`` — **200, not 201**. We
          required 201, so a *successful* upload raised.
        - ``DocumentService::insertAtPath`` returns a bare ``true`` on success, so
          there is **no id to parse**. We demanded one and failed the upload that
          had, in fact, worked. On failure it returns ``false`` → ``responseHandler``
          emits **404 with an empty body**, so 404 (not a 4xx-generic) is the real
          failure signal.
        - The route reads ``$request->query->get('path')`` — the category is a
          **query parameter**. We sent it as form data, so OpenEMR saw ``null`` and
          ``isValidPath(null)`` rejected the upload.

        Because OpenEMR returns no id, a successful upload yields
        :data:`OPENEMR_NO_HANDLE` rather than an invented one — the honest record of
        "OpenEMR has this document; its API gave us no handle for it". An id is
        still parsed opportunistically in case a deployment's envelope carries one.
        Fail-closed is unchanged: anything but a 200 with a truthy body raises.
        """
        files = {"document": (filename, content, mime_type)}
        # `doc_type` is ours (not read by the route); `path` MUST ride the query
        # string — the controller takes it from $request->query, never the body.
        path = f"/patient/{pid}/document"
        if category:
            path = f"{path}?path={quote(category, safe='/')}"
        resp = await self._send(
            "POST",
            path,
            files=files,
            data={"doc_type": doc_type},
            idempotency_key=idempotency_key,
        )
        # OpenEMR's own DocumentRestController returns 200 (responseHandler(..., 200))
        # — this client used to demand 201, so a SUCCESSFUL upload raised. Requiring
        # only 200 would be the same brittleness inverted: both codes mean "created",
        # and which one a deployment sends is not a thing to be dogmatic about. The
        # real failure signal (404 + empty body) still raises, as does anything else.
        self._require(resp, (200, 201))
        body = _try_json(resp)
        if body is False or body is None:
            raise OpenEmrWriteError("OpenEMR rejected the document upload")
        if isinstance(body, Mapping):
            try:
                return _document_id(body)
            except OpenEmrWriteError:
                return OPENEMR_NO_HANDLE
        return OPENEMR_NO_HANDLE

    # --- transport --------------------------------------------------------

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        files: Mapping[str, tuple[str, bytes, str]] | None = None,
        data: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> httpx.Response:
        """One request with a single forced-refresh retry on 401.

        A JSON body (``json_body``) and a multipart body (``files`` + optional
        ``data`` form fields) are mutually exclusive shapes; multipart sets no
        explicit ``Content-Type`` so httpx emits the ``multipart/form-data``
        boundary itself. Transport errors (timeouts, connection failures) and
        token-acquisition failures raise ``OpenEmrWriteError`` — the write is
        unconfirmed, so it is FAILED, never assumed committed.
        """
        url = f"{self._base_url}{path}"

        async def _do(force_refresh: bool) -> httpx.Response:
            token = await self._token_provider.get_token(force=force_refresh)
            headers = {
                "Authorization": f"{token.token_type} {token.access_token}",
                "Accept": "application/json",
            }
            if json_body is not None and files is None:
                headers["Content-Type"] = "application/json"
            if idempotency_key:
                headers["Idempotency-Key"] = idempotency_key
            if files is not None:
                return await self._client.request(
                    method, url, files=files, data=data, headers=headers
                )
            return await self._client.request(method, url, json=json_body, headers=headers)

        try:
            resp = await _do(force_refresh=False)
            if resp.status_code == 401:
                resp = await _do(force_refresh=True)
        except TokenAcquisitionError as exc:
            raise OpenEmrWriteError(f"token acquisition failed: {exc}") from exc
        except httpx.HTTPError as exc:
            raise OpenEmrWriteError("write could not be confirmed (transport error)") from exc
        return resp

    def _require(self, resp: httpx.Response, expected: int | tuple[int, ...]) -> None:
        """Raise unless the response carries an expected success code.

        Accepts a tuple where OpenEMR's success code genuinely varies. That is not
        laxity: every other status — including the 404-with-empty-body that IS the
        document route's failure signal — still raises, so fail-closed is intact.
        """
        allowed = (expected,) if isinstance(expected, int) else expected
        if resp.status_code in allowed:
            return
        validation = None
        if resp.status_code in (400, 422):
            body = _try_json(resp)
            if isinstance(body, Mapping):
                validation = body.get("validationErrors") or body.get("validation") or body
        raise OpenEmrWriteError(
            f"write returned status={resp.status_code} "
            f"(expected {allowed[0] if len(allowed) == 1 else ' or '.join(map(str, allowed))})",
            status_code=resp.status_code,
            validation=validation,
        )

    def _json(self, resp: httpx.Response) -> dict[str, Any]:
        """Parse a JSON object body, or fail-closed (unconfirmed ⇒ FAILED)."""
        body = _try_json(resp)
        if not isinstance(body, dict):
            raise OpenEmrWriteError("write response body was not a JSON object")
        return body


# --- module helpers ---------------------------------------------------------


def _allergy_comment(reaction: str | None, source: WriteSource | None) -> str:
    """Fold the reaction and the source provenance into OpenEMR's one usable field.

    OpenEMR accepts only ``{title, begdate, enddate, diagnosis, comments}`` on an
    allergy (``AllergyIntoleranceRestController::WHITELISTED_FIELDS``); anything
    else is silently dropped by ``filterData``. ``comments`` is therefore the only
    place either value can land, so both share it — reaction first, because that is
    the clinical content a physician reads; provenance second, because that is
    audit context. Either may be absent; an empty result means send no field at all
    rather than an empty one.
    """
    parts = [f"Reaction: {reaction}" if reaction else "", source.provenance_note() if source else ""]
    return " | ".join(part for part in parts if part)


def _try_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception as exc:  # any parse failure ⇒ unconfirmed ⇒ FAILED
        raise OpenEmrWriteError("write response was not JSON") from exc


def _require_id(body: Mapping[str, Any], key: str) -> str:
    """Extract a non-empty id under ``key``, or fail-closed.

    Accepts an int or a non-empty string; an int is stringified. A missing,
    empty, or non-scalar id means we cannot confirm the write — treated FAILED.
    """
    value = body.get(key)
    if isinstance(value, bool):  # bool is an int subclass — never a valid id
        raise OpenEmrWriteError(f"write response {key!r} was not a usable id")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value
    raise OpenEmrWriteError(f"write response missing a usable {key!r} id")


def _document_id(body: Mapping[str, Any]) -> str:
    """Extract a usable document id from the create envelope, or fail-closed.

    OpenEMR's document create envelope is not standardized across ids, so try the
    document-specific key first, then the generic id/uuid, at the top level and
    inside a ``data`` object. An int is stringified; a missing/empty/non-scalar id
    means the upload is unconfirmed — treated FAILED.
    """
    sources: list[Mapping[str, Any]] = [body]
    nested = body.get("data")
    if isinstance(nested, Mapping):
        sources.append(nested)
    for source in sources:
        for key in ("document_id", "id", "uuid"):
            value = source.get(key)
            if isinstance(value, bool):  # bool is an int subclass — never a valid id
                continue
            if isinstance(value, int):
                return str(value)
            if isinstance(value, str) and value.strip():
                return value
    raise OpenEmrWriteError("document upload response missing a usable document id")


def _envelope_list(body: Mapping[str, Any]) -> list[Any]:
    """The ``data`` list of a Standard-API list envelope, or ``[]``."""
    data = body.get("data")
    return data if isinstance(data, list) else []


def _most_recent_today(encounters: list[Any], today: str) -> str | None:
    """The id of the latest encounter dated ``today`` (``YYYY-MM-DD``), or None."""
    dated: list[tuple[str, str]] = []
    for enc in encounters:
        if not isinstance(enc, Mapping):
            continue
        date_value = enc.get("date")
        raw_id = enc.get("id")
        if isinstance(date_value, str) and date_value.startswith(today) and raw_id is not None:
            text_id = str(raw_id)
            if text_id:
                dated.append((date_value, text_id))
    if not dated:
        return None
    dated.sort()
    return dated[-1][1]
