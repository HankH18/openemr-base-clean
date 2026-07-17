"""Content hash: order-invariance for dict keys, stability for lists."""

from __future__ import annotations

from copilot.worker.hashing import content_hash_for_resources


def test_empty_input_has_stable_nonempty_hash() -> None:
    h = content_hash_for_resources([])
    assert isinstance(h, str) and len(h) == 64


def test_dict_key_order_does_not_affect_hash() -> None:
    a = [{"resourceType": "Observation", "id": "1", "status": "final"}]
    b = [{"status": "final", "id": "1", "resourceType": "Observation"}]
    assert content_hash_for_resources(a) == content_hash_for_resources(b)


def test_nested_dict_key_order_does_not_affect_hash() -> None:
    a = [{"id": "1", "valueQuantity": {"unit": "mg", "value": 5}}]
    b = [{"id": "1", "valueQuantity": {"value": 5, "unit": "mg"}}]
    assert content_hash_for_resources(a) == content_hash_for_resources(b)


def test_list_order_matters() -> None:
    x = [{"id": "1"}, {"id": "2"}]
    y = [{"id": "2"}, {"id": "1"}]
    assert content_hash_for_resources(x) != content_hash_for_resources(y)


def test_value_change_flips_hash() -> None:
    a = [{"id": "1", "value": "0.02"}]
    b = [{"id": "1", "value": "2.34"}]
    assert content_hash_for_resources(a) != content_hash_for_resources(b)
