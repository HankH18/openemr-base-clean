# AgentForge Clinical Co-Pilot — Cost Analysis

Development spend to date, unit LLM economics, and projected monthly cost at
**100 / 1,000 / 10,000 / 100,000 users** (a "user" is one active clinician /
hospitalist). Every number is an estimate with its assumptions stated; the
model is built to be recomputed as real Langfuse token/latency data lands (see
`OBSERVABILITY.md`).

The point of this document is not a single dollar figure — it is the
**cost-architecture story**: which components cost money, which are engineered
to cost *nothing* until work actually changes, and what has to change at each
order of magnitude. This revision extends the model to the **Week-2 multimodal**
surface — Claude-vision document extraction, Voyage guideline embeddings, and
Cohere reranking (§2–§4, §6) — and adds a first-class **latency treatment
(p50 + p95)** for every meaningful path in **§9**.

The model IDs and their prices are the source of truth for every LLM figure
below, and all are read straight from the code:

- Models are configured in `agent/copilot/config.py` — `anthropic_model_synthesis`
  defaults to **`claude-sonnet-5`** ("Model for synthesis and chat"),
  `anthropic_model_gating` defaults to **`claude-haiku-4-5-20251001`**
  ("Cheaper model for classification / entailment"), and the Week-2
  `anthropic_model_vision` defaults to **`claude-sonnet-5`** (structured
  extraction from document page images).
- Week-2 retrieval adds two non-Anthropic SKUs: `voyage_embedding_model` =
  **`voyage-3.5`** (guideline-corpus embeddings) and `cohere_rerank_model` =
  **`rerank-v3.5`** (retrieval rerank). Local OCR is **Tesseract**, self-hosted
  in-container (`ocr_language` / `ocr_dpi`) — no vendor per-call fee.
- Per-token rates for **all** of these live in
  `agent/copilot/observability/pricing.py`, and USD cost per request is computed
  deterministically from the reported token counts by
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
| **Verification entailment** (`LlmEntailment`, optional narrative-drift check) | `claude-haiku-4-5-20251001` | Defined + configured, but constructed nowhere; every call to the verifier passes `entailment=None`. The gating model `anthropic_model_gating` *is* read beyond `config.py` — the graph-path critic `RealCritic` (`agent/copilot/graph/critic.py:211`) uses it for an LLM safety pass that would bill at the haiku rate — but that critic runs **only** on the multi-agent graph path, gated by `chat_graph_enabled` (**default OFF** in code, `config.py:449`, so a keyless local clone + the deterministic eval gate run the inline path — **but the deployed demo sets it ON** (`COPILOT_CHAT_GRAPH_ENABLED=true`), so the graph *and* its critic ARE live there). In the default/keyless build `build_critic` returns the keyless `StubCritic` (no LLM), so the gating tier is dormant — **$0**. **On the deploy** (Anthropic key set + graph on), `build_critic` constructs the real `RealCritic`, which makes one haiku gating call per graph chat turn — a small per-turn cost the **$0** default-build figure does not include. |

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

### 1d. Week-2 multimodal paths — new model calls, same change-gated posture

Week 2 adds two feature surfaces, each with its own model calls. Both are
**key-gated**: with no `VOYAGE_API_KEY` / `COHERE_API_KEY` the embedder and
reranker fall back to deterministic **keyless stubs** (no outbound call, **$0**),
and with no `ANTHROPIC_API_KEY` the vision extractor falls back the same way
(`build_vision` returns `StubVision()` — `agent/copilot/documents/vision.py:540-544`).
So, exactly as with Week-1 synthesis, the Week-2 model spend is **$0 until an
operator wires real keys.**

> **The key gate is what makes Week-2 model spend $0 by default.** With no
> `VOYAGE_API_KEY` / `COHERE_API_KEY` / `ANTHROPIC_API_KEY`, the embedder, reranker,
> and vision extractor all fall back to deterministic keyless stubs that make **zero
> outbound calls** — the reachable upload endpoint runs the stub extractor and bills
> nothing until a real key is set. This key gate is load-bearing and verified.
>
> **Note on the `document_ingestion_enabled` flag.** An earlier version of this
> section described this setting as a *dead* flag — declared but read nowhere, so it
> gated nothing and was "slated for deletion." **That is no longer true.** The flag is
> now a genuine ingestion kill switch: declared at `agent/copilot/config.py:431` with
> `default=True`, and enforced at `agent/copilot/api/routes/documents.py:180` — when
> false, `POST /v1/documents` returns **503** and no document is accepted. It is an
> *operator control on the upload surface, not a cost control*: it defaults on, so the
> $0-by-default guarantee is the **key gate** above, not this flag. The
> document-ingestion HTTP surface is still mounted whenever the agent runs
> (`register_routers`, `agent/copilot/api/app.py:40-59`) and is protected by the SMART
> session + rounding-list RBAC gate like every PHI route; the flag adds an intake-off
> switch on top. See `W2_ARCHITECTURE.md` §Assumptions. **No cost claim in this
> document rests on it.**

