# Load-Test Results — AgentForge Clinical Co-Pilot

Baseline **CPU, memory, latency, and throughput** profiles for the `copilot`
agent at **10** and **50** concurrent users. Raw machine-readable output:
`results_10u.json`, `results_50u.json` (regenerate with `bash loadtest/run.sh`).

> **Provenance — read first.** These numbers were captured on **2026-07-19** by
> re-running the httpx driver (`loadtest/smoke_load.py`) against an
> `auth_mode=disabled` instance (identity from the request `clinician_id`), with
> the LLM stubbed and a throwaway SQLite DB — the same offline harness `run.sh`
> boots. They characterize the **service layer in disabled mode**, not a
> `smart`-mode deployment: there the data routes require an `af_session` session
> cookie and would 401 this unauthenticated driver (authenticated smart-mode load
> testing needs a seeded session — out of scope; see `locustfile.py`). This run
> **also samples the agent process's CPU% + RSS** (new — see the *CPU & Memory*
> subsections); earlier captures recorded latency/throughput only.
>
> **Why the latency here is higher than the earlier archived capture.** The chat
> and `rounds/start` paths fetch FHIR, and this harness has **no live FHIR
> backend**, so those fetches exhaust the client's retry budget (3 attempts,
> ~0.2–2 s jittered backoff — `copilot/resilience.py` `DEFAULT_RETRY`) before
> failing. Chat pulls **6** resource types per turn, so it pays that retry cost
> ~6× (~1.9 s) even though the LLM is stubbed. That is an **absent-dependency
> environment condition**, not a service regression, and it is **not**
> production-representative (live FHIR returns in ms). The archived 2026-07-10
> capture predates this retry wiring on the stub fetch path, which is why its
> chat p50 was ~56 ms. Data routes added since (`/v1/rounds/refresh`,
> `/v1/rounds/alerts`, `/v1/patients/{id}/observations`, and the flag-gated
> `/v1/writes*`) were **not** part of this captured mix; no figures for them are
> fabricated here.

## Run conditions (read this before the numbers)

This is a **service-layer / transport smoke test**, run offline and clearly
labeled as such — it measures how the real FastAPI stack (routing, Pydantic
validation, correlation-ID middleware, authorization boundary, DB reads/writes,
error handling) behaves under concurrency, and what it costs in CPU + memory. It
is **not** an end-to-end LLM+FHIR latency benchmark.

| Dimension | This run |
|---|---|
| Harness | `loadtest/smoke_load.py` (httpx async driver) — the Locust fallback: Locust's `gevent` dep would not build in this sandbox, so the canonical `locustfile.py` could not run here. Same endpoint mix + weights. |
| App | `copilot.api.app:app` under a **single** uvicorn worker, in-process poller disabled |
| Auth mode | **`disabled`** — identity from the request `clinician_id`; pre-SMART. A `smart`-mode instance would 401 the data routes without an `af_session` cookie |
| Database | seeded throwaway **SQLite file** (`/tmp/copilot_loadtest.db`) — **not** the production Postgres |
| LLM | **no** `ANTHROPIC_API_KEY` → chat uses the deterministic `StubAgent`; **no** real Claude call is made |
| FHIR | **no** live OpenEMR backend reachable — chat / `rounds/start` fetches fail *after exhausting retry backoff* (see provenance note) |
| Langfuse | not configured (no-op) |
| Duration | 20 s per stage; ramp = all users at once |
| Resource sampling | `psutil` process-tree sampling of the uvicorn process (+ children), every **0.5 s** for the whole run; peak + mean of CPU% and RSS. `cpu_percent` is psutil per-process utilization where **100 % = one fully-busy core** (can exceed 100 % on multiple cores). Driven from `loadtest/.venv` (`loadtest/requirements.txt`), which the agent venv need not contain — psutil samples any PID regardless of interpreter. |
| Host | 2026-07-19, macOS (arm64, Apple silicon); Python 3.12 |

**What this does and does not tell you:**
- ✅ It exercises `/v1/chat` and `/v1/rounds/*` at 10 and 50 concurrent users
  (the required surfaces) and reports real percentiles, throughput, **and the
  agent process's CPU%/RSS** for the service layer.
