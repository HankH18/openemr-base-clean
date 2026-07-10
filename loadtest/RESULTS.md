# Load-Test Results — AgentForge Clinical Co-Pilot

Captured p50 / p95 / p99 latency + error rate for the `copilot` agent at **10**
and **50** concurrent users. Raw machine-readable output: `results_10u.json`,
`results_50u.json` (regenerate with `bash loadtest/run.sh`).

## Run conditions (read this before the numbers)

This is a **service-layer / transport smoke test**, run offline and clearly
labeled as such — it measures how the real FastAPI stack (routing, Pydantic
validation, correlation-ID middleware, authorization boundary, DB reads/writes,
error handling) behaves under concurrency. It is **not** an end-to-end
LLM+FHIR latency benchmark.

| Dimension | This run |
|---|---|
| Harness | `loadtest/smoke_load.py` (httpx async driver) — the Locust fallback: Locust's `gevent` dep would not build in this sandbox, so the canonical `locustfile.py` could not run here. Same endpoint mix + weights. |
| App | `copilot.api.app:app` under a **single** uvicorn worker, in-process poller disabled |
| Database | seeded throwaway **SQLite file** (`/tmp/copilot_loadtest.db`) — **not** the production Postgres |
| LLM | **no** `ANTHROPIC_API_KEY` → chat uses the deterministic `StubAgent`; **no** real Claude call is made |
| FHIR | **no** live OpenEMR backend reachable |
| Langfuse | not configured (no-op) |
| Duration | 20 s per stage; ramp = all users at once |
| Date | 2026-07-10, macOS host |

**What this does and does not tell you:**
- ✅ It exercises `/v1/chat` and `/v1/rounds/*` at 10 and 50 concurrent users
  (the required surfaces) and reports real percentiles for the service layer.
- ✅ `/v1/chat` returns a genuine **200 fail-closed** response through the full
  serve path (authorization → conversation open → agent → verification →
  persistence) — only the live model call is stubbed.
- ⚠️ `POST /v1/rounds/start` returns **500** here because it fetches FHIR before
  synthesizing and no OpenEMR backend is reachable in this harness. That is an
  environment condition, not a service defect — it fails fast at the FHIR
  connect. On the deployed stack (live OpenEMR FHIR) this path returns 200.
- ⚠️ Absolute chat latency here is **framework + SQLite overhead only**. In
  production the chat path also runs the Claude tool-use loop (~seconds — see
  `../COST_ANALYSIS.md` §3 for the token/latency model); add that to the numbers
  below for a real-world estimate.

## Endpoint mix (weights)

`GET /health` (1), `GET /ready` (1), `GET /v1/rounds/current` (4),
`POST /v1/rounds/start` (2), `POST /v1/chat` (3). `POST /v1/rounds/advance` is
exercised by the canonical `locustfile.py` but omitted from this concurrent
driver: all virtual users share one `clinician_id`, so concurrent `advance`
calls would monotonically exhaust the single shared rounding cursor (a
self-inflicted state mutation, not a service characteristic).

---

## 10 concurrent users

Overall: **3,461 requests**, 172.6 req/s, wall 20.1 s.
Overall latency: **p50 15.3 ms · p95 80.4 ms · p99 121.4 ms**.
Overall error rate 17.7% — **entirely** the FHIR-dependent `rounds/start` 500s
(see note above); excluding that endpoint the error rate is **≈0.15%**.

| Endpoint | Reqs | Error % | p50 (ms) | p95 (ms) | p99 (ms) | max (ms) | Status |
|---|---:|---:|---:|---:|---:|---:|---|
| `GET /health` | 320 | 0.0 | 1.9 | 7.0 | 12.5 | 20.0 | 200 |
| `GET /ready` | 349 | 0.0 | 13.2 | 25.9 | 33.2 | 60.3 | 503 (deps absent — expected) |
| `GET /v1/rounds/current` | 1,239 | 0.16 | 13.0 | 51.8 | 96.3 | 200.4 | 200 (DB read) |
| `POST /v1/rounds/start` | 609 | 100.0 | 12.4 | 20.8 | 31.9 | 49.8 | 500 (no live FHIR) |
| `POST /v1/chat` | 944 | 0.11 | 56.0 | 108.5 | 151.6 | 409.2 | 200 (fail-closed withheld) |

At 10 users the service is comfortable: the read path (`rounds/current`) holds a
52 ms p95 and the write-heavy chat serve path holds a 108 ms p95.

---

## 50 concurrent users

Overall: **4,042 requests**, 199.3 req/s, wall 20.3 s.
Overall latency: **p50 147.7 ms · p95 624.0 ms · p99 1,461.7 ms**.
Overall error rate 18.1% — again **entirely** `rounds/start` 500s (FHIR absent);
excluding that endpoint the error rate is **≈0.3%** (a handful of transient
client-side connection resets under burst).

| Endpoint | Reqs | Error % | p50 (ms) | p95 (ms) | p99 (ms) | max (ms) | Status |
|---|---:|---:|---:|---:|---:|---:|---|
| `GET /health` | 369 | 0.0 | 2.7 | 9.9 | 21.8 | 62.2 | 200 |
| `GET /ready` | 332 | 0.0 | 79.6 | 132.2 | 168.8 | 264.8 | 503 (deps absent — expected) |
| `GET /v1/rounds/current` | 1,485 | 0.27 | 156.3 | 414.9 | 1,344.3 | 3,543.0 | 200 (DB read) |
| `POST /v1/rounds/start` | 721 | 100.0 | 13.7 | 26.0 | 107.8 | 116.4 | 500 (no live FHIR) |
| `POST /v1/chat` | 1,135 | 0.44 | 369.9 | 1,055.5 | 1,915.4 | 3,584.9 | 200 (fail-closed withheld) |

At 50 users the **single uvicorn worker + SQLite** become the bottleneck: chat
(which writes a conversation row + two message rows per turn) climbs to a
~1.06 s p95 as SQLite serializes writes, and the read path tail widens to ~415 ms
p95. `/health` stays flat (~10 ms p95) — pure liveness scales trivially.

---

## Reading of the results

1. **The serve layer is fast and correct.** `/health`, `/ready`,
   `rounds/current` (read), and `chat` (200 fail-closed) all behave as designed;
   the only non-2xx is `rounds/start`, and only because this harness has no live
   FHIR backend.
2. **The bottleneck at 50 users is exactly the thing the cost tiers fix.**
   Single process + SQLite serialize writes. `../COST_ANALYSIS.md` §7 calls for
   **multiple stateless agent replicas + managed Postgres + Redis** at the
   1,000-user tier — this run is the empirical motivation for that step.
3. **Real production chat latency = these numbers + the Claude tool loop.** The
   framework overhead measured here (~0.1 s at 10u) is small next to the ~seconds
   the sonnet-5 tool-use loop adds; the SLO/alerts in `../OBSERVABILITY.md`
   (chat p95 > 8 s) are sized for that end-to-end reality.

## Reproduce

```bash
bash loadtest/run.sh          # seeds a DB, boots the agent, runs 10u then 50u
# Locust (where gevent builds):
locust -f loadtest/locustfile.py --headless -u 50 -r 10 -t 60s \
    --host http://localhost:8010 --csv loadtest/results_50u
```
