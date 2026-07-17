"""The vision boundary: doc-type parsing and the ClaudeVision tool contract.

Two audit findings are guarded here.

1. ``parse_doc_type`` silently defaulted an unknown kind to ``lab_pdf``, so a
   mistyped/renamed type was extracted with the WRONG schema with no error. The
   HTTP route rejects unknown types, but that is the sink — the service, CLI and
   graph reach the parser instead, so it must fail loud at the source.

2. ``ClaudeVision`` had ZERO test coverage — and it is exactly where every
   real-model bug in this project has lived (dates returned as strings; ``facts``
   returned as a JSON string on dense documents). Its contract is exercised here
   with a fake client: no network, no key.
"""

from __future__ import annotations

from typing import Any

import pytest

from copilot.config import Settings
from copilot.documents.vision import (
    ClaudeVision,
    DocumentType,
    UnknownDocumentTypeError,
    VisionExtractionError,
    build_vision,
    parse_doc_type,
)
from copilot.domain.documents import LabReport


class _Block:
    def __init__(self, name: str, payload: Any) -> None:
        self.type = "tool_use"
        self.name = name
        self.input = payload


class _Response:
    def __init__(self, payload: Any, name: str = "record_extraction") -> None:
        self.content = [_Block(name, payload)]


class _FakeMessages:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._response


class _FakeClient:
    def __init__(self, response: Any) -> None:
        self.messages = _FakeMessages(response)


def _keyed() -> Settings:
    return Settings(anthropic_api_key="sk-test")


# --- parse_doc_type: fail loud at the source --------------------------------


@pytest.mark.parametrize("raw", ["lab_pdf", "intake_form", "medication_list"])
def test_every_known_kind_parses(raw: str) -> None:
    assert parse_doc_type(raw).value == raw


@pytest.mark.parametrize("raw", ["radiology_xray", "intake_lab_report", "", "LAB_PDF"])
def test_unknown_kind_raises_instead_of_defaulting_to_lab_pdf(raw: str) -> None:
    # The regression: any of these silently became lab_pdf and were extracted
    # with the lab schema.
    with pytest.raises(UnknownDocumentTypeError) as excinfo:
        parse_doc_type(raw)
    assert "expected one of" in str(excinfo.value), "the error must name the valid kinds"


# --- ClaudeVision tool contract ---------------------------------------------


def test_claude_vision_forces_the_tool_whose_schema_is_the_pydantic_schema() -> None:
    # "The schema is the source of truth": the tool's input_schema IS the model's
    # JSON schema, and the tool call is forced — not merely requested.
    import anyio

    client = _FakeClient(_Response({"facts": [{"field_path": "hemoglobin", "value": "13.5"}]}))
    vision = ClaudeVision(_keyed(), client=client)
    report = anyio.run(vision.extract, [], DocumentType.lab_pdf)

    assert isinstance(report, LabReport)
    call = client.messages.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "record_extraction"}
    assert call["tools"][0]["input_schema"] == LabReport.model_json_schema()


def test_claude_vision_validates_output_through_the_strict_schema() -> None:
    # Raw model output can never bypass validation: a wrong-typed value raises
    # rather than being coerced into a confident-but-wrong extraction.
    import anyio
    from pydantic import ValidationError

    client = _FakeClient(_Response({"facts": [{"field_path": "hemoglobin", "value": 13.5}]}))
    vision = ClaudeVision(_keyed(), client=client)
    with pytest.raises(ValidationError):
        anyio.run(vision.extract, [], DocumentType.lab_pdf)


def test_claude_vision_recovers_a_stringified_facts_payload() -> None:
    # The dense-document failure mode observed live: the model returns `facts` as
    # a JSON string. It must be recovered, not lost.
    import anyio

    client = _FakeClient(_Response({"facts": '[{"field_path": "lactate", "value": "4.2"}]'}))
    vision = ClaudeVision(_keyed(), client=client)
    report = anyio.run(vision.extract, [], DocumentType.lab_pdf)
    assert [f.value for f in report.facts] == ["4.2"]


def test_claude_vision_raises_when_the_model_returns_no_tool_call() -> None:
    import anyio

    client = _FakeClient(_Response({"facts": []}, name="something_else"))
    vision = ClaudeVision(_keyed(), client=client)
    with pytest.raises(VisionExtractionError):
        anyio.run(vision.extract, [], DocumentType.lab_pdf)


def test_claude_vision_refuses_to_construct_without_a_key() -> None:
    with pytest.raises(VisionExtractionError):
        ClaudeVision(Settings(anthropic_api_key=""))


def test_build_vision_is_key_gated() -> None:
    from copilot.documents.vision import StubVision

    assert isinstance(build_vision(Settings(anthropic_api_key="")), StubVision)
    assert isinstance(build_vision(_keyed()), ClaudeVision)


# --- the ingestion kill switch is real, not phantom --------------------------


def test_document_ingestion_flag_defaults_on_preserving_todays_behavior() -> None:
    # It previously defaulted False and gated nothing. True preserves the live
    # behavior exactly while making the switch meaningful.
    assert Settings().document_ingestion_enabled is True


def test_upload_returns_503_when_ingestion_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # The regression guarded: the flag was declared as a "master switch" and read
    # NOWHERE, so an operator could not actually stop intake.
    from fastapi.testclient import TestClient

    from copilot.api.app import create_app
    from copilot.config import get_settings

    monkeypatch.setenv("COPILOT_DOCUMENT_INGESTION_ENABLED", "false")
    monkeypatch.setenv("COPILOT_ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    try:
        client = TestClient(create_app(get_settings(), probe_factories=[]))
        r = client.post(
            "/v1/documents",
            files={"file": ("x.pdf", b"%PDF-1.4\n", "application/pdf")},
            data={"patient_id": "1001", "clinician_id": "42", "doc_type": "lab_pdf"},
        )
        assert r.status_code == 503, f"disabled ingestion must 503, got {r.status_code}"
    finally:
        get_settings.cache_clear()
