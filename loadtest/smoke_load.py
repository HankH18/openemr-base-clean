"""Dependency-light load driver — the fallback when Locust won't install.

Locust is the preferred harness (see ``locustfile.py``), but its ``gevent``
dependency requires a C toolchain that some CI sandboxes lack. This driver
reproduces the same measurement — concurrent users hammering the four required
surfaces, then p50/p95/p99 latency + error rate + throughput per endpoint —
using only ``httpx`` (already a project dependency) and the stdlib ``asyncio``.

**Resource profiling (CPU + memory).** Latency/throughput characterize the
*response*; they say nothing about what the server is spending to produce it. So
alongside the request metrics this driver samples the agent *process* from the
outside — its CPU% and resident memory (RSS) — at a fixed interval for the whole
run, and records peak + mean of each into the JSON output. Sampling uses
``psutil`` when available (preferred; samples the uvicorn process + any children)
and degrades gracefully to an honest "not sampled" note when it is not. Because
the sampler observes another process, it needs that process's PID: pass
``--target-pid`` (``run.sh`` passes the uvicorn PID it just launched), or let the
driver auto-discover the ``uvicorn ... copilot.api.app`` process when the flag is
omitted.

Usage:

    python loadtest/smoke_load.py --host http://localhost:8010 \
        --users 10 --duration 20 --target-pid "$APP_PID" \
        --out loadtest/results_10u.json
    python loadtest/smoke_load.py --host http://localhost:8010 \
        --users 50 --duration 20 --target-pid "$APP_PID" \
        --out loadtest/results_50u.json

Run the driver itself with an interpreter that has ``psutil`` installed (see
``loadtest/requirements.txt`` / ``loadtest/.venv``); the agent under test still
boots with ``agent/.venv``. ``psutil`` samples any PID regardless of which
interpreter the agent runs under, so only the driver's venv needs it.

Each virtual user loops for ``--duration`` seconds, issuing weighted requests
across the endpoint mix (same weights as locustfile.py) with a small think-time
between calls. A response is counted as an ERROR when its status is >= 400,
EXCEPT ``/ready`` 503 (an expected not-ready signal, not a failure).

**Auth mode:** like ``locustfile.py``, this driver sends ``clinician_id`` in the
request body/query, which only authorizes the data routes under
``auth_mode=disabled`` (the default; what ``run.sh`` boots). On a ``smart``-mode
deployment those routes return 401 without an ``af_session`` session cookie, so
this unauthenticated driver must be pointed at a ``disabled``-mode instance.
Authenticated smart-mode load testing would need a seeded session cookie
replayed on every request (out of scope here).

Lives under ``loadtest/`` (outside the agent's pytest ``testpaths``); not named
``test_*`` — never collected or imported by the agent test suite.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import threading
import time
from dataclasses import dataclass, field

import httpx

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is optional at runtime
    psutil = None  # type: ignore[assignment]

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


def discover_agent_pid() -> int | None:
    """Best-effort find the running ``uvicorn ... copilot.api.app`` PID.

    Used only when ``--target-pid`` is not supplied. Returns the first process
    whose command line mentions both ``uvicorn`` and ``copilot.api.app``.
    """
    if psutil is None:
        return None
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
        except (psutil.Error, KeyError):
            continue
        joined = " ".join(cmdline)
        if "uvicorn" in joined and "copilot.api.app" in joined:
            return int(proc.info["pid"])
    return None


class ResourceSampler(threading.Thread):
    """Samples an external process's CPU% + RSS on a fixed interval, in a thread.

    Runs in its own OS thread so its cadence is decoupled from the asyncio event
    loop (which is saturated by the httpx virtual users at high concurrency).
    Samples the target process *and* any children (a single-worker uvicorn is one
    process, but summing children is robust if that ever changes). ``cpu_percent``
    is psutil per-process utilization where 100% == one fully-busy core, so a
    multi-core process can exceed 100%.
    """

    def __init__(self, pid: int, interval_s: float) -> None:
        super().__init__(name="resource-sampler", daemon=True)
        self.pid = pid
        self.interval_s = interval_s
        # NB: not ``_stop`` — that name shadows Thread._stop(), which join() calls.
        self._stop_event = threading.Event()
        self._root: object | None = None
        # psutil.Process objects MUST be cached across samples, keyed by pid:
        # cpu_percent(interval=None) returns the CPU delta since the *previous
        # call on the same object*. A fresh object (as proc.children() returns
        # every call) is always a "first call" and reports 0.0 — which silently
        # drops all CPU spent in child processes. Reusing the objects fixes that.
        self._procs: dict[int, object] = {}
        self.cpu_samples: list[float] = []
        self.rss_samples_mb: list[float] = []
        self.error: str | None = None

    def _refresh_tree(self) -> None:
        """Add newly-appeared descendants to the cache (primed), keep the rest."""
        if self._root is None:
            return
        try:
            children = self._root.children(recursive=True)  # type: ignore[attr-defined]
        except psutil.Error:  # type: ignore[union-attr]
            return
        for child in children:
            if child.pid in self._procs:
                continue
            self._procs[child.pid] = child
            try:
                child.cpu_percent(interval=None)  # prime the new object's baseline
            except psutil.Error:  # type: ignore[union-attr]
                pass

    def run(self) -> None:
        if psutil is None:
            self.error = "psutil not installed"
            return
        try:
            self._root = psutil.Process(self.pid)
        except Exception as exc:  # noqa: BLE001 - report any attach failure verbatim
            self.error = f"cannot attach to pid {self.pid}: {exc}"
            return
        # Cache + prime the root and its initial descendants so the first real
        # sample measures a full interval's CPU delta rather than 0.0.
        self._procs = {self.pid: self._root}
        try:
            self._root.cpu_percent(interval=None)  # type: ignore[attr-defined]
        except psutil.Error:  # type: ignore[union-attr]
            pass
        self._refresh_tree()
        while not self._stop_event.wait(self.interval_s):
            self._refresh_tree()  # pick up any children spawned mid-run
            cpu_total = 0.0
            rss_total = 0
            alive = False
            for pid in list(self._procs):
                proc = self._procs[pid]
                try:
                    cpu_total += proc.cpu_percent(interval=None)  # type: ignore[attr-defined]
                    rss_total += proc.memory_info().rss  # type: ignore[attr-defined]
                    alive = True
                except psutil.Error:  # type: ignore[union-attr]
                    self._procs.pop(pid, None)  # process exited — stop tracking it
                    continue
            if alive:
                self.cpu_samples.append(cpu_total)
                self.rss_samples_mb.append(rss_total / (1024.0 * 1024.0))

    def stop(self) -> None:
        self._stop_event.set()

    def summary(self) -> dict[str, object]:
        n = len(self.cpu_samples)
        if psutil is None:
            note = (
                "psutil not installed in the driver interpreter — CPU/RSS not "
                "sampled. Install loadtest/requirements.txt into loadtest/.venv."
            )
            sampler = "none"
        elif self.error is not None:
            note = f"resource sampling unavailable: {self.error}"
            sampler = "none"
        elif n == 0:
            note = "no resource samples captured (run shorter than one interval?)"
            sampler = "psutil"
        else:
            note = (
                "psutil process-tree sampling (uvicorn process + children) of the "
                "agent under test; cpu_percent is per-process utilization where "
                "100% == one fully-busy core (may exceed 100% on multi-core)."
            )
            sampler = "psutil"
        return {
            "sampler": sampler,
            "target_pid": self.pid,
            "interval_s": self.interval_s,
            "samples": n,
            "cpu_percent_peak": round(max(self.cpu_samples), 2) if n else 0.0,
            "cpu_percent_mean": round(sum(self.cpu_samples) / n, 2) if n else 0.0,
            "rss_mb_peak": round(max(self.rss_samples_mb), 2) if n else 0.0,
            "rss_mb_mean": round(sum(self.rss_samples_mb) / n, 2) if n else 0.0,
            "note": note,
        }


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


async def run(
    host: str,
    users: int,
    duration: int,
    target_pid: int | None = None,
    sample_interval_s: float = 0.5,
) -> dict[str, object]:
    endpoints = _endpoints()
    stats: dict[str, EndpointStats] = {name: EndpointStats(name) for name, _, _ in endpoints}
    limits = httpx.Limits(max_connections=users * 2, max_keepalive_connections=users * 2)

    sampler: ResourceSampler | None = None
    if target_pid is not None:
        sampler = ResourceSampler(target_pid, sample_interval_s)
        sampler.start()

    started = time.monotonic()
    deadline = started + duration
    async with httpx.AsyncClient(base_url=host, timeout=30.0, limits=limits) as client:
        await asyncio.gather(
            *[_user_loop(client, endpoints, stats, deadline) for _ in range(users)]
        )
    wall = time.monotonic() - started

    if sampler is not None:
        sampler.stop()
        sampler.join(timeout=sample_interval_s * 2 + 1.0)
    resource = (
        sampler.summary()
        if sampler is not None
        else {
            "sampler": "none",
            "target_pid": None,
            "interval_s": sample_interval_s,
            "samples": 0,
            "cpu_percent_peak": 0.0,
            "cpu_percent_mean": 0.0,
            "rss_mb_peak": 0.0,
            "rss_mb_mean": 0.0,
            "note": "no target pid supplied or discovered — CPU/RSS not sampled.",
        }
    )

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
        "resource": resource,
        "per_endpoint": per_endpoint,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="http://localhost:8010")
    ap.add_argument("--users", type=int, default=10)
    ap.add_argument("--duration", type=int, default=20)
    ap.add_argument("--out", default=None)
    ap.add_argument(
        "--target-pid",
        type=int,
        default=None,
        help="PID of the agent (uvicorn) process to sample CPU%%/RSS for. "
        "If omitted, the driver tries to auto-discover the uvicorn "
        "copilot.api.app process.",
    )
    ap.add_argument(
        "--sample-interval",
        type=float,
        default=0.5,
        help="Seconds between CPU%%/RSS samples of the target process.",
    )
    args = ap.parse_args()

    target_pid = args.target_pid
    if target_pid is None:
        target_pid = discover_agent_pid()
        if target_pid is not None:
            print(f"[resource] auto-discovered agent pid {target_pid}")
        else:
            print(
                "[resource] no --target-pid and could not auto-discover the "
                "uvicorn process — CPU/RSS will not be sampled."
            )

    result = asyncio.run(
        run(args.host, args.users, args.duration, target_pid, args.sample_interval)
    )
    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text + "\n")


if __name__ == "__main__":
    main()
