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
- An unknown/absent metric returns an **empty ``points`` list** — never a
  fabricated series.

Mounted automatically by ``copilot.api.app.register_routers`` (it exposes a
module-level ``router``); no edit to ``app.py`` is required.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Request

from copilot.agent.grounding import describe_resource, extract_temporal, humanize_label
from copilot.auth import is_authorized
from copilot.config import get_settings
from copilot.domain.contracts import (
    ObservationSeries,
    ObservationSeriesPoint,
    ReferenceRange,
)
from copilot.domain.primitives import ClinicianId, PatientId, ResourceType
from copilot.fhir.client import FhirClient
from copilot.fhir.provider import build_fhir_client
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


def _fhir_client() -> FhirClient:
    """Build the FHIR reader for one series fetch.

    Real Backend Services token when configured, else a stub bearer — see
    ``copilot.fhir.provider.build_token_provider``. A module-level seam so tests
    can substitute an in-memory double.
    """
    return build_fhir_client(get_settings())


@router.get(
    "/patients/{patient_id}/observations",
    summary="A patient's grounded time-series for one metric (oldest→newest)",
)
async def observation_series(
    patient_id: Annotated[int, Path(gt=0)],
    metric: Annotated[str, Query(min_length=1)],
    clinician_id: Annotated[int, Query(gt=0)],
    request: Request,
) -> ObservationSeries:
    # Parse the raw ids into validated primitives at the boundary.
    pid = PatientId(value=patient_id)
    cid = ClinicianId(value=clinician_id)

    # Authorization boundary (UC-6), identical to chat: refuse a patient the
    # clinician has not established on their rounding list — never leak. Generic
    # reason: no internal detail about who is (or is not) authorized.
    if not await is_authorized(cid, pid):
        raise HTTPException(status_code=403, detail="Patient is not on your rounding list")

    obs: Observability = request.app.state.observability
    async with (
        obs.span("observations.series", clinician_id=cid.value, patient_id=pid.value),
        _fhir_client() as fhir,
    ):
        bundle = await fhir.search(ResourceType.Observation, {"patient": str(pid)})

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

    series = ObservationSeries(
        patient_id=pid.value,
        metric=metric,
        unit=_series_unit(matched),
        reference_range=_series_range(matched),
        points=[point for _, point in scored],
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


def _point(res: Mapping[str, Any]) -> tuple[datetime, ObservationSeriesPoint] | None:
    """Ground one Observation into a series point, or ``None`` to drop it.

    Fail-closed: no groundable value, no timestamp, an unparseable timestamp, or
    a missing resource id all drop the point rather than fabricate a reading. The
    stored ``value``/``timestamp`` are verbatim from the same extractors the
    verification gate uses, so a plotted point is as auditable as a claim.
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


def _series_unit(resources: Sequence[Mapping[str, Any]]) -> str:
    """The metric's display unit — the first non-empty across its readings."""
    for res in resources:
        unit = _unit(res)
        if unit:
            return unit
    return ""


def _series_range(resources: Sequence[Mapping[str, Any]]) -> ReferenceRange | None:
    """The metric's reference band — the first derivable across its readings."""
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
