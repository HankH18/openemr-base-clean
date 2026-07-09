# Backlog — AgentForge Clinical Co-Pilot E2E

Atomic features for the swarm. Each: ID, objective, deps, owned files, UC + metric mapping.
Metric groups (frozen): `feat_chat`, `feat_rounds`, `feat_authz`, `feat_background`.
Priority is re-derived every cycle from the latest `analyze` verdicts (Phase 8).

Legend: `[metric:X]` = contributes to that frozen feature metric · `[deliverable]` = built but not
a frozen metric (validated by smoke/build) · `[quality]` = maps to global lint/coverage metrics.

---

## Enablers (cycle 1 — foundational, no analysis yet → dependency order)

### E0 — Mechanical lint/format + mypy cleanup  [quality]
- Objective: `ruff format .`, `ruff check . --fix`, and fix the 18 strict-mypy errors so the tree is
  clean and every feature branch forks from a formatted base. **Solo wave (wave 0)** — repo-wide, no
  concurrent branches (a broad reformat collides with everything).
- Owns: broad, but ALONE. Deps: none. Drives `lint_type_errors` toward 0.

### E1 — Runtime agent factory + deterministic StubAgent  [metric:feat_chat]
- Objective: `build_agent(settings, fhir_client, ...)` selecting a live Anthropic tool-use loop vs a
  deterministic `StubAgent` by `COPILOT_ANTHROPIC_API_KEY` presence (mirror `build_observability`).
  StubAgent: given (patient_id, message, fetched FHIR resources) emit `Claim`s with real `source_ref`s
  (reuse the StubSynthesizer pattern) — deterministic, no key. This is what makes every chat/rounds
  acceptance test run green without a key.
- Owns: new `copilot/agent/{__init__,base,factory,stub,loop}.py`. Deps: none. Wave 1.

### E2 — Serve-time verification `verify_answer()`  [metric:feat_chat]
- Objective: implement the documented-but-absent serve-time path: `verify_answer(claims, patient_id,
  fhir_client) -> VerificationResult` that re-fetches cited resources by ID and runs the existing
  `Verifier`; fail-closed (unverifiable → withheld/degraded).
- Owns: new `copilot/verification/serve.py` (additive; do NOT restructure `core.py`). Deps: none. Wave 1.

### E3 — Repository extension (conversation/message/cursor/last_seen)  [metric:feat_chat,feat_rounds]
- Objective: add `MemoryRepository` methods: create_conversation, append_message, get_conversation
  (history); get/upsert rounding_cursor; get/set last_seen. Tables/models already exist.
- Owns: `copilot/memory/repository.py` (single writer this cycle). Deps: none. Wave 1.

### E4 — Correlation-ID middleware + Observability injection + config  [metric:feat_chat; quality]
- Objective: ASGI middleware generating/attaching a correlation ID per request; inject
  `build_observability(settings)` into `create_app`; add chat/agent settings to `Settings`.
- Owns: `copilot/api/app.py`, new `copilot/api/middleware.py`, `copilot/config.py`. Deps: none. Wave 1.

---

## Feature: Grounded chat (UC-2, UC-7)  [metric:feat_chat]

### C1 — `POST /v1/chat` endpoint + grounded response  (wave 2, cycle 1)
- Objective: route `{clinician_id,patient_id,message,correlation_id?}` → agent (E1) drafts claims →
  serve-time verify (E2) → JSON `{answer, claims:[{text,source_ref}], verification:{passed,action},
  conversation_id, correlation_id}`. Fail-closed. Deps: E1,E2,E3,E4. Owns: new `copilot/api/routes/chat.py`
  (+ additive router import in `app.py`).
### C2 — Multi-turn conversation persistence + `GET /v1/conversations/{id}`
- Objective: persist each turn (E3); return conversation_id; history retrievable. Deps: C1.
### C3 — Graceful uncertainty (UC-7)
- Objective: fully-unverifiable question → `action=withheld`, answer surfaces "can't confirm" + source
  pointer, never a fabricated value. Deps: C1.

## Feature: Rounds + ranking (UC-1, UC-3, UC-4)  [metric:feat_rounds]

### R1 — `POST /v1/rounds/start` / `GET /v1/rounds/current` — PatientCard by acuity
### R2 — `POST /v1/rounds/advance` — set last_seen, return next card; cursor persists across reload
### R3 — Deterministic acuity ranking + `rank_reason` evidence (interrogable, UC-4)
- Deps: E3 (cursor/last_seen), and memory files present (F-BG). Owns: new `copilot/api/routes/rounds.py`,
  new `copilot/rounds/ranking.py`. (rounds routes single-writer.)

## Feature: Authorization boundary (UC-6)  [metric:feat_authz]

### A1 — Serve-time authz re-check: refuse patient not on clinician's authorized/rounding list (403 + reason)
### A2 — Cross-patient isolation: chat scoped to patient A cannot surface patient B data
- Deps: C1/R1 exist. Owns: new `copilot/auth/authorization.py` + additive guards in route files
  (coordinate single-writer per route file).

## Feature: Background loop + persistence + alerts (UC-5)  [metric:feat_background]

### B1 — Poller runtime wiring: `on_result` verifies + persists memory files; `active_patients` source; lifespan starts scheduler
### B2 — `POST /v1/rounds/refresh` (manual tick) + freshness/staleness reflected in cards
### B3 — `GET /v1/rounds/alerts`: preempt offer when a not-yet-seen patient crosses the deterioration threshold
- Deps: E1,E2,E3. Owns: `copilot/worker/*` (wiring), new `copilot/api/routes/alerts.py`, lifespan in `app.py`.

---

## Deliverables (not frozen metrics)

### UI1 — React SMART-launch chat panel  [deliverable]
- Objective: minimal but **polished** React panel consuming `/v1/rounds/*` + `/v1/chat`. Design system:
  praised-but-not-overexposed OSS (NOT MUI/Chakra/Ant/shadcn) — final pick via landscape scan at build
  time; leaning Radix Themes / React Aria Components / Park UI. Validated by a smoke build, not a metric.
- Deps: chat + rounds API green. Owns: new `agent/web/` (or `copilot-web/`). Scheduled late (cycle ≥4).

## Quality backlog (slotted only behind converging metrics; quality-review cycles 3 & 6)
- Domain-rule enrichment: reference-range numeric checks; med reconciliation (lists vs prescriptions).
  FHIR Bundle pagination. Postgres-backed migration test. These map to feature/coverage metrics when touched.
