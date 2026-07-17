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

These tests pin the boundary's two halves, which is the whole point: a null extra
is dropped (it carries no information), and an extra that carries a VALUE still
raises (that is real data the schema does not model — we want it loud).
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


def test_an_extra_key_carrying_a_value_still_fails_loud() -> None:
    # The other half of the boundary. Dropping this would silently discard real
    # clinical data landing in a field the schema does not model.
    with pytest.raises(ValidationError) as exc:
        _extract(_intake_payload({"value_frequency": "BID"}), DocumentType.intake_form)
    assert "extra" in str(exc.value).lower()


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
