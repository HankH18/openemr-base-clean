# Codebase map — Week 2 multimodal evidence agent

Brownfield build on the accepted Week 1 Clinical Co-Pilot. Spec: `/W2_ARCHITECTURE.md`
(+ ideation set `agent/research/week2/`). This map is what task packets quote from — keep compact + accurate.

## Stack & layout
- **Backend**: Python 3.12, FastAPI `>=0.115,<0.116`, Pydantic v2, SQLAlchemy 2 async, Postgres (prod) /
  SQLite (tests, aiosqlite), Anthropic SDK `>=0.40,<1` (pinned <1), APScheduler, **Langfuse v2** (`<3` — v3
  breaks silently), httpx, cryptography, authlib, **pgvector 0.5** (new, Week 2).
- **Agent package**: `agent/copilot/` — subpkgs: `agent/` (chat loop), `api/` (+`routes/`), `fhir/`,
  `writeback/`, `verification/`, `rounds/`, `chat/`, `worker/` (poller, NOT LLM workers),
  `observability/`, `memory/`, `domain/`, `auth/`.
- **Frontend**: `agent/web/` — React 18 + Vite 6 + TS + React Aria Components (only UI lib), hand-written
  CSS tokens, hand-rolled SVG (no chart/PDF lib). Mock vs live via `VITE_API_BASE_URL`.
- **Deploy**: single droplet, `docker-compose.deploy.yml`, Caddy sole ingress (`/v1/* /health /ready
  /openapi.json /docs` → agent:8000; SPA static from `agent/web/dist`). `agent-postgres` image must
  become `pgvector/pgvector:pg16` for prod.

## Entry points
- API app factory: `copilot/api/app.py` → `create_app(settings, probe_factories)`. **Routers auto-mount**:
  any module in `copilot/api/routes/` exposing `router: APIRouter` is included (`register_routers`,
  app.py:38) — drop a file, no app.py edit. `/health` (app.py:140), `/ready` (app.py:148, injectable
  probe factories in readiness.py).
- Chat agent loop: `copilot/agent/claude.py` `ClaudeAgent.answer` (hand-rolled Anthropic tool loop,
  `_MAX_TOOL_ITERATIONS=6`, `_TOOLS` plain dicts, arg-less). Protocol `agent/base.py`; `build_agent`
  factory `agent/factory.py` picks StubAgent vs ClaudeAgent by API-key presence.
- Poller: `copilot/worker/` (APScheduler). NOT the Week-2 "workers" — new graph goes in NEW `copilot/graph/`.

## Data model (agent DB — `copilot/memory/`)
- `db.py`: `Base`, `JSONType` (JSONB on PG / JSON on SQLite), `AutoIncBigInt`, `_utc_default` (naive-UTC),
  `_utc_aware_default` (auth tables), **`embedding_column(dim)`** (pgvector `Vector` on PG / JSON on SQLite),
  `EMBEDDING_DIM=1024`. `session_scope()` = commit-on-success ctx mgr. `MemoryRepository` (repository.py)
  is the ONLY DB gateway ("contracts in, contracts out; no SQL leaks"); hand-written `_x_to_json`/
  `_x_from_json` serializers with `.get()` back-compat defaults.
- Tables (migrations `0001`–`0006`): memory_file, sync_state, last_seen, rounding_cursor, conversation,
  message, audit_log(+entry_mode), clinician, physician_session, login_txn, **[Week2 0005]**
  source_document, document_page, extraction, extracted_fact, **[Week2 0006]** guideline_document,
  guideline_chunk(embedding). Claims persist as JSON inside `memory_file.summary` (NO claims table).
- **Migrations chain 0001→0006 verified** (alembic upgrade clean). New migration → `0007`, chain off `0006`.

## Verification (the load-bearing invariant — `copilot/verification/` + `domain/`)
- `Claim.source_ref: FhirReference` (`domain/contracts.py`, `domain/primitives.py`). FhirReference =
  (resource_type, resource_id, field, value, last_updated, timestamp). **Code builds the ref, not the model**
  (claude.py `_build_answer` + `agent/grounding.py`).
- Gate: `verification/core.py` `Verifier._verify_claim` — attribution + value-match (+numeric-in-text) +
  temporal. Serve-time re-fetch by ID (`verification/serve.py` `verify_answer`) → **fail-closed**; unverifiable
  claim dropped. "no grounded claims → withheld" override lives in `chat/service.py` (NOT the verifier).
  `ResourceType` is a **closed StrEnum** (`primitives.py`) — no DocumentReference/Binary.
