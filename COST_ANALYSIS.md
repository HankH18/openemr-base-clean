# AgentForge Clinical Co-Pilot — Cost Analysis

Development spend to date, unit LLM economics, and projected monthly cost at
**100 / 1,000 / 10,000 / 100,000 users** (a "user" is one active clinician /
hospitalist). Every number is an estimate with its assumptions stated; the
model is built to be recomputed as real Langfuse token/latency data lands (see
`OBSERVABILITY.md`).

The point of this document is not a single dollar figure — it is the
**cost-architecture story**: which components cost money, which are engineered
to cost *nothing* until work actually changes, and what has to change at each
order of magnitude.

Two model IDs and their prices are the source of truth for every LLM figure
below, and both are read straight from the code:

- Models are configured in `agent/copilot/config.py` — `anthropic_model_synthesis`
  defaults to **`claude-sonnet-5`** ("Model for synthesis and chat") and
  `anthropic_model_gating` defaults to **`claude-haiku-4-5-20251001`**
  ("Cheaper model for classification / entailment").
- Per-token rates live in `agent/copilot/observability/pricing.py`, and USD cost
  per request is computed deterministically from the reported token counts by
  `cost_usd(model, input_tokens, output_tokens)`.

---

## 1. What actually costs money in this system

The co-pilot is deliberately built so cost tracks **clinical change**, not
patient count or clock ticks. The single most important thing to get right in a
cost model is **which request paths actually call an LLM** versus which are
deterministic grounding — they look similar from the outside and cost wildly
differently.

### 1a. The one live LLM path in the current build

| Source | Model | When it fires | Cost lever |
|---|---|---|---|
| **Grounded chat** (`POST /v1/chat`) | `claude-sonnet-5` (`ClaudeAgent`, tool-use loop over FHIR) | Per interactive clinician question — including **chat drill-down** and **temporal ("what changed since…") Q&A**, which are answered by the *same* chat call | Tokens per turn; interactive, not batchable |

Chat is wired live: `ChatService.chat` → `build_agent` returns the real
`ClaudeAgent` whenever `ANTHROPIC_API_KEY` is set (otherwise the deterministic
`StubAgent`). It runs an Anthropic tool-use loop (`get_labs` / `get_medications`)
at `claude-sonnet-5`, `max_tokens=2048`, up to 6 tool iterations. Temporal
questions ride this same call — the system prompt lets the model use each
resource's clinical time, and the code (not the model) fills each claim's
`source_ref` timestamp from the cited resource. **There is no separate LLM call
for drill-down or temporal Q&A.**

### 1b. LLM-capable paths that are built and configured but NOT wired into a live path today

These exist in the codebase (with token/cost telemetry) but are on **no**
request path in the current build, so their LLM cost is **$0 today**:

| Capability | Model | Status in current build |
|---|---|---|
| **Background synthesis** (`ClaudeSynthesizer`) | `claude-sonnet-5` | Defined + configured, but the wired poller, the serve-time refresh, and rounds-start all instantiate the deterministic **`StubSynthesizer`** (`worker/runtime.py`, `worker/pipeline.py`, `rounds/service.py`). The poller itself is also **OFF by default** (`poller_enabled=False`). So background synthesis calls no LLM today. |
| **Verification entailment** (`LlmEntailment`, optional narrative-drift check) | `claude-haiku-4-5-20251001` | Defined + configured, but constructed nowhere; every call to the verifier passes `entailment=None`. `anthropic_model_gating` is referenced only in `config.py`. So the gating tier is dormant — **$0**. |

The synthesis unit economics in §3b are therefore a **projection of what
background synthesis costs once the LLM synthesizer is switched in** — not a
cost the current build incurs.

### 1c. Deterministic paths — $0 LLM cost, by design

- **`POST /v1/rounds/start` / `/advance` / `/current` / `/jump`.** Rounding
  cards are assembled by the **deterministic** `StubSynthesizer` and the
  deterministic acuity ranker; the card's per-metric chart rows come from
  `rounds/summary.py::build_summary_claims`, which is **pure grouping/collapsing
  with no model call**. The physician-facing round loop is Postgres + FHIR reads.
- **The fail-closed verification gate** (`verification/core.py`, `verification/serve.py`).
  Attribution + numeric-value match + temporal-drift re-derivation, run over a
  live FHIR re-fetch. **Entirely deterministic** — it is deliberately *not*
  promptable, and the optional LLM entailment pass is not wired in (§1b). The
  gate that protects every served answer costs **$0 in LLM**.
