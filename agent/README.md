# copilot — Clinical Co-Pilot agent service

Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy 2 · Anthropic Claude.

Reads OpenEMR **only through its FHIR/REST API** using two OAuth actors:

- **SMART App Launch** — physician-delegated tokens for interactive chat.
- **SMART Backend Services** — `client_credentials` JWT assertion with
  `system/*.read` scopes for the background poller.

See `../ARCHITECTURE.md` for the full design and rationale.

## Layout

```
copilot/
  api/            FastAPI routes: /health, /ready, /v1/rounds/*, /v1/chat
  domain/         Pydantic v2 contracts + typed domain primitives
  fhir/           Async httpx FHIR client + OAuth token acquisition
  memory/         SQLAlchemy models + repository for the agent-owned DB
  verification/   Deterministic citation + numeric-match gate + domain rules
  worker/         APScheduler poller — change-gated synthesis loop
migrations/       Alembic migration scripts (target: PostgreSQL 16)
tests/            pytest suite
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
