# Clinical Co-Pilot — Technical Decisions

Brief ADR-style record of the key decisions and what each one costs. Pulled from the Phase 4
options analysis and the assumptions/fragility audit. Revisit an entry if its fragility flag
fires. Where a decision was made *for* you (a recommended default you accepted under time
pressure), it's marked **[recommended default]** so you can challenge it later.

---

## Product shape — conversational, memory-file-backed, one patient at a time
**Decision:** No visible dashboard/board. Background processing maintains a per-patient memory
file; the physician interacts only through chat, which presents the top patient, allows
drill-down, advances on "done," and soft-preempts on mid-rounds deterioration.
**Why:** Dissolves the "is this a dashboard?" tension the brief warns against; the guided,
stateful hand-off is a shape no static view can replicate.
**Benefits:** Clean "why an agent" defense; frontend collapses to a chat panel (less to build).
**Tradeoffs:** All value now depends on the memory file being correct and fresh → raises the
stakes on verification and staleness handling.
**Alternatives considered:** Ephemeral on-demand briefing; a live ranked triage board.

## Grounding — memory file with per-fact provenance
**Decision:** Each fact in a memory file carries a source_ref (FHIR resource type + ID + value +
`lastUpdated`). Chat claims chain through the file to the real record.
**Why:** The memory file is itself an LLM synthesis; it can't be the terminal source of truth.
**Benefits:** Two-layer verification (synthesis-time + serve-time); minimal PHI at rest
(summary + pointers, not raw charts); FHIR **Provenance** is real and backs this natively.
**Tradeoffs:** Every fact must carry provenance plumbing; serve-time re-check adds work.
**Alternatives considered:** Treating the summary as source (rejected — grounds itself in itself).

## (1) Integration — separate service, data via OpenEMR API only
**Decision:** Standalone Python service; all patient data via FHIR/REST; chat as physician
(SMART App Launch), poller as system actor (SMART Backend Services). No direct DB.
**Why:** Platform-enforced authorization; no PHP monolith surgery; fits Claude/Python.
**Benefits:** Cleanest trust boundary ("no read path bypasses OpenEMR authz"); strong CTO story.
**Tradeoffs:** Multi-call patient assembly; some free-text may be thinner via FHIR than raw DB;
higher per-call latency than in-process.
**Alternatives considered:** In-process PHP module (toolchain fight, GACL authz); hybrid DB path.
**Fragility flag:** If FHIR latency/completeness disappoints under load → add a DB fast-path for
the **poller only**, behind the existing interface. Signal: p95 pull latency in load tests.

## (2) Runtime — Python / FastAPI / Pydantic v2 / Pydantic AI / Langfuse  **[recommended default]**
**Decision:** Python backend; FastAPI; Pydantic v2 contracts; thin Pydantic AI agent loop;
Langfuse observability. (You flipped from an initial TS recommendation deliberately.)
**Why:** Contracts (Pydantic), observability (Langfuse), and eval are strongest in Python.
**Benefits:** All three graded engineering requirements land on their best footing; verification
stays first-class code (thin loop, not a heavy framework).
**Tradeoffs:** Two-language repo (Python API + React UI) — standard, but real.
**Alternatives considered:** TypeScript + Vercel AI SDK (your home turf); LangGraph (heavier,
stickier — reserved for genuinely graph-shaped flows).
**Fragility flag:** Pydantic AI is young/fast-moving — pin exact versions; if it fights you >~1h,
drop to the raw Anthropic SDK tool-loop (design unchanged).

## (3) State — Postgres as system of record; Redis deferred
**Decision:** Encrypted Postgres 16 (JSONB memory files + relational markers/cursor/sync/audit).
Redis added later only for hot path + soft-preempt pub/sub.
**Why:** Durable, transactional, encryptable, backup-able — the store a hospital CTO expects.
**Benefits:** One durable store; memory files regenerable → lower standing PHI risk; pgvector
available later.
**Tradeoffs:** You own migrations; slightly heavier than a KV store for hot writes.
**Alternatives considered:** Redis-as-primary (wrong for durable PHI); SQLite (weak under
concurrency/scale).
**Fragility flag:** A mandated retention/immutability rule will constrain the schema — design
assuming one lands. Signal: compliance audit surfacing a specific rule.

## (4) Change detection & scheduling — watermark + count-gate + hash-confirm; poll; phased worker
**Decision:** Per-patient FHIR `_lastUpdated` count query gates cheap detection; content-hash
confirms materiality before any Claude call. In-process asyncio loop (MVP) → separated worker +
Postgres-backed queue (prod). Poll only patients on an active rounding list.
**Why:** OpenEMR has no working FHIR Subscription; count queries + `meta.lastUpdated` are
verified-present.
**Benefits:** Cost/latency scale with **change rate**, not patients × frequency; strong,
defensible AI-cost story.
**Tradeoffs:** Two-step detection logic; trusts `_lastUpdated` accuracy; MVP loop isn't HA.
**Alternatives considered:** Watermark-only (wasteful); full-pull-and-hash (defeats the point);
external cron only (half a solution).
**Fragility flag:** If `_lastUpdated` isn't a reliable per-resource filter → hash-based detection
for that resource type. If a sub-"minutes" freshness SLA appears → build event-driven (DB
triggers/CDC) behind the same interface.

