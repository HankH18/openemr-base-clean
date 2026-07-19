"""P2 bite-proof: strict-extraction ValidationErrors must not carry the PHI value.

The four extraction schemas run in Pydantic strict mode with ``extra="forbid"``.
When a VLM emits a wrong-typed field, ``schema.model_validate(...)`` raises a
``ValidationError`` whose message embeds the offending value verbatim
(``input_value=342.7``, ``input_value='07/13/2026'`` — a collection/birth date).
``vision._extract`` re-raises unchanged and there is no app-level handler, so that
value travels into the stdout traceback logs: PHI in the logs.

The fix is ``hide_input_in_errors=True`` on each schema's ``ConfigDict``. It strips
the input value from the message while KEEPING the field path and error type, so a
malformed extraction still fails loudly and remains diagnosable — the log now says
*which field* and *what kind* of error, never *what the patient's value was*.

Each test therefore asserts BOTH halves: the offending value is absent (the leak is
closed) and the field path + error type survive (the error is still actionable).
Reverting the config flips the value back into ``str(exc)`` and reddens these — that
is the bite.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from copilot.domain.documents import (
    ExtractedFact,
    IntakeForm,
    LabReport,
    MedicationListDocument,
)


def test_extracted_fact_wrong_typed_value_is_not_in_the_error() -> None:
    # A lab value the VLM handed back as a bare float instead of the verbatim
    # string the schema requires. 342.7 could be any patient's result.
    with pytest.raises(ValidationError) as excinfo:
        ExtractedFact.model_validate({"field_path": "glucose", "value": 342.7})

    message = str(excinfo.value)
    assert "342.7" not in message, "the offending value must never reach the error text (it lands in logs)"
    assert "value" in message, "the field path must survive so the failure stays diagnosable"
    assert "string_type" in message, "the error type must survive — we hide the value, not the fault"


def test_extracted_fact_bad_date_string_is_not_in_the_error() -> None:
    # The exact PHI shape the defect names: a date string on collection_date that
    # fails the datetime parse. '07/13/2026' is a birth/collection date — precisely
    # what must not appear in a traceback.
    with pytest.raises(ValidationError) as excinfo:
        ExtractedFact.model_validate(
            {"field_path": "collected", "value": "x", "collection_date": "07/13/2026"}
        )

    message = str(excinfo.value)
    assert "07/13/2026" not in message, "a birth/collection date must never leak into the error"
    assert "collection_date" in message, "the field path must survive"


def test_lab_report_nested_fact_value_is_not_in_the_error() -> None:
    # The real vision path: schema.model_validate on the CONTAINER, with the wrong
    # type down on facts[0].value. The nested model's config must hide it too.
    with pytest.raises(ValidationError) as excinfo:
        LabReport.model_validate({"facts": [{"field_path": "wbc", "value": 7.2}]})

    message = str(excinfo.value)
    assert "7.2" not in message, "a nested fact's value must be hidden as well as a top-level one"
    assert "facts.0.value" in message, "the full field path must survive for triage"
    assert "string_type" in message


def test_intake_form_header_field_value_is_not_in_the_error() -> None:
    # A container-level (non-fact) field: date_of_birth handed back as an int.
    # 19580311 is a DOB — the container's own config must hide it.
    with pytest.raises(ValidationError) as excinfo:
        IntakeForm.model_validate(
            {
                "facts": [{"field_path": "d", "value": "x", "category": "demographic"}],
                "date_of_birth": 19580311,
            }
        )

    message = str(excinfo.value)
    assert "19580311" not in message, "a DOB on a container header field must be hidden"
    assert "date_of_birth" in message, "the field path must survive"


def test_medication_list_nested_fact_value_is_not_in_the_error() -> None:
    # The fourth schema, via its nested IntakeFact.
    with pytest.raises(ValidationError) as excinfo:
        MedicationListDocument.model_validate(
            {"facts": [{"field_path": "m", "value": 7.2, "category": "medication"}]}
        )

    message = str(excinfo.value)
    assert "7.2" not in message, "MedicationListDocument must hide its nested fact value too"
    assert "facts.0.value" in message