- **Observations time-series** (`GET /v1/patients/{id}/observations`). A grounded
  FHIR search collapsed to per-metric points; no model call.
- **Poller ticks with no change.** Each tick first issues cheap FHIR
  `_summary=count` queries per resource type since the stored watermark. If every
  count is zero, the patient is skipped — **no resource pull, no synthesis**. A
  content-hash then catches cosmetic-only updates (a moved `lastUpdated` with
  identical payload) and still skips. Synthesis happens *only* on a real,
  substantive change — and even then, in the current build, via the deterministic
  stub.

This "cost-scales-with-change" design, plus the deterministic-by-default posture,
is the single most important lever in every tier below.

---

## 2. Real Anthropic token pricing (source of truth)

Per-million-token pricing for the two models this system is configured to call,
copied verbatim from `agent/copilot/observability/pricing.py` (rate card "as of
2026-07"; an unrecognised model falls back to the sonnet-tier rate so an unknown
model is never costed as free):

| Model | Role in system | Input $/1M | Output $/1M |
|---|---|---|---|
| `claude-sonnet-5` | chat (live) + synthesis (projected) | **$3.00** (intro **$2.00** through 2026‑08‑31) | **$15.00** (intro **$10.00**) |
| `claude-haiku-4-5-20251001` | optional entailment / gating tier (dormant today) | **$1.00** | **$5.00** |

The intro sonnet-5 rate is a real per-token discount noted in the pricing
docstring; the `cost_usd` table itself carries the **standard** $3/$15, so every
figure below uses standard pricing and is therefore conservative — 2026 intro
pricing would cut the LLM line ~33%.

Prompt caching (used from the 1,000-user tier up): cache **reads** ≈ 0.1× input
price; cache **writes** ≈ 1.25× (5-minute TTL). Batch API (used at 10k+ for the
non-interactive synthesis path, once the LLM synthesizer is enabled): **50%** off.

---

## 3. Unit economics (per request, grounded in the code)

Every LLM turn is measured: `ClaudeAgent` accumulates input/output tokens across
the whole tool-use loop and counts tool calls; `ChatService._record_token_usage`
stamps `input_tokens` / `output_tokens` / `cost_usd` / `tool_calls` onto the
`chat` span and emits an `llm.usage` event, with `cost_usd` computed by
`observability/pricing.py`. So the figures below are the *shape* of a turn; the
per-request truth lands on the trace.

### 3a. One grounded chat turn — `claude-sonnet-5`, `max_tokens=2048` (LIVE)

`ClaudeAgent` runs the Anthropic tool-use loop: Claude calls `get_labs` /
`get_medications`, each returning a FHIR bundle re-sent as context on the next
call, so input tokens compound across the loop. Modeling a typical two-tool turn
over a moderate record (token counts are illustrative assumptions):

| API call | Input tokens | Output tokens |
|---|---:|---:|
| 1 — system (≈350) + 2 tool defs (≈150) + question (≈30) | 530 | 120 (tool_use) |
| 2 — prior + labs bundle (≈4,000) | 4,650 | 80 (tool_use) |
| 3 — prior + meds bundle (≈2,500) → final grounded JSON | 7,230 | 350 |
| **Billed total** | **≈12,400** | **≈550** |

Cost = 12,400/1e6 × $3 + 550/1e6 × $15 = **$0.0372 + $0.0083 ≈ $0.046 per chat turn**.
Round to **$0.05/turn** (standard) — **~$0.03/turn** at intro pricing.

### 3b. One synthesis — `claude-sonnet-5`, `max_tokens=2048` (PROJECTED — LLM synthesizer not wired today)

`ClaudeSynthesizer` (the LLM synthesizer that the current build does *not* wire
in — see §1b) would send a strict-JSON system prompt (≈250 tokens) plus the
patient's *changed* FHIR resources (≈4,000 tokens for a fresh lab panel + a new
order) and get back a claims list (≈600 tokens). It logs its own tokens + a
`cost_usd` line via `pricing.cost_usd`:

Cost = 4,250/1e6 × $3 + 600/1e6 × $15 = **$0.0128 + $0.0090 ≈ $0.022 per synthesis**.

Because synthesis is change-gated, this cost would be paid **only** on ticks that
detect real change — not on every poll. **In the current build this line is $0**
(the poller uses the deterministic stub and is off by default); the number is
what a synthesized change *would* cost once the LLM synthesizer is enabled.

