"""Observation time-series drill-down API — one metric, many grounded points.

``GET /v1/patients/{patient_id}/observations?metric=<label>&clinician_id=<int>``
returns a patient's readings for a single metric, oldest→newest, so the UI can
draw a trend line. It is deliberately *orthogonal* to the verified-claim
contract: the rounds card carries exactly one point-in-time claim per metric
(protecting verification's 1-claim-1-value invariant), while this endpoint is a
lazily-fetched series where each point stays independently grounded
(``resource_id`` + verbatim ``value`` + ``timestamp``) — as auditable as a claim.

Fail-closed throughout:

- Same rounding-list authorization gate as chat (``is_authorized``) → **403**
  when the clinician has not established this patient on their list. Because the
  query is patient-scoped *and* authorization-gated, there is no cross-patient
  leak.
- A point with no groundable value or no usable timestamp is **dropped**.
- A reading recorded in a **different unit** from the series' own (``Cel`` in a
  ``degF`` history) is **dropped**, never relabelled with the series unit and
  never converted — see :func:`_series_unit`.
- An unknown/absent metric returns an **empty ``points`` list** — never a
  fabricated series.

Mounted automatically by ``copilot.api.app.register_routers`` (it exposes a
module-level ``router``); no edit to ``app.py`` is required.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Request

from copilot.agent.grounding import describe_resource, extract_temporal, humanize_label
from copilot.api.deps import resolve_acting_context
from copilot.auth import is_authorized
from copilot.config import get_settings
from copilot.domain.contracts import (
    ObservationSeries,
    ObservationSeriesPoint,
    ReferenceRange,
)
from copilot.domain.primitives import ClinicianId, PatientId, ResourceType
from copilot.fhir.client import FhirClient
from copilot.fhir.provider import build_fhir_client, build_fhir_client_for_session
from copilot.memory.db import session_scope
from copilot.memory.repository import MemoryRepository
from copilot.observability import Observability, current_correlation_id
from copilot.rounds.ranges import reference_bounds
from copilot.rounds.summary import _unit, group_observations

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["observations"])

# Flags that mean "no abnormality" — an HL7 ``N`` / OpenEMR ``no`` collapses to
# an empty string so the chart never colours a normal point.
_NORMAL_FLAGS = frozenset({"", "n", "no", "normal"})

# Per-request physician-session id, set by the route in smart mode so the
# zero-arg ``_fhir_client`` seam can build the physician's delegated per-session
# reader without changing its signature (tests monkeypatch that zero-arg seam).
# ``None`` (disabled mode / no session) ⇒ the shared system-token reader.
_session_id_ctx: ContextVar[str | None] = ContextVar("observations_session_id", default=None)


def _fhir_client() -> FhirClient:
    """Build the FHIR reader for one series fetch.

    Smart mode: when a physician session id is set for this request, build the
    physician's delegated per-session client, so OpenEMR attributes the read to
    that physician. Otherwise (disabled mode): the environment-appropriate system
    client — real Backend Services token when configured, else a stub bearer (see
    ``copilot.fhir.provider.build_token_provider``). A module-level seam so tests
    can substitute an in-memory double.
    """
    settings = get_settings()
    session_id = _session_id_ctx.get()
    if session_id is not None:
        return build_fhir_client_for_session(settings, session_id)
    return build_fhir_client(settings)


@router.get(
    "/patients/{patient_id}/observations",
    summary="A patient's grounded time-series for one metric (oldest→newest)",
)
async def observation_series(
    patient_id: Annotated[int, Path(gt=0)],
    metric: Annotated[str, Query(min_length=1)],
    request: Request,
    clinician_id: Annotated[int | None, Query(gt=0)] = None,
) -> ObservationSeries:
    # Parse the raw ids into validated primitives at the boundary.
    pid = PatientId(value=patient_id)
    # Identity per the auth-mode contract: disabled → the query clinician_id;
    # smart → the session cookie (401 if none, 403 if the query id disagrees). The
    # session id (smart mode) selects the physician's delegated read token.
    acting = await resolve_acting_context(get_settings(), request, clinician_id)
    cid = acting.clinician_id

    # Authorization boundary (UC-6), identical to chat: refuse a patient the
    # clinician has not established on their rounding list — never leak. Generic
    # reason: no internal detail about who is (or is not) authorized.
    if not await is_authorized(cid, pid):
        raise HTTPException(status_code=403, detail="Patient is not on your rounding list")

    obs: Observability = request.app.state.observability
    # Bind the physician session (smart mode) so the zero-arg ``_fhir_client``
    # seam builds the delegated per-session reader; always reset it afterwards.
    ctx_token = _session_id_ctx.set(acting.session_id)
    try:
        async with (
            obs.span("observations.series", clinician_id=cid.value, patient_id=pid.value),
            _fhir_client() as fhir,
        ):
            bundle = await fhir.search(ResourceType.Observation, {"patient": str(pid)})
    finally:
        _session_id_ctx.reset(ctx_token)

    resources = _bundle_resources(bundle)

    # Group by the humanized metric label (reusing the summary grouping helper),
    # then keep every group whose label matches the requested metric. Merging
    # across groups is defensive: two raw code displays can humanize to one label.
    matched: list[Mapping[str, Any]] = []
    for raw_label, group in group_observations(resources).items():
        if humanize_label(raw_label) == metric:
            matched.extend(group)

    # Each point is grounded independently; unusable points are dropped.
    scored = [pt for res in matched if (pt := _point(res)) is not None]
    scored.sort(key=lambda pt: pt[0])

    # One unit for the whole series, and *only* the readings recorded in it: a
    # point is never relabelled with another point's unit (see _series_unit).
    unit = _series_unit(scored)
    series = ObservationSeries(
        patient_id=pid.value,
        metric=metric,
        unit=unit,
        reference_range=_series_range([res for res in matched if _unit(res) == unit]),
        points=[point for _, point_unit, point in scored if point_unit == unit],
    )

    # HIPAA §164.312(b): this authorized read returned PHI (lab values), so it
    # leaves an append-only trail. Recorded after the series is built, never on
    # the 403 path (no PHI was returned there).
    await _record_read_audit(cid, pid, series)

    return series


# --- helpers ---------------------------------------------------------------


async def _record_read_audit(
    clinician_id: ClinicianId, patient_id: PatientId, series: ObservationSeries
) -> None:
    """Append the HIPAA access-trail row for this observation-series PHI read.

    Fail-open: the series is already produced and returned to the clinician, so a
    failed audit write must never turn a served read into an error. The write
    runs in its own transaction; any failure is logged and swallowed.
    ``resources_returned`` is the set of Observations the series actually returned
    — empty for an unknown/absent metric, in which case the authorized access is
    still recorded (a row naming the patient and clinician, with no resources).
    """
    try:
        async with session_scope() as session:
            await MemoryRepository(session).record_audit(
                correlation_id=current_correlation_id(),
                action="observations.series",
                patient_id=patient_id,
                clinician_id=clinician_id.value,
                resources_returned=[point.resource_id for point in series.points],
            )
    except Exception:
        _logger.exception(
            "failed to write observation-series read audit row",
            extra={"patient_id": patient_id.value, "clinician_id": clinician_id.value},
        )


def _bundle_resources(bundle: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The resource dicts of a search Bundle, skipping malformed entries."""
    out: list[Mapping[str, Any]] = []
    for entry in bundle.get("entry") or []:
        if isinstance(entry, Mapping):
            res = entry.get("resource")
            if isinstance(res, Mapping):
                out.append(res)
    return out


