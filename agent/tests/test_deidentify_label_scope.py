"""The labelled-name scrubber must not eat ordinary clinical prose.

Guards a real bug found by an outside audit: ``_LABELED_NAME_RE`` carried a
leading ``(?i)``, which applied case-insensitivity to the *value* as well as the
label — so ``[A-Z]`` matched a lowercase letter and the greedy Title-Case run
swallowed the rest of the query. ``"pt: severe sepsis with lactate elevation"``
collapsed to ``"patient"``, destroying the query *before* retrieval (deidentify
runs on every query at the egress choke-point).

Failure mode guarded: a physician's question silently reduced to one token, so
retrieval returns unrelated evidence — while PHI scrubbing must still hold.
"""

from __future__ import annotations

import pytest

from copilot.rag.deidentify import deidentify


@pytest.mark.parametrize(
    "query",
    [
        "pt: severe sepsis with lactate elevation",
        "pt: worsening renal function overnight",
        "name: unclear per chart",
        "patient: febrile and confused since morning",
    ],
)
def test_lowercase_prose_after_a_label_is_preserved(query: str) -> None:
    # The value is not Title-Case, so it is prose, not a name — keep it verbatim.
    assert deidentify(query) == query


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Patient: Marisol Quintanilla has sepsis", "patient has sepsis"),
        ("patient name: Jordan Rivera", "patient"),
        ("pt: Marisol Quintanilla", "patient"),
        ("Name - John Doe", "patient"),
    ],
)
def test_labelled_title_case_names_are_still_scrubbed(text: str, expected: str) -> None:
    # The PHI guarantee must survive the fix — a labelled real name still goes.
    assert deidentify(text) == expected
    assert "Quintanilla" not in deidentify(text)
    assert "Rivera" not in deidentify(text)


def test_clinical_terms_and_numerics_survive() -> None:
    query = "does the patient have AKI and DKA? lactate 4.2, pH 7.1"
    scrubbed = deidentify(query)
    for token in ("AKI", "DKA", "4.2", "7.1", "lactate"):
        assert token in scrubbed, f"clinical signal {token!r} must reach retrieval"
