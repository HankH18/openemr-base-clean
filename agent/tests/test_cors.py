"""CORS is off by default and on (with the configured origins) when set."""

from __future__ import annotations

from fastapi.testclient import TestClient

from copilot.api.app import create_app
from copilot.config import Settings


def _client(**overrides: object) -> TestClient:
    return TestClient(create_app(Settings(_env_file=None, **overrides), probe_factories=[]))


def test_no_cors_header_by_default() -> None:
    r = _client().get("/health", headers={"Origin": "http://localhost:4317"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_cors_header_when_origin_configured() -> None:
    r = _client(cors_allow_origins="http://localhost:4317").get(
        "/health", headers={"Origin": "http://localhost:4317"}
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://localhost:4317"