---

## 4. Per-user monthly usage model (stated assumptions)

| Assumption | Value |
|---|---|
| Working days / month | 22 |
| Chat turns / clinician / day | 25 |
| Patients on a clinician's panel | 15 |
| Substantive syntheses / patient / day (after change-gating; ~144 raw ticks/day at the 300s default interval, but only real changes cost) | 6 |
| Patient-panel sharing (a patient synthesized once serves every clinician viewing them) | none at 100 users; grows with scale |

**Current build (chat is the only live LLM cost):**

- Chat: 25 × $0.05 = **$1.25 / clinician / day → ≈ $28 / clinician / month**
  (standard; ≈ $17 at intro pricing).
- Background synthesis and the entailment tier are **deterministic / dormant**
  today, so they add **$0**.

**Full system, once the LLM synthesizer is enabled (adds the §3b line):**

- Chat: 25 × $0.05 = **$1.25**
- Synthesis: 15 patients × 6 × $0.022 = **$1.98**
- **≈ $3.23 / clinician / day → ≈ $71 / clinician / month** (standard; ≈ $45 at intro).

The projected tier table in §6 costs the **full system** (chat + LLM synthesis),
since that is the architecture being scaled; the current build sits flat at the
chat-only ≈ $28/user/mo until the synthesizer is switched on.

---

## 5. Development spend to date

The co-pilot was built agent-assisted across six committed units (FHIR client +
SMART App Launch / Backend Services OAuth; change-gated poller + synthesizer;
grounded verification layer; eval suite + CI; Langfuse observability +
correlation IDs). The eval suite (`agent/evals`) exercises the **real**
synthesis + verification path against the LLM (so `ClaudeSynthesizer` /
`LlmEntailment` *do* run during evals, even though the deployed poller uses the
deterministic stub). **Development spend to date** breaks down as:

| Item | Estimate | Notes |
|---|---|---|
| Claude API — agent-assisted code generation | ~$150 | interactive build sessions |
| Claude API — eval-suite + acceptance runs (unit 5) | ~$80 | `agent/evals` exercises the real synthesis/verification path against the LLM |
| Claude API — manual live verification of chat/rounds against the droplet | ~$40 | grounded-answer spot checks |
| **Total Claude API spent, to date** | **≈ $270** | prior-provenance, order-of-magnitude estimate — recompute from Langfuse once historical traces are retained |
| Infrastructure — 1 dev droplet + Langfuse (Cloud hobby *or* the self-hosted compose profile) | ~$50 / month while building | droplet at 198.199.68.x, Caddy TLS |

Engineer time is tracked separately and is not part of the API/infra cost line
above. These are **stated estimates, not measured actuals**. The eval and
observability tooling built in units 5–6 means every dollar of future spend is
now measurable per-request (tokens + `cost_usd` land on each Langfuse trace), so
these convert to **measured** actuals as soon as production traffic flows.

---

## 6. Projected monthly cost by tier

Users = active clinicians. LLM cost = users × full-system per-user/month (§4,
chat + LLM synthesis), adjusted per tier for the cost levers that come online at
that scale. Infra is order-of-magnitude. **All figures standard sonnet-5
pricing; estimates; assume the LLM synthesizer is enabled.** (In the current
build, the LLM line is chat-only — divide the per-user figure roughly in half.)

| Tier (users) | Per-user LLM/mo | LLM total/mo | Infra/mo | **Total/mo** | **$/user/mo** |
|---:|---:|---:|---:|---:|---:|
| **100** | $71 | $7,100 | ~$60 | **≈ $7,160** | ~$72 |
| **1,000** | $57 | $57,000 | ~$1,200 | **≈ $58,200** | ~$58 |
| **10,000** | $42 | $420,000 | ~$12,000 | **≈ $432,000** | ~$43 |
| **100,000** | $30 | $3,000,000 | ~$90,000 | **≈ $3,090,000** | ~$31 |

Per-user LLM cost **falls** with scale because each tier unlocks a new cost
lever (dedup, caching, Haiku routing, Batch API, committed-use pricing) — see
§7. At 2026 intro pricing every LLM line drops a further ~33%.

---

## 7. Per-tier architecture changes

### 100 users — *single droplet (current architecture)*

- **Deploy:** one DigitalOcean droplet (4 vCPU / 8 GB, ~$48/mo), the FastAPI
  `copilot` agent + React UI behind Caddy (TLS), managed Postgres (~$15/mo),
  Langfuse (Cloud hobby ~$0, or the self-hosted compose `observability` profile).
  Poller runs in-process in the app lifespan.
