"""Build the patient-card chart summary — one row per metric, with a trend.

The chart summary is a point-in-time snapshot, not a time series: a physician
wants the *current* value of each metric plus how it moved, not a flat list of
every reading with no dates. So this collapses a patient's fetched resources
into one claim per clinical concept:

- **Observations** (labs/vitals) are grouped by metric and collapsed to their
  **latest** reading, annotated with the change (↑/↓) and the elapsed time since
  the prior reading — e.g. "Heart rate: 92 /min  ↓12 · 22h since prior".
- **Everything else** (conditions, meds, allergies) appears once, as-is.

Deterministic: the same resources always yield the same summary. Every claim's
source_ref points at the exact resource it came from, so the trust story holds.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from copilot.agent.grounding import (
    claim_text,
    describe_resource,
    extract_temporal,
    humanize_label,
)
from copilot.domain.contracts import Claim
from copilot.domain.primitives import FhirReference, ResourceType


def build_summary_claims(resources: Sequence[Mapping[str, Any]]) -> list[Claim]:
    """Collapse fetched resources into one grounded claim per metric/concept."""
    observations: list[Mapping[str, Any]] = []
    others: list[Mapping[str, Any]] = []
    for res in resources:
        rtype = res.get("resourceType")
        if not isinstance(rtype, str) or res.get("id") is None:
            continue
        (observations if rtype == "Observation" else others).append(res)

    claims: list[Claim] = []

    # Observations → one-per-metric (latest reading) + a trend vs the prior one.
    for group in group_observations(observations).values():
        claim = _observation_claim(group)
        if claim is not None:
            claims.append(claim)

    # Everything else → one claim each.
    for res in others:
        described = describe_resource(res)
        if described is None:
            continue
        display, field, value = described
        rtype = str(res.get("resourceType"))
        claims.append(
            Claim(
                text=claim_text(rtype, display, str(value)),
                source_ref=FhirReference(
                    resource_type=ResourceType(rtype)
                    if rtype in ResourceType.__members__
                    else ResourceType.Observation,
                    resource_id=str(res.get("id")),
                    field=field,
                    value=str(value),
                    timestamp=extract_temporal(res),
                ),
            )
        )
    return _dedupe_medications(claims)


def build_change_claims(resources: Sequence[Mapping[str, Any]], hours: float = 12.0) -> list[Claim]:
    """What changed since the physician last saw the patient (~``hours`` ago).

    Anchored to the patient's own timeline: the reference "now" is the latest
    observation time, and a metric is reported only if its latest reading falls
    within ``hours`` of that AND is notable — either abnormal (an interpretation
    flag / OpenEMR ``abnormal``) or moved vs the prior reading. Returns ``[]``
    when the data carries no timestamps to anchor the window. Deterministic.
    """
    groups = group_observations(resources)
    times = [t for group in groups.values() for res in group if (t := _effective(res)) is not None]
    if not times:
        return []
    window_start = max(times) - timedelta(hours=hours)

    claims: list[Claim] = []
    for group in groups.values():
        latest_time = _effective(group[0])
        if latest_time is None or latest_time < window_start:
            continue  # not measured within the window — nothing new to report
        if not (_is_abnormal(group[0]) or _changed(group)):
            continue  # measured recently but unremarkable
        claim = _observation_claim(group)
        if claim is not None:
            claims.append(claim)
    return claims


# --- helpers ---------------------------------------------------------------


def _normalize_medication_value(value: str) -> str:
    """Trim, lowercase, and drop trailing dots so med values compare cleanly."""
    return value.strip().lower().rstrip(".").strip()


def _dedupe_medications(claims: list[Claim]) -> list[Claim]:
    """Collapse duplicate medication rows for the same drug.

    The seed sometimes carries the same drug twice — a bare name
    ("Hydromorphone") and a full sig ("Hydromorphone 0.5 mg IV q4h PRN pain").
    Drop any ``MedicationRequest`` claim whose normalized value is a *strict*
    prefix of another medication claim's value, keeping the longer/more
    informative one. Non-medication claims are never dropped, empty-value claims
    are kept, and original order is preserved. Mirrors ``dedupeMedicationClaims``
    in web/src/labels.ts.
    """
    med_values = [
        _normalize_medication_value(claim.source_ref.value)
        for claim in claims
        if claim.source_ref.resource_type == ResourceType.MedicationRequest
    ]
    result: list[Claim] = []
    for claim in claims:
        if claim.source_ref.resource_type != ResourceType.MedicationRequest:
            result.append(claim)
            continue
        value = _normalize_medication_value(claim.source_ref.value)
        if not value:
            result.append(claim)
            continue
        is_prefix_of_another = any(
            len(other) > len(value) and other.startswith(value) for other in med_values
        )
        if not is_prefix_of_another:
            result.append(claim)
    return result


def group_observations(resources: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    """Group groundable Observations by metric label, each sorted latest-first.

    The metric label is the one :func:`describe_resource` derives, so callers
    that group here agree on both *which* Observations count and *which* reading
    is latest — the chart summary, the deterioration-change view, the per-metric
    series endpoint, and the acuity ranking all collapse identically.
    """
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for res in resources:
        if res.get("resourceType") != "Observation" or res.get("id") is None:
            continue
        described = describe_resource(res)
        if described is None:  # no groundable value (e.g. a panel container)
            continue
        groups.setdefault(described[0], []).append(res)
    for group in groups.values():
        group.sort(key=_sort_key, reverse=True)
    return groups


def _observation_claim(group: list[Mapping[str, Any]]) -> Claim | None:
    """One claim for a metric group: latest value + unit + trend vs prior."""
    latest = group[0]
    described = describe_resource(latest)
    if described is None:
        return None
    label, field, value = described
    unit = _unit(latest)
    head = f"{humanize_label(label)}: {value}{(' ' + unit) if unit else ''}"
    return Claim(
        text=head + _trend(group),
        source_ref=FhirReference(
            resource_type=ResourceType.Observation,
            resource_id=str(latest.get("id")),
            field=field,
            value=str(value),
            timestamp=extract_temporal(latest),
        ),
    )


_ABNORMAL_CODES = {"HH", "LL", "H", "L", "A", "AA"}


def _is_abnormal(res: Mapping[str, Any]) -> bool:
    """True when an Observation carries an abnormal interpretation flag."""
    interp = res.get("interpretation")
    if isinstance(interp, list) and interp and isinstance(interp[0], Mapping):
        coding = interp[0].get("coding")
        if isinstance(coding, list) and coding and isinstance(coding[0], Mapping):
            code = coding[0].get("code")
            if isinstance(code, str) and code.upper() in _ABNORMAL_CODES:
                return True
    raw = res.get("abnormal")
    return isinstance(raw, str) and raw.strip().lower() not in ("", "n", "normal")


def _changed(group: list[Mapping[str, Any]]) -> bool:
    """True when the latest numeric reading differs from the prior one."""
    if len(group) < 2:
        return False
    latest, prior = _numeric(group[0]), _numeric(group[1])
    return latest is not None and prior is not None and latest != prior


def _parse(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _effective(res: Mapping[str, Any]) -> datetime | None:
    """Best clinical timestamp: effectiveDateTime, then issued, then lastUpdated."""
    for key in ("effectiveDateTime", "issued"):
        raw = res.get(key)
        if isinstance(raw, str) and (dt := _parse(raw)) is not None:
            return dt
    meta = res.get("meta")
    if isinstance(meta, Mapping) and isinstance(meta.get("lastUpdated"), str):
        return _parse(meta["lastUpdated"])
    return None


def _sort_key(res: Mapping[str, Any]) -> datetime:
    return _effective(res) or datetime.min.replace(tzinfo=UTC)


# OpenEMR emits UCUM codes in valueQuantity.unit; map the common vitals ones to
# the display a clinician expects. Anything unmapped passes through verbatim.
_UNIT_DISPLAY: dict[str, str] = {
    "in_i": "in",
    "[in_i]": "in",
    "lb_av": "lb",
    "[lb_av]": "lb",
    "degF": "°F",
    "[degF]": "°F",
    "Cel": "°C",
    "degC": "°C",
}


def _unit(res: Mapping[str, Any]) -> str:
    q = res.get("valueQuantity")
    if isinstance(q, Mapping):
        unit = q.get("unit")
        if isinstance(unit, str) and unit.strip() and unit != "1":
            return _UNIT_DISPLAY.get(unit, unit)
    return ""


def _numeric(res: Mapping[str, Any]) -> float | None:
    q = res.get("valueQuantity")
    if isinstance(q, Mapping):
        v = q.get("value")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def _fmt_num(x: float) -> str:
    return str(int(x)) if x == int(x) else f"{x:.1f}"


def _relative(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{max(minutes, 1)}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def _trend(group: list[Mapping[str, Any]]) -> str:
    """Suffix describing the change vs the prior reading, or '' if none."""
    if len(group) < 2:
        return ""
    latest, prior = group[0], group[1]
    le, pe = _effective(latest), _effective(prior)
    gap = _relative((le - pe).total_seconds()) if (le and pe and le > pe) else ""
    tail = f" · {gap} since prior" if gap else ""
    lv, pv = _numeric(latest), _numeric(prior)
    if lv is None or pv is None:
        return f" · updated{tail}" if tail else ""
    delta = lv - pv
    if delta == 0:
        return f"  → no change{tail}"
    arrow = "↑" if delta > 0 else "↓"
    return f"  {arrow}{_fmt_num(abs(delta))}{tail}"
