"""feat_artifacts — the engineering-rigor deliverables the doc names.

FROZEN GOALS (file/content checks) at fixed repo-root paths:
- COST_ANALYSIS.md   — dev spend + projected cost at the 100/1k/10k/100k-user tiers
- api-collection/    — an importable Bruno/Postman collection covering the endpoints
- loadtest/          — a load-test script + captured p50/p95/p99 results
- OBSERVABILITY.md    — the Langfuse dashboard + alerts spec (metrics that must be watched)

Baseline: none of these exist — all fail until authored.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _read(p: Path) -> str:
    return p.read_text(errors="ignore").lower() if p.is_file() else ""


def test_cost_analysis_doc():
    text = _read(ROOT / "COST_ANALYSIS.md")
    assert text, "COST_ANALYSIS.md must exist at the repo root"
    assert "cost" in text
    assert any(k in text for k in ("dev spend", "development", "spent", "to date")), (
        "cost analysis must state development spend"
    )
    flat = text.replace(",", "")
    for tier in ("100", "1000", "10000", "100000"):
        assert tier in flat, f"cost analysis must project the {tier}-user tier"


def test_api_collection_present():
    d = ROOT / "api-collection"
    assert d.is_dir(), "an importable API collection must live at repo-root api-collection/"
    files = [p for p in d.rglob("*") if p.is_file()]
    assert files, "api-collection/ must contain the collection files (Bruno .bru or Postman .json)"
    joined = " ".join(_read(p) for p in files)
    assert "/v1/chat" in joined and "/v1/rounds/start" in joined, (
        "the API collection must cover the chat + rounds endpoints"
    )


def test_load_test_harness_and_results():
    d = ROOT / "loadtest"
    assert d.is_dir(), "a loadtest/ dir must exist at the repo root"
    scripts = [p for p in d.rglob("*") if p.is_file() and p.suffix in (".py", ".js", ".yml", ".yaml")]
    assert scripts, "loadtest/ must contain a load-test script (locust/k6/artillery)"
    results = [p for p in d.rglob("*") if p.is_file() and p.suffix in (".md", ".json", ".csv", ".txt")]
    joined = " ".join(_read(p) for p in results)
    assert any(k in joined for k in ("p95", "p99", "p50")), (
        "loadtest/ must include captured results with latency percentiles (p50/p95/p99)"
    )


def test_observability_dashboard_spec():
    text = _read(ROOT / "OBSERVABILITY.md") or _read(ROOT / "agent" / "OBSERVABILITY.md")
    assert text, "an OBSERVABILITY.md dashboard/alerts spec must exist"
    assert "alert" in text, "the observability spec must define alerts"
    assert any(k in text for k in ("p95", "latency", "error rate", "tokens", "verification")), (
        "the observability spec must name the real-time metrics to watch"
    )
