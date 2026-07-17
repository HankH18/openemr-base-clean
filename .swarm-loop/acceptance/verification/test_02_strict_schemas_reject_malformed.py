"""feat_verification criterion 2 — strict extraction schemas reject bad input.

`LabReport` / `IntakeForm` / `ExtractedFact` are strict Pydantic schemas: a
malformed or partial payload raises a validation error and is never silently
coerced into a valid-looking object (W2 principle: the schema is the source of
truth; raw VLM output never bypasses it). FROZEN GOAL HARNESS.
"""

from __future__ import annotations

import pytest

from ._helpers import VALID_FACT_PAYLOAD, field, schema_class


def test_02_strict_schemas_reject_malformed():
    import pydantic

    lab_report = schema_class("LabReport")
    intake_form = schema_class("IntakeForm")
    extracted_fact = schema_class("ExtractedFact")

    for cls in (lab_report, intake_form, extracted_fact):
        assert issubclass(cls, pydantic.BaseModel), f"{cls.__name__} must be a Pydantic model"
        # Partial/empty input must be rejected — a strict extraction schema has
        # required content and never defaults itself into a valid report.
        with pytest.raises(pydantic.ValidationError):
            cls.model_validate({})

    # The documented ExtractedFact field set constructs...
    try:
        fact = extracted_fact.model_validate(dict(VALID_FACT_PAYLOAD))
    except pydantic.ValidationError as exc:
        pytest.fail(f"ExtractedFact rejects the documented W2 field set: {exc}")
    # ...and the value stays a verbatim string — parsed, never coerced numeric.
    assert isinstance(field(fact, "value"), str)

    # A malformed field is REJECTED (validation error), never coerced.
    for corrupt in (
        {**VALID_FACT_PAYLOAD, "bbox": "not-a-box"},
        {**VALID_FACT_PAYLOAD, "supported": "definitely"},
        {**VALID_FACT_PAYLOAD, "match_confidence": "high"},
    ):
        with pytest.raises(pydantic.ValidationError):
            extracted_fact.model_validate(corrupt)
