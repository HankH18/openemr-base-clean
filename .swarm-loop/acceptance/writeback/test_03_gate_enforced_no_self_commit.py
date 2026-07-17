"""feat_writeback criterion 3 — the gate is structurally enforced.

`propose()` yields a typed ProposedWrite (candidate + verdict, no committed id)
and performs NO OpenEMR call; commit requires the explicit confirm step and
re-verifies — a mismatched confirm token is refused with no write; a
double-confirm replays the first CommittedWrite (exactly one POST).
FROZEN GOAL HARNESS.
"""

from __future__ import annotations

import pytest

from ._helpers import (
    commit_flex,
    field,
    make_write_service,
    opt_field,
    propose_flex,
    write_calls,
)


async def test_03_gate_propose_never_commits_and_double_confirm_is_idempotent(
    db_path, settings, fake_openemr
):
    service = make_write_service(settings)

    proposed, key = await propose_flex(
        service,
        kind_value="medical_problem",
        raw_value="Type 2 diabetes mellitus",
        patient_pid=1005,
    )

    # Typed echo-back: a candidate and a verdict — never a committed proof.
    field(proposed, "candidate", what="ProposedWrite")
    field(proposed, "verdict", what="ProposedWrite")
    assert opt_field(proposed, "new_id", default=None) is None, (
        "ProposedWrite must not carry a committed id — the agent cannot self-commit"
    )
    assert not fake_openemr.WRITE_CALLS, (
        "propose must perform NO OpenEMR write call whatsoever"
    )

    # Commit re-verifies: a confirm token that does not match the proposed
    # candidate is refused, and nothing is written.
    with pytest.raises(Exception):
        await commit_flex(service, proposed=proposed, key="tampered-key-000", patient_pid=1005)
    assert not write_calls(fake_openemr, "medical_problem"), (
        "a refused commit must not reach OpenEMR"
    )

    # The explicit confirm commits exactly once; a double-confirm replays.
    committed1 = await commit_flex(service, proposed=proposed, key=key, patient_pid=1005)
    committed2 = await commit_flex(service, proposed=proposed, key=key, patient_pid=1005)
    id1 = str(field(committed1, "new_id", "id", what="CommittedWrite"))
    id2 = str(field(committed2, "new_id", "id", what="CommittedWrite"))
    assert id1 == id2, "double-confirm must replay the first CommittedWrite"
    assert len(write_calls(fake_openemr, "medical_problem")) == 1, (
        "idempotent double-confirm: exactly one POST may reach OpenEMR"
    )
