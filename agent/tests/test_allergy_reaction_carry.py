"""An allergy's reaction must survive the write to OpenEMR.

Guards a silent data-loss bug: `create_allergy` sent `reaction`, but OpenEMR's
`AllergyIntoleranceRestController::WHITELISTED_FIELDS` is
{title, begdate, enddate, diagnosis, comments} and `filterData()` drops everything
else WITHOUT error. So a physician confirming an intake-derived
"Penicillin — rash and hives" got a 201 and a chart row with no reaction — the
clinically important half vanished and nothing reported it.

Failure mode guarded: sending a field OpenEMR silently discards, i.e. believing a
value reached the chart when it did not.
"""

from __future__ import annotations

from copilot.domain.writes import WriteSource
from copilot.fhir.write_client import _allergy_comment

# The verbatim whitelist from src/RestControllers/AllergyIntoleranceRestController.php.
OPENEMR_ALLERGY_WHITELIST = {"title", "begdate", "enddate", "diagnosis", "comments"}


def test_reaction_is_carried_in_the_only_field_openemr_accepts() -> None:
    comment = _allergy_comment("rash and hives", None)
    assert "rash and hives" in comment, "the reaction must survive into comments"


def test_reaction_and_provenance_share_comments() -> None:
    source = WriteSource(source_document_id=7, extracted_fact_id=42, quote="Penicillin")
    comment = _allergy_comment("swelling", source)
    assert "swelling" in comment, "clinical content must be present"
    assert comment.index("Reaction") < comment.index(source.provenance_note()[:6]), (
        "reaction leads — it is what a physician reads; provenance is audit context"
    )


def test_absent_values_produce_no_field() -> None:
    # Never send an empty comments field just to have one.
    assert _allergy_comment(None, None) == ""


def test_reaction_only_and_source_only_each_stand_alone() -> None:
    assert _allergy_comment("hives", None) == "Reaction: hives"
    source = WriteSource(source_document_id=1, extracted_fact_id=2, quote="Sulfa")
    assert _allergy_comment(None, source) == source.provenance_note()


def test_we_never_send_a_field_openemr_would_silently_drop() -> None:
    # The structural guard: pin the payload keys to OpenEMR's real whitelist, so
    # reintroducing `reaction` (or any other dropped field) fails here instead of
    # vanishing into a 201 in production.
    import inspect

    from copilot.fhir import write_client

    src = inspect.getsource(write_client.OpenEmrWriteClient.create_allergy)
    sent = {line.split('"')[1] for line in src.splitlines() if 'payload["' in line}
    sent |= {"title", "begdate"}  # set in the literal
    unknown = sent - OPENEMR_ALLERGY_WHITELIST
    assert not unknown, (
        f"create_allergy sends {sorted(unknown)}, which OpenEMR's whitelist "
        f"{sorted(OPENEMR_ALLERGY_WHITELIST)} silently drops — the value would never "
        f"reach the chart and nothing would error"
    )
