"""Grounded reference-range parsing + the closed standard-vitals table.

Colour-coding the chart summary needs each metric's *normal band*, and both the
band and the colour must stay grounded in the record — never invented. Three
grounded sources feed it, and all three live here so the summary builder and the
observation-series endpoint derive them identically:

- :func:`parse_range_text` turns a recorded range **string** into ``(low, high)``
  bounds, each independently optional. It handles the two shapes OpenEMR emits:
  two-sided (``"135-145"``, ``"0.5-2.2"``, ``"3.5 - 5.1"``, ``"12 to 20"``) and
  one-sided (``"<0.04"``, ``">10"``, ``"<=5"``, ``"≥ 2.5"``). The string is the
  seed's ``range`` column / an Observation ``referenceRange[0].text`` — parsed,
  not fabricated.
- :func:`reference_bounds` reads a FHIR ``referenceRange[0]`` element: it prefers
  the structured ``low.value`` / ``high.value`` and falls back to parsing the
  free-text ``text``. That fallback is why a text-only range like troponin's
  ``"<0.04"`` now yields a band (previously dropped, because only structured
  two-sided ranges were read).
- :data:`VITALS_RANGES` is a small, closed table of standard adult vital-sign
  reference ranges — the ONLY supplement, and only for vitals, which (unlike
  labs) carry no ``referenceRange`` in the record. It is a documented clinical
  baseline, NOT a population table and NOT per-metric guessing. A metric that is
  neither a recorded range nor a known vital gets NO band (fail-closed → the UI
  renders neutral).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# A signed decimal (leading +/- and either "12" or ".5" or "12.5" tolerated).
_NUM = r"[-+]?\d*\.?\d+"

# "<0.04" / "<=0.04" / "≤ 0.04"  and  ">10" / ">=10" / "≥ 2.5".
_ONE_SIDED_RE = re.compile(rf"^\s*(?P<op><=|>=|≤|≥|<|>)\s*(?P<num>{_NUM})\s*$")
# "135-145" / "0.5-2.2" / "3.5 - 5.1" / "12 to 20". The separator is an ASCII
# hyphen, a unicode en/em dash, or the word "to"; the dash class is built from
# code points so the source carries no ambiguous dash characters.
_DASHES = "-" + chr(0x2013) + chr(0x2014)
_TWO_SIDED_RE = re.compile(rf"^\s*(?P<low>{_NUM})\s*(?:[{_DASHES}]|to)\s*(?P<high>{_NUM})\s*$")

# "<" / "<=" / "≤": the value must stay below the number, so it is a HIGH bound.
# Any other (">" / ">=" / "≥") makes the number a LOW bound.
_UPPER_OPS = frozenset({"<", "<=", "≤"})


def _to_float(raw: str) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def parse_range_text(text: str) -> tuple[float | None, float | None]:
    """Parse a recorded reference-range string into ``(low, high)`` bounds.

    - **Two-sided** — ``"135-145"``, ``"0.5-2.2"`` → ``(low, high)`` (reversed
      order is tolerated and swapped).
    - **One-sided** — ``"<0.04"`` / ``"<=0.04"`` → ``(None, high)``;
      ``">10"`` / ``">=10"`` → ``(low, None)``.

    Returns ``(None, None)`` for an empty, non-string, or unparseable value — the
    caller then falls back or renders neutral, never inventing a band.
    """
    if not isinstance(text, str):
        return (None, None)
    s = text.strip()
    if not s:
        return (None, None)

    one = _ONE_SIDED_RE.match(s)
    if one is not None:
        num = _to_float(one.group("num"))
        if num is None:
            return (None, None)
        if one.group("op") in _UPPER_OPS:
            return (None, num)
        return (num, None)  # a lower-bound operator (">", ">=", "≥")

    two = _TWO_SIDED_RE.match(s)
    if two is not None:
        low = _to_float(two.group("low"))
        high = _to_float(two.group("high"))
        if low is not None and high is not None and low > high:
            low, high = high, low
        return (low, high)

    return (None, None)


def _bound_value(node: Any) -> float | None:
    """Read the numeric ``value`` out of a structured referenceRange low/high."""
    if isinstance(node, Mapping):
        v = node.get("value")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def reference_bounds(res: Mapping[str, Any]) -> tuple[float | None, float | None]:
    """``(low, high)`` from an Observation's first ``referenceRange``.

    Prefers the structured ``low.value`` / ``high.value``; when neither is
    present, falls back to parsing the element's free-text ``text`` (so a
    text-only ``"<0.04"`` yields a band). Returns ``(None, None)`` when nothing
    is derivable. Every bound is read from the record — never invented.
    """
    ranges = res.get("referenceRange")
    if not isinstance(ranges, list) or not ranges:
        return (None, None)
    first = ranges[0]
    if not isinstance(first, Mapping):
        return (None, None)
    low = _bound_value(first.get("low"))
    high = _bound_value(first.get("high"))
    if low is not None or high is not None:
        return (low, high)
    text = first.get("text")
    if isinstance(text, str):
        return parse_range_text(text)
    return (None, None)


# --- Standard adult vital-sign reference ranges ----------------------------
#
# A documented clinical baseline for adult vital signs — the ONLY supplement,
# and only for vitals, which do not carry a ``referenceRange`` in the record.
# This is NOT a population table and NOT per-metric guessing: it is the small,
# closed set of textbook adult ranges, keyed by the normalized (lower-cased,
# whitespace-collapsed) humanized metric label. A metric absent from this table
# and lacking a recorded range gets NO band (fail-closed).
VITALS_RANGES: dict[str, tuple[float, float]] = {
    "heart rate": (60.0, 100.0),  # beats/min
    "pulse": (60.0, 100.0),  # beats/min (alias)
    "respiratory rate": (12.0, 20.0),  # breaths/min
    "oxygen saturation": (95.0, 100.0),  # % SpO2
    "spo2": (95.0, 100.0),  # % SpO2 (alias)
    "systolic blood pressure": (90.0, 120.0),  # mmHg
    "diastolic blood pressure": (60.0, 80.0),  # mmHg
}

# Temperature's band depends on the recorded unit, so it is keyed separately.
_TEMPERATURE_RANGES: dict[str, tuple[float, float]] = {
    "c": (36.1, 37.2),  # °C
    "f": (97.0, 99.0),  # °F
}
_TEMPERATURE_LABELS = frozenset({"temperature", "body temperature"})
_CELSIUS_UNITS = frozenset({"c", "cel", "degc", "celsius"})
_FAHRENHEIT_UNITS = frozenset({"f", "degf", "fahrenheit"})


def vitals_range(metric_label: str, unit: str = "") -> tuple[float | None, float | None]:
    """Standard adult reference band for a vital, or ``(None, None)`` if unknown.

    The ONLY supplement for vitals — which carry no ``referenceRange`` in the
    record. ``metric_label`` is the humanized metric name (e.g. ``"Heart Rate"``);
    ``unit`` selects the temperature band (°C vs °F) and is otherwise ignored. An
    unrecognized vital — or a temperature in an unknown unit — returns
    ``(None, None)`` so the caller renders neutral rather than guessing.
    """
    key = " ".join(metric_label.strip().lower().split())
    if key in _TEMPERATURE_LABELS:
        u = unit.strip().lower().replace("°", "")
        if u in _CELSIUS_UNITS:
            return _TEMPERATURE_RANGES["c"]
        if u in _FAHRENHEIT_UNITS:
            return _TEMPERATURE_RANGES["f"]
        return (None, None)
    return VITALS_RANGES.get(key, (None, None))