- **Week 2 change**: evolve source_ref → discriminated union `{fhir|document|guideline}` (back-compat default
  `fhir`); add document-grounding (re-check vs stored extracted_fact + bbox≥threshold) + guideline
  (quote-in-chunk) paths. Preserve fail-closed. Fix repository.py (de)serializers for the union.

## Write path (`copilot/fhir/write_client.py` + `writeback/service.py`)
- `OpenEmrWriteClient` uses OpenEMR **Standard REST API** (`…/apis/default/api`). Currently writes vitals(8
  closed metrics)+meds+encounters. Gated OFF (`settings.writeback_enabled=False`). Propose→confirm gate
  (`WriteService`), `agent_proposed_physician_confirmed` entry mode reserved.
- **Verified in OpenEMR routes** (`apis/routes/_rest_routes_standard.inc.php`): document upload =
  `POST /api/patient/:pid/document` (multipart `$_FILES['document']`, `path` category, `eid`); also
  `medical_problem`+`allergy` are writable. **Labs/Observation NOT writable via any API** → doc-derived labs
  stay agent-store-grounded. Week 2: add `upload_document(...)` + extend to problems/allergies.

## Observability (`copilot/observability/`)
- `Observability` Protocol + Noop/Langfuse dual (`factory.py`). **Traces are FLAT** — `span()` opens a NEW
  top-level trace keyed by correlation-id (`langfuse_backend.py`); Week 2 must add nesting (worker child spans).
  `pricing.py` static rate card (add vision+Voyage+Cohere). Correlation-id middleware `api/middleware.py`
  (ContextVar `current_correlation_id()` + `X-Correlation-ID`). **JSON logging declared but NOT wired**
  (`python-json-logger` dep, no dictConfig) — Week 2 wires it.

## Conventions (task packets MUST follow)
- New file starts `from __future__ import annotations`; strict native types; `declare` N/A (Python).
- **Stub/Real dual behind a `Protocol` + `build_*` factory**, keyed on API-key presence, so keyless CI stays
  green. Mirror this for every new agent/worker/client (see `agent/factory.py`, observability/factory.py).
- **Tool-forced JSON** for new LLM extraction (a deliberate departure — current code uses prompt-instructed
  JSON + tolerant slice). Schema (Pydantic) is source of truth; raw VLM output never bypasses validation.
- PSR-3-style: no PHI (or raw doc text / extracted clinical values) in logs/traces/evals. Single
  `deidentify()` choke-point before Voyage/Cohere egress + logging.
- Cost/usage: reuse `observability/pricing.cost_usd` + `_usage_tokens`/`_extract_text` helpers.

## Test layout & tooling
- `agent/tests/` + `agent/evals/`. `pytest` `testpaths=["tests","evals"]`, **`filterwarnings=["error"]`**
  (pin new deps, clear warnings or tests fail), `asyncio_mode=auto`, marker `llm` (skipped w/o key).
- Deterministic eval runner `evals/run_evals.py` (real app + `evals/_fake_openemr.py` respx + StubAgent, NO
  live LLM, boolean assertions, exits nonzero on fail — 11 cases today; grow to 50). Schema built via
  `Base.metadata.create_all` (not alembic) in the harness.
- **Agent venv**: `agent/.venv` (Python 3.12.13, uv-managed, NO pip — use `uv pip install --python
  .venv/bin/python`). Run gates: `.venv/bin/{ruff check, mypy, pytest}` from `agent/`. `openemr-cmd` NOT on host.
- Reusable acceptance-harness infra exists in `.swarm-loop-week1-archive/acceptance/` (run.py, conftest.py,
  _fake_openemr.py, build_check.py, project_tests.py, quality_count.py) — COPY + adapt for Week 2.

## Hazards / hotspots (single-writer per cycle; sequence collisions)
- **`copilot/config.py`** (Settings) — everyone adds fields. Centralize new config fields in ONE task/wave.
- **`copilot/api/app.py`** — auto-router means new routes DON'T touch it (additive files); probe factories do.
- **`domain/primitives.py` + `domain/contracts.py`** — the citation-union change is a hotspot; one owner.
- **`copilot/memory/models.py` + `repository.py`** — schema + serializers; one owner per cycle.
- **`copilot/verification/core.py` + `serve.py`** — the fail-closed gate; highest-risk; one owner, heavy tests.
- **`agent/pyproject.toml`** — dependency changes get their OWN sequenced task, never parallel.
- **`agent/web/src/api/types.ts`, `state/*`, `api/client.ts`** — frontend contract hotspots.
- The **doc-archive guard**: `archive/**` is read-blocked; `.swarm-loop-week1-archive/` is NOT under it (readable).
- pgvector migration does `CREATE EXTENSION vector` (PG-only, dialect-guarded) — prod needs the pgvector image.
