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

import httpx

from copilot.domain.primitives import PatientId, utcnow
from copilot.domain.writes import (
    CommittedWrite,
    MedicationWrite,
    VitalWrite,
    WritableMetric,
    WriteKind,
)
from copilot.fhir.auth import TokenAcquisitionError, TokenProvider

_ENCOUNTER_REASON = "AgentForge Co-Pilot bedside entry"


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
    """Small async writer for vitals + medications on the Standard REST API."""

    def __init__(
        self,
        base_url: str,
        token_provider: TokenProvider,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        verify: bool = True,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token_provider = token_provider
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=timeout, verify=verify)
        self._now = now

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
        """
        resp = await self._send("GET", f"/patient/{pid}/encounter")
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
        created = await self._send("POST", f"/patient/{pid}/encounter", json_body=payload)
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
        """POST a new medication-list row. Returns proof or raises."""
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

    # --- transport --------------------------------------------------------

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> httpx.Response:
        """One request with a single forced-refresh retry on 401.

        Transport errors (timeouts, connection failures) and token-acquisition
        failures raise ``OpenEmrWriteError`` — the write is unconfirmed, so it is
        FAILED, never assumed committed.
        """
        url = f"{self._base_url}{path}"

        async def _do(force_refresh: bool) -> httpx.Response:
            token = await self._token_provider.get_token(force=force_refresh)
            headers = {
                "Authorization": f"{token.token_type} {token.access_token}",
                "Accept": "application/json",
            }
            if json_body is not None:
                headers["Content-Type"] = "application/json"
            if idempotency_key:
                headers["Idempotency-Key"] = idempotency_key
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

    def _require(self, resp: httpx.Response, expected: int) -> None:
        """Raise unless the response is exactly the expected success code."""
        if resp.status_code == expected:
            return
        validation = None
        if resp.status_code in (400, 422):
            body = _try_json(resp)
            if isinstance(body, Mapping):
                validation = body.get("validationErrors") or body.get("validation") or body
        raise OpenEmrWriteError(
            f"write returned status={resp.status_code} (expected {expected})",
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
