# AgentForge Clinical Co-Pilot — Observability & Alerting Spec

The Langfuse dashboard and alert rules for the `copilot` agent. This is
grounded in what the code **actually emits today** — the observability backend
is fully wired (`copilot/observability/langfuse_backend.py`), gated on three
credentials (`LANGFUSE_HOST` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`);
absent them it is a zero-cost no-op. See `agent/LANGFUSE_SETUP.md` to turn it on.

**Design fact that shapes everything below:** the correlation ID is threaded as
the Langfuse **trace id** (via `CorrelationIdMiddleware` → `current_correlation_id()`
→ `client.trace(id=cid)`), and echoed on the `X-Correlation-ID` response header.
So a single trace id stitches together the HTTP request, the chat/rounds spans,
the poller ticks it triggered, and the verification decision — end to end, one
click.

---

## 1. What the code emits today (the raw signal)

| Signal | Type | Emitted by | Carries |
|---|---|---|---|
| `chat` | span | `ChatService.chat` | `patient_id`, `clinician_id`; output: `action`, `passed`, `claims` count |
| `rounds.start` / `rounds.current` / `rounds.advance` / `rounds.jump` | span | `api/routes/rounds.py` | `clinician_id` |
| `verification.result` | event | `record_verification` | `passed` (bool), `action` (`served`/`degraded`/`withheld`), `patient_id`, `correlation_id` |
| `poller.tick` | span | `Poller.tick` | `patient_id` |
| `poller.result` | event | `Poller.tick` | `outcome` (`no_change`/`hash_unchanged`/`synthesized`/`error`), `error` |
| `poller.staleness` | event | `record_poller_staleness` | `patient_id`, `age_seconds` |
| token usage + model | span metadata | Anthropic SDK response `usage` (input/output/cache tokens) surfaced onto the enclosing span | tokens ⇒ cost |

Every span/event inherits the request's correlation id as its trace id, so all
of the above line up on one timeline per request.

---

## 2. Real-time metrics to watch (dashboard tiles)

Organize the Langfuse dashboard into four rows. Metric names map 1:1 to the
signals in §1.

### Row A — Traffic & latency (the golden signals)
- **Request rate** — traces/min, split by route (`chat`, `rounds.*`).
- **p50 / p95 / p99 latency** — per route. Chat is the tail that matters (it
  runs a multi-call tool loop against Anthropic + a live FHIR re-fetch); rounds
  and `/health` are the fast, deterministic paths. Watch **p95 latency** and
  **p99 latency** as the primary SLO signals.
- **Error rate** — 5xx / total, per route. Chat and rounds should be ≈0%;
  `/ready` 503s are expected when a dependency is down and are tracked
  separately (a readiness gauge, not an error).

### Row B — Model economics
- **Tokens** — input + output **tokens** per trace, summed per minute and per
  model (`claude-sonnet-5` for chat + synthesis, `claude-haiku-4-5` for the
  optional entailment tier). Split cache-read vs. uncached input tokens.
- **Cost** — derived from tokens × model price (see `COST_ANALYSIS.md`), tiled
  as $/hour and $/chat-turn.
- **Tool-call count** — tool-use iterations per chat turn (the `get_labs` /
  `get_medications` loop). A creeping average signals prompt drift or a record
  shape that forces extra round-trips — both raise tokens and latency.
- **Retry count** — Anthropic 429/5xx retries and OAuth `force`-refresh retries
  (401 → re-mint token). A spike means rate-limit pressure or token-endpoint
  trouble before it becomes user-visible latency.

### Row C — Trust / correctness (the domain-specific signals)
- **Verification pass/fail** — `verification.result` split by `action`:
  **served** vs. **degraded** vs. **withheld**, and `passed` true/false. This is
  the heart of the product's promise (grounded, fail-closed answers). A rising
  **withheld** rate means the agent increasingly can't ground its claims against
  a live FHIR re-fetch — either the record drifted or synthesis is degrading.
- **Withheld rate** — `withheld / total chat turns`. The single most important
  trust metric: a fail-closed withhold is safe but a *spike* is a signal.
- **Claims per served answer** — dropping toward zero means answers are getting
  thinner even when "served."

### Row D — Background freshness (the poller)
- **Poller outcome mix** — `poller.result` by `outcome`. Healthy steady state is
  mostly `no_change` / `hash_unchanged` (the change-gating working, spend near
  zero) with a modest `synthesized` fraction. A jump in `error` is the top
  failure mode.
- **Poller staleness** — `poller.staleness.age_seconds`, the age of the oldest
  patient's memory file. This is the "are the rounding cards actually fresh?"
  gauge — a rising p95 staleness means the poll loop is falling behind or
  wedged.
- **Synthesis rate** — syntheses/min ⇒ directly drives background LLM cost.

---

## 3. The four alerts (with thresholds)

Each alert names the signal, the threshold, why it matters, and the first
response. Thresholds are starting points to tune against the first week of
real traffic (Langfuse retains the history to calibrate them).

### Alert 1 — Chat error rate / availability
- **Condition:** `chat` 5xx error rate **> 2%** over a rolling **5-minute**
  window (warn), **> 5%** (page).
- **Why:** chat is the interactive surface; 5xx here is a broken clinician
  experience. Distinct from an intentional `withheld` (which is a *successful*
  200 fail-closed response, Alert 3).
- **First response:** check the enclosing trace for the failing span —
  Anthropic 5xx/429 (see retry count), FHIR re-fetch failure, or DB error — via
  the correlation id on the user's `X-Correlation-ID`.

### Alert 2 — Chat p95 latency (SLO breach)
- **Condition:** `chat` **p95 latency > 8 s** over **10 minutes** (warn),
  **p99 latency > 15 s** (page).
- **Why:** the chat tool loop calls Anthropic 2–3× and re-fetches FHIR live; a
  latency blowout usually means Anthropic slowness/rate-limiting or a slow
  OpenEMR FHIR endpoint. Correlate with **tool-call count**, **retry count**,
  and **tokens** on the same dashboard.
- **First response:** inspect whether tool-call count or tokens rose (prompt/
  record issue) vs. flat tokens but high latency (upstream Anthropic/FHIR).

### Alert 3 — Verification withheld-rate spike (trust regression)
- **Condition:** rolling **withheld rate > 20%** of chat turns over **15
  minutes**, or a **2× increase** vs. the trailing 24-hour baseline.
- **Why:** the fail-closed gate withholding *more often* means the agent
  increasingly cannot ground its answers against a live FHIR re-fetch — a silent
  correctness regression (model drift, a changed FHIR schema, or an OpenEMR
  read returning stale/empty bundles). Safe, but it degrades the product to
  "I can't confirm that" and must be investigated.
- **First response:** pull recent `verification.result` events with
  `action=withheld`, open their traces, and check whether the cited resources
  are being re-fetched successfully.

### Alert 4 — Poller stalled / error surge (stale rounding cards)
- **Condition:** `poller.result` **error fraction > 10%** over **15 minutes**,
  **OR** `poller.staleness` p95 `age_seconds` **> 1800** (30 min — the same
  window past which the UI itself flags a card "stale").
- **Why:** the rounding cards are a cache over OpenEMR; if the change-gated poll
  loop wedges (FHIR auth failure via SMART Backend Services, count-query errors,
  synthesis failures), clinicians silently round on stale summaries. This is the
  documented top failure mode — `record_poller_staleness` exists specifically to
  feed this alert.
- **First response:** check `poller.result` `error` strings (count-query vs.
  resource-pull vs. synthesis) and the Backend Services token provider; a rising
  `consecutive_failures` in `sync_state` corroborates.

---

## 4. Alert routing & hygiene

- **Page** (PagerDuty/Opsgenie): Alert 1 page-tier, Alert 2 page-tier, Alert 4
  when staleness > 30 min (patient-safety-adjacent — clinicians acting on stale
  data).
- **Warn** (Slack): everything else, so the tail doesn't page at 3 a.m. for a
  transient blip.
- **Correlation-first triage:** every alert links to a Langfuse trace filter by
  correlation id — the same id the clinician sees in `X-Correlation-ID`, so a
  user-reported issue and the alert converge on one trace.
- **Langfuse is advisory for `/ready`:** a Langfuse outage never takes the
  service out of rotation (probe is flagged `advisory`), so "observability is
  down" is itself a warn-tier alert, never a page that implies user impact.