| Week-2 path | Model / tool | When it fires | Cost lever |
|---|---|---|---|
| **Document extraction** (`POST /v1/documents` → `ClaudeVision`) | `claude-sonnet-5` (vision), `max_tokens=4096`, tool-forced JSON | Once per uploaded document, on ingest | Page images per doc (tokens ≈ page area); one-shot, batchable |
| **Local OCR** (`Tesseract`, in-container) | self-hosted CPU | Every ingested page (rasterize → OCR word boxes for bbox reconciliation) | **$0 marginal** — no vendor fee; adds latency, not dollars |
| **Guideline embeddings** (`voyage-3.5`) | Voyage AI | Corpus **ingest** (once per corpus build/refresh, cached) + **per retrieval query** (embed the de-identified query) | Corpus is one-time/amortized; per-query embed is negligible |
| **Retrieval rerank** (`rerank-v3.5`) | Cohere | Per guideline-retrieval query (best-effort; fail-open to fused order) | ~1 search/query; the dominant *retrieval-side* unit cost, still tiny |

PHI posture is preserved: document **images** go only to Claude (as in Week 1);
a `deidentify()` choke-point strips identifiers before any Voyage or Cohere call.
That scrub is **shape-based, with a known gap**: it removes structured identifiers
(email/SSN/date/phone/5+-digit runs) and *label-gated* names (`Patient: <Name>`),
but **not an arbitrary free-text name** typed into a question — see
`W2_ARCHITECTURE.md` §Security. It bounds egress; it is not Safe Harbor
de-identification, and no cost line here depends on it.
Guideline retrieval is **fail-open** — a rerank/embed failure degrades evidence
quality but never withholds the grounded patient answer — so it is a
quality/latency surface, not an availability one.

---

## 2. Real model + SKU pricing (source of truth)

Per-million-token pricing for every model + SKU this system is configured to
call, copied verbatim from `agent/copilot/observability/pricing.py` (rate card
"as of 2026-07"; an unrecognised model falls back to the sonnet-tier rate so an
unknown model is never costed as free):

| Model | Role in system | Input $/1M | Output $/1M |
|---|---|---|---|
| `claude-sonnet-5` | chat (live) + synthesis (projected) + **Week-2 vision extraction** | **$3.00** (intro **$2.00** through 2026‑08‑31) | **$15.00** (intro **$10.00**) |
| `claude-haiku-4-5-20251001` | optional entailment (dormant) / gating tier (**on in the deployed graph**; dormant in the keyless default build) | **$1.00** | **$5.00** |
| `voyage-3.5` | **Week-2** guideline embeddings (corpus ingest + per query) | **$0.06** | **$0.00** (embeddings emit no output tokens) |
| `rerank-v3.5` | **Week-2** retrieval rerank (per query) | **$0.25** (normalized — see below) | **$0.00** |
| **Tesseract OCR** | **Week-2** local page OCR (bbox reconciliation) | **$0.00** (self-hosted, in-container) | **$0.00** |

The intro sonnet-5 rate is a real per-token discount from Anthropic's
**external, publicly-published rate card** — it is *not* encoded in `pricing.py`.
That table carries only the **standard** row (`"claude-sonnet-5": (3.0, 15.0)`)
and its docstring records "provider list prices … as of 2026-07"; there is **no
intro-rate row or docstring note** in the code. So every figure below uses the
standard $3/$15 the code actually costs at and is therefore conservative — the
external 2026 intro pricing would cut the Anthropic line ~33%. Note the **vision** model
(`anthropic_model_vision`) defaults to `claude-sonnet-5`, so it resolves to the
same real $3/$15 row — never the unknown-model fallback.

