"""Guard the vision extractor against a JSON-stringified tool payload.

Tool-forced JSON should arrive structured, but on dense real documents a vision
model sometimes returns ``facts`` — or the whole object — as a JSON string, which
would fail strict validation (``facts`` is not a ``list``). ``_destringify``
recovers it before validation; a well-formed payload is untouched and an
unparseable string is left for strict validation to reject loudly.
"""

from __future__ import annotations

from copilot.documents.vision import _destringify


def test_stringified_facts_list_is_parsed() -> None:
    out = _destringify({"facts": '[{"field_path": "hemoglobin", "value": "13.5"}]'})
    assert out["facts"] == [{"field_path": "hemoglobin", "value": "13.5"}]


def test_whole_object_stringified_under_facts_is_recovered() -> None:
    # The dense-document failure mode: the model stringified the entire object.
    out = _destringify({"facts": '{"collected_at": "2026-07-13T06:20", "facts": [{"field_path": "lactate", "value": "4.2"}]}'})
    assert out["collected_at"] == "2026-07-13T06:20"
    assert out["facts"] == [{"field_path": "lactate", "value": "4.2"}]


def test_well_formed_payload_is_untouched() -> None:
    payload = {"facts": [{"field_path": "wbc", "value": "15.8"}], "specimen": "serum"}
    assert _destringify(payload) is payload


def test_unparseable_string_is_left_for_strict_validation() -> None:
    # Not JSON → returned as-is; strict model_validate then rejects it loudly.
    payload = {"facts": "not json at all"}
    assert _destringify(payload) == payload
