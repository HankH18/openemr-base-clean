"""Tests for the OpenEMR-aligned intake extraction category.

Week-2 intake facts are tagged with the OpenEMR record they round-trip to
(``IntakeFact.category``), so ``allergy``/``medication``/``medical_problem`` facts
map 1:1 into the write-back path. Guards: the tag is required + typed, parses the
plain string a vision model emits, and the stub extraction is fully categorized.
Lab extraction stays uncategorized (byte-for-byte unchanged).
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from copilot.config import Settings
from copilot.documents.vision import DocumentType, build_vision
from copilot.domain.documents import (
    ExtractedFact,
    IntakeCategory,
    IntakeFact,
    IntakeForm,
    LabReport,
)


def test_category_covers_the_six_openemr_homes() -> None:
    assert {c.value for c in IntakeCategory} == {
        "demographic",
        "chief_complaint",
        "medication",
        "allergy",
        "medical_problem",
        "family_history",
    }


def test_intake_fact_requires_a_category() -> None:
    # Failure mode guarded: an untagged intake fact silently losing its OpenEMR home.
    with pytest.raises(ValidationError):
        IntakeFact.model_validate({"field_path": "allergies[0]", "value": "Penicillin"})


def test_intake_fact_parses_the_string_a_model_emits() -> None:
    # Vision models return JSON strings, not enum members — the same strict-vs-string
    # trap that once broke date parsing must not bite the category enum.
    fact = IntakeFact.model_validate(
        {"field_path": "allergies[0]", "value": "Penicillin", "category": "allergy"}
    )
    assert fact.category is IntakeCategory.allergy


def test_intake_fact_rejects_an_unknown_category() -> None:
    with pytest.raises(ValidationError):
        IntakeFact.model_validate(
            {"field_path": "x", "value": "y", "category": "not_a_record_type"}
        )


def test_lab_extraction_stays_uncategorized() -> None:
    report = asyncio.run(build_vision(Settings(anthropic_api_key="")).extract([], DocumentType.lab_pdf))
    assert isinstance(report, LabReport)
    assert report.facts
    assert all(type(fact) is ExtractedFact for fact in report.facts)


def test_stub_intake_extraction_is_fully_categorized() -> None:
    report = asyncio.run(
        build_vision(Settings(anthropic_api_key="")).extract([], DocumentType.intake_form)
    )
    assert isinstance(report, IntakeForm)
    assert report.facts, "stub intake extraction should return facts"
    for fact in report.facts:
        assert isinstance(fact, IntakeFact)
        assert isinstance(fact.category, IntakeCategory)
