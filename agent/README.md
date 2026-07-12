# copilot — Clinical Co-Pilot agent service

Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy 2 · Anthropic Claude (synthesis/chat
`claude-sonnet-5`, gating/entailment `claude-haiku-4-5`).

Reads OpenEMR **only through its FHIR/REST API** using two OAuth actors:

- **SMART App Launch** — per-physician delegated tokens for interactive reads and writes.
  Enabled with `COPILOT_AUTH_MODE=smart` (default `disabled`, which takes identity from the
  request — the no-login demo); in `smart` mode every data route takes identity from an opaque
  server-side session, and reads/writes ride the logged-in physician's own token.
- **SMART Backend Services** — `client_credentials` JWT assertion with `system/*.read` scopes
  for the background poller (always a scoped system actor, independent of any login).

See `../ARCHITECTURE.md` for the full design and rationale.

## Layout

```
copilot/
  api/            FastAPI routes: /health, /ready, /v1/auth/*, /v1/rounds/*,
                  /v1/chat (+/v1/conversations/{id}), /v1/writes (+confirm),
                  /v1/patients/{id}/observations
  agent/          Claude tool-loop agent + deterministic stub + grounding
  auth/           Per-physician SMART session, identity, authorization (auth_mode)
  chat/           Grounded chat service
  rounds/         Acuity ranking + rounding-session service + chart summary
  domain/         Pydantic v2 contracts + typed domain primitives
  fhir/           Async httpx FHIR client + OAuth token acquisition (read + write)
  memory/         SQLAlchemy models + repository (append-only audit, retention sweep)
  verification/   Deterministic citation + numeric/temporal match gate + domain rules
  writeback/      Physician write-back service — propose → confirm (flag-gated, OFF)
  observability/  Langfuse backend + token pricing (no-op without keys)
  worker/         APScheduler poller — change-gated synthesis loop
migrations/       Alembic migration scripts (target: PostgreSQL 16)
tests/            pytest suite (511 passing)
```

## Local dev (Mac)

```bash
cd agent
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e '.[dev]'
pytest -q
```

## Config

Every config value is read via `copilot.config.Settings` (Pydantic settings).
Nothing hardcoded — see `.env.example` at the repo root and the operator
queue at the top of `RUNLOG.md` for which secrets require human setup.

## Running the API locally

```bash
uvicorn copilot.api.app:app --reload --port 8000
curl -s http://localhost:8000/health   # liveness only
curl -s http://localhost:8000/ready    # depends on Postgres + OpenEMR
```

Without a live Postgres or OpenEMR, `/ready` returns 503 with a JSON body
naming the unreachable dependencies. `/health` returns 200 as long as the
process is alive.
