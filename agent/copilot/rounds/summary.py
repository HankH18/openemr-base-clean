"""Build the patient-card chart summary — one row per metric, with a trend.

The chart summary is a point-in-time snapshot, not a time series: a physician
wants the *current* value of each metric plus how it moved, not a flat list of
every reading with no dates. So this collapses a patient's fetched resources
into one claim per clinical concept:

- **Observations** (labs/vitals) are grouped by metric and collapsed to their
  **latest** reading, annotated with the change (↑/↓) and the elapsed time since
  the prior reading — e.g. "Heart rate: 92 /min  ↓12 · 22h since prior".
- **Everything else** (conditions, meds, allergies) appears once, as-is.

Deterministic: the same resources always yield the same summary. The one
exception is a record dated *after* now — see :func:`_is_future` — which the
card flags rather than trusts; that judgement necessarily reads the clock, so it
changes (once, in the safe direction) when the clock passes the stated time.
Every claim's source_ref points at the exact resource it came from, so the trust
story holds.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, TypeGuard

from copilot.agent.grounding import (
    claim_text,
    describe_resource,
    extract_temporal,
    humanize_label,
)
from copilot.domain.contracts import Claim, ClaimSeverity, TrendDirection, ValueDirection
from copilot.domain.primitives import Citation, FhirReference, ResourceType, utcnow
from copilot.rounds.ranges import reference_bounds, vitals_range


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
    flag / OpenEMR ``abnormal``), moved vs the prior reading, dated in the future
    (a record whose own clock is wrong is worth a physician's eyes, and must never
    be dropped quietly), or recorded in a **different unit from its prior**.
    Returns ``[]`` when the data carries no timestamps to anchor the window.

    The mixed-unit term is not a nicety. This module does not convert units (see
    :func:`_trusted_pair`), so ``37.0 Cel`` → ``98.6 degF`` (unchanged) and
    ``37.0 Cel`` → ``104.0 degF`` (a fever) are indistinguishable to it: both are
    simply "not comparable". Gating the row on a derivable change therefore drops
    the fever too — silently, behind "No recorded changes". A pair we cannot
    compare is a row we must SHOW, carrying the "no trend" text that names why.

    The anchor is never a reading dated after now. A single mistyped year (2027),
    a device clock running ahead, or a future ``issued`` would otherwise drag the
    window past every real reading, and the card would go empty — which the UI
    renders as the affirmative "No recorded changes since your last review".
    That is fail-*open*: a brand-new tachycardia would vanish behind a sentence
    saying nothing had changed. This module withholds visibly or not at all.
    """
    groups = group_observations(resources)
    times = [t for group in groups.values() for res in group if (t := _effective(res)) is not None]
    if not times:
        return []
    # Anchor on the patient's own timeline, but only on readings that are not in
    # the future. When *every* reading is future-dated there is no trustworthy
    # patient-timeline anchor at all, so fall back to the wall clock: the future
    # readings then still land inside the window and get reported (flagged), which
    # beats reporting "nothing changed".
    now = utcnow()
    dated_by_now = [t for t in times if t <= now]
    anchor = max(dated_by_now) if dated_by_now else now
    window_start = anchor - timedelta(hours=hours)

    claims: list[Claim] = []
    for group in groups.values():
        latest_time = _effective(group[0])
        if latest_time is None or latest_time < window_start:
            continue  # not measured within the window — nothing new to report
        if not (
            _is_abnormal(group[0])
            or _changed(group)
            or _is_future(group[0])
            or _mixed_unit_prior(group) is not None
        ):
            continue  # measured recently but unremarkable
        claim = _observation_claim(group)
        if claim is not None:
            claims.append(claim)
    return claims


# --- helpers ---------------------------------------------------------------


def _normalize_medication_value(value: str) -> str:
    """Trim, lowercase, and drop trailing dots so med values compare cleanly."""
    return value.strip().lower().rstrip(".").strip()


def _is_medication_ref(ref: Citation) -> TypeGuard[FhirReference]:
    """True when this citation is a fhir ``MedicationRequest`` reference.

    A ``TypeGuard`` so callers narrow the citation union once and then read the
    fhir-only ``resource_type`` / ``value`` fields safely — a document or
    guideline citation has neither.
    """
    return isinstance(ref, FhirReference) and ref.resource_type == ResourceType.MedicationRequest


