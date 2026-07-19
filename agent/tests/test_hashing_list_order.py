"""FIX 4 — the resource-LIST order must not affect the content hash.

The poller compares ``content_hash_for_resources(resources)`` against the prior
hash to decide whether to re-run the LLM synthesizer (``poller.py``). The
resources come from a FHIR fetch, whose element order is not material content:
the same unchanged set returned in a different server order produced a different
digest, which read as "changed" and triggered a spurious, billable
re-synthesis. The canonicalized resource list is now sorted by
``(resourceType, id)`` before hashing, so only content — not order — moves the
digest. This also restores the module docstring's stated invariant ("insertion
order ... do not affect the digest"). Nested-list order INSIDE a resource is
deliberately still significant (see ``test_hashing.py``).
"""

from __future__ import annotations

from copilot.worker.hashing import content_hash_for_resources


def test_two_resources_hash_equal_regardless_of_order() -> None:
    a = {"resourceType": "Observation", "id": "1", "valueQuantity": {"value": 5}}
    b = {"resourceType": "Condition", "id": "2", "code": {"text": "NSTEMI"}}
    assert content_hash_for_resources([a, b]) == content_hash_for_resources([b, a])


def test_order_invariance_holds_for_many_resources() -> None:
    res = [
        {"resourceType": "Observation", "id": str(i), "valueQuantity": {"value": i}}
        for i in range(6)
    ]
    forward = content_hash_for_resources(res)
    backward = content_hash_for_resources(list(reversed(res)))
    assert forward == backward


def test_same_type_distinct_ids_are_order_invariant() -> None:
    x = {"resourceType": "Observation", "id": "1", "valueQuantity": {"value": 1}}
    y = {"resourceType": "Observation", "id": "2", "valueQuantity": {"value": 2}}
    assert content_hash_for_resources([x, y]) == content_hash_for_resources([y, x])


def test_content_change_still_flips_hash() -> None:
    # Order-invariance must not blunt real change detection.
    a = [{"resourceType": "Observation", "id": "1", "valueQuantity": {"value": 5}}]
    b = [{"resourceType": "Observation", "id": "1", "valueQuantity": {"value": 6}}]
    assert content_hash_for_resources(a) != content_hash_for_resources(b)
