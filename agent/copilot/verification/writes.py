"""The deterministic write-verification gate — the read-side gate, reversed.

Sibling to ``core.py``: pure, deterministic, **not promptable**. No model calls.
Given a typed ``WriteCandidate`` it returns a ``WriteVerdict`` from three checks:

1. **Enum membership** — the plausibility spec is looked up by an exhaustive
   ``match`` over ``WritableMetric`` (no ``default``), so a metric outside the
   closed set structurally cannot reach the range check.
2. **Unit sanity** — the supplied unit must match the metric's expected unit
   (normalized, alias-aware). A mismatch is a **hard block** — a value in the
   wrong unit is a different value.
3. **Physiologic plausibility** — a small closed per-metric table of absolute
   min/max. Out-of-range is a **soft, overridable warning** for
   ``human_direct`` (a genuine critical value must stay recordable) and a
   **hard block** for the reserved strict mode (Phase 2 agent proposals).

Medications carry no metric/unit/range; their deterministic gate is a
non-empty title (enforced by the type) plus a well-formed ``YYYY-MM-DD``
``begdate``.

Range-message formatting reuses ``rules.py`` helpers so a write warning reads
the same as a read-side reference-range flag.
"""

from __future__ import annotations

from dataclasses import dataclass

from copilot.domain.primitives import is_iso_date
from copilot.domain.writes import (
    WritableMetric,
    WriteCandidate,
    WriteEntryMode,
    WriteKind,
    WriteVerdict,
)
from copilot.verification.rules import _fmt, _format_range


@dataclass(frozen=True)
class MetricSpec:
    """One metric's unit + absolute physiologic bounds.

    ``unit_aliases`` are stored already-normalized (see ``_norm_unit``); the
    canonical unit is included among them. Bounds are deliberately generous —
    "is this possible for any living human", not a reference range.
    """

    canonical_unit: str
    unit_aliases: frozenset[str]
    min_value: float
    max_value: float


def _norm_unit(unit: str) -> str:
    """Fold a unit to a comparison key: lower-case, no degree sign/spaces/dots."""
    return unit.strip().lower().replace("°", "").replace(" ", "").replace(".", "")


def _spec_for(metric: WritableMetric) -> MetricSpec:
    """The plausibility spec for a metric — exhaustive ``match``, no ``default``.

    Adding a ``WritableMetric`` without a case here fails type-checking, so the
    closed set and this table can never silently drift apart.
    """
    match metric:
        case WritableMetric.heart_rate:
            return MetricSpec("bpm", frozenset({"bpm", "beats/min", "/min"}), 10.0, 300.0)
        case WritableMetric.spo2:
            return MetricSpec("%", frozenset({"%", "percent"}), 50.0, 100.0)
        case WritableMetric.systolic_bp:
            return MetricSpec("mmHg", frozenset({"mmhg"}), 40.0, 300.0)
        case WritableMetric.diastolic_bp:
            return MetricSpec("mmHg", frozenset({"mmhg"}), 20.0, 200.0)
        case WritableMetric.respiratory_rate:
            return MetricSpec(
                "breaths/min", frozenset({"breaths/min", "/min", "brpm", "rpm"}), 4.0, 80.0
            )
        case WritableMetric.temperature:
            return MetricSpec("°F", frozenset({"f", "degf", "fahrenheit"}), 80.0, 115.0)
        case WritableMetric.weight:
            return MetricSpec("lb", frozenset({"lb", "lbs", "pound", "pounds"}), 0.5, 1000.0)
        case WritableMetric.height:
            return MetricSpec("in", frozenset({"in", "inch", "inches"}), 4.0, 108.0)


def _out_of_range_blocks(mode: WriteEntryMode) -> bool:
    """Does an out-of-plausibility value hard-block in this mode?

    Exhaustive ``match`` (no ``default``): ``human_direct`` soft-warns (the
    physician typed it and may override); the reserved agent mode hard-blocks
    (a hallucinated value must fall back to human entry — Phase 2).
    """
    match mode:
        case WriteEntryMode.human_direct:
            return False
        case WriteEntryMode.agent_proposed_physician_confirmed:
            return True


def _unit_ok(unit: str, spec: MetricSpec) -> bool:
    return _norm_unit(unit) in spec.unit_aliases


def verify_write(
    candidate: WriteCandidate,
    mode: WriteEntryMode = WriteEntryMode.human_direct,
) -> WriteVerdict:
    """Deterministically verify one candidate. Never raises; returns a verdict.

    ``mode`` selects out-of-range severity (soft for ``human_direct``, hard for
    the reserved strict mode). It is a separate parameter — not read from the
    candidate — so a caller can verify a human-direct edit under strict rules
    if it ever needs to.
    """
    match candidate.kind:
        case WriteKind.vital:
            return _verify_vital(candidate, mode)
        case WriteKind.medication:
            return _verify_medication(candidate)


def _verify_vital(candidate: WriteCandidate, mode: WriteEntryMode) -> WriteVerdict:
    vital = candidate.vital
    if vital is None:  # unreachable given WriteCandidate's own validator; belt-and-braces.
        return WriteVerdict(kind=WriteKind.vital, blocked=True, errors=["missing vital payload"])

    spec = _spec_for(vital.metric)
    errors: list[str] = []
    warnings: list[str] = []

    if not _unit_ok(vital.unit, spec):
        errors.append(
            f"unit {vital.unit!r} does not match {vital.metric.value} "
            f"(expected {spec.canonical_unit})"
        )

    out_of_range = vital.value < spec.min_value or vital.value > spec.max_value
    if out_of_range:
        message = (
            f"{vital.metric.value} {_fmt(vital.value)} {spec.canonical_unit} is outside the "
            f"physiologic range ({_format_range(spec.min_value, spec.max_value)})"
        )
        if _out_of_range_blocks(mode):
            errors.append(message)
        else:
            warnings.append(message)

    return WriteVerdict(
        kind=WriteKind.vital,
        metric=vital.metric,
        blocked=bool(errors),
        warnings=warnings,
        errors=errors,
    )


def _verify_medication(candidate: WriteCandidate) -> WriteVerdict:
    med = candidate.medication
    if med is None:  # unreachable given WriteCandidate's own validator; belt-and-braces.
        return WriteVerdict(
            kind=WriteKind.medication, blocked=True, errors=["missing medication payload"]
        )

    errors: list[str] = []
    if not med.title.strip():
        errors.append("medication title is empty")
    for label, value in (("begdate", med.begdate), ("enddate", med.enddate)):
        if value is not None and not is_iso_date(value):
            errors.append(f"{label} {value!r} is not a valid YYYY-MM-DD date")

    return WriteVerdict(kind=WriteKind.medication, blocked=bool(errors), errors=errors)
