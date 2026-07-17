# AgentForge Clinical Co-Pilot — Observability & Alerting Spec

The observability surface for the `copilot` agent: Langfuse traces, correlation
IDs, and the append-only audit trail. This is grounded in what the code
**actually emits today**.

Three independent, keys-gated / flag-gated pieces make up the surface:

- **Langfuse tracing.** Fully wired (`copilot/observability/langfuse_backend.py`,
  chosen by `copilot/observability/factory.py`) and gated on three credentials
  (`LANGFUSE_HOST` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`). Absent any
  one, the factory returns `NoopObservability` — a zero-cost no-op with the same
  API, so nothing branches on "is Langfuse configured?". Now **self-hostable**:
  the deploy compose ships a Langfuse v2 stack behind an opt-in `observability`
  profile (see below and `agent/LANGFUSE_SETUP.md`).
- **Correlation IDs.** Threaded per request by `CorrelationIdMiddleware`
  (`copilot/api/middleware.py`) and used as the Langfuse **trace id**.
- **Append-only audit trail.** A Postgres `audit_log` table (`memory/models.py`),
  written on every PHI read and every write-back action — independent of whether
  Langfuse is on — with a floor-protected retention sweep (`memory/retention.py`).

**Design fact that shapes everything below:** the correlation ID is threaded as
the Langfuse **trace id** (via `CorrelationIdMiddleware` → `current_correlation_id()`
→ `client.trace(id=cid)`), echoed on the `X-Correlation-ID` response header, and
also stamped on every `audit_log` row. So one id stitches together the HTTP
request, the chat/rounds spans, the verification decision, the poller ticks it
triggered, **and** the immutable access-trail row — end to end, one click.

---

## 1. What the code emits today (the raw signal)

| Signal | Type | Emitted by | Carries |
|---|---|---|---|
| `chat` | span | `ChatService.chat` | `patient_id`, `clinician_id`; output: `action`, `passed`, `claims` count; plus `input_tokens`, `output_tokens`, `cost_usd`, `tool_calls` attributes (LLM path only) |
| `llm.usage` | event | `ChatService._record_token_usage` | `model`, `input_tokens`, `output_tokens`, `cost_usd`, `tool_calls`, `correlation_id` — the per-turn token/cost record |
| `rounds.start` / `rounds.current` / `rounds.advance` / `rounds.jump` | span | `api/routes/rounds.py` | `clinician_id` |
| `observations.series` | span | `api/routes/observations.py` | `clinician_id`, `patient_id` (grounded time-series read; no LLM) |
| `verification.result` | event | `record_verification` | `passed` (bool), `action` (`served`/`degraded`/`withheld`), `patient_id`, `correlation_id` |
| `poller.tick` | span | `Poller.tick` | `patient_id` |
| `poller.result` | event | `Poller.tick` | `outcome` (`no_change`/`hash_unchanged`/`synthesized`/`error`), `error` |
| `poller.staleness` | event | `record_poller_staleness` | `patient_id`, `age_seconds`, `correlation_id` |

Every span/event inherits the request's correlation id as its trace id, so all
of the above line up on one timeline per request.

**Token/cost is measured, not estimated.** `ClaudeAgent` accumulates
input/output tokens across the whole tool-use loop and counts tool calls;
`ChatService._record_token_usage` stamps them on the `chat` span and emits
`llm.usage`, with `cost_usd` computed deterministically by
`observability/pricing.py::cost_usd(model, in, out)`. This is the raw feed for
`COST_ANALYSIS.md`.

**What is and isn't an LLM call.** Chat (`ClaudeAgent`, `claude-sonnet-5`) is the
only wired LLM path. Rounds cards, the poller (deterministic `StubSynthesizer`),
the observations series, and the verification gate are all deterministic — they
emit spans/events for latency and outcome, but no token or cost. The LLM
synthesizer (`ClaudeSynthesizer`, `claude-sonnet-5`) and the entailment tier
(`LlmEntailment`, `claude-haiku-4-5-20251001`) are configured but wired into no
live path today (see `COST_ANALYSIS.md` §1), so no `synthesis`/`entailment`
token signal is produced in the current build.

---

## 2. Real-time metrics to watch (dashboard tiles)

Organize the Langfuse dashboard into four rows. Metric names map 1:1 to the
signals in §1.

### Row A — Traffic & latency (the golden signals)
- **Request rate** — traces/min, split by route (`chat`, `rounds.*`,
  `observations.series`).
- **p50 / p95 / p99 latency** — per route. Chat is the tail that matters (it
  runs a multi-call tool loop against Anthropic + a live FHIR re-fetch); rounds,
  the observations series, and `/health` are the fast, deterministic paths. Watch
  **p95** and **p99 latency** as the primary SLO signals.
- **Error rate** — 5xx / total, per route. Chat and rounds should be ≈0%;
  `/ready` 503s are expected when a dependency is down and are tracked separately
  (a readiness gauge, not an error).

### Row B — Model economics
- **Tokens** — input + output **tokens** per trace, summed per minute and per
  model. Today all LLM tokens are `claude-sonnet-5` on the `chat` path; the
  `claude-haiku-4-5-20251001` gating tier and LLM synthesis are configured but
  not wired, so they contribute nothing until switched on. Split cache-read vs.
  uncached input tokens.
- **Cost** — from the `cost_usd` already on the `chat` span / `llm.usage` event
  (see `COST_ANALYSIS.md`), tiled as $/hour and $/chat-turn.
- **Tool-call count** — `tool_calls` per chat turn (the `get_labs` /
  `get_medications` loop). A creeping average signals prompt drift or a record
  shape that forces extra round-trips — both raise tokens and latency.
- **Retry count** — Anthropic 429/5xx retries and OAuth `force`-refresh retries
  (401 → re-mint token). A spike means rate-limit pressure or token-endpoint
  trouble before it becomes user-visible latency.

### Row C — Trust / correctness (the domain-specific signals)
The product's promise is grounded, fail-closed answers, enforced by a
**deterministic** verification gate (`verification/core.py` +
`verification/serve.py`): attribution + numeric-value match + temporal-drift
re-derivation over a live FHIR re-fetch — no LLM in the gate, and the optional
`LlmEntailment` narrative-drift pass is not wired in (`entailment=None`
everywhere). The observability that matters:
- **Verification pass/fail** — `verification.result` split by `action`:
  **served** vs. **degraded** vs. **withheld**, and `passed` true/false. A rising
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
  failure mode. (The poller is off by default — `poller_enabled=false`; this row
  is live once it is enabled.)
- **Poller staleness** — `poller.staleness.age_seconds`, the age of the oldest
  patient's memory file. The "are the rounding cards actually fresh?" gauge — a
  rising p95 staleness means the poll loop is falling behind or wedged.
- **Synthesis rate** — syntheses/min. Drives background LLM cost *once the LLM
  synthesizer is enabled*; today synthesis is deterministic, so this is a
  freshness signal, not a cost one.

---

## 3. The append-only audit trail (HIPAA §164.312(b))

Independent of Langfuse, every PHI read and every write-back action appends one
immutable row to `audit_log` (`memory/models.py::AuditLogRow`), written through
`MemoryRepository.record_audit` (insert-only). Each row carries `correlation_id`
(the same id the clinician sees on `X-Correlation-ID`), `clinician_id`,
`patient_id`, `action`, `resources_returned` (the FHIR ids the request actually
returned), `entry_mode`, and `at`.

**Read / access actions** (`entry_mode` NULL):

| `action` | Emitted by | Notes |
|---|---|---|
| `chat` | `ChatService._record_read_audit` | `resources_returned` = the resource ids the answer cited (empty when withheld) |
| `observations.series` | `api/routes/observations.py` | recorded after the series is built; never on the 403 path |
| `rounds.start` / `rounds.current` / `rounds.advance` / `rounds.jump` | `rounds/service.py` | rounding-navigation access, keyed by clinician |
| `poller.read` | `worker/runtime.py` | background tick read; mints a correlation id since there is no request one |

**Write-back actions** (`entry_mode` populated):

| `action` | Emitted by | `entry_mode` |
|---|---|---|
| `write_proposed` | `WriteService.propose` | `human_direct` (Phase 1) |
| `write_committed` | `WriteService.commit` | `human_direct`; `resources_returned` names the created resource |
| `write_failed` | `WriteService.commit` (on `OpenEmrWriteError`) | `human_direct`; recorded before the error re-raises |

`entry_mode` is the physician-attribution surface (`human_direct` today; the
schema reserves `agent_proposed_physician_confirmed` for Phase 2). Reads leave it
NULL, so the column is backward-compatible. Audit writes are **fail-open**: the
answer/write is already produced, so a failed audit write is logged and swallowed
— it never turns a served read into a 500. Write-back itself is **OFF by default**
(`writeback_enabled=false`), so the write actions only appear once an operator
enables it.

**Per-clinician attribution under SMART.** With `auth_mode="smart"` (live on the
droplet), `resolve_acting_context` (`api/deps.py`) resolves the acting
`ClinicianId` from the opaque server-side session cookie — 401 with no session,
403 if a request-supplied `clinician_id` disagrees with the session. The chat and
observations audit rows record that session-resolved id, so the trail attributes
every read/write to the **logged-in physician**, not a request-asserted value.
(In `auth_mode="disabled"`, the demo default, the id comes from the request as
before.)

**Retention sweep — floor-protected, `audit_retention_years` = 6 default**
(`memory/retention.py`). Operator-invoked (never in a request handler):

- `sweep_audit_log` — cutoff is the *earlier* of `now − audit_retention_years`
  and `now − HIPAA_AUDIT_FLOOR_YEARS` (a hard `6`-year constant, not a config
  value), so a row younger than six years is never even reported as eligible,
  regardless of how `audit_retention_years` is set. And because no cold-storage
  archive target is wired in, the sweep **deletes nothing** (`deleted` is always
  0 — there is no DELETE against `audit_log` anywhere in the codebase). It reports
  scanned/eligible counts only. That is the fail-safe: enabling the sweep can
  never destroy the trail.
- `sweep_chat` — a separate, shorter retention for conversation/message PHI,
  gated on `chat_retention_days` (default `0` ⇒ never purge). Only a positive day
  count deletes, and only conversations strictly older than the cutoff.

---

## 4. The four alerts (with thresholds)

Each alert names the signal, the threshold, why it matters, and the first
response. Thresholds are starting points to tune against the first week of real
traffic (Langfuse retains the history to calibrate them).

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
  correctness regression (model drift, a changed FHIR schema, or an OpenEMR read
  returning stale/empty bundles). Safe, but it degrades the product to "I can't
  confirm that" and must be investigated.
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

## 5. Alert routing & hygiene

- **Page** (PagerDuty/Opsgenie): Alert 1 page-tier, Alert 2 page-tier, Alert 4
  when staleness > 30 min (patient-safety-adjacent — clinicians acting on stale
  data).
- **Warn** (Slack): everything else, so the tail doesn't page at 3 a.m. for a
  transient blip.
- **Correlation-first triage:** every alert links to a Langfuse trace filter by
  correlation id — the same id the clinician sees in `X-Correlation-ID` and the
  same id on the `audit_log` row, so a user-reported issue, the alert, and the
  access record all converge on one id.
- **Langfuse is advisory for `/ready`:** a Langfuse outage never takes the
  service out of rotation (probe is flagged `advisory`), so "observability is
  down" is itself a warn-tier alert, never a page that implies user impact.

---

## 6. How to view traces

Traces render in Langfuse → **Tracing → Traces**, keyed by correlation id. Issue
a chat (via the UI or `POST /v1/chat`), note the `X-Correlation-ID` response
header, and a `chat` trace appears under that id carrying its spans and a
`verification.result` event; rounding calls appear as `rounds.*` and (once
enabled) the poller adds `poller.tick` / `poller.result`.

Turning it on is three env vars — no code change:

- **Cloud:** set `LANGFUSE_HOST` (e.g. `https://us.cloud.langfuse.com`) +
  `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY`.
- **Self-hosted (keeps PHI-adjacent trace metadata on your own infra):** the
  deploy compose ships a Langfuse **v2** stack (`langfuse/langfuse:2`, 2.95.11) +
  its own `langfuse-postgres`, on a private `observability` network, **off by
  default** behind the `observability` compose profile — a plain
  `docker compose up` never starts it. Bring it up with
  `docker compose -f docker-compose.deploy.yml --profile observability up -d`,
  mint keys via an SSH tunnel to the loopback-only UI, and point
  `LANGFUSE_HOST=http://langfuse:3000` at the in-network service. The SDK is
  pinned `langfuse>=2.55,<3`, so the image stays on v2 (v2 needs only Postgres;
  v3 would additionally require ClickHouse + Redis + object storage).

Full walkthrough (both options, verification steps, gotchas):
`agent/LANGFUSE_SETUP.md`. The gate is **all-or-nothing** — any one of the three
keys blank ⇒ silent no-op, and `/ready` reports Langfuse as advisory rather than
returning 503.


## 7. Week-2 SLOs & alerts (document ingestion + guideline retrieval)

Week 2 adds two latency-critical hot paths beyond the Week-1 chat/rounds
surface: multimodal **document ingestion** (upload → rasterize → OCR → vision
extraction → OCR-reconcile → append-only persist) and hybrid **guideline
retrieval** (sparse + dense fusion + rerank). Both get first-class SLOs, a
latency artifact, and alerts with response actions.

### 7.1 SLO definitions (targets)

Latency SLOs are **p95** (the tail a clinician actually feels), measured over a
rolling window. The stubbed, LLM-free baseline is captured by
`agent/scripts/latency_report.py --out artifacts/latency_report.json` (numeric
`p50`/`p95` per pipeline); production p95 is read from the same spans in
Langfuse. The agent status page (`GET /status`) surfaces the current numbers.

| SLO | Signal | p95 target (warn / page) | Notes |
|-----|--------|--------------------------|-------|
| **Document ingestion latency** | `doc.ingest` + `extraction.run` span duration | **< 12 s** warn / **< 30 s** page | Real vision extraction dominates; the stub baseline is sub-second. Per-document, end to end. |
| **Document ingestion success rate** | `source_document.status` terminal split | **≥ 98%** `extracted` (non-`failed`) | A `failed` ingestion is fail-closed (zero orphan facts); a rising failed fraction is the alert below. |
| **Evidence retrieval latency** | `guideline.retrieve` span duration | **< 800 ms** warn / **< 2 s** page | Includes de-identify → embed → sparse+dense fuse → rerank. Rerank is best-effort (falls back to fused order). |
| **Extraction field pass rate** | supported / total `extracted_fact` | **≥ 90%** field-level | The "no-invention" gate — a value not found in the page OCR is `supported=false`. Surfaced on `/status` as `extraction_field_pass_rate`. |

The p50 numbers are reported alongside p95 for context but are not the gate; the
tail is what breaches an SLO.

> **Where these thresholds come from.** `W2_ARCHITECTURE.md` §SLOs & alerting carries
> the **defence** of the ingestion number (why **12 s** warn / **30 s** page and not some
> other value) — grounded in the committed LLM-free floor
> (`agent/artifacts/latency_report.json`: `doc_ingestion` **p50 36.0 ms / p95 151.5 ms**,
> n=5) plus the vision-call-dominated real-path estimate in `COST_ANALYSIS.md` §9c
> (≈ 5 s p50 / ≈ 10 s p95, so 12 s ≈ estimate + ~20% headroom). **The two documents carry
> the same numbers; this table is the operational source of truth** — change it here first,
> then reconcile the defence.
>
> **Honest labelling:** the floor is **measured** (stub path, n=5); the warn/page targets
> are **SLO-anchored estimates** for the real path, because no production traces are
> retained yet. They are starting points to tune against the first week of real document +
> retrieval traffic — not validated production percentiles.

### 7.2 Week-2 alert definitions (with response actions)

Each alert names the signal, threshold, why it matters, and the first on-call
response. Thresholds are starting points to tune against the first week of real
document + retrieval traffic.

### Alert 5 — Document-ingestion failure surge
- **Condition:** `source_document` **`failed` fraction > 5%** over **30 minutes**
  (warn), **> 15%** (page), OR any single-document ingestion **p95 latency
  breaches the 30 s page target**.
- **Why it matters:** ingestion fails closed — a failed run persists zero facts,
  so a surge silently starves the chart of freshly uploaded labs/intake data.
- **First response (runbook):** filter the corpus/logs by the failing
  `correlation_id`; check which stage raised (OpenEMR upload vs. rasterize/OCR
  vs. vision extraction vs. persist). A spike concentrated at the upload stage
  points at the OpenEMR write surface (token/credentials); at rasterize/OCR, at
  a malformed PDF or a missing tesseract binary; at extraction, at the vision
  model/key. Re-drive one document by hand via `POST /v1/documents`.

### Alert 6 — Evidence-retrieval latency / degradation
- **Condition:** `guideline.retrieve` **p95 latency > 2 s** over **10 minutes**
  (page), OR a sustained **rerank fallback rate > 25%** (warn — the Cohere
  reranker is erroring and retrieval is serving the fused sparse+dense order).
- **Why it matters:** slow or de-ranked retrieval degrades the *separate*
  guideline-evidence block in chat; the grounded patient answer still serves
  (evidence retrieval is fail-open and never withholds the answer), so this is a
  quality/latency alert, not an availability page for the answer itself.
- **First response (runbook):** check the `embedder`/`reranker` entries on
  `/ready` (graded — `ok` / `degraded` / `down`) and the Cohere/Voyage key
  configuration; confirm the `pgvector` dependency is `ok` (`readiness.py:54-81`
  probes only that the `vector` **extension** is installed — without it the
  `embedding` column cannot be stored, so no chunk carries a vector and dense
  ranking silently contributes nothing: `retriever.py:244-248` skips rows whose
  `embedding is None`, leaving sparse-only retrieval). Inspect a slow
  `guideline.retrieve` trace for which sub-step (embed vs. **in-Python** cosine
  scan vs. rerank) dominates — note the scan is O(corpus) in the app process,
  **not** an indexed DB search (see `W2_ARCHITECTURE.md` §RAG index), so a
  retrieval slowdown that tracks corpus growth points there rather than at the
  network hops.

Routing follows §5: Alert 5 page-tier on the page condition (warn-tier
otherwise), Alert 6 warn-tier for the fallback signal and page-tier for the
latency breach. Every alert links to a Langfuse trace filtered by
`correlation_id`, the same id stamped on the JSON access logs and the
`audit_log` row.
