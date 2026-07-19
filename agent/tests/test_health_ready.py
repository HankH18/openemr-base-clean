"""API tests: /health always alive, /ready reflects dependency status.

Also covers the additive HTML content negotiation on ``/ready``: a browser
(``Accept`` ranking ``text/html`` above JSON) gets a rendered page, while every
programmatic client (``*/*``, ``application/json``, no ``Accept``) keeps the
byte-identical JSON contract.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi.testclient import TestClient

from copilot.api.app import create_app
from copilot.config import Settings
from copilot.domain.contracts import ReadinessDependency, ReadinessResponse

# What a real browser sends: text/html at q=1.0, JSON only via */*;q=0.8.
_BROWSER_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
)


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
        {
            "name": "openemr_fhir",
            "ok": False,
            "detail": "connection refused",
            "advisory": False,
            "status": "down",
        }
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
    assert langfuse == {
        "name": "langfuse",
        "ok": False,
        "detail": "down",
        "advisory": True,
        "status": "degraded",
    }


# --- Additive HTML content negotiation on /ready ----------------------------


def _expected_ready_json(*probes: Callable[[], Awaitable[ReadinessDependency]]) -> dict:
    """The exact JSON the pre-HTML handler produced for these probe results.

    Deterministic because the fake probes above return fixed dependencies, so we
    can rebuild the model and assert key/value equality against the wire body.
    """

    import asyncio

    deps = asyncio.run(_run(probes))
    return ReadinessResponse.from_dependencies(deps).model_dump(mode="json")


async def _run(
    probes: tuple[Callable[[], Awaitable[ReadinessDependency]], ...],
) -> list[ReadinessDependency]:
    return [await p() for p in probes]


def test_ready_browser_accept_returns_html_page() -> None:
    """A browser Accept header yields a 200 HTML page enumerating every dep + a pill."""
    probes = (_ok("document_store"), _ok("migrations"), _ok("llm"))
    client = _build_app(*probes)
    resp = client.get("/ready", headers={"Accept": _BROWSER_ACCEPT})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    # Every probed dependency is named, and each carries a status pill.
    for name in ("document_store", "migrations", "llm"):
        assert name in body
    assert body.count('class="pill') == len(probes)
    assert "READY" in body


def test_ready_browser_accept_reflects_not_ready_with_503() -> None:
    """The HTML page keeps the 503 status and shows a down pill when gating fails."""
    client = _build_app(_ok("document_store"), _fail("migrations", "no migration applied"))
    resp = client.get("/ready", headers={"Accept": _BROWSER_ACCEPT})

    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("text/html")
    assert "NOT READY" in resp.text
    assert 'class="pill down"' in resp.text
    assert "no migration applied" in resp.text


def test_ready_json_is_byte_identical_for_json_and_wildcard_accept() -> None:
    """`application/json`, `*/*`, and no Accept all return the unchanged JSON contract."""
    probes = (_ok("document_store"), _fail("langfuse", "down"), _ok("llm"))
    client = _build_app(*probes)
    expected = _expected_ready_json(*probes)

    # */* is what httpx/TestClient/curl send by default.
    r_star = client.get("/ready")
    # An explicit application/json request.
    r_json = client.get("/ready", headers={"Accept": "application/json"})

    for resp in (r_star, r_json):
        assert resp.headers["content-type"].startswith("application/json")
        assert resp.json() == expected
    # Byte-for-byte identical bodies across the two machine Accept variants.
    assert r_star.content == r_json.content


def test_ready_html_escapes_dependency_detail() -> None:
    """Dependency detail is HTML-escaped — no raw markup reaches the page."""
    client = _build_app(_fail("migrations", "schema <broken> & 'stale'"))
    resp = client.get("/ready", headers={"Accept": _BROWSER_ACCEPT})
    assert "<broken>" not in resp.text
    assert "&lt;broken&gt;" in resp.text
