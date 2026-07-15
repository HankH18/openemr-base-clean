"""Tests for the third ingestible document type: medication lists.

A medication-list document flows through the same upload -> extract -> reconcile
-> persist pipeline as ``lab_pdf`` and ``intake_form``, driven entirely by
``schema_for`` (no pipeline edit). These guards prove the wiring end-to-end at the
extraction boundary: the doc type parses, resolves to the strict
``MedicationListDocument`` schema, and the keyless stub returns medication facts
that are pinned to the OpenEMR ``medication`` category, validate strictly, and
reconcile against the recorded OCR page.
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
    MedicationFact,
    MedicationListDocument,
)


def test_parse_doc_type_accepts_medication_list() -> None:
    assert parse_doc_type("medication_list") is DocumentType.medication_list


def test_schema_for_returns_medication_list_document() -> None:
    assert schema_for(DocumentType.medication_list) is MedicationListDocument


def test_medication_fact_is_pinned_to_the_medication_category() -> None:
    # The default supplies the category (the VLM need not emit it) ...
    fact = MedicationFact.model_validate({"field_path": "medications[0].name", "value": "Lisinopril"})
    assert fact.category is IntakeCategory.medication
    # ... and it round-trips through the IntakeFact persistence path.
    assert isinstance(fact, IntakeFact)


def test_medication_fact_rejects_a_non_medication_category() -> None:
    # A medication list is homogeneous — an allergy can never sneak in.
    with pytest.raises(ValidationError):
        MedicationFact.model_validate(
            {"field_path": "medications[0].name", "value": "Penicillin", "category": "allergy"}
        )


def test_medication_fact_keeps_value_a_verbatim_string() -> None:
    # extra="forbid" + strict: an unknown key or a coerced value fails loudly.
    fact = MedicationFact.model_validate({"field_path": "medications[0].name", "value": "10"})
    assert fact.value == "10"
    assert isinstance(fact.value, str)
    with pytest.raises(ValidationError):
        MedicationFact.model_validate(
            {"field_path": "m", "value": "Lisinopril", "dose": "10mg"}
        )


def test_medication_list_document_requires_facts() -> None:
    with pytest.raises(ValidationError):
        MedicationListDocument.model_validate({})


def test_stub_extraction_returns_categorized_medication_facts() -> None:
    report = asyncio.run(
        build_vision(Settings(anthropic_api_key="")).extract([], DocumentType.medication_list)
    )
    assert isinstance(report, MedicationListDocument)
    assert report.facts, "stub medication-list extraction should return facts"
    for fact in report.facts:
        assert isinstance(fact, MedicationFact)
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