- **LLM:** the only wired LLM path is grounded chat; background synthesis is the
  deterministic stub and the poller is off by default, so the current build's
  LLM line is chat-only. No caching or dedup yet — every chat turn is a
  full-price call.
- **Bottleneck:** none at this scale; a single agent process comfortably serves
  100 clinicians (see `loadtest/RESULTS.md` — the service layer sustains 50
  concurrent users with sub-30 ms p95 on the serve paths).

### 1,000 users — *horizontal agent fleet + shared caches*

- **Deploy:** 3–5 stateless agent replicas behind a load balancer; Postgres
  moves to an HA managed instance; **Redis** added for the rounding cursor /
  conversation cache. The poller is **split out of the app process** into a
  dedicated worker and **sharded by patient** so ticks don't duplicate.
- **LLM levers (−20% per user):**
  - **Patient-panel dedup** — a patient synthesized once serves every clinician
    rounding them (the memory file is per-patient, not per-clinician). Sharing
    starts to matter here.
  - **Prompt caching** on the stable chat system+tools prefix (and on the
    synthesis system prompt, once the LLM synthesizer is enabled).
- **Bottleneck:** Postgres write throughput (chat persists conversation turns) →
  addressed by connection pooling + Redis.

### 10,000 users — *multi-region, autoscaling, tiered model routing*

- **Deploy:** Kubernetes agent fleet with HPA autoscaling; Postgres with read
  replicas (round loop is read-heavy); dedicated poller worker pool; multi-region
  for latency + failover. Langfuse self-hosted (compose profile) or Team tier.
- **LLM levers (−40% per user vs. base):**
  - **Batch API (50% off)** for the non-interactive synthesis, once the LLM
    synthesizer is enabled — it is background and tolerant of minutes of latency,
    so it is a perfect batch fit.
  - **Haiku routing** — wire the dormant `claude-haiku-4-5-20251001` gating tier
    (5× cheaper) for the optional entailment / any classification, rather than
    sonnet-5.
  - Aggressive prompt caching across the fleet.
- **Bottleneck:** poller fan-out (10k users × 15 patients = 150k patient-polls
  per interval) → sharded workers + change-gating keep model spend flat because
  most ticks are `no_change`.

### 100,000 users — *committed-use, org-wide dedup, batch-everything*

- **Deploy:** sharded multi-tenant control plane; per-region agent + poller
  fleets; Postgres sharded by tenant/facility; global object store for memory
  files; enterprise Langfuse.
- **LLM levers (−58% per user vs. base):**
  - **Anthropic committed-use / volume pricing** on the sustained sonnet-5 spend.
  - **Batch API for 100% of synthesis**, org-wide **patient dedup** (large
    hospital systems share patients across many clinicians), and prompt caching
    at scale.
  - SMART **Backend Services** OAuth (`client_credentials` + `private_key_jwt`)
    already scopes the poller to minimal `system/*.read` — no per-user token
    overhead as the poller fleet grows.
- **Bottleneck:** FHIR read volume against OpenEMR and Anthropic rate limits →
  regional sharding, request coalescing, and committed-throughput agreements.
  The change-gated poller remains the reason total spend grows sub-linearly with
  users.

---

## 8. Sensitivity — the levers that move the number most

1. **Whether the LLM synthesizer is wired in.** Today the poller uses the
   deterministic `StubSynthesizer`, so background synthesis is $0. Switching in
   `ClaudeSynthesizer` roughly *doubles* per-user LLM cost (adds the §3b line).
   This is the single biggest step-change in the whole model.
2. **Chat turns / clinician / day** — the dominant interactive cost, and today
   the *only* LLM cost. Halving it (better rounding cards → fewer drill-downs)
   cuts ~40% of the full-system per-user LLM, and ~all of the current build's.
3. **Change rate per patient** — the synthesis cost once enabled. A quieter panel
   (fewer new labs/orders) means more `no_change` ticks and near-zero synthesis.
4. **Intro vs. standard sonnet-5 pricing** — ~33% swing on the entire LLM line
   through 2026‑08‑31.
5. **Patient-panel sharing** — at 10k+ users, org-wide dedup is worth more than
   any single infra optimization.

Because every request is now traced with tokens + `cost_usd` (`OBSERVABILITY.md`),
these estimates should be replaced with measured actuals within the first week
of real traffic.
