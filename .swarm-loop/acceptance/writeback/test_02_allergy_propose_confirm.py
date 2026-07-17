"""feat_writeback criterion 2 — allergy through the propose→confirm gate.

An agent-proposed allergy flows propose (no write) → explicit confirm →
POST to the Standard-API allergy route (respx fake); committed + audited with
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


async def test_02_create_allergy_via_gate(db_path, settings, fake_openemr):
    service = make_write_service(settings)

    proposed, key = await propose_flex(
        service,
        kind_value="allergy",
        raw_value="penicillin",
        patient_pid=1003,
    )
    candidate = field(proposed, "candidate", what="ProposedWrite")
    mode = field(candidate, "entry_mode", what="WriteCandidate")
    assert str(getattr(mode, "value", mode)) == "agent_proposed_physician_confirmed"
    assert not write_calls(fake_openemr, "allergy"), "propose must not write"

    committed = await commit_flex(service, proposed=proposed, key=key, patient_pid=1003)
    new_id = field(committed, "new_id", "id", what="CommittedWrite")
    assert str(new_id).strip(), "a committed write returns a usable id"

    calls = write_calls(fake_openemr, "allergy")
    assert len(calls) == 1, f"expected exactly one allergy POST (got {len(calls)})"
    assert calls[0]["method"] == "POST" and calls[0]["patient_id"] == "1003"
    assert calls[0]["has_body"], "the create must carry a JSON body"

    audits = [
        r for r in audit_entries(db_path, patient_id=1003) if "commit" in (r.action or "")
    ]
    assert audits, "a committed write must be audited (write_committed)"
    assert any(
        r.entry_mode == "agent_proposed_physician_confirmed" for r in audits
    ), "the audit row must attribute the write as agent_proposed_physician_confirmed"