- ✅ `/v1/chat` returns a genuine **200 fail-closed** response through the full
  serve path (authorization → conversation open → agent → verification →
  persistence) — only the live model call is stubbed. With no reachable FHIR it
  grounds on zero resources and honestly withholds ("I can't confirm that…").
- ⚠️ `POST /v1/rounds/start` returns **500** here because it fetches FHIR before
  synthesizing and no OpenEMR backend is reachable in this harness. That is an
  environment condition, not a service defect. On the deployed stack (live
  OpenEMR FHIR) this path returns 200.
- ⚠️ Absolute chat/`rounds/start` latency here is **framework + SQLite + FHIR
  retry-backoff** (the backoff dominates — see provenance note), *not* the model.
  In production the chat path replaces the retry-backoff-then-fail with fast live
  FHIR reads **plus** the Claude tool-use loop (~seconds — see
  `../COST_ANALYSIS.md` §3 for the token/latency model).

## Endpoint mix (weights)

`GET /health` (1), `GET /ready` (1), `GET /v1/rounds/current` (4),
`POST /v1/rounds/start` (2), `POST /v1/chat` (3). `POST /v1/rounds/advance` is
exercised by the canonical `locustfile.py` but omitted from this concurrent
driver: all virtual users share one `clinician_id`, so concurrent `advance`
calls would monotonically exhaust the single shared rounding cursor (a
self-inflicted state mutation, not a service characteristic).

---

## 10 concurrent users

Overall: **317 requests**, 14.2 req/s, wall 22.3 s.
Overall latency: **p50 16.9 ms · p95 2,170.2 ms · p99 2,407.2 ms** (the p95/p99
tail is the chat + `rounds/start` FHIR-retry backoff, not the framework).
Overall error rate 15.1% — **entirely** the FHIR-dependent `rounds/start` 500s
(see note above); every other endpoint is 0% here.

| Endpoint | Reqs | Error % | p50 (ms) | p95 (ms) | p99 (ms) | max (ms) | Status |
|---|---:|---:|---:|---:|---:|---:|---|
| `GET /health` | 26 | 0.0 | 1.7 | 3.2 | 22.6 | 29.0 | 200 |
| `GET /ready` | 24 | 0.0 | 15.6 | 19.3 | 20.5 | 20.8 | 503 (deps absent — expected) |
| `GET /v1/rounds/current` | 123 | 0.0 | 5.4 | 17.7 | 69.3 | 100.2 | 200 (DB read) |
| `POST /v1/rounds/start` | 48 | 100.0 | 335.2 | 518.9 | 539.4 | 542.3 | 500 (no live FHIR; retry backoff) |
| `POST /v1/chat` | 96 | 0.0 | 1,905.6 | 2,388.4 | 2,545.7 | 2,957.2 | 200 (fail-closed withheld) |

At 10 users the pure-serve paths are comfortable — `/health` ~3 ms p95, the DB
read (`rounds/current`) ~18 ms p95. The chat/`rounds/start` latency is dominated
by FHIR retry-backoff against an absent backend (see provenance note).

### CPU & Memory — 10 users

Sampled every 0.5 s over the run (43 samples) via psutil, agent process tree:

| Metric | Peak | Mean |
|---|---:|---:|
| CPU % (100 % = 1 core) | **35.8%** | **17.5%** |
| RSS (MB) | **139.6** | **134.5** |

At 10 users the single worker is lightly loaded — well under one core (mean
~0.17 of a core; peak ~0.36). Note that much of each chat/`rounds/start` request
is spent **asleep in retry backoff**, so this CPU figure is *lower* than a
live-FHIR run would show for the same request count. Resident memory sits around
~135 MB (Python + FastAPI + SQLAlchemy + the loaded model-free agent graph).

---

## 50 concurrent users

Overall: **1,693 requests**, 76.5 req/s, wall 22.1 s.
Overall latency: **p50 21.9 ms · p95 2,174.2 ms · p99 2,483.6 ms**.
Overall error rate 17.3% — again **entirely** `rounds/start` 500s (FHIR absent);
every other endpoint is 0%.

