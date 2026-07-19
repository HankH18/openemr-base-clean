"""Content-negotiated HTML for /status and /v1/status (+ the negotiation helper).

The status routes are a machine contract (the frozen acceptance suite parses
their JSON). These tests pin the additive behaviour: a browser gets a rendered
HTML page; every programmatic client keeps the byte-identical JSON. The
negotiation predicate is unit-tested directly, then the two routes are exercised
end-to-end with a monkeypatched payload so the rendering + passthrough are
asserted without a live agent DB.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from copilot.api.app import create_app
from copilot.api.routes import status as status_mod
from copilot.api.status_html import prefers_html, render_status_html
from copilot.config import get_settings

_BROWSER_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
)

# A representative, fully-populated status payload matching _status_payload()'s
# shape — every branch of the renderer is exercised by this fixture.
_SAMPLE: dict[str, Any] = {
    "ingestion_count": 342,
    "extraction_field_pass_rate": 0.917,
    "retrieval_hit_rate": 0.0,
    "retrieval_hit_rate_available": False,
    "routing_decisions": {"chat.answer.served": 208, "write.propose": 37},
    "eval_by_category": {
        "schema_valid": {"passed": 53.0, "total": 53.0, "pass_rate": 1.0},
        "citation_present": {"passed": 51.0, "total": 53.0, "pass_rate": 0.9622641509433962},
    },
    "eval_dataset": {
        "name": "golden_v2_53case",
        "case_count": 53,
        "captured_at": "2026-07-18T22:14:03Z",
        "pass_rate": 0.9811,
    },
    "latency_ms": {"p50": 412.5, "p95": 1830.0},
    "error_rate": 0.0146,
    "metric_sources": {
        "ingestion_count": "measured: agent DB, source_document rows",
        "extraction_field_pass_rate": "measured: agent DB, extracted_fact.supported / total",
        "retrieval_hit_rate": "unavailable: retrieval outcomes are not recorded — placeholder 0.0.",
        "routing_decisions": "measured: agent DB, audit_log.action counts",
        "eval_by_category": "recorded: evals/gate_baseline.json — the 53-case golden set",
        "eval_dataset": "recorded: evals/gate_baseline.json provenance block",
        "latency_ms": "recorded: artifacts/latency_report.json — committed baseline.",
        "error_rate": "measured: agent DB, source_document.status == 'failed' / total",
    },
}


# --- prefers_html: the negotiation contract ---------------------------------


@pytest.mark.parametrize(
    ("accept", "expected"),
    [
        (_BROWSER_ACCEPT, True),  # browser: text/html q=1.0 > json q=0.8
        ("text/html", True),  # explicit html only
        ("*/*", False),  # curl / httpx / TestClient default
        ("application/json", False),  # explicit json
        ("", False),  # absent Accept header
        ("text/html, application/json", False),  # equal preference -> JSON (the contract)
        ("application/json, text/html;q=0.5", False),  # html deprioritised
        ("text/*", True),  # type wildcard for text still beats absent json
    ],
)
def test_prefers_html_matches_the_contract(accept: str, expected: bool) -> None:
    assert prefers_html(accept) is expected


# --- routes: negotiation + JSON identity + HTML content ---------------------


@pytest.fixture()
def status_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def _fake_payload() -> dict[str, Any]:
        return _SAMPLE

    monkeypatch.setattr(status_mod, "_status_payload", _fake_payload)
    get_settings.cache_clear()
    return TestClient(create_app(get_settings(), probe_factories=[]))


@pytest.mark.parametrize("path", ["/status", "/v1/status"])
def test_status_json_unchanged_for_machine_clients(status_client: TestClient, path: str) -> None:
    """*/*, application/json, and no Accept all return the exact payload as JSON."""
    r_star = status_client.get(path)  # */* default
    r_json = status_client.get(path, headers={"Accept": "application/json"})

    for resp in (r_star, r_json):
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        assert resp.json() == _SAMPLE
    assert r_star.content == r_json.content  # byte-identical


@pytest.mark.parametrize("path", ["/status", "/v1/status"])
def test_status_browser_gets_html_with_aggregates(status_client: TestClient, path: str) -> None:
    """A browser gets a 200 HTML page surfacing the aggregates + provenance."""
    resp = status_client.get(path, headers={"Accept": _BROWSER_ACCEPT})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    # Aggregates are visible.
    assert "Documents ingested" in body and "342" in body
    assert "91.7%" in body  # extraction pass rate
    assert "1830.0" in body  # p95 latency
    assert "chat.answer.served" in body and "208" in body  # routing table
    assert "citation_present" in body  # eval rubric row
    assert "golden_v2_53case" in body  # dataset provenance
    # Provenance labels are surfaced with their kind badges.
    for kind in ("measured", "recorded", "unavailable"):
        assert kind in body


def test_status_retrieval_hit_rate_shown_as_not_recorded(status_client: TestClient) -> None:
    """The placeholder 0.0 is rendered as 'not recorded', never a misleading number."""
    body = status_client.get("/v1/status", headers={"Accept": _BROWSER_ACCEPT}).text
    assert "not recorded" in body


def test_render_status_html_escapes_untrusted_strings() -> None:
    """Dict keys/values reaching the page are HTML-escaped."""
    from datetime import UTC, datetime

    payload = dict(_SAMPLE)
    payload["routing_decisions"] = {"evil<script>": 1}
    html = render_status_html(payload, generated_at=datetime.now(UTC))
    assert "<script>" not in html
    assert "evil&lt;script&gt;" in html