**Model-swap coverage (higher tiers now on the rate card).** Beyond the SKUs in the
table above, `pricing.py` also carries explicit rows for the **Opus** and **Fable**
tiers — `claude-opus-4-8` and `claude-opus-4-7` at **$5.00 / $25.00** per 1M
input/output, and `claude-fable-5` at **$10.00 / $50.00** — so that if an operator
points `anthropic_model_synthesis` (or `anthropic_model_vision`) at one of them, spend
is costed at the real rate rather than silently falling through to the sonnet-tier
default (Opus would be ~40% under-reported, Fable much more). **The reference
deployment runs `claude-sonnet-5` ($3/$15)**, so these rows change no figure in this
document; they exist only to keep the cost accounting correct across a model swap.

**Two Week-2 rates are documented normalizations, straight from the
`pricing.py` docstring:**

- **`voyage-3.5`** is Voyage AI's list price, **$0.06 per 1M input tokens**;
  embedding calls have no output, so the output rate is $0.
- **`rerank-v3.5`** is priced by Cohere per **search unit** ($2.00 per 1k
  searches; one unit = query + up to 100 documents), not per token. The pricing
  table's surface is per-token, so the code normalizes: a rerank call sends
  ~20 candidate chunks × ~400 tokens ≈ 8k input tokens per search, i.e.
  `$0.002 / 8k tokens ⇒ $0.25 / 1M input tokens` — deliberately on the
  conservative (high) side so rerank spend is never under-reported. Per-query,
  that is Cohere's native **$0.002 / search**.
- **OCR** is Tesseract running in the container — CPU only, **no vendor
  per-call fee** ($0 marginal). It contributes to ingestion *latency* (§9c),
  not to the dollar line.

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

### 3c. One document extraction — `claude-sonnet-5` (vision), `max_tokens=4096` (Week-2)

`ClaudeVision` rasterizes each page at `ocr_dpi=200`, base64-encodes the PNG(s),
and makes **one** tool-forced call whose input schema *is* the strict extraction
schema. The input is dominated by the **page images**, not text. Assumptions
(illustrative; the per-request truth lands on the trace via `cost_usd`):

| Component | Tokens |
|---|---:|
| Page images — a letter page at 200 DPI, downscaled to Anthropic's per-image working size (image tokens ≈ width×height ÷ 750), **~1,600 tokens/page**, typical **2-page** doc | 3,200 in |
| System prompt + forced-tool JSON schema | ~500 in |
| Structured extraction (facts list) — bounded by `max_tokens=4096` | ~800 out |
| **Billed total** | **≈ 3,700 in / 800 out** |

Cost = 3,700/1e6 × $3 + 800/1e6 × $15 = $0.0111 + $0.0120 ≈ **$0.023 per
document** (standard). Round up to **~$0.03/document** for conservative planning
(≈ $0.015 at intro pricing; a single-page doc ≈ $0.013). Extraction is **one
shot per upload** and non-interactive, so it is a natural **Batch API (50% off)**
candidate at scale (§6).

### 3d. Guideline embeddings — `voyage-3.5` (Week-2)

Two distinct cost moments, both tiny:

- **Per-corpus ingest (one-time / on refresh).** Chunks are ≤ `MAX_CHUNK_CHARS`
  (1,200 chars ≈ ~300 tokens each) and embedded once, then cached. A ~1,000-chunk
  starter corpus ≈ 300,000 tokens → 300,000/1e6 × $0.06 = **$0.018 one-time**
  (even a 10,000-chunk corpus is ~$0.18). Amortized across all users ⇒ ~$0/user.
- **Per query.** Only the short de-identified query is embedded (~40 tokens):
  40/1e6 × $0.06 ≈ **$0.0000024 / query** — rounds to zero even at millions of
  queries.

### 3e. Retrieval rerank — `rerank-v3.5` (Week-2)

