# RUNLOG — Early Submission

Running log of what got built, what decisions were made, what needed a human
in the loop. One entry per work unit; append as you go.

## Ground rules being observed

- Commit per working unit; push to `gitlab` remote after each.
- Tests must pass before advancing to the next unit.
- No deploys, no secrets typed in, no touching the droplet from this loop.
- Stay within `ARCHITECTURE.md`. If reality contradicts it, note it and stop
  rather than diverging.
- If stuck ~3 attempts, stop and record; don't hack.

## Operator-action queue (things I stopped for)

_Empty as of Unit 1 start._ Anticipated future entries:

- **Anthropic API key** for Claude — needed to actually run synthesis /
  verification-entailment / chat. Env var: `ANTHROPIC_API_KEY`. Until it is
  set, the LLM abstraction returns a stub and eval cases requiring live
  Claude are skipped.
- **Langfuse credentials** — `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`,
  `LANGFUSE_SECRET_KEY`. Until set, observability is a no-op.
- **OpenEMR SMART client registrations** — one **SMART App Launch** confidential
  client (for interactive chat, physician-delegated) and one **SMART Backend
  Services** client (`client_credentials` JWT-assertion, `system/*.read`) for
  the poller. Registrations must be done in the OpenEMR admin UI; the client
  IDs + JWKs URL go into env vars, not into the repo.

---

## Unit 1 — Agent service scaffold + Postgres migrations

**Status:** ✅ complete. 23 tests pass; compose validates.

**What shipped**
- `agent/` Python package: `copilot/` (`api`, `domain`, `memory`), tests,
  Alembic migrations, Dockerfile, `pyproject.toml` with dev extras.
- FastAPI app factory (`create_app`) with fully injectable readiness
  probes. `/health` returns 200 while the process is alive; `/ready`
  returns 200 only when Postgres + OpenEMR FHIR + LLM + Langfuse all
  probe green (503 with per-dep detail otherwise) — matches ARCHITECTURE
  §"Interfaces & contracts".
- Pydantic v2 domain layer: typed `PatientId` / `ClinicianId` primitives
  (positive-int, frozen), closed `ResourceType` enum, `FhirReference`
  (structured source pointer for every claim), and the full contract set
  called out in ARCHITECTURE (`Claim`, `LabResult`, `MedicationList`,
  `MemoryFileSummary`, `VerificationResult`, etc.).
- SQLAlchemy 2 async models for all seven ARCHITECTURE tables
  (`memory_file`, `sync_state`, `last_seen`, `rounding_cursor`,
  `conversation`, `message`, `audit_log`). `JSONType` decorator maps JSON
  → JSONB on Postgres, plain JSON on SQLite so unit tests are portable.
- Alembic baseline migration (`0001_baseline`); Alembic env reads DB URL
  from `Settings` so no URL ever lives in a checked-in file.
- Deploy compose gains a `postgres:16-alpine` service (`agent-postgres`,
  agent-only network, no host publish) and a `copilot-api` build/run
  service.  `.env.deploy.example` gains the corresponding vars.

**Decisions**
- SQLite (`aiosqlite`) as the default `COPILOT_DATABASE_URL` so tests run
  hermetically — Postgres integration checks live in the compose stack.
- `path_separator = os` in `alembic.ini` (silences Alembic deprecation).
- Bumped `respx` to `>=0.22` — 0.21 doesn't work with `httpx>=0.28`
  (transport-internals change).  Same version bump the wider Python
  ecosystem is already on.
- Added `greenlet>=3` explicitly and used `sqlalchemy[asyncio]` extras.
  Without them, `AsyncEngine.dispose()` blows up under 3.12.
- `/ready` refuses to say ready until LLM + Langfuse credentials are
  present, per the "not an unconditional 200" line in ARCHITECTURE.
- Kept probes sequential inside `/ready` so a slow probe doesn't blur
  which dep caused the delay in traces.
- Reserved `agent` compose network already present from Task 6 — the
  agent joins it without a network rewrite.

**Tests (23 pass)**
- Domain primitive validation (positive-int PIDs, frozen, closed enum).
- Health + ready endpoints with injected probe stubs (ok / partial-fail /
  ordering).
- Real probes: Postgres OK against in-memory SQLite; Postgres fail on
  bad URL; OpenEMR FHIR OK/500/connect-error via `respx`; LLM ok if key
  set; Langfuse requires all three env vars.
- Alembic upgrade-head → downgrade-base round-trip against SQLite.

**Deferred / next**
- Postgres-native migration test via `testcontainers` — deferred to
  when we wire CI in Unit 5.
- Live Anthropic + Langfuse pings inside their probes — added when
  Units 3 and 6 wire the real SDKs.