def _dedupe_medications(claims: list[Claim]) -> list[Claim]:
    """Collapse duplicate medication rows for the same drug.

    The seed sometimes carries the same drug twice — a bare name
    ("Hydromorphone") and a full sig ("Hydromorphone 0.5 mg IV q4h PRN pain").
    Drop any ``MedicationRequest`` claim whose normalized value is a *strict*
    prefix of another medication claim's value, keeping the longer/more
    informative one. Non-medication claims are never dropped, empty-value claims
    are kept, and original order is preserved. Mirrors ``dedupeMedicationClaims``
    in web/src/labels.ts.

    Only the fhir citation variant carries ``resource_type`` / ``value``, so a
    document- or guideline-cited claim can never be a ``MedicationRequest`` row
    and is passed through untouched.
    """
    med_values = [
        _normalize_medication_value(claim.source_ref.value)
        for claim in claims
        if _is_medication_ref(claim.source_ref)
    ]
    result: list[Claim] = []
    for claim in claims:
        ref = claim.source_ref
        if not _is_medication_ref(ref):
            result.append(claim)
            continue
        value = _normalize_medication_value(ref.value)
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
    """One claim for a metric group: latest value + unit + trend vs prior.

    Also carries three record-grounded classifications for the chart-summary
    colour-coding: ``severity`` (from the abnormal flag), ``trend_direction``
    (from the latest-vs-prior distance to the metric's reference band), and
    ``value_direction`` (the raw up/down/none motion of the latest reading vs the
    prior one). All three are presentation hints — ``source_ref.value`` stays
    verbatim, so verification is unaffected.
    """
    latest = group[0]
    described = describe_resource(latest)
    if described is None:
        return None
    label, field, value = described
    unit = _unit(latest)
    head = f"{humanize_label(label)}: {value}{(' ' + unit) if unit else ''}"
    low, high = _metric_bounds(latest, label, unit)
    return Claim(
        text=head + _trend(group),
        source_ref=FhirReference(
            resource_type=ResourceType.Observation,
            resource_id=str(latest.get("id")),
            field=field,
            value=str(value),
            timestamp=extract_temporal(latest),
        ),
        severity=_classify_severity(latest),
        trend_direction=_classify_trend(group, low, high),
        value_direction=_value_direction(group),
    )


def _value_direction(group: list[Mapping[str, Any]]) -> ValueDirection:
    """Motion of the latest reading vs the prior one: ``up`` / ``down`` / ``none``.

    ``none`` when the value is unchanged or the two readings are not comparable
    at all (:func:`_trusted_pair`: no prior, non-numeric, mixed units, or a
    future-dated latest reading). Grounded in the same successive record values
    the trend classifier uses — this is the sign of ``latest - prior``, so it is
    derivable independently of any reference range and drives the UI's uniform
    movement arrow (↑ / ↓ / —).
    """
    pair = _trusted_pair(group)
    if pair is None:
        return ValueDirection.none
    (latest, _), (prior, _) = pair
    if latest == prior:
        return ValueDirection.none
    return ValueDirection.up if latest > prior else ValueDirection.down


# --- record-grounded classification (severity + trend direction) -----------


def _abnormal_code(res: Mapping[str, Any]) -> str:
    """The record's abnormal flag as a raw string, or '' when normal/absent.

    Prefers ``interpretation[0].coding[0].code`` (US Core convention); falls back
    to a top-level ``abnormal`` (OpenEMR seed convention). Verbatim — the caller
    classifies it; this never invents a flag.
    """
    interp = res.get("interpretation")
    if isinstance(interp, list) and interp and isinstance(interp[0], Mapping):
        coding = interp[0].get("coding")
        if isinstance(coding, list) and coding and isinstance(coding[0], Mapping):
            code = coding[0].get("code")
            if isinstance(code, str):
                return code
    raw = res.get("abnormal")
    return raw if isinstance(raw, str) else ""


_NORMAL_FLAGS = frozenset({"", "n", "no", "normal"})


def _is_critical_flag(flag: str) -> bool:
    """True for a flag denoting a *critical* result (double-letter / vhigh/vlow).

    Mirrors the frontend chart's ``severityOf`` and the verification critical
    sets: ``HH``/``LL``/``AA``, ``vhigh``/``vlow``, ``critical_*``, ``<<``/``>>``,
    and any ``crit``/``panic`` wording. Everything else abnormal is a warning.
    """
    f = flag.strip().lower()
    if f in ("hh", "ll", "aa", "<<", ">>", "critical_high", "critical_low"):
        return True
    return f.startswith("vh") or f.startswith("vl") or "crit" in f or "panic" in f


