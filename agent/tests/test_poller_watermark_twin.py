"""FIX 1 — `_max_last_updated` is the poller-side twin of `rounds._watermark`.

Real FHIR data mixes tz-aware `meta.lastUpdated` stamps (`...Z`) with naive
ones. Comparing an aware `datetime` against a naive one raises `TypeError`
(not `ValueError`), which the parse `try` does not guard — so an un-normalized
comparison crashes `Poller.tick` for that patient. Each parsed stamp must be
normalized to UTC when naive before comparison, mirroring
`rounds.service._watermark` / `rounds.summary._parse`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from copilot.worker.poller import _max_last_updated


def _res(last_updated: str) -> dict:
    return {"meta": {"lastUpdated": last_updated}}


def test_mixed_aware_then_naive_does_not_raise() -> None:
    # aware first establishes `best`; the naive one must not TypeError against it.
    resources = [
        _res("2026-07-10T05:00:00Z"),  # aware
        _res("2026-07-11T05:00:00"),  # naive → treated as UTC, and it is the max
        _res("2026-07-09T05:00:00Z"),  # aware, earlier
    ]
    result = _max_last_updated(resources)
    assert result == datetime(2026, 7, 11, 5, 0, 0, tzinfo=UTC)
    assert result is not None and result.tzinfo is not None


def test_mixed_naive_then_aware_does_not_raise() -> None:
    # naive first establishes `best`; the aware one must not TypeError against it.
    resources = [
        _res("2026-07-09T05:00:00"),  # naive
        _res("2026-07-12T05:00:00Z"),  # aware, and the max
    ]
    result = _max_last_updated(resources)
    assert result == datetime(2026, 7, 12, 5, 0, 0, tzinfo=UTC)


def test_all_naive_returns_aware_utc() -> None:
    resources = [_res("2026-07-05T00:00:00"), _res("2026-07-06T00:00:00")]
    result = _max_last_updated(resources)
    assert result == datetime(2026, 7, 6, 0, 0, 0, tzinfo=UTC)


def test_unparseable_and_missing_are_skipped() -> None:
    resources = [
        {"meta": {"lastUpdated": "not-a-date"}},
        {"meta": {}},
        {},
        _res("2026-07-07T00:00:00Z"),
    ]
    assert _max_last_updated(resources) == datetime(2026, 7, 7, 0, 0, 0, tzinfo=UTC)


def test_empty_set_is_none() -> None:
    assert _max_last_updated([]) is None