## (5) Verification — layered, deterministic-first, fail-closed, at both boundaries
**Decision:** Deterministic citation + numeric exact-match as the hard gate; optional LLM
entailment for narrative; curated domain rules (allergy–med conflict, abnormal/critical labs via
`range`/`abnormal`, small dosage/interaction table). Runs at synthesis **and** serve;
live-refetches cited resources by ID for numeric/critical values. Unverifiable → withheld or
degraded.
**Why:** The trust decision must be provable and not promptable.
**Benefits:** Attribution + numbers guaranteed deterministically; prompt-injection can't pass the
gate; honest handling of uncertainty; feeds the verification-pass/fail dashboard metric.
**Tradeoffs:** Attribution ≠ semantic correctness (entailment mitigates, doesn't eliminate); the
domain-rule set is a deliberate subset, not a full CDS.
**Alternatives considered:** RAG-and-hope; pure extraction (too rigid); LLM-judge as sole
guarantee (non-deterministic).
**Fragility flag:** If output is reclassified as clinical *decision support* → human-in-the-loop
sign-off + certified CDS become mandatory (largest possible design change).

## (6) Authorization — SMART App Launch (chat) + Backend Services (poller) + serve-time re-check
**Decision:** Chat reads as the physician (OpenEMR enforces); poller reads as a minimal-scope
system actor; every served answer/memory file is re-authorized against the clinician's
patient panel before disclosure. Refuse out-of-scope patients (UC-6).
**Why:** Makes OpenEMR the authorization authority; models the poller correctly.
**Benefits:** Broad read never becomes broad disclosure; clean trust-boundary story; injection
can't escalate access (enforced outside the LLM).
**Tradeoffs:** Nurse/resident-supervision roles not in v1.
**Alternatives considered:** Poller reading with a shared admin token (rejected — unscoped,
unauditable).
**Fragility flag:** Real multi-role/supervision requirements → extend via SMART scopes + OpenEMR
roles. "Authorized for patient" depends on care-team data being populated (thin in demo).

## (7) Observability & eval — Langfuse + correlation IDs; boundary/invariant/adversarial suite
**Decision:** Correlation ID on every invocation, threaded through all LLM/tool/verification/log
events. Dashboards: request/error/latency (p50/p95)/tool-calls/retries/**verification
pass-fail**/**staleness**. Alerts: p95 latency, error rate, tool-failure rate, poller staleness.
Eval: pytest, boundary + invariant + regression + adversarial (authz, injection) + domain-rule,
ground truth from the synthetic dataset, CI-integrated.
**Why:** Graded requirements; and eval ground truth is free because you generate the data.
**Benefits:** Full trace reconstruction from logs; staleness alert guards the top failure mode.
**Tradeoffs:** LLM-judge eval cases are non-deterministic — pin judge prompts, prefer
deterministic asserts.
**Alternatives considered:** LangSmith, Braintrust (fine; Langfuse chosen for familiarity/Python).
**Fragility flag:** Eval realism depends on synthetic-data realism — enrich if demo reveals gaps.

## (8) Hosting — docker-compose on one VM behind Caddy TLS (MVP) → orchestrated (prod)
**Decision:** Agent containers deploy on the **same infra** as OpenEMR via compose behind a TLS
reverse proxy; scale path is orchestrated (ECS/EKS), worker fleet, managed Postgres/Redis,
per-facility isolation, event-driven detection.
**Why:** Satisfies the brief's same-infra constraint; sprint-simple.
**Benefits:** Live, reachable, cheap to run; clear 300-user scale answer for the interview.
**Tradeoffs:** Single VM has no HA.
**Alternatives considered:** Managed PaaS (can conflict with same-infra); k8s from day one
(overkill for the sprint).
**Fragility flag:** An uptime/HA requirement forces the orchestrated path early.

## (9) UI — React chat panel via SMART App Launch, in-context OpenEMR module tab, SSE
**Decision:** Chat panel launched in patient context via SMART App Launch, embedded as an
OpenEMR module tab; streamed responses over SSE.
**Why:** The standard "embedded in the EHR" pattern; reuses the chat auth actor + patient context.
**Benefits:** True in-context integration; TS/React stays in your comfort zone.
**Tradeoffs:** iframe/CSP config; depends on OpenEMR launch-context support (present:
`SMARTAuthorizationController`, `PatientContextSearchController`).
**Alternatives considered:** Standalone SPA with manual patient select (the fallback if
in-context launch proves limited).

## Stage 0 — generate a synthetic clinical dataset (from the audit)
**Decision:** Before agent work, generate realistic clinical data (encounters, labs with
ranges/abnormal flags, meds, vitals trends, notes, a scripted overnight deterioration).
**Why:** Shipped demo data is demographics-only — nothing to synthesize otherwise.
**Benefits:** Unblocks the whole build; doubles as eval ground truth.
**Tradeoffs:** Upfront effort before any "AI" is visible.
**Fragility flag:** If it's not realistic enough, eval passes but demos expose gaps — enrich.