def _point(res: Mapping[str, Any]) -> tuple[datetime, str, ObservationSeriesPoint] | None:
    """Ground one Observation into ``(instant, unit, point)``, or ``None`` to drop it.

    Fail-closed: no groundable value, no timestamp, an unparseable timestamp, or
    a missing resource id all drop the point rather than fabricate a reading. The
    stored ``value``/``timestamp`` are verbatim from the same extractors the
    verification gate uses, so a plotted point is as auditable as a claim.

    The reading's own display unit is returned alongside — never folded into the
    point — so the caller can keep the series to a single unit. It rides beside
    the point rather than on it because ``ObservationSeriesPoint`` is a published
    response contract; the caller drops mismatched points, so every point that
    survives is in the series' unit and nothing needs a per-point label.
    """
    described = describe_resource(res)
    if described is None:
        return None
    value = described[2]
    if not value.strip():
        return None

    raw_ts = extract_temporal(res)
    if raw_ts is None:
        return None
    instant = _parse_instant(raw_ts)
    if instant is None:
        return None

    rid = res.get("id")
    if not isinstance(rid, str) or not rid:
        return None

    return (
        instant,
        _unit(res),
        ObservationSeriesPoint(
            resource_id=rid,
            value=value,
            timestamp=raw_ts,
            abnormal=_abnormal_flag(res),
        ),
    )


