# AgentForge Clinical Co-Pilot — Access & Run Guide

Where to find each version of the app and how to run it. **No live secrets are
committed here** — this file points at where each credential lives; the actual
values are in gitignored `.env` files or handed off separately.

---

## Public demo (droplet)

- **App URL:** https://agentforge.hankholcomb.com (HTTPS via Caddy + automatic
  Let's Encrypt TLS — a valid cert, no TLS/certificate warning). The old
  `http://198.199.68.21` bare-IP URL is retired.
- **⚠ Chrome "Dangerous site" warning — Google Safe Browsing FALSE POSITIVE (review pending).**
  Google Safe Browsing has flagged `agentforge.hankholcomb.com` as suspected phishing/social
  engineering, so Chrome (and Safari/Firefox, which share the list) may show a red **"Dangerous
  site"** interstitial. It is a **false positive**: the site is a self-hosted OpenEMR SMART login
  (a password form + clinical branding) on a new personal-domain subdomain with no reputation —
  the exact shape an automated phishing classifier over-flags. Only **synthetic demo data** is
  served; nothing malicious. A correction was reported to Google on **2026-07-19** and a review
  is pending (a clean false positive typically clears in hours–days). **To proceed now:** click
  **Details → "visit this unsafe site"**, or watch the demo video (a recording sidesteps the
  interstitial entirely). The flag is per-host, so the retired bare IP `http://198.199.68.21/`
  is an unflagged fallback if a clickable link is needed.
- **Access:** **per-physician SMART sign-in** (`COPILOT_AUTH_MODE=smart` on the
  droplet) — there is **no basic-auth guard**. Loading the URL lands on the OpenEMR
  SMART sign-in; sign in with the **OpenEMR admin credentials** (`OE_USER` /
  `OE_PASS` from the droplet's gitignored `.env`, handed off separately — never in
  git), approve the consent screen, and you're authenticated as that clinician —
  identity, reads, and any writes run under that delegated token. The OpenEMR login
  page carries an AgentForge restyle (`custom/assets`: "AgentForge — Clinical
  Co-Pilot" wordmark, accent sign-in button, patient-login/email hidden), served
  publicly because Caddy proxies `/custom/*` for the login page.
- **Retrieval keys (by design):** the public demo runs **real Claude vision** (Anthropic
  key set) but the RAG **embeddings + rerank are the keyless deterministic stubs** —
  `VOYAGE_API_KEY`/`COHERE_API_KEY` are intentionally not set on the droplet, so `/ready`
  reports `embedder`/`reranker` = `stub (keyless)` (advisory, still serving). Retrieval still
  returns grounded, cited guideline evidence; set those two keys to run real Voyage/Cohere.
- **Status:** the UI, per-physician SMART login, live patient data (rounds/chat over
  the seeded 15-patient census), and `/ready` (all deps green incl. Langfuse) are
  live.
- **Observability:** Langfuse Cloud — https://cloud.langfuse.com (your
  *AgentForge-Gauntlet* project). Chat/rounds appear under Tracing → Traces,
  keyed by the `X-Correlation-ID` response header. A **self-hosted** alternative
  (keeps PHI-adjacent traces on the droplet) is built into the deploy compose,
  off by default behind the `observability` profile — bring it up with
  `--profile observability`, access the UI via SSH tunnel (not publicly exposed),
  and set `LANGFUSE_HOST=http://langfuse:3000`. See `agent/LANGFUSE_SETUP.md`
  → "Self-hosted (droplet)".
- **Ingress model:** Caddy is the sole public listener, serving TLS on `:443` with
  `:80` auto-redirected to https. It serves the React SPA, reverse-proxies the agent
  API (`/v1/*`, `/health`, `/ready`, `/docs`, `/openapi.json`) → internal
  `agent:8000`, and — required for SMART login — proxies OpenEMR's OAuth2/login
  surface (`/oauth2/*`, `/public/*`, `/library/*`, `/custom/*`, `/apis/*`, `/sites/*`)
  → internal `openemr`. OpenEMR's **admin UI (`/interface/*`) is not publicly
  routed** — only the login/OAuth/FHIR surface is.
- **Ops:** `ssh root@agentforge.hankholcomb.com` (or the droplet IP); deploy dir
  `/root/openemr-base-clean`;
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
| Co-Pilot UI | https://agentforge.hankholcomb.com (SMART sign-in, OpenEMR admin creds) | http://localhost:5173 |
| Agent API | same-origin `…/v1/*`, `/ready`, `/docs` | http://localhost:8000 |
| OpenEMR | internal; OAuth2/login + FHIR surface proxied for SMART login (admin UI not routed) | http://localhost:8300 · https://localhost:9300 (admin/pass) |
| Langfuse | cloud.langfuse.com (AgentForge-Gauntlet); self-host opt-in via `--profile observability` (SSH tunnel) | same (if keys set in `.env.local`) |
| Secrets | droplet `/root/openemr-base-clean/.env` + `secrets/` + `Caddyfile` | `agent/.env.local` + `agent/secrets/` |
