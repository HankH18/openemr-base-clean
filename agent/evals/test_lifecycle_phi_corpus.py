"""Unit coverage for the lifecycle-family PHI corpus generator.

Locks the fix that cleared ``phi_check``'s "missing event families" warning: the
corpus ``gate.py --write-phi-corpus`` emits now carries a scrubbed telemetry line
for each of the five lifecycle event families, captured through the REAL scrub
layers. These tests pin (a) that all five families are represented, (b) that the
lines are PHI-clean because the real scrubs held, (c) that the ``patient_id`` was
genuinely pseudonymized rather than dropped, and (d) that the lines BITE — a
neutered ``deidentify`` leaks the probe's identifiers, proving they are real
PHI-carrying paths and not inert padding.

Runs fully in-process, keyless, no network — the same properties the gate itself
guarantees.

@package   OpenEMR
@link      https://www.open-emr.org
@license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
"""

from __future__ import annotations

import evals.lifecycle_phi_corpus as lifecycle
from evals.rubrics import count_phi

# The five families the frozen ``.swarm-loop/acceptance/phi_check.py`` expects a
# real end-to-end capture to contain (mirrors its ``_EXPECTED_EVENT_FAMILIES``).
_EXPECTED_FAMILIES = (
    "doc.ingest",
    "extraction.run",
    "guideline.retrieve",
    "worker.handoff",
    "verification.result",
)

# The families whose realistic PHI vector is free text routed through
# ``deidentify`` (so a neutered scrub leaks on their lines). ``verification.result``
# is excluded on purpose: its real emission carries a ``patient_id`` protected by
# the pseudonymizer, not ``deidentify``.
_FREE_TEXT_FAMILIES = (
    "doc.ingest",
    "extraction.run",
    "guideline.retrieve",
    "worker.handoff",
)


def _family_line(lines: list[str], family: str) -> str:
    matches = [line for line in lines if f'"event": "{family}"' in line]
    assert matches, f"corpus is missing a line for the {family!r} family"
    return matches[0]


def test_all_five_lifecycle_families_are_represented() -> None:
    """The corpus carries a line for each family, so phi_check stops warning."""
    combined = "\n".join(lifecycle.lifecycle_corpus_lines())
    for family in _EXPECTED_FAMILIES:
        assert family in combined, f"{family!r} must appear in the corpus"


def test_lifecycle_lines_are_phi_clean() -> None:
    """The real scrubs held, so the captured egress carries zero PHI."""
    combined = "\n".join(lifecycle.lifecycle_corpus_lines())
    assert count_phi(combined) == 0
    # Probe identifiers must be gone from the scrubbed output.
    assert "Marisol" not in combined
    assert "123-45-6789" not in combined
    assert "m.quint@example.com" not in combined


def test_patient_id_is_pseudonymized_not_dropped() -> None:
    """PatientPseudonymizer.scrub mapped the id to a stable pt_… token at egress."""
    lines = lifecycle.lifecycle_corpus_lines()
    for family in ("doc.ingest", "verification.result"):
        line = _family_line(lines, family)
        assert '"pt_' in line, f"{family!r} must carry a pseudonymized patient_id"
        assert f'"patient_id": {lifecycle._PATIENT_ID}' not in line, "raw id must not egress"


def test_the_probe_is_genuinely_phi_bearing() -> None:
    """Otherwise a clean scan would prove nothing about the scrub."""
    assert count_phi(lifecycle._PHI_TEXT) >= 3


def test_neutering_deidentify_makes_the_family_lines_leak(monkeypatch) -> None:
    """Anti-vacuous bite: identity deidentify -> the free-text family lines leak.

    Proves the lines are real PHI-carrying paths whose cleanliness depends on the
    live scrub, not hardcoded clean strings.
    """
    monkeypatch.setattr(lifecycle, "deidentify", lambda text: text)
    lines = lifecycle.lifecycle_corpus_lines()
    assert count_phi("\n".join(lines)) > 0, "a neutered scrub must leak PHI into the corpus"
    for family in _FREE_TEXT_FAMILIES:
        line = _family_line(lines, family)
        assert count_phi(line) > 0, f"{family!r} must leak the probe's PHI when deidentify is off"
