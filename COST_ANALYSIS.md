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

---

## 1. What actually costs money in this system

The co-pilot is deliberately built so cost tracks **clinical change**, not
patient count or clock ticks. Three cost sources:

| Source | Model | When it fires | Cost lever |
|---|---|---|---|
| **Grounded chat** (`POST /v1/chat`) | `claude-sonnet-5` (tool-use loop over FHIR) | Per interactive clinician question | Tokens per turn; interactive, not batchable |
| **Background synthesis** (change-gated poller) | `claude-sonnet-5` (`ClaudeSynthesizer`) | Only when a watched patient's record **actually changed** | Change-gating + content-hash dedup; batchable |
| **Verification entailment** (optional narrative-drift check) | `claude-haiku-4-5` | Optional per-claim check on the serve path | Cheap tier; off by default today |

Two things that look expensive but are **$0 in LLM cost**, by design:

- **`POST /v1/rounds/start` / `/advance` / `/current` / `/jump`.** Rounding
  cards are assembled by a **deterministic** synthesizer (`StubSynthesizer`)
  plus a deterministic acuity ranker — no model call. The physician-facing
  round loop is pure Postgres + FHIR reads.
- **Poller ticks with no change.** Each tick first issues cheap FHIR
  `_summary=count` queries per resource type since the stored watermark. If
  every count is zero, the patient is skipped — **no resource pull, no model
  call**. A content-hash then catches cosmetic-only updates (a moved
  `lastUpdated` with identical payload) and still skips synthesis. The
  expensive Claude call happens *only* on a real, substantive change.

This "cost-scales-with-change" design is the single most important lever in
every tier below.

---

## 2. Real Anthropic token pricing (source of truth)

Per-million-token list pricing for the two models this system calls:

| Model | Role in system | Input $/1M | Output $/1M |
|---|---|---|---|
| `claude-sonnet-5` | chat + poller synthesis | **$3.00** (intro **$2.00** through 2026‑08‑31) | **$15.00** (intro **$10.00**) |
| `claude-haiku-4-5` | optional entailment / gating tier | **$1.00** | **$5.00** |

Prompt caching (used from the 1,000-user tier up): cache **reads** ≈ 0.1× input
price; cache **writes** ≈ 1.25× (5-minute TTL). Batch API (used at 10k+ for the
non-interactive poller path): **50%** off standard.

All figures below use **standard** sonnet-5 pricing ($3 / $15) so estimates are
conservative; the 2026 intro pricing would cut the LLM line ~33%.

---

## 3. Unit economics (per request, grounded in the code)

### 3a. One grounded chat turn — `claude-sonnet-5`, `max_tokens=2048`

`ClaudeAgent` runs an Anthropic tool-use loop: Claude calls `get_labs` /
`get_medications`, each returning a FHIR bundle that is re-sent as context on
the next call, so input tokens compound across the loop. Modeling a typical
two-tool turn over a moderate record:

| API call | Input tokens | Output tokens |
|---|---:|---:|
| 1 — system (≈350) + 2 tool defs (≈150) + question (≈30) | 530 | 120 (tool_use) |
| 2 — prior + labs bundle (≈4,000) | 4,650 | 80 (tool_use) |
| 3 — prior + meds bundle (≈2,500) → final grounded JSON | 7,230 | 350 |
| **Billed total** | **≈12,400** | **≈550** |

Cost = 12,400/1e6 × $3 + 550/1e6 × $15 = **$0.0372 + $0.0083 ≈ $0.046 per chat turn**.
Round to **$0.05/turn** (standard) — **~$0.03/turn** at intro pricing.

### 3b. One poller synthesis — `claude-sonnet-5`, `max_tokens=2048`

`ClaudeSynthesizer` sends a strict-JSON system prompt (≈250 tokens) plus the
patient's *changed* FHIR resources (≈4,000 tokens for a fresh lab panel + a new
order) and gets back a claims list (≈600 tokens):

Cost = 4,250/1e6 × $3 + 600/1e6 × $15 = **$0.0128 + $0.0090 ≈ $0.022 per synthesis**.

Because synthesis is change-gated, this cost is paid **only** on ticks that
detect real change — not on every poll.

---

## 4. Per-user monthly usage model (stated assumptions)

| Assumption | Value |
|---|---|
| Working days / month | 22 |
| Chat turns / clinician / day | 25 |
| Patients on a clinician's panel | 15 |
| Substantive syntheses / patient / day (after change-gating; ~144 raw ticks/day at the 300s default interval, but only real changes cost) | 6 |
| Patient-panel sharing (a patient synthesized once serves every clinician viewing them) | none at 100 users; grows with scale |

**Per clinician / day (100-user tier, no dedup):**

