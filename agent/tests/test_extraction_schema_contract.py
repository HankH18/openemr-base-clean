"""The extraction schemas are the canonical contract — tests for its floor.

Spec p6: "The extraction schemas are the canonical contracts... The schema is
the source of truth — not what the model happens to return." Spec req 2 (p4)
names the required lab and intake fields.

Two properties are pinned here, and they pull in opposite directions on purpose:

1. **An empty extraction is not a valid extraction.** "I read a lab report and
   found nothing" is well-typed but never honest, so every document model
   requires at least one fact. Without this floor a vision model that read
   nothing returns a clean, confident, empty document and every downstream
   consumer treats it as success.
2. **An honest *partial* extraction is still valid.** Real scanned documents
   genuinely lack a unit, a reference range, or an abnormal flag. Rejecting
   those would not improve data quality — it would pressure the extractor to
   invent values to satisfy the schema, converting a visible gap into an
   invisible fabrication. So the gap is recorded and reported instead.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from copilot.domain.documents import (
    ExtractedFact,
    IntakeCategory,
    IntakeFact,
    IntakeForm,
    LabField,
    LabReport,
    MedicationListDocument,
)

# --- 1. an empty extraction fails loudly, on all three models ---------------

_MODELS = (LabReport, IntakeForm, MedicationListDocument)


@pytest.mark.parametrize("model", _MODELS, ids=lambda m: m.__name__)
def test_an_empty_fact_list_is_rejected(
    model: type[LabReport] | type[IntakeForm] | type[MedicationListDocument],
) -> None:
    # The hole this closes: `{"facts": []}` used to validate on all three models,
    # so a VLM that read nothing produced a confident, empty, "successful" result.
    with pytest.raises(ValidationError):
        model.model_validate({"facts": []})


@pytest.mark.parametrize("model", _MODELS, ids=lambda m: m.__name__)
def test_an_omitted_fact_list_is_rejected(
    model: type[LabReport] | type[IntakeForm] | type[MedicationListDocument],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({})


@pytest.mark.parametrize("model", _MODELS, ids=lambda m: m.__name__)
def test_the_non_empty_floor_is_stated_in_the_tool_schema_handed_to_the_model(
    model: type[LabReport] | type[IntakeForm] | type[MedicationListDocument],
) -> None:
    # `model_json_schema()` IS the forced tool's input_schema (see
    # test_vision_contract), so the floor is declared to the extractor up front,
    # not merely enforced after the fact.
    assert model.model_json_schema()["properties"]["facts"]["minItems"] == 1


def test_medication_list_rejects_a_payload_that_filters_down_to_empty() -> None:
    # `min_length=1` alone cannot catch this: `_keep_only_medications` is an
    # `mode="after"` validator, so it runs *once the field constraint has already
    # passed* and could hand back an empty list from a non-empty payload. Dropping
    # a stray line is tolerated; dropping every line means the document was
    # misclassified, and that must fail rather than persist as an empty med list.
    with pytest.raises(ValidationError, match="no medication-category facts"):
        MedicationListDocument.model_validate(
            {
                "facts": [
                    {"field_path": "patient.name", "value": "Jordan Rivera", "category": "demographic"},
                    {"field_path": "patient.dob", "value": "1971-02-03", "category": "demographic"},
                ]
            }
        )


def test_medication_list_still_drops_a_stray_line_when_medications_survive() -> None:
    # The tolerant path is unchanged: filtering only fails when nothing survives.
    doc = MedicationListDocument.model_validate(
        {
            "facts": [
                {"field_path": "patient.name", "value": "Jordan Rivera", "category": "demographic"},
                {"field_path": "medications[0].name", "value": "Lisinopril", "category": "medication"},
            ]
        }
    )
    assert [f.value for f in doc.facts] == ["Lisinopril"]


# --- 2. an honest partial real-world extraction still validates -------------


def test_a_qualitative_lab_result_with_no_unit_or_reference_range_validates() -> None:
    # A real serology result: "Non-Reactive" has no unit, no reference range, and
    # no abnormal flag — the report simply does not print them. This is an honest
    # document, not a broken extraction, and it must survive the contract.
    report = LabReport.model_validate(
        {
            "facts": [{"field_path": "hcv_antibody", "value": "Non-Reactive", "page_no": 1}],
            "specimen": "Serum",
            "collected_at": "2026-03-04T09:12:00",
        }
    )
    assert report.facts[0].value == "Non-Reactive"
    assert report.facts[0].unit is None


def test_a_partial_lab_fact_reports_its_gaps_instead_of_hiding_them() -> None:
    # The judgment call, pinned: the four fields stay optional, but absence is
    # *data*. A caller can count and gate on the gap; nothing is silently None.
    fact = ExtractedFact.model_validate({"field_path": "hcv_antibody", "value": "Non-Reactive"})
    missing = fact.missing_lab_fields()
    assert LabField.unit in missing
    assert LabField.reference_range in missing
    assert LabField.abnormal in missing
    assert LabField.collection_date in missing
    # Never missing — these two are hard-required by the schema itself.
    assert LabField.test_name not in missing
    assert LabField.value not in missing


def test_a_fully_populated_reconciled_lab_fact_reports_no_gaps() -> None:
    fact = ExtractedFact.model_validate(
        {
            "field_path": "hemoglobin",
            "value": "13.5",
            "unit": "g/dL",
            "reference_range": "13.0-17.0",
            "abnormal": "N",
            "collection_date": "2026-03-04T09:12:00",
            "page_no": 1,
            "bbox": [0.32, 0.10, 0.06, 0.03],
            "match_confidence": 0.97,
            "supported": True,
        }
    )
    assert fact.missing_lab_fields() == frozenset()


def test_source_citation_counts_as_present_only_once_reconciliation_anchors_it() -> None:
    # The citation is earned from the page, not claimed by the model: an
    # un-reconciled fact (no bbox) reports the citation missing even if the
    # payload asserts `supported`.
    claimed = ExtractedFact.model_validate(
        {"field_path": "hemoglobin", "value": "13.5", "supported": True}
    )
    assert LabField.source_citation in claimed.missing_lab_fields()

    anchored = claimed.model_copy(update={"bbox": [0.32, 0.10, 0.06, 0.03], "match_confidence": 0.97})
    assert LabField.source_citation not in anchored.missing_lab_fields()


def test_a_report_level_collection_date_satisfies_its_facts() -> None:
    # A real lab prints the collection date once, in the specimen header — not on
    # every analyte row. Resolving the per-fact gap against `collected_at` keeps
    # the signal meaningful instead of flagging every fact on every report.
    payload = {"facts": [{"field_path": "hemoglobin", "value": "13.5", "unit": "g/dL"}]}
    without = LabReport.model_validate(payload)
    assert LabField.collection_date in without.incomplete_facts[0][1]

    with_header = LabReport.model_validate({**payload, "collected_at": "2026-03-04T09:12:00"})
    assert LabField.collection_date not in with_header.incomplete_facts[0][1]


def test_incomplete_facts_lists_only_the_facts_with_gaps() -> None:
    report = LabReport.model_validate(
        {
            "facts": [
                {
                    "field_path": "hemoglobin",
                    "value": "13.5",
                    "unit": "g/dL",
                    "reference_range": "13.0-17.0",
                    "abnormal": "N",
                    "bbox": [0.32, 0.10, 0.06, 0.03],
                    "match_confidence": 0.97,
                    "supported": True,
                },
                {"field_path": "hcv_antibody", "value": "Non-Reactive"},
            ],
            "collected_at": "2026-03-04T09:12:00",
        }
    )
    assert [fact.field_path for fact, _ in report.incomplete_facts] == ["hcv_antibody"]


def test_an_intake_form_missing_a_category_validates_and_reports_coverage() -> None:
    # A real form with no allergies section ("NKDA") is honest paperwork. The spec
    # names allergies a required intake field, but requiring one fact per category
    # would reject that form; coverage is reported instead of enforced.
    form = IntakeForm.model_validate(
        {
            "facts": [
                {"field_path": "patient.name", "value": "Jordan Rivera", "category": "demographic"},
                {"field_path": "chief_complaint", "value": "Chest pain", "category": "chief_complaint"},
            ]
        }
    )
    assert form.categories_present == frozenset(
        {IntakeCategory.demographic, IntakeCategory.chief_complaint}
    )
    assert IntakeCategory.allergy not in form.categories_present


def test_the_hardening_did_not_weaken_strictness() -> None:
    # Guard rail: min_length must not have relaxed anything else. A coerced value,
    # an unknown key, and a frozen mutation all still fail.
    with pytest.raises(ValidationError):  # value coerced from float
        LabReport.model_validate({"facts": [{"field_path": "hgb", "value": 13.5}]})
    with pytest.raises(ValidationError):  # extra="forbid"
        LabReport.model_validate({"facts": [{"field_path": "hgb", "value": "13.5"}], "bogus": 1})
    fact = IntakeFact.model_validate({"field_path": "a", "value": "b", "category": "allergy"})
    with pytest.raises(ValidationError):  # frozen
        fact.value = "c"  # type: ignore[misc]
