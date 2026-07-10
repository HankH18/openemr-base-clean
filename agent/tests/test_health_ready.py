"""API tests: /health always alive, /ready reflects dependency status."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi.testclient import TestClient

from copilot.api.app import create_app
from copilot.config import Settings
from copilot.domain.contracts import ReadinessDependency


def _ok(name: str) -> Callable[[], Awaitable[ReadinessDependency]]:
    async def probe() -> ReadinessDependency:
        return ReadinessDependency(name=name, ok=True)

    return probe


def _fail(name: str, detail: str = "boom") -> Callable[[], Awaitable[ReadinessDependency]]:
    async def probe() -> ReadinessDependency:
        return ReadinessDependency(name=name, ok=False, detail=detail)

    return probe


def _build_app(*probes: Callable[[], Awaitable[ReadinessDependency]]) -> TestClient:
    settings = Settings()
    factories = [lambda _s, p=p: p for p in probes]  # type: ignore[misc]
    return TestClient(create_app(settings=settings, probe_factories=factories))


def test_health_returns_200_and_reports_version() -> None:
    client = _build_app(_ok("postgres"))
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["alive"] is True
    assert isinstance(body["version"], str) and body["version"]


def test_ready_returns_200_when_all_probes_ok() -> None:
    client = _build_app(_ok("postgres"), _ok("openemr_fhir"), _ok("llm"), _ok("langfuse"))
    resp = client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert {d["name"]: d["ok"] for d in body["dependencies"]} == {
        "postgres": True,
        "openemr_fhir": True,
        "llm": True,
        "langfuse": True,
    }


def test_ready_returns_503_when_any_probe_fails() -> None:
    client = _build_app(
        _ok("postgres"), _fail("openemr_fhir", "connection refused"), _ok("llm"), _ok("langfuse")
    )
    resp = client.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ready"] is False
    failing = [d for d in body["dependencies"] if not d["ok"]]
    assert failing == [
        {"name": "openemr_fhir", "ok": False, "detail": "connection refused", "advisory": False}
    ]


def test_ready_preserves_probe_order() -> None:
    """Ordering matters for humans reading the JSON; assert it's stable."""
    client = _build_app(_ok("a"), _ok("b"), _ok("c"), _ok("d"))
    resp = client.get("/ready")
    names = [d["name"] for d in resp.json()["dependencies"]]
    assert names == ["a", "b", "c", "d"]


def test_advisory_dependency_failure_does_not_block_readiness() -> None:
    """A failing *advisory* dep is reported but never turns /ready into 503."""

    def _advisory_fail(name: str) -> Callable[[], Awaitable[ReadinessDependency]]:
        async def probe() -> ReadinessDependency:
            return ReadinessDependency(name=name, ok=False, detail="down", advisory=True)

        return probe

    client = _build_app(
        _ok("postgres"), _ok("openemr_fhir"), _ok("llm"), _advisory_fail("langfuse")
    )
    resp = client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    langfuse = next(d for d in body["dependencies"] if d["name"] == "langfuse")
    assert langfuse == {"name": "langfuse", "ok": False, "detail": "down", "advisory": True}
