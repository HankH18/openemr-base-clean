# AgentForge Clinical Co-Pilot — Access & Run Guide

Where to find each version of the app and how to run it. **No live secrets are
committed here** — this file points at where each credential lives; the actual
values are in gitignored `.env` files or handed off separately.

---

## Public demo (droplet)

- **App URL:** http://198.199.68.21/ (plain HTTP, bare IP — the default deploy).
  HTTPS at a real domain is prepared and opt-in: once a DNS A-record exists, copy
  `Caddyfile.https.example` → `Caddyfile` for automatic Let's Encrypt TLS (compose
  already publishes `:443`). See `DEPLOY.md` → "Cut over to HTTPS".
- **Guard:** HTTP basic-auth, username **`demo`** (password handed off separately;
  it lives only as a bcrypt hash in the droplet's gitignored `Caddyfile`). Rotate
  with `caddy hash-password` → update `/root/openemr-base-clean/Caddyfile` →
  `docker compose -f docker-compose.deploy.yml up -d caddy`.
- **Status:** the UI, the guard, and `/ready` (all four deps green incl. Langfuse)
  are live. **Live patient data (rounds/chat) is pending** the backend-OAuth +
  cohort seed on the droplet — see `DEPLOY.md` → "Finish live data on the droplet".
- **Observability:** Langfuse Cloud — https://cloud.langfuse.com (your
  *AgentForge-Gauntlet* project). Chat/rounds appear under Tracing → Traces,
  keyed by the `X-Correlation-ID` response header. A **self-hosted** alternative
  (keeps PHI-adjacent traces on the droplet) is built into the deploy compose,
  off by default behind the `observability` profile — bring it up with
  `--profile observability`, access the UI via SSH tunnel (not publicly exposed),
  and set `LANGFUSE_HOST=http://langfuse:3000`. See `agent/LANGFUSE_SETUP.md`
  → "Self-hosted (droplet)".
- **Ingress model:** Caddy is the sole public listener on `:80`. It serves the
  React SPA and reverse-proxies `/v1/*`, `/health`, `/ready`, `/docs`,
  `/openapi.json` → internal `agent:8000`. OpenEMR is **not** publicly exposed.
- **Ops:** `ssh root@198.199.68.21`; deploy dir `/root/openemr-base-clean`;
  `docker compose -f docker-compose.deploy.yml ps`. Secrets live in that dir's
  gitignored `.env` (OpenEMR admin `OE_USER`/`OE_PASS`, DB passwords, Anthropic +
  Langfuse keys) and `secrets/backend-key.pem`. OpenEMR's own admin UI is
  internal-only (reach it via an SSH tunnel or a temporary port publish if needed).

---

## Run locally

### First-time setup
```bash
# Python agent
cd agent && uv venv --python 3.12 && source .venv/bin/activate && uv pip install -e '.[dev]'

# Local OpenEMR (system of record) — app at http://localhost:8300 / https://localhost:9300
cd docker/development-easy && docker compose up -d --wait      # login: admin / pass

# One-time: register the backend-services client + write the local key
cd agent && ./scripts/register_backend_client.py \
  --base-url https://localhost:9300 --out-key secrets/backend-key.pem --insecure
```

### Option A — mock UI (no backend, fully reproducible; this is what the demo script films)
```bash
cd agent/web && npm install && npm run dev      # http://localhost:5173  (built-in 5-patient cohort)
```

### Option B — live (real agent + Claude + local OpenEMR FHIR)
```bash
# 1. Local OpenEMR up + seeded (see scripts/seed/).
# 2. Start the agent (sources the gitignored live config, then runs uvicorn):
cd agent && set -a && . ./.env.local && set +a && ./.venv/bin/uvicorn copilot.api.app:app --port 8000
# 3. Point the UI at the live agent:
cd agent/web && VITE_API_BASE_URL=http://localhost:8000 npm run dev   # http://localhost:5173
```
- Health check: `curl http://localhost:8000/ready`
- API docs: `http://localhost:8000/docs`
- Local FHIR target: `https://localhost:9300/apis/default/fhir` (HTTPS — SMART/OAuth
  need TLS; `COPILOT_TLS_VERIFY=false` for the self-signed dev cert).

### Where local credentials live
- `agent/.env.local` (gitignored) — Anthropic key, local OAuth client id, model ids,
  Langfuse keys, patient-id template, DB URL, poller switch. Sourced into the shell
  (not read by pydantic's `.env` mechanism), then read from the environment.
- `agent/secrets/backend-key.pem` (gitignored) — local backend-services private key.
- Local OpenEMR login: `admin` / `pass` (standard dev default).

---

## Quick reference

| | Public (droplet) | Local |
|---|---|---|
| Co-Pilot UI | http://198.199.68.21/ (guard `demo` / handoff) | http://localhost:5173 |
| Agent API | same-origin `…/v1/*`, `/ready`, `/docs` | http://localhost:8000 |
| OpenEMR | internal only | http://localhost:8300 · https://localhost:9300 (admin/pass) |
| Langfuse | cloud.langfuse.com (AgentForge-Gauntlet); self-host opt-in via `--profile observability` (SSH tunnel) | same (if keys set in `.env.local`) |
| Secrets | droplet `/root/openemr-base-clean/.env` + `secrets/` + `Caddyfile` | `agent/.env.local` + `agent/secrets/` |