def _parse_instant(raw: str) -> datetime | None:
    """Parse an ISO timestamp (tolerating a trailing ``Z``) to an aware instant."""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _abnormal_flag(res: Mapping[str, Any]) -> str:
    """The Observation's abnormal flag, or ``''`` when normal/absent.

    Prefers ``interpretation[0].coding[0].code`` (US Core convention); falls back
    to a top-level ``abnormal`` (OpenEMR seed convention). A normal indication
    collapses to an empty string so the chart only marks true out-of-range points.
    """
    flag = _raw_abnormal(res)
    return "" if flag.strip().lower() in _NORMAL_FLAGS else flag


def _raw_abnormal(res: Mapping[str, Any]) -> str:
    interp = res.get("interpretation")
    if isinstance(interp, list) and interp and isinstance(interp[0], Mapping):
        coding = interp[0].get("coding")
        if isinstance(coding, list) and coding and isinstance(coding[0], Mapping):
            code = coding[0].get("code")
            if isinstance(code, str):
                return code
    raw = res.get("abnormal")
    return raw if isinstance(raw, str) else ""


def _series_unit(points: Sequence[tuple[datetime, str, ObservationSeriesPoint]]) -> str:
    """The series' unit: the unit of the **newest** usable reading (``''`` if none).

    Not "the first non-empty unit across the readings" (the old rule). OpenEMR
    permits a temperature in ``Cel`` and in ``degF`` — and a weight in kg and lb —
    within one metric's history, and that rule stamped one reading's unit onto
    every point in the series: 37.0 and 98.6 plotted on a single axis labelled °F,
    which a physician reads as profound hypothermia. The unit is now decided by
    one reading and applied only to readings that actually carry it; the caller
    drops the rest.

    The newest reading wins so the chart's axis agrees with the unit the rounds
    card prints for that metric's current value. Consequence, accepted knowingly:
    if the newest reading switches units, older points in the old unit drop out
    and the chart goes sparse until history refills in the new unit. A sparse
    chart is honest; a relabelled one is a lie.

    Points are **dropped rather than converted** on purpose.
    ``ObservationSeriesPoint.value`` is the verbatim source string — the same
    discipline a claim's citation keeps — so a converted point would plot a number
    that appears in no record and could not be audited back to one. °C→°F is an
    exact mapping and would be safe to compute, but there is nowhere honest to put
    the result under this contract, and a table that also had to cover kg/lb,
    mmol/L↔mg/dL (analyte-specific) and the rest would be a fabrication surface.
    """
    return points[-1][1] if points else ""


def _series_range(resources: Sequence[Mapping[str, Any]]) -> ReferenceRange | None:
    """The metric's reference band — the first derivable across its readings.

    The caller passes only the readings recorded in the series' unit, so the band
    describes the axis the points are plotted on: a ``Cel`` record's 36.1-37.2
    can never end up bounding a °F chart.
    """
    for res in resources:
        rng = _reference_range_of(res)
        if rng is not None:
            return rng
    return None


def _reference_range_of(res: Mapping[str, Any]) -> ReferenceRange | None:
    """The metric's reference band, via the shared grounded parser.

    Reuses :func:`copilot.rounds.ranges.reference_bounds`, which reads the
    structured ``low.value`` / ``high.value`` and falls back to parsing a
    free-text ``referenceRange[0].text`` — so a one-sided text range like
    troponin's ``"<0.04"`` now yields a (half-open) band instead of nothing.
    """
    low, high = reference_bounds(res)
    if low is None and high is None:
        return None
    return ReferenceRange(low=low, high=high)
