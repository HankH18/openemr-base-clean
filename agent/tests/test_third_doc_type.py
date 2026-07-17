"""Tests for the third ingestible document type: medication lists.

A medication-list document flows through the same upload -> extract -> reconcile
-> persist pipeline as ``lab_pdf`` and ``intake_form``, driven entirely by
``schema_for`` (no pipeline edit). ``MedicationListDocument`` uses the same
``list[IntakeFact]`` shape as ``IntakeForm`` (the shape real vision validates
reliably) and a validator that keeps only ``medication``-category facts, so a
stray non-medication line is dropped rather than failing the whole extraction
while the persisted list stays homogeneous.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from copilot.config import Settings
from copilot.documents.fixtures import STUB_MEDLIST_FACTS, STUB_PAGE_TOKENS
from copilot.documents.vision import DocumentType, build_vision, parse_doc_type, schema_for
from copilot.domain.documents import (
    IntakeCategory,
    IntakeFact,
    MedicationListDocument,
)


def test_parse_doc_type_accepts_medication_list() -> None:
    assert parse_doc_type("medication_list") is DocumentType.medication_list


def test_schema_for_returns_medication_list_document() -> None:
    assert schema_for(DocumentType.medication_list) is MedicationListDocument


def test_medication_list_keeps_only_medication_facts() -> None:
    # A stray non-medication line (e.g. a scanned header the VLM picks up) is
    # dropped, not fatal — the persisted list stays homogeneous.
    doc = MedicationListDocument.model_validate(
        {
            "facts": [
                {"field_path": "medications[0].name", "value": "Lisinopril", "category": "medication"},
                {"field_path": "patient.name", "value": "Jordan Rivera", "category": "demographic"},
                {"field_path": "medications[1].name", "value": "Metformin", "category": "medication"},
            ]
        }
    )
    assert [f.value for f in doc.facts] == ["Lisinopril", "Metformin"]
    assert all(f.category is IntakeCategory.medication for f in doc.facts)


def test_medication_list_document_requires_facts() -> None:
    with pytest.raises(ValidationError):
        MedicationListDocument.model_validate({})


def test_medication_facts_keep_value_a_verbatim_string() -> None:
    # extra="forbid" + strict: an unknown key or a coerced value fails loudly.
    doc = MedicationListDocument.model_validate(
        {"facts": [{"field_path": "medications[0].name", "value": "10", "category": "medication"}]}
    )
    assert doc.facts[0].value == "10"
    assert isinstance(doc.facts[0].value, str)
    with pytest.raises(ValidationError):
        MedicationListDocument.model_validate(
            {"facts": [{"field_path": "m", "value": "Lisinopril", "category": "medication", "dose": "10mg"}]}
        )


def test_stub_extraction_returns_categorized_medication_facts() -> None:
    report = asyncio.run(
        build_vision(Settings(anthropic_api_key="")).extract([], DocumentType.medication_list)
    )
    assert isinstance(report, MedicationListDocument)
    assert report.facts, "stub medication-list extraction should return facts"
    for fact in report.facts:
        assert isinstance(fact, IntakeFact)
        assert fact.category is IntakeCategory.medication
        assert isinstance(fact.value, str) and fact.value


def test_stub_medication_values_reconcile_against_the_recorded_page() -> None:
    # Recorded together: every stub medication value is a token on the stub OCR
    # page, so each fact reconciles to a bbox downstream (the no-invention gate).
    token_texts = {token["text"] for token in STUB_PAGE_TOKENS}
    for fact in STUB_MEDLIST_FACTS:
        assert fact["value"] in token_texts, (
            f"stub medication value {fact['value']!r} is not on the recorded OCR page"
        )