def _classify_severity(res: Mapping[str, Any]) -> ClaimSeverity:
    """Severity from the record's abnormal flag: '' → normal, high/low → warning,
    vhigh/vlow / HH/LL → critical. Grounded in the flag, never in a guessed range.
    """
    flag = _abnormal_code(res)
    if flag.strip().lower() in _NORMAL_FLAGS:
        return ClaimSeverity.normal
    return ClaimSeverity.critical if _is_critical_flag(flag) else ClaimSeverity.warning


def _distance_to_range(value: float, low: float | None, high: float | None) -> float:
    """How far ``value`` sits outside ``[low, high]``; 0.0 when inside (or on) it.

    Handles one-sided bands: with only a high bound there is no "below" distance,
    and vice-versa — so troponin's ``<0.04`` still measures over-shoot correctly.
    """
    if high is not None and value > high:
        return value - high
    if low is not None and value < low:
        return low - value
    return 0.0


def _metric_bounds(
    res: Mapping[str, Any], label: str, unit: str
) -> tuple[float | None, float | None]:
    """The metric's reference band, grounded: the record's own ``referenceRange``
    (labs) or, ONLY when the record carries none, the standard vitals table.
    """
    low, high = reference_bounds(res)
    if low is None and high is None:
        return vitals_range(humanize_label(label), unit)
    return (low, high)


def _classify_trend(
    group: list[Mapping[str, Any]], low: float | None, high: float | None
) -> TrendDirection | None:
    """Improving / worsening / steady, from distance-to-range of latest vs prior.

    Grounded: uses only the record's own values and the record/standard range.
    Entering the band or shrinking the distance is ``improving``; leaving it or
    growing the distance is ``worsening``; both-in-range or no numeric change is
    ``steady``. Returns ``None`` when it cannot be judged — no range at all, or
    no comparable pair of readings (:func:`_trusted_pair`: no prior, non-numeric,
    mixed units, or a future-dated latest reading) — so the UI renders neutral
    rather than guessing a direction. The range itself is expressed in the
    record's unit, so a mixed-unit pair could not be scored against it honestly
    even if the two readings were comparable to each other.
    """
    pair = _trusted_pair(group)
    if pair is None:
        return None
    (latest, _), (prior, _) = pair
    if low is None and high is None:
        return None
    d_latest = _distance_to_range(latest, low, high)
    d_prior = _distance_to_range(prior, low, high)
    if latest == prior or (d_latest == 0.0 and d_prior == 0.0):
        return TrendDirection.steady
    if d_latest < d_prior:
        return TrendDirection.improving
    if d_latest > d_prior:
        return TrendDirection.worsening
    return TrendDirection.steady


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
    """True when the latest numeric reading differs from the prior one.

    False whenever the two are not comparable (:func:`_trusted_pair`) — an
    unreliable ordering cannot establish a change, and neither can a pair of
    readings in different units.

    False here means "no change is *derivable*", NOT "nothing changed". The
    distinction is the whole point: a group that is notable only because of a
    future-dated record (:func:`_is_future`) or a mixed-unit pair
    (:func:`_mixed_unit_prior`) is surfaced by :func:`build_change_claims` on its
    own terms. Every caller that treats this as "nothing changed" must carry those
    terms too, or it will report an unexaminable change as no change at all.
    """
    pair = _trusted_pair(group)
    if pair is None:
        return False
    return pair[0][0] != pair[1][0]


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
# Keys are matched case-insensitively (see :func:`_unit`), which also picks up the
# UCUM *case-insensitive* code set (``CEL``, ``[DEGF]``, ``[IN_I]``, ``[LB_AV]``)
# that some feeds emit — every one of those denotes the same unit as its
# case-sensitive spelling here.
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

