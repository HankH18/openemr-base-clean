"""Real Claude vision intermittently invents a null-valued key; strict rejects all.

Observed live, against the deployed container with a real key, on the real
``demo/sample_docs/sample_intake_form.pdf``::

    pydantic_core._pydantic_core.ValidationError: 1 validation error for IntakeForm
    facts.22.value_frequency
      Extra inputs are not permitted [type=extra_forbidden, input_value=None]

``value_frequency`` exists in NO schema — the model invented it, with a ``None``
value, on one fact out of 42, and ``extra="forbid"`` then threw away the whole
extraction. A re-run came back clean: the failure is INTERMITTENT, so a green run
proves nothing. Every keyless test missed it because ``StubVision`` replays a
recording that never invents a key — fixture-shaped != reality-shaped.

Measured over 4 real runs of the same document, the model's decoration is
intermittent and NOT limited to nulls::

    run 0: 46 facts, 0 invented keys
    run 1: 39 facts, 0 invented keys
    run 2: 47 facts, 0 invented keys
    run 3: 46 facts, 2 invented keys   field_path_note='Dose', value_confidence=None

So these tests pin the boundary that survives contact with the real model: an
invented key is DROPPED (null or valued), and a valued one is LOGGED so genuine
schema drift stays visible. Strictness lives where it actually protects — required
fields, declared-field types, and reconciliation against the OCR tokens.
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from pydantic import ValidationError

from copilot.config import Settings
from copilot.documents.raster import RasterizedPage
from copilot.documents.vision import ClaudeVision, DocumentType


class _FakeBlock:
    type = "tool_use"
    name = "record_extraction"

    def __init__(self, payload: dict[str, Any]) -> None:
        self.input = payload


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.content = [_FakeBlock(payload)]


class _FakeMessages:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def create(self, **_kwargs: Any) -> _FakeResponse:
        return _FakeResponse(self._payload)


class _FakeClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.messages = _FakeMessages(payload)


def _extract(payload: dict[str, Any], doc_type: DocumentType) -> Any:
    settings = Settings(anthropic_api_key="test-key")
    vision = ClaudeVision(settings, client=_FakeClient(payload))
    page = RasterizedPage(page_no=1, image=b"\x89PNG\r\n\x1a\n", width=100, height=100)
    return anyio.run(vision.extract, [page], doc_type)


def _intake_payload(extra: dict[str, Any]) -> dict[str, Any]:
    """The real payload shape observed live, with one fact carrying `extra`."""
    return {
        "patient_name": "RIVERA, JORDAN A.",
        "date_of_birth": "03/11/1958",
        "facts": [
            {"field_path": "demographic.0", "value": "RIVERA, JORDAN A.", "category": "demographic"},
            # fact 22 in the live failure — a medication fact
            {
                "field_path": "medication.1",
                "value": "Metformin 1000 mg",
                "unit": "1000 mg",
                "category": "medication",
                **extra,
            },
        ],
    }


def test_null_extra_key_no_longer_destroys_the_whole_extraction() -> None:
    # The exact live failure: value_frequency=None on one fact of many.
    result = _extract(_intake_payload({"value_frequency": None}), DocumentType.intake_form)

    assert len(result.facts) == 2, "every fact survives — one invented null key loses nothing"
    assert result.facts[1].value == "Metformin 1000 mg"
    assert result.facts[1].unit == "1000 mg", "declared fields are untouched"
    assert not hasattr(result.facts[1], "value_frequency")


def test_an_extra_key_carrying_a_value_is_dropped_but_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # CONTRACT CHANGED, deliberately, against live evidence. This asserted that a
    # valued extra must RAISE — on the theory that it is real data the schema does
    # not model. Four real-vision runs refuted the premise: the valued extras are
    # decorations (`field_path_note='Dose'` beside a well-formed fact), and aborting
    # a 46-fact extraction over one is indefensible. `extra="forbid"` was never what
    # protected this boundary either — `value`/`field_path`/`category` are REQUIRED
    # (so relocated content still fails), and the no-invention gate is reconciliation
    # against the page's OCR tokens. Fail-loud is kept, as a log rather than a lost
    # extraction: this test fails if the drop ever goes silent.
    with caplog.at_level("WARNING"):
        result = _extract(_intake_payload({"field_path_note": "Dose"}), DocumentType.intake_form)

    assert len(result.facts) == 2, "the decoration must not cost us the real facts"
    assert result.facts[1].value == "Metformin 1000 mg"
    assert any("undeclared" in r.message for r in caplog.records), (
        "dropping an undeclared key that carried a VALUE must never be silent — "
        "that is how real schema drift would hide"
    )


def test_a_null_extra_at_the_top_level_is_dropped_too() -> None:
    payload = _intake_payload({})
    payload["reviewed_by"] = None  # invented, not in IntakeForm
    result = _extract(payload, DocumentType.intake_form)
    assert result.patient_name == "RIVERA, JORDAN A."


def test_a_missing_required_field_still_fails() -> None:
    # Proof the normalization did not soften strictness: value is required.
    payload = {"facts": [{"field_path": "demographic.0", "category": "demographic"}]}
    with pytest.raises(ValidationError):
        _extract(payload, DocumentType.intake_form)