One Cohere search per guideline-retrieval query over the fused candidate set
(~20 chunks). At the normalized rate that is 8,000/1e6 × $0.25 = **$0.002 /
query** (= Cohere's native $2.00/1k searches). Rerank is best-effort and
**fail-open** — a Cohere error falls back to the fused sparse+dense order at
$0, so this line is bounded above by the query volume and never blocks an answer.

**Per-retrieval total (embed + rerank) ≈ $0.002/query**, essentially all of it
Cohere. Retrieval fires only on guideline-seeking chat turns, so it rides on top
of a fraction of chat turns, not all of them (§4).

---

## 4. Per-user monthly usage model (stated assumptions)

| Assumption | Value |
|---|---|
| Working days / month | 22 |
| Chat turns / clinician / day | 25 |
| Patients on a clinician's panel | 15 |
| Substantive syntheses / patient / day (after change-gating; ~288 raw ticks/day at the 300 s default interval — 86,400 s ÷ 300 s — but only real changes cost) | 6 |
| Patient-panel sharing (a patient synthesized once serves every clinician viewing them) | none at 100 users; grows with scale |
| Documents ingested / clinician / day (Week-2) | 2 |
| Guideline retrievals / clinician / day (Week-2; fires on ~half of the 25 chat turns) | 12 |
| Guideline corpus embeds (Week-2) | one-time / on refresh (amortized ≈ $0) |

**Current build (chat is the only live LLM cost):**

- Chat: 25 × $0.05 = **$1.25 / clinician / day → ≈ $28 / clinician / month**
  (standard; ≈ $17 at intro pricing).
- Background synthesis and the entailment tier are **deterministic / dormant**
  today, so they add **$0**.

**Full system, once the LLM synthesizer is enabled (adds the §3b line):**

- Chat: 25 × $0.05 = **$1.25**
- Synthesis: 15 patients × 6 × $0.022 = **$1.98**
- **≈ $3.23 / clinician / day → ≈ $71 / clinician / month** (standard; ≈ $45 at intro).

**Week-2 multimodal add-on (once document ingestion + guideline retrieval are enabled):**

- Vision extraction: 2 docs × $0.03 = **$0.06 / clinician / day**
- Guideline retrieval (rerank + query embed): 12 × $0.002 = **$0.024 / clinician / day**
- Corpus embeddings: one-time / amortized ≈ **$0**; OCR (self-hosted) ≈ **$0**
- **≈ $0.084 / clinician / day → ≈ $1.85 / clinician / month** (standard; round **~$2**).

Stacked on the full system, that is **≈ $73 / clinician / month** (chat +
synthesis + Week-2 multimodal). The Week-2 model lines are only **~3%** of the
LLM bill and are dominated by **vision extraction**; retrieval is a rounding
error, and OCR/corpus-embeds are ~$0.

The projected tier table in §6 costs the **full system** (chat + LLM synthesis),
since that is the architecture being scaled; the current build sits flat at the
chat-only ≈ $28/user/mo until the synthesizer is switched on. §6 then folds the
Week-2 multimodal add-on on top, with its own per-tier levers (document dedup,
Batch API for the non-interactive vision path, amortized corpus embeds).

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

### 6b. Week-2 multimodal add-on, folded into the tiers

The Week-2 model lines (§3c–§3e) stack **on top** of the chat + synthesis figures
above. They carry their own scale levers: a document **extracted once serves
every clinician viewing that patient** (document dedup, mirroring patient-panel
dedup), the non-interactive vision path is a **Batch API (50% off)** fit, the
guideline **corpus is embedded once and shared** (amortized ≈ $0), and prompt
caching applies to the stable vision system+tool prefix.

| Tier (users) | Week-2 add-on /user/mo | Lever applied | Combined LLM+multimodal /user/mo | **New total/mo** |
|---:|---:|---|---:|---:|
| **100** | ~$2.0 | none (baseline) | ~$73 | **≈ $7,360** |
| **1,000** | ~$1.5 | −20%: doc dedup + vision-prefix caching | ~$58 | **≈ $59,700** |
| **10,000** | ~$1.1 | −40%: Batch API vision + caching + dedup | ~$43 | **≈ $443,000** |
| **100,000** | ~$0.8 | −58%: org-wide doc dedup + batch-everything + committed-use | ~$31 | **≈ $3,170,000** |

The add-on is **~3% of the LLM bill at 100 users and shrinks with scale** — the
cost story is unchanged: chat and (projected) synthesis dominate; the multimodal
surface is a modest, dedup-friendly increment. All figures standard pricing;
intro pricing trims the Anthropic-priced portion (chat, synthesis, vision) a
further ~33%.

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
  100 clinicians (see `loadtest/RESULTS.md` — at 50 concurrent users the fast
  serve paths stay well-bounded: `/health` sub-30 ms p95, and the readiness and
  DB-read paths sub-60 ms p95 — `/ready` p95 53.1 ms, `rounds/current` p95
  59.0 ms).

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
  - **Haiku routing** — the `claude-haiku-4-5-20251001` gating tier already backs
    the graph-path critic (5× cheaper than sonnet); extend it to the still-dormant
    optional entailment / any other classification, rather than sonnet-5.
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
6. **Week-2 vision volume** — documents ingested/clinician/day × pages/doc is the
   whole multimodal cost line (§3c dominates §3d–§3e). It is small today (~3% of
   the LLM bill) but grows linearly with page count; **document dedup** (extract
   once, serve every viewer) and **Batch API** on the non-interactive vision path
   keep it sub-linear with users. Retrieval (Voyage + Cohere) and OCR are rounding
   errors and move the number negligibly.

Because every request is now traced with tokens + `cost_usd` (`OBSERVABILITY.md`),
these estimates should be replaced with measured actuals within the first week
of real traffic.

---

## 9. Latency — p50 and p95 across the meaningful paths

Latency has two sources of truth, and each cell below is labeled **MEASURED** or
**ESTIMATE**:

1. **Measured service-layer percentiles** from the **2026-07-10 warm-stub
   capture** of the offline httpx driver (`auth_mode=disabled`, **stubbed** LLM,
   throwaway SQLite — *framework + transport only, no real model or FHIR call*).
   These are the meaningful serve-layer floor: the fast paths *before* the
   FHIR-retry-on-absent-backend cost was wired onto the stub fetch path.
   **Important — the live `loadtest/RESULTS.md` was re-captured 2026-07-19** and
   now shows materially higher end-to-end chat / `rounds/start` numbers (chat
   p50 ~1,906 ms / p95 ~2,388 ms at 10 users, `RESULTS.md` line 96) purely
   because that harness has no live FHIR backend and exhausts the client
   retry-backoff (~1.9 s over 6 resource types) before failing — an
   absent-dependency artifact the file's own provenance note documents (RESULTS.md
   §Provenance), **not** production-representative. §9a below therefore reports
   the archived 07-10 floor, not the 07-19 numbers now in that file.