- Chat: 25 × $0.05 = **$1.25**
- Synthesis: 15 patients × 6 × $0.022 = **$1.98**
- **≈ $3.23 / clinician / day → ≈ $71 / clinician / month** in LLM (standard pricing; ≈ $45 at intro pricing).

---

## 5. Development spend to date

The co-pilot was built agent-assisted across six committed units (FHIR client +
SMART App Launch / Backend Services OAuth; change-gated poller + synthesizer;
grounded verification layer; eval suite + CI; Langfuse observability +
correlation IDs). **Development spend to date** breaks down as:

| Item | Estimate | Notes |
|---|---|---|
| Claude API — agent-assisted code generation | ~$150 | interactive build sessions |
| Claude API — eval-suite + acceptance runs (unit 5) | ~$80 | `agent/evals` exercises the real synthesis/verification path against the LLM |
| Claude API — manual live verification of chat/rounds against the droplet | ~$40 | grounded-answer spot checks |
| **Total Claude API spent, to date** | **≈ $270** | order-of-magnitude; recompute from Langfuse once historical traces are retained |
| Infrastructure — 1 dev droplet + Langfuse Cloud (hobby) | ~$50 / month while building | droplet at 198.199.68.x, Caddy TLS |

Engineer time is tracked separately and is not part of the API/infra cost line
above. The eval and observability tooling built in units 5–6 means every dollar
of future spend is now measurable per-request (tokens + cost land on each
Langfuse trace), so these development-spend estimates convert to **measured**
actuals as soon as production traffic flows.

---

## 6. Projected monthly cost by tier

Users = active clinicians. LLM cost = users × per-user/month (§4), adjusted per
tier for the cost levers that come online at that scale. Infra is
order-of-magnitude. **All figures standard sonnet-5 pricing; estimates.**

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
  Langfuse Cloud (hobby, ~$0). Poller runs in-process in the app lifespan.
- **LLM:** no caching or dedup yet — every chat turn and every changed-patient
  synthesis is a full-price call. This is the current build.
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
  - **Prompt caching** on the stable chat system+tools prefix and the synthesis
    system prompt.
- **Bottleneck:** Postgres write throughput (chat persists conversation turns) →
  addressed by connection pooling + Redis.

### 10,000 users — *multi-region, autoscaling, tiered model routing*

- **Deploy:** Kubernetes agent fleet with HPA autoscaling; Postgres with read
  replicas (round loop is read-heavy); dedicated poller worker pool; multi-region
  for latency + failover. Langfuse self-hosted or Team tier.
- **LLM levers (−40% per user vs. base):**
  - **Batch API (50% off)** for the non-interactive poller synthesis — it is
    background and tolerant of minutes of latency, so it is a perfect batch fit.
  - **Haiku routing** — the optional verification entailment / any
    classification is routed to `claude-haiku-4-5` (5× cheaper) rather than
    sonnet-5.
  - Aggressive prompt caching across the fleet.
- **Bottleneck:** poller fan-out (10k users × 15 patients = 150k patient-polls
  per interval) → sharded workers + change-gating keep the model spend flat
  because most ticks are `no_change`.

### 100,000 users — *committed-use, org-wide dedup, batch-everything*

- **Deploy:** sharded multi-tenant control plane; per-region agent + poller
  fleets; Postgres sharded by tenant/facility; global object store for memory
  files; enterprise Langfuse.
- **LLM levers (−58% per user vs. base):**
  - **Anthropic committed-use / volume pricing** on the sustained sonnet-5 spend.
  - **Batch API for 100% of poller synthesis**, org-wide **patient dedup**
    (large hospital systems share patients across many clinicians), and prompt
    caching at scale.
  - SMART **Backend Services** OAuth (`client_credentials` + `private_key_jwt`)
    already scopes the poller to minimal `system/*.read` — no per-user token
    overhead as the poller fleet grows.
- **Bottleneck:** FHIR read volume against OpenEMR and Anthropic rate limits →
  regional sharding, request coalescing, and committed-throughput agreements.
  The change-gated poller remains the reason total spend grows sub-linearly with
  users.

---

## 8. Sensitivity — the levers that move the number most

1. **Chat turns / clinician / day** — the dominant interactive cost. Halving it
   (better rounding cards → fewer drill-downs) cuts ~40% of per-user LLM.
2. **Change rate per patient** — the poller cost. A quieter panel (fewer new
   labs/orders) means more `no_change` ticks and near-zero synthesis cost.
3. **Intro vs. standard sonnet-5 pricing** — ~33% swing on the entire LLM line
   through 2026‑08‑31.
4. **Patient-panel sharing** — at 10k+ users, org-wide dedup is worth more than
   any single infra optimization.

Because every request is now traced with tokens + cost (`OBSERVABILITY.md`),
these estimates should be replaced with measured actuals within the first week
of real traffic.
