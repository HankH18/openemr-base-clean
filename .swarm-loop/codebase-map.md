# Codebase Map — AgentForge Clinical Co-Pilot (`agent/`)

> Build target of this loop: the **Python agent service** in `agent/`. The OpenEMR
> PHP monolith around it is the (unmodified) system of record and is NOT the build
> target. All metric commands run against `agent/` via its `.venv` (Python 3.12).
> Source of truth for intended behavior: `../ARCHITECTURE.md` + `../USERS.md` (UC-1…UC-7).

## Stack & tooling (verified cycle 0)
- Python **3.12** in `agent/.venv` (uv). Package = `copilot`. Build backend: hatchling.
- FastAPI 0.115 · Pydantic v2 · SQLAlchemy 2 (async) · Alembic · httpx · anthropic SDK
  (used directly — **pydantic-ai is NOT a dependency** despite the README blurb) ·
  APScheduler · authlib · langfuse.
- **Run tests:** `cd agent && ./.venv/bin/python -m pytest -q` (runs `tests/` + `evals/`).
  All deterministic on **in-memory SQLite**; `respx` mocks OpenEMR HTTP. No Postgres/
  OpenEMR/Anthropic key needed. pytest config: `asyncio_mode=auto`, `testpaths=[tests,evals]`,
  `filterwarnings=["error"]` (**warnings are errors**), `--strict-markers`.
- **Coverage:** `./.venv/bin/python -m pytest --cov=copilot --cov-report=term`.
- **Lint:** `./.venv/bin/ruff check .` (+ `ruff format --check .`). **Type:** `./.venv/bin/mypy copilot` (strict).
- **CI** (`../.gitlab-ci.yml`): one job `agent:tests` → `pytest -q --junitxml=../pytest.xml`,
  runs only on `agent/**` changes. **No lint/mypy/coverage in CI.**
- Config: `Settings` (`copilot/config.py`), env prefix **`COPILOT_`** (e.g. `COPILOT_ANTHROPIC_API_KEY`),
  every field has a default so the process always boots. Postgres URL default falls back to
  in-memory sqlite.

## Baseline (cycle 0, measured)
- Tests: **107 passed, 2 skipped** (the 2 skips are `@pytest.mark.llm` live-Claude eval cases).
- Coverage: **89%**. ruff: **75** findings. mypy strict: **18** errors (6 files). 16 files unformatted.

## Components (`copilot/`)
- **`api/`** — FastAPI app. `create_app(settings, probe_factories)` (`api/app.py:44`), module ASGI
  `app` (`:87`); Dockerfile runs `uvicorn copilot.api.app:app`. **Only routes today: `GET /health`,
  `GET /ready`** (4 readiness probes: Postgres SELECT 1, OpenEMR `/metadata`, LLM key-presence,
  Langfuse cred-presence). **No chat/rounds/serve routes, no SSE, no per-request correlation-ID
  middleware, no Observability injected, no lifespan hook.**
- **`fhir/`** — async FHIR client + OAuth. `FhirClient.read/search/count_since` (`client.py`),
  bearer+accept headers, one 401 force-refresh retry. `count_since` does the
  `_lastUpdated=gt…&_summary=count` change query. `SmartAppLaunchTokenProvider` (auth-code + refresh;
  **browser redirect/code capture is delegated to the API layer, unbuilt**) and
  `BackendServicesTokenProvider` (client_credentials + private_key_jwt RS384/ES384). `StaticTokenProvider`
  for tests. **Gaps:** no full-patient assembler, no Bundle pagination (first page only), no PKCE,
  retry only on 401.
- **`worker/`** — `synthesizer.py`: `LlmSynthesizer` protocol with `ClaudeSynthesizer` (live, **raises if
  no key**) and `StubSynthesizer` (deterministic, one claim/resource — the test/eval workhorse).
  `poller.py`: `Poller.tick` = count-gate → hash-confirm (`hashing.py`) → synthesize; returns
  `PollerResult`; **does NOT persist or verify** (by design). `scheduler.py`: `PollerScheduler`
  (APScheduler) calls `poller.tick` then an injected `on_result` callback. **No process starts the
  scheduler; no `on_result`, no `active_patients` source, no `__main__`/lifespan — the background loop
  never actually runs or persists anything yet.**
