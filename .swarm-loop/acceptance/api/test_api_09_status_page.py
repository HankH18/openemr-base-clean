"""feat_api criterion 9 — the agent-served status page (Decision 7): a
``/status`` (or ``/v1/status``) endpoint returns the health aggregates read
from the agent DB + eval artifacts. Structural check only: keys present,
values well-typed — no judgement on the values.

FROZEN GOALS, black-box over HTTP. Payload contract pinned here (JSON):
- ``ingestion_count``: int — documents ingested;
- ``extraction_field_pass_rate``: number — field-level extraction pass rate;
- ``retrieval_hit_rate``: number;
- ``routing_decisions``: object (decision -> count) or int total;
- ``eval_by_category``: object — eval pass/fail (or rate) per category;
- ``latency`` (or ``latency_ms``): object with numeric ``p50`` and ``p95``;
- ``error_rate``: number.
"""

from __future__ import annotations


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def test_api_09_status_page_health_aggregates(client):
    r = client.get("/status")
    if r.status_code == 404:
        r = client.get("/v1/status")
    assert r.status_code == 200, (
        "an agent-served status page must exist at /status or /v1/status "
        f"(Decision 7); got {r.status_code}"
    )
    ctype = r.headers.get("content-type", "")
    assert ctype.startswith("application/json"), (
        f"the status aggregates must be served as JSON; got content-type {ctype!r}"
    )
    body = r.json()
    assert isinstance(body, dict), f"status payload must be an object; got {type(body).__name__}"

    problems: list[str] = []
    if not (isinstance(body.get("ingestion_count"), int) and not isinstance(body.get("ingestion_count"), bool)):
        problems.append("ingestion_count: int")
    if not _num(body.get("extraction_field_pass_rate")):
        problems.append("extraction_field_pass_rate: number")
    if not _num(body.get("retrieval_hit_rate")):
        problems.append("retrieval_hit_rate: number")
    routing = body.get("routing_decisions")
    if not (isinstance(routing, dict) or (isinstance(routing, int) and not isinstance(routing, bool))):
        problems.append("routing_decisions: object (decision -> count) or int")
    if not isinstance(body.get("eval_by_category"), dict):
        problems.append("eval_by_category: object (category -> pass/fail or rate)")
    latency = body.get("latency") or body.get("latency_ms")
    if not (isinstance(latency, dict) and _num(latency.get("p50")) and _num(latency.get("p95"))):
        problems.append("latency (or latency_ms): object with numeric p50 and p95")
    if not _num(body.get("error_rate")):
        problems.append("error_rate: number")

    assert not problems, (
        "status page payload is missing / ill-typed aggregates — required: "
        + "; ".join(problems)
        + f"; got keys {sorted(body)}"
    )