# The lookup table _unit() actually reads: the same map re-keyed case-folded, so a
# folded probe hits it. Derived rather than hand-written — hand-folding the literal
# above would lose the canonical UCUM spellings that make it readable, and keying it
# by hand invites a silent miss (a folded probe against an unfolded key simply
# returns the input, quietly disabling the whole table). The assert makes a
# collision — two keys folding together with different displays — a startup failure
# rather than a wrong unit on a card.
_UNIT_DISPLAY_FOLDED: dict[str, str] = {k.casefold(): v for k, v in _UNIT_DISPLAY.items()}
assert len(_UNIT_DISPLAY_FOLDED) == len(_UNIT_DISPLAY), "case-folded unit keys collide"


def _unit(res: Mapping[str, Any]) -> str:
    """The reading's unit as a *display label*, normalized so equal units compare equal.

    Two normalizations, each lossless for a label:

    - **Strip surrounding whitespace.** Padding carries no unit semantics. Returning
      it unstripped made ``'mg/dL '`` and ``'mg/dL'`` unequal, which
      :func:`_trusted_pair` cannot tell from a genuine unit mismatch — so a real
      80-point glucose rise was withheld as "no trend" over a trailing space.
      (Stripping before the ``"1"`` check also catches a padded UCUM unity unit.)
    - **Case-fold the map LOOKUP KEY** — never the returned value.

    Case-folding is safe *here* and is deliberately refused in
    :func:`copilot.verification.core._units_equal`; the difference is what is being
    compared. That function tests two **arbitrary, open-set** unit strings for
    equivalence, where folding would make ``mg`` (milligram) equal ``Mg``
    (megagram) — a 1e9 error, the exact hazard it exists to catch. This function
    only *looks up* a **closed set** of eight display labels for inch, pound,
    Fahrenheit and Celsius. A miss returns the stripped original with its case
    intact, so ``mg`` and ``Mg`` never reach the map and stay distinct — the UCUM
    case-sensitivity that matters is preserved downstream. Within the map no two
    keys fold together, and every case-variant of a key (``Cel``/``cel``/``CEL``,
    ``degF``/``[DEGF]``) denotes the *same* unit in both UCUM code sets, so a
    folded hit can never be a different unit. :func:`copilot.rounds.ranges.vitals_range`
    already folds this same closed temperature set for the same reason.
    """
    q = res.get("valueQuantity")
    if isinstance(q, Mapping):
        unit = q.get("unit")
        if isinstance(unit, str) and (stripped := unit.strip()) and stripped != "1":
            return _UNIT_DISPLAY_FOLDED.get(stripped.casefold(), stripped)
    return ""


def _numeric(res: Mapping[str, Any]) -> tuple[float, str] | None:
    """The reading's numeric value *with the unit it was recorded in*.

    The unit rides along because every consumer compares two readings, and a raw
    float is not comparable across units. OpenEMR permits a temperature in ``Cel``
    and in ``degF`` (and a weight in kg and lb) within one metric's history: read
    unit-blind, 37.0 → 98.6 is a 61.6-degree rise when the patient never changed.

    The unit is the *display* form (:func:`_unit`), so two spellings of the same
    unit (``Cel``/``degC``) compare equal — that is normalization of a label the
    card already prints, not a conversion of a value.
    """
    q = res.get("valueQuantity")
    if isinstance(q, Mapping):
        v = q.get("value")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return (float(v), _unit(res))
    return None


def _is_future(res: Mapping[str, Any]) -> bool:
    """True when the record's own clinical timestamp is dated after now.

    A reading cannot have been taken later than the present, so this is a record
    defect — a mistyped year, a device clock ahead, a bogus ``issued``. What it
    costs us is *ordering*: the record-stated sequence (and therefore the sign of
    any delta against it) cannot be trusted, since we cannot tell a
    mistyped-today from a stale reading stamped into the future.
    """
    t = _effective(res)
    return t is not None and t > utcnow()


def _trusted_pair(
    group: list[Mapping[str, Any]],
) -> tuple[tuple[float, str], tuple[float, str]] | None:
    """Latest & prior ``(value, unit)`` when a comparison between them holds up.

    ``None`` — no comparison is derivable — when any of these is true:

    - there is no prior reading, or either reading is non-numeric;
    - the two were recorded in **different units**: their raw floats measure
      different things, so every delta, direction and trend between them is
      meaningless (this is the "temperature rose 61.6°, trend improving" bug);
    - the latest reading is **dated in the future**: which reading is actually
      later is then unknowable, so the sign of the delta is a coin flip.

    Fail-closed, matching this module's posture everywhere else: each consumer
    renders neutral rather than assert a change it cannot stand behind. No unit
    conversion is attempted — a wrong mapping would be worse than the omission,
    and the card says out loud (see :func:`_trend`) that it is withholding.
    """
    if len(group) < 2:
        return None
    latest, prior = _numeric(group[0]), _numeric(group[1])
    if latest is None or prior is None:
        return None
    if latest[1] != prior[1]:
        return None
    if _is_future(group[0]):
        return None
    return (latest, prior)


