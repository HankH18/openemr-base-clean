"""Dependency-light load driver — the fallback when Locust won't install.

Locust is the preferred harness (see ``locustfile.py``), but its ``gevent``
dependency requires a C toolchain that some CI sandboxes lack. This driver
reproduces the same measurement — concurrent users hammering the four required
surfaces, then p50/p95/p99 latency + error rate per endpoint — using only
``httpx`` (already a project dependency) and the stdlib ``asyncio``.

Usage:

    python loadtest/smoke_load.py --host http://localhost:8010 \
        --users 10 --duration 20 --out loadtest/results_10u.json
    python loadtest/smoke_load.py --host http://localhost:8010 \
        --users 50 --duration 20 --out loadtest/results_50u.json

Each virtual user loops for ``--duration`` seconds, issuing weighted requests
across the endpoint mix (same weights as locustfile.py) with a small think-time
between calls. A response is counted as an ERROR when its status is >= 400,
EXCEPT ``/ready`` 503 (an expected not-ready signal, not a failure).

Lives under ``loadtest/`` (outside the agent's pytest ``testpaths``); not named
``test_*`` — never collected or imported by the agent test suite.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
from dataclasses import dataclass, field

import httpx

CLINICIAN_ID = 1
PATIENT_IDS = [101, 102, 103]
CHAT_QUESTIONS = [
    "What is the patient's most recent potassium?",
    "Summarize what changed since yesterday.",
    "Are there any critical labs?",
    "What medications is the patient on?",
]

# (name, weight, builder) — builder returns (method, path, json_body|None)
def _endpoints() -> list[tuple[str, int, object]]:
    return [
        ("GET /health", 1, lambda: ("GET", "/health", None)),
        ("GET /ready", 1, lambda: ("GET", "/ready", None)),
        (
            "GET /v1/rounds/current",
            4,
            lambda: ("GET", f"/v1/rounds/current?clinician_id={CLINICIAN_ID}", None),
        ),
        (
            "POST /v1/rounds/start",
            2,
            lambda: ("POST", "/v1/rounds/start", {"clinician_id": CLINICIAN_ID, "patient_ids": PATIENT_IDS}),
        ),
        # NOTE: /v1/rounds/advance is intentionally OMITTED from this concurrent
        # driver. All virtual users share one clinician_id, so concurrent
        # advance calls monotonically exhaust the single shared rounding cursor
        # (index runs past the list end), which then makes /v1/rounds/current
        # return 404 — a self-inflicted state mutation, not a service
        # characteristic. `advance` IS exercised by the canonical locustfile.py
        # (there each simulated clinician can own its own cursor). Here we keep
        # the mix idempotent so the measured percentiles reflect the service,
        # not cursor bookkeeping.
        (
            "POST /v1/chat",
            3,
            lambda: (
                "POST",
                "/v1/chat",
                {
                    "clinician_id": CLINICIAN_ID,
                    "patient_id": random.choice(PATIENT_IDS),
                    "message": random.choice(CHAT_QUESTIONS),
                },
            ),
        ),
    ]


@dataclass
class EndpointStats:
    name: str
    latencies_ms: list[float] = field(default_factory=list)
    status_counts: dict[int, int] = field(default_factory=dict)
    errors: int = 0

    def record(self, latency_ms: float, status: int) -> None:
        self.latencies_ms.append(latency_ms)
        self.status_counts[status] = self.status_counts.get(status, 0) + 1
        is_error = status >= 400 and not (self.name == "GET /ready" and status == 503)
        if is_error:
            self.errors += 1


def _pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _weighted_choice(endpoints: list[tuple[str, int, object]]) -> tuple[str, int, object]:
    total = sum(w for _, w, _ in endpoints)
    r = random.uniform(0, total)
    upto = 0.0
    for ep in endpoints:
        upto += ep[1]
        if r <= upto:
            return ep
    return endpoints[-1]


async def _user_loop(
    client: httpx.AsyncClient,
    endpoints: list[tuple[str, int, object]],
    stats: dict[str, EndpointStats],
    deadline: float,
) -> None:
    while time.monotonic() < deadline:
        name, _weight, builder = _weighted_choice(endpoints)
        method, path, body = builder()  # type: ignore[operator]
        t0 = time.perf_counter()
        try:
            if method == "GET":
                resp = await client.get(path)
            else:
                resp = await client.post(path, json=body)
            status = resp.status_code
        except Exception:
            status = 599  # transport failure
        latency_ms = (time.perf_counter() - t0) * 1000.0
        stats[name].record(latency_ms, status)
        await asyncio.sleep(random.uniform(0.01, 0.05))  # small think time


async def run(host: str, users: int, duration: int) -> dict[str, object]:
    endpoints = _endpoints()
    stats: dict[str, EndpointStats] = {name: EndpointStats(name) for name, _, _ in endpoints}
    limits = httpx.Limits(max_connections=users * 2, max_keepalive_connections=users * 2)
    started = time.monotonic()
    deadline = started + duration
    async with httpx.AsyncClient(base_url=host, timeout=30.0, limits=limits) as client:
        await asyncio.gather(
            *[_user_loop(client, endpoints, stats, deadline) for _ in range(users)]
        )
    wall = time.monotonic() - started

    per_endpoint = []
    total_reqs = 0
    total_errs = 0
    all_latencies: list[float] = []
    for name, _, _ in endpoints:
        s = stats[name]
        lat = sorted(s.latencies_ms)
        n = len(lat)
        total_reqs += n
        total_errs += s.errors
        all_latencies.extend(s.latencies_ms)
        per_endpoint.append(
            {
                "endpoint": name,
                "requests": n,
                "errors": s.errors,
                "error_rate_pct": round(100.0 * s.errors / n, 2) if n else 0.0,
                "p50_ms": round(_pct(lat, 0.50), 2),
                "p95_ms": round(_pct(lat, 0.95), 2),
                "p99_ms": round(_pct(lat, 0.99), 2),
                "max_ms": round(max(lat), 2) if lat else 0.0,
                "status_counts": s.status_counts,
            }
        )
    all_sorted = sorted(all_latencies)
    return {
        "host": host,
        "users": users,
        "duration_s": duration,
        "wall_s": round(wall, 2),
        "total_requests": total_reqs,
        "throughput_rps": round(total_reqs / wall, 1) if wall else 0.0,
        "total_errors": total_errs,
        "overall_error_rate_pct": round(100.0 * total_errs / total_reqs, 2) if total_reqs else 0.0,
        "overall_p50_ms": round(_pct(all_sorted, 0.50), 2),
        "overall_p95_ms": round(_pct(all_sorted, 0.95), 2),
        "overall_p99_ms": round(_pct(all_sorted, 0.99), 2),
        "per_endpoint": per_endpoint,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="http://localhost:8010")
    ap.add_argument("--users", type=int, default=10)
    ap.add_argument("--duration", type=int, default=20)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    result = asyncio.run(run(args.host, args.users, args.duration))
    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text + "\n")


if __name__ == "__main__":
    main()
