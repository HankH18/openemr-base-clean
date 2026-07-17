"""feat_writeback criterion 1 — medical_problem through the propose→confirm gate.

An agent-proposed medical problem flows propose (no write) → explicit confirm →
`OpenEmrWriteClient` POST to the Standard-API medical_problem route (respx
fake); the committed write returns a usable id and is audited with
entry_mode='agent_proposed_physician_confirmed'. FROZEN GOAL HARNESS.
"""

from __future__ import annotations

from ._helpers import (
    audit_entries,
    commit_flex,
    field,
    make_write_service,
    propose_flex,
    write_calls,
)


async def test_01_create_medical_problem_via_gate(db_path, settings, fake_openemr):
    service = make_write_service(settings)

    proposed, key = await propose_flex(
        service,
        kind_value="medical_problem",
        raw_value="Essential hypertension",
        patient_pid=1001,
    )
    candidate = field(proposed, "candidate", what="ProposedWrite")
    mode = field(candidate, "entry_mode", what="WriteCandidate")
    assert str(getattr(mode, "value", mode)) == "agent_proposed_physician_confirmed"
    assert not write_calls(fake_openemr, "medical_problem"), "propose must not write"

    committed = await commit_flex(service, proposed=proposed, key=key, patient_pid=1001)
    new_id = field(committed, "new_id", "id", what="CommittedWrite")
    assert str(new_id).strip(), "a committed write returns a usable id"

    calls = write_calls(fake_openemr, "medical_problem")
    assert len(calls) == 1, f"expected exactly one medical_problem POST (got {len(calls)})"
    assert calls[0]["method"] == "POST" and calls[0]["patient_id"] == "1001"
    assert calls[0]["has_body"], "the create must carry a JSON body"

    audits = [
        r for r in audit_entries(db_path, patient_id=1001) if "commit" in (r.action or "")
    ]
    assert audits, "a committed write must be audited (write_committed)"
    assert any(
        r.entry_mode == "agent_proposed_physician_confirmed" for r in audits
    ), "the audit row must attribute the write as agent_proposed_physician_confirmed"