2. **SLO-anchored estimates** for the paths whose latency is dominated by an
   upstream model call. The stubbed p50/p95 harness
   (`agent/scripts/latency_report.py`) measures the LLM-free floor; the
   end-to-end numbers are estimated against the **published SLO targets**
   (`OBSERVABILITY.md` §7.1 and Alert 2) because no production traces are
   retained yet. Every estimate states its basis.

### 9a. Fast serve paths — MEASURED (2026-07-10 warm-stub floor, 10 concurrent users)

> These are the archived **2026-07-10** serve-layer floor (the FHIR-absent retry
> inflation stripped out), **not** the numbers currently in `loadtest/RESULTS.md`
> — that file was re-captured 2026-07-19 and its chat / `rounds/start` rows are
> retry-dominated (see the §9 note above and the file's own provenance section).

| Path | p50 (ms) | p95 (ms) | Note |
|---|---:|---:|---|
| `GET /health` | **1.9** | **7.0** | pure liveness |
| `GET /ready` | **13.2** | **25.9** | 503 when deps absent (expected) |
| `GET /v1/rounds/current` (DB read) | **13.0** | **51.8** | deterministic round loop, Postgres read |
| `POST /v1/rounds/start` | **12.4** | **20.8** | 500 in-harness (no live FHIR); 200 on the deployed stack |
| `POST /v1/chat` (serve layer, stubbed agent, 200 fail-closed) | **56.0** | **108.5** | full serve path **minus** the live Claude call |

At **50** concurrent users in that same 2026-07-10 capture the single uvicorn
worker + SQLite become the write bottleneck: `rounds/current` widens to **p50
156.3 / p95 414.9 ms** and the stubbed chat serve path to **p50 369.9 / p95
1,055.5 ms**, while `/health` stays flat (~10 ms p95). This is the empirical
motivation for the §7 1,000-user step (stateless replicas + managed Postgres +
Redis). (In the current 2026-07-19 `loadtest/RESULTS.md` the 50-user chat row is
instead retry-dominated — **p50 ~1,884 / p95 ~2,420 ms**, RESULTS.md line 132 —
so it no longer isolates the write bottleneck; the 07-10 numbers above do.)

### 9b. LLM chat turn (synthesis) — ESTIMATE (SLO-anchored)

Real chat = the ~0.06 s p50 / ~0.1 s p95 **serve-layer floor** (the 2026-07-10
warm-stub chat serve path in §9a — i.e. with the FHIR-absent retry inflation
stripped out, *not* the ~1.9 s retry-dominated figure now in `loadtest/RESULTS.md`)
**plus** the Claude `sonnet-5` tool-use loop (2–3 sequential Anthropic calls +
a live FHIR re-fetch, §3a). The model calls dominate:

| Path | p50 | p95 | Basis |
|---|---:|---:|---|
| **Grounded chat turn** (interactive) | **≈ 3.5 s** *(est)* | **≈ 7.5 s** *(est)* | sized to `OBSERVABILITY.md` Alert 2: chat **p95 < 8 s** warn, **p99 < 15 s** page; 2–3 sequential sonnet-5 calls + FHIR re-fetch |
| **Background synthesis** (non-interactive, projected) | **≈ 2 s** *(est)* | **≈ 5 s** *(est)* | single sonnet-5 call, no tool loop; tolerant of minutes ⇒ Batch-friendly |

p50 for the chat turn is a **conservative estimate** (~half the p95 SLO): a
typical two-tool turn makes fewer/faster round-trips than the tail case the SLO
guards. Replace with measured Langfuse percentiles once traffic flows.

### 9c. Document ingestion (upload → extract) — MEASURED floor + ESTIMATE

Pipeline: upload → rasterize → **Tesseract OCR** → **Claude-vision extraction**
→ OCR reconcile → append-only persist.

| Path | p50 | p95 | Basis |
|---|---:|---:|---|
| **Stub baseline** (keyless, no vision call) | **sub-second** *(measured)* | **sub-second** *(measured)* | `latency_report.py` — rasterize + OCR + reconcile + persist only |
| **Real ingestion** (vision enabled) | **≈ 5 s** *(est)* | **≈ 10 s** *(est)* | `OBSERVABILITY.md` §7.1: `doc.ingest` **p95 < 12 s** warn / **< 30 s** page; dominated by the one vision call (§3c); CPU rasterize+OCR adds ~0.5–1 s/page |

Ingestion **fails closed** (a failed run persists zero facts) and is
non-interactive, so the tail matters less than for chat — a document a few
seconds slow is invisible to the clinician.

### 9d. Hybrid RAG retrieval — MEASURED floor + ESTIMATE

Pipeline: de-identify → **Voyage embed** (query) → pgvector + FTS fuse →
**Cohere rerank**.

| Path | p50 | p95 | Basis |
|---|---:|---:|---|
| **Stub baseline** (keyless embed + rerank) | **low single-digit ms** *(measured)* | **low single-digit ms** *(measured)* | `latency_report.py` over the seeded corpus — DB fusion only, no network (`artifacts/latency_report.json`: `evidence_retrieval` p50 **1.37** / p95 **2.02** ms, n=5) |
| **Real retrieval** (Voyage + Cohere) | **≈ 450 ms** *(est)* | **≈ 800 ms** *(est)* | `OBSERVABILITY.md` §7.1: `guideline.retrieve` **p95 < 800 ms** warn / **< 2 s** page; two network hops (embed + rerank) dominate |

Retrieval is **fail-open**: a Cohere/Voyage timeout falls back to the fused
sparse+dense order, so a slow rerank caps *added* latency without ever blocking
the grounded patient answer.

### 9e. Latency bottleneck reading (per path)

- **Serve layer (Week-1):** single uvicorn worker + SQLite serialize writes at
  50u — chat climbs to a ~1.06 s p95, `rounds/current` to ~415 ms (the 2026-07-10
  warm-stub capture, §9a; the current 07-19 file's chat row is retry-dominated
  instead). Fixed by the §7 1,000-user tier (replicas + managed Postgres +
  Redis). *Bottleneck: write serialization.*
- **Chat / synthesis:** the **Anthropic tool-use loop** — 2–3 sequential model
  calls plus a live FHIR re-fetch — is the whole tail. `tool_calls` is the
  lever (fewer round-trips ⇒ lower p95); prompt caching cuts per-call input
  processing. *Bottleneck: sequential upstream model calls.*
- **Document ingestion:** the **Claude-vision call** dominates; latency scales
  with page count (per-page image tokens). Batch API trades latency for cost on
  this non-interactive path. *Bottleneck: the vision model call (+ CPU OCR per
  page).*
- **Retrieval:** the **two network hops** (Voyage embed + Cohere rerank)
  dominate; DB fusion is low single-digit ms. Co-locating/caching the embedder and
  reranker near the app trims the tail, and fail-open bounds the worst case.
  *Bottleneck: embed + rerank network round-trips.*

These estimates convert to **measured** p50/p95 as soon as production traces are
retained — the same Langfuse spans in `OBSERVABILITY.md` §1 and §7.1 carry
per-path duration, so this section recomputes from the trace exactly as the
cost lines do.