def _mixed_unit_prior(group: list[Mapping[str, Any]]) -> str | None:
    """The prior reading's unit when latest & prior are numeric but in DIFFERENT units.

    ``None`` when there is no such pair, or when the two agree (after
    :func:`_unit` normalization) and are therefore comparable. The empty string is
    a *hit*, not a miss — an unlabelled prior is still a unit mismatch against a
    labelled latest — so callers must test ``is not None``, never truthiness.

    The single definition of "mixed unit", shared by the row gate in
    :func:`build_change_claims` and the card text in :func:`_trend`. They must
    agree: if the gate can drop a row the text was written to explain, the
    explanation is unreachable in the one case it exists for — which is exactly how
    a real change came to vanish behind "No recorded changes since your last
    review".
    """
    if len(group) < 2:
        return None
    latest, prior = _numeric(group[0]), _numeric(group[1])
    if latest is None or prior is None or latest[1] == prior[1]:
        return None
    return prior[1]


def _decimals(x: float) -> int:
    """How many decimal places a source value states (0 for a whole number).

    Read off the value's shortest round-trip repr — i.e. the precision the record
    itself carries — so a delta is never rendered coarser than its operands.
    """
    exponent = Decimal(repr(float(x))).normalize().as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def _fmt_num(x: float, decimals: int = 0) -> str:
    """Format a delta at ``decimals`` places — the precision of its operands.

    Precision must never be coarser than the data it describes. Hardcoding one
    decimal place (the old behaviour) printed every clinically decisive troponin
    move as ``0.0``: with a reference band of ``<0.04``, the serial rise that
    rules in MI (0.04 → 0.08) rendered as "up by zero". Callers pass the greater
    of the two operands' :func:`_decimals`, which is always exactly enough — the
    difference of two values with at most *n* decimal places has at most *n* —
    and rounds away float representation noise (0.04 - 0.01 is 0.030000000000000002).

    A whole number still renders as a whole number: no ``92.0``, and no
    significant-figure formatting that would turn 1234 into ``1.23e+03``.
    """
    if x == int(x):
        return str(int(x))
    return f"{x:.{decimals}f}"


def _relative(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{max(minutes, 1)}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def _trend(group: list[Mapping[str, Any]]) -> str:
    """Suffix describing the change vs the prior reading, or '' if none.

    Every branch that cannot derive a delta says so on the card instead of
    printing a number it cannot defend — a withheld trend must be visible, never
    a quiet omission dressed up as a routine reading.
    """
    latest = group[0]
    if _is_future(latest):
        # The record's clinical time is ahead of the clock, so neither the elapsed
        # gap nor the ordering (and hence the delta's sign) means anything. Print
        # the defect: a mistyped year is a thing a physician must see, not a thing
        # to launder into "↑1 · 168d since prior".
        return "  · dated in the future — verify record"
    if len(group) < 2:
        return ""
    prior = group[1]
    le, pe = _effective(latest), _effective(prior)
    gap = _relative((le - pe).total_seconds()) if (le and pe and le > pe) else ""
    tail = f" · {gap} since prior" if gap else ""
    pair = _trusted_pair(group)
    if pair is None:
        prior_unit = _mixed_unit_prior(group)
        if prior_unit is not None:
            # Same metric, two units: the delta between the raw floats is fiction.
            # build_change_claims gates on this same predicate, so this sentence is
            # reachable on the change card, not just the summary card.
            return f"  · prior in {prior_unit or 'no unit'} — no trend{tail}"
        return f" · updated{tail}" if tail else ""
    (lv_value, _), (pv_value, _) = pair
    delta = lv_value - pv_value
    if delta == 0:
        return f"  → no change{tail}"
    arrow = "↑" if delta > 0 else "↓"
    places = max(_decimals(lv_value), _decimals(pv_value))
    return f"  {arrow}{_fmt_num(abs(delta), places)}{tail}"