| Endpoint | Reqs | Error % | p50 (ms) | p95 (ms) | p99 (ms) | max (ms) | Status |
|---|---:|---:|---:|---:|---:|---:|---|
| `GET /health` | 136 | 0.0 | 1.9 | 12.3 | 86.9 | 87.4 | 200 |
| `GET /ready` | 153 | 0.0 | 15.2 | 53.1 | 194.7 | 257.7 | 503 (deps absent — expected) |
| `GET /v1/rounds/current` | 649 | 0.0 | 6.4 | 59.0 | 220.4 | 797.6 | 200 (DB read) |
| `POST /v1/rounds/start` | 292 | 100.0 | 347.5 | 538.1 | 596.2 | 615.3 | 500 (no live FHIR; retry backoff) |
| `POST /v1/chat` | 463 | 0.0 | 1,883.9 | 2,420.5 | 2,661.9 | 2,857.5 | 200 (fail-closed withheld) |

At 50 users throughput climbs to ~77 req/s (~5× the 10-user rate — most user time
is spent in FHIR retry-backoff waits, which overlap freely across users, so
adding users adds concurrency the single worker can absorb). The read path
(`rounds/current`) tail widens (p99 ~220 ms) as the single worker + SQLite
serialize under burst, while `/health` stays flat (~12 ms p95).

### CPU & Memory — 50 users

Sampled every 0.5 s over the run (43 samples) via psutil, agent process tree:

| Metric | Peak | Mean |
|---|---:|---:|
| CPU % (100 % = 1 core) | **105.2%** | **61.0%** |
| RSS (MB) | **182.3** | **181.4** |

At 50 users the single worker saturates **a full core at peak** (105%) and runs
at ~0.6 core on average — the CPU headroom on one worker is the ceiling this
run demonstrates. Resident memory rises to ~181 MB (from ~135 MB at 10 users):
more concurrent requests → more in-flight coroutine/connection/ORM state, but
growth is modest and bounded (no leak signature across the run — peak ≈ mean).

---

## Reading of the results

1. **The serve layer is fast and correct.** `/health`, `/ready`,
   `rounds/current` (read), and `chat` (200 fail-closed) all behave as designed;
   the only non-2xx is `rounds/start`, and only because this harness has no live
   FHIR backend.
2. **CPU is the single-worker ceiling; memory is flat.** One uvicorn worker tops
   out at ~1 core (105% peak) at 50 users and holds a bounded ~181 MB RSS. Both
   scale with the cost tiers: `../COST_ANALYSIS.md` §7 calls for **multiple
   stateless agent replicas + managed Postgres + Redis** at the 1,000-user tier —
   more replicas add cores; the flat, bounded per-worker RSS makes replica
   sizing predictable.
3. **This harness's chat/`rounds` latency is FHIR-retry backoff, not the model
   and not the framework.** With live FHIR those fetches return in ms; real
   production chat latency is instead dominated by the Claude tool-use loop
   (~seconds). The SLO/alerts in `../OBSERVABILITY.md` (chat p95 > 8 s) are sized
   for that end-to-end reality.

## Reproduce

```bash
# One-time: create the driver venv (needs psutil for CPU/RSS sampling; the agent
# venv does not — psutil samples the uvicorn PID from outside).
python -m venv loadtest/.venv
loadtest/.venv/bin/pip install -r loadtest/requirements.txt

bash loadtest/run.sh          # seeds a DB, boots the agent, runs 10u then 50u
                              # (captures latency + throughput + CPU% + RSS)

# Locust (where gevent builds) — latency/throughput only, no CPU/RSS sampling:
locust -f loadtest/locustfile.py --headless -u 50 -r 10 -t 60s \
    --host http://localhost:8010 --csv loadtest/results_50u
```

The `resource` block in each `results_*.json` carries the CPU/RSS fields
(`cpu_percent_peak/mean`, `rss_mb_peak/mean`, `samples`, `interval_s`,
`target_pid`, `sampler`). If psutil is unavailable or no target PID is found,
that block records an honest "not sampled" note rather than a fabricated number.
</content>