- **`verification/`** — `core.py` `Verifier.verify_memory_file`: per-claim **attribution** (source_ref
  present in context) + **value match** (exact string / numeric-equal + "extra number in text"
  detection); fail-closed action mapping: all pass→`served`, none pass→`withheld`, mixed→`degraded`
  (empty claims→`served` so domain flags surface). `rules.py`: `allergy_medication_conflict` (curated
  PCN/sulfa/NSAID classes, naive substring) + `critical_lab`/`abnormal_lab` (US Core interpretation
  codes + OpenEMR `abnormal` fallback). `entailment.py`: optional live-LLM entailment, **off by default,
  advisory only (not gating)**. **The whole package has NO production caller** — invoked only in
  tests/evals. Serve-time `verify_answer(claims, patient_id, fhir_client)` is **documented in
  `__init__.py` but NOT implemented.**
- **`memory/`** — `models.py`: all **7 tables** (`memory_file`, `sync_state`, `last_seen`,
  `rounding_cursor`, `conversation`, `message`, `audit_log`). `repository.py` `MemoryRepository`:
  implements only `get/upsert_sync_state`, `get/save_memory_file`, `record_audit`. **No repo methods
  for conversation, message, last_seen, rounding_cursor** (tables + models exist, no API, no callers).
  `db.py`: async engine, `session_scope()`, `JSONType` (JSONB on PG / JSON on sqlite). Migrations:
  real baseline `0001_baseline.py` (all 7 tables); tested up/down on **SQLite only**.
- **`domain/`** — `primitives.py`: frozen Pydantic value objects `PatientId`, `ClinicianId`,
  `CorrelationId`, `FhirReference`, `ResourceType` enum (9 R4 types). `contracts.py`: `Claim`,
  `MemoryFileSummary`, `LabResult`, `MedicationList`, **`PatientCard`+`Freshness`**, `VerificationResult`
  family, health/readiness models. Contracts are the source of truth for tool I/O shapes.
- **`observability/`** — `Observability` protocol; `NoopObservability` + `LangfuseObservability`;
  **runtime factory `build_observability(settings)`** auto-selects (noop unless all 3 langfuse creds set);
  correlation-ID `ContextVar` plumbing. (This factory pattern is the model to copy for the LLM/agent.)

## Data model highlights (agent-owned Postgres; see `../ARCHITECTURE.md#data-model`)
- `memory_file`: per-patient summary (JSONB list of `Claim{text, source_ref:{resource_type,id,field,value},
  last_updated}`), `acuity_score` (0–10), `rank_reason`, `source_watermark`, `content_hash`, `stale`.
- `sync_state`: per-patient watermark + hash + `consecutive_failures` (poller-written).
- `last_seen` (clinician,patient) = the "done" marker. `rounding_cursor` = ordered ids + index + completed.
- `conversation`/`message` = chat history (patient-scoped, has correlation_id). `audit_log` = append-only.

## Intended HTTP contract (from ARCHITECTURE, to be built)
`POST /v1/rounds/start` · `GET /v1/rounds/current` · `POST /v1/rounds/advance` ·
`POST /v1/chat` (SSE, grounded, per-claim source_ref) · `GET /v1/rounds/alerts` · `/health` · `/ready`.

## Conventions (follow these in new code)
- Strict typing; frozen Pydantic value objects; protocol + injected-impl seams (see synthesizer,
  observability) so everything is testable without live deps. **Fail-closed** on missing key / missing
  data / tool failure — never "assume true on error". No PHI in logs (IDs + correlation IDs only).
- New deterministic tests over in-memory sqlite + respx; inject fakes rather than hitting network.
- Keep the LLM out of the trust path: authorization, verification pass/fail, ranking are deterministic code.

## Hazards / hotspots (single-writer per cycle; watch for merge collisions)
- **`copilot/api/app.py`** — every API feature wants to touch route registration / app wiring.
  Prefer APIRouter-per-feature files imported into `create_app`; treat `app.py` as additive-only.
- **`copilot/config.py`** — new settings land here; single writer per cycle.
- **`copilot/domain/contracts.py`** — shared contracts; additive-only, single writer per cycle.
- **`copilot/memory/repository.py` & `models.py`** — many features need new repo methods; sequence them.
- **`pyproject.toml`** (deps) & **Alembic migration chain** (`down_revision`) — NEVER parallelize;
  dedicated sequenced task only. Two branches adding migrations both as rev 0002 will collide.
- Acceptance/goal harness lives under `.swarm-loop/` — **off-limits to all workers** (frozen).
