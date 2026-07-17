"""ClaudeAgent reply parsing — tolerant of fences/prose, fails closed.

The chat agent must never raise (→ 500) on a model formatting quirk. It extracts
the JSON object from the reply when wrapped, and returns None (→ the service
withholds honestly) when no answer object can be recovered.
"""

from __future__ import annotations

from copilot.agent.claude import _json_object_slice, _parse_answer


def test_parse_plain_json() -> None:
    payload = _parse_answer('{"answer": "K is 5.7 mEq/L", "claims": []}')
    assert payload is not None
    assert payload.answer == "K is 5.7 mEq/L"
    assert payload.claims == []


def test_parse_strips_markdown_fences() -> None:
    payload = _parse_answer('```json\n{"answer": "hi", "claims": []}\n```')
    assert payload is not None
    assert payload.answer == "hi"


def test_parse_strips_surrounding_prose() -> None:
    payload = _parse_answer('Here is the result:\n{"answer": "ok", "claims": []}\nHope that helps.')
    assert payload is not None
    assert payload.answer == "ok"


def test_parse_keeps_nested_claim_objects() -> None:
    payload = _parse_answer(
        '{"answer":"a","claims":[{"resource_type":"Observation","resource_id":"o1"}]}'
    )
    assert payload is not None
    assert len(payload.claims) == 1
    assert payload.claims[0].resource_id == "o1"


def test_parse_returns_none_on_prose_refusal() -> None:
    assert _parse_answer("I'm sorry, I can't help with that request.") is None


def test_parse_returns_none_on_empty() -> None:
    assert _parse_answer("") is None


def test_json_object_slice() -> None:
    assert _json_object_slice('noise {"a": 1} trailing') == '{"a": 1}'
    assert _json_object_slice("no braces at all") is None
    assert _json_object_slice("") is None
