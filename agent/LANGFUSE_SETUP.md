# Setting up Langfuse observability

The Langfuse integration is **fully wired in code**. To turn it on you only need
to (1) provision a Langfuse project and (2) set three environment variables. No
code changes are required.

## What you need to do (the short version)

1. Create a Langfuse project and copy its **public** + **secret** keys (Part 1).
2. Set the three credentials (Part 2):
   - **Droplet:** add them to the deploy `.env` (unprefixed) and redeploy the agent.
   - **Local:** add them (COPILOT_-prefixed) to `agent/.env.local`.
3. Verify traces appear (Part 3).

That's it. Everything below is the detail behind those three steps.

---

## What's already built (nothing to do here)

Committed and tested — with creds set, a live chat request produces a trace:

| Piece | Location |
|-------|----------|
| Creds-gated factory (real backend when 3 creds set, else no-op) | `copilot/observability/factory.py` |
| **Chat** span + verification event (`record_verification`) | `copilot/chat/service.py` |
| **Rounds** spans (`rounds.start` / `rounds.current` / `rounds.advance`) | `copilot/api/routes/rounds.py` |
| **Poller** emits `poller.tick` / `poller.result` to the real backend | `copilot/worker/runtime.py` → `copilot/worker/poller.py` |
| Buffered events flushed on app shutdown | `copilot/api/app.py` lifespan |
| Correlation ID threaded as the Langfuse **trace id** | `copilot/api/middleware.py` + `copilot/observability/langfuse_backend.py` |
| SDK pinned to **v2** (backend uses the v2 client API) | `pyproject.toml` (`langfuse>=2.55,<3`) |
| `/ready` treats Langfuse as **advisory** (missing creds ≠ 503) | `copilot/api/readiness.py` |

The gate is **all-or-nothing**: all three creds set ⇒ tracing on; any blank ⇒ silent no-op.

---

## Part 1 — Provision Langfuse and get keys

### Option A — Langfuse Cloud (recommended; ~2 minutes)

1. Sign up at **https://cloud.langfuse.com** and create an organization + project.
2. Project **Settings → API Keys → Create new API key**.
3. Copy the **Public key** (`pk-lf-…`) and **Secret key** (`sk-lf-…`).
4. Note the **host** for your region — EU: `https://cloud.langfuse.com`,
   US: `https://us.cloud.langfuse.com`.

### Option B — Self-host on the droplet (docker)

Only if data must stay on your VM (heavier — adds Postgres + ClickHouse + Redis):

```bash
git clone https://github.com/langfuse/langfuse.git && cd langfuse
docker compose up -d
```

Open `http://<droplet-ip>:3000`, create a project, copy the keys. Host becomes
`http://<droplet-ip>:3000`. For a demo, **Option A is simpler**.

---

## Part 2 — Set the credentials

### On the droplet (recommended path — this is where the live demo runs)

The deploy compose maps `COPILOT_LANGFUSE_*: ${LANGFUSE_*:-}`
(`docker-compose.deploy.yml:170-172`), so the deploy `.env` uses **unprefixed**
names. SSH in and:

```bash
ssh root@198.199.68.21
cd /root/openemr-base-clean
# Edit .env — set these three (they are gitignored; never commit them):
#   LANGFUSE_HOST=https://us.cloud.langfuse.com
#   LANGFUSE_PUBLIC_KEY=pk-lf-...
#   LANGFUSE_SECRET_KEY=sk-lf-...
nano .env
# Recreate the agent so it picks up the new env + the v2 SDK pin:
docker compose -f docker-compose.deploy.yml --env-file .env up -d --build agent
```

### Locally (uvicorn reads `agent/.env.local` via pydantic — needs the prefix)

```dotenv
# agent/.env.local  (gitignored)
COPILOT_LANGFUSE_HOST=https://us.cloud.langfuse.com
COPILOT_LANGFUSE_PUBLIC_KEY=pk-lf-...
COPILOT_LANGFUSE_SECRET_KEY=sk-lf-...
```

---

## Part 3 — Verify

1. **Readiness** — Langfuse now flips to ok and `/ready` is 200:
   ```bash
   curl -s http://<agent-host>/ready | jq '.dependencies[] | select(.name=="langfuse")'
   # { "name": "langfuse", "ok": true, "detail": "creds present", "advisory": true }
   ```
2. **Traces** — issue a chat (via the UI or `POST /v1/chat`) and note the
   `X-Correlation-ID` response header. In Langfuse → **Tracing → Traces** a
   trace named `chat` appears, keyed by that correlation ID, carrying a
   `verification.result` event (`passed=true/false`, `action=served|withheld`).
   Rounding calls appear as `rounds.start` / `rounds.current` / `rounds.advance`;
   enabling the poller (`COPILOT_POLLER_ENABLED=true`) adds `poller.tick` /
   `poller.result`.

---

## Gotchas

- **All-or-nothing gate** — one missing key = silent no-op. Set all three.
- **v2 SDK** — the dependency is pinned `<3`; a v3 install traces nothing and
  never errors (the backend uses the v2 `client.trace()` API). Rebuilding the
  agent image (`--build` above) installs the correct version.
- **Never commit** `.env` / `.env.local` or the keys — both are gitignored.
- Langfuse is **advisory** for `/ready`: missing creds are reported but do not
  return 503, because chat and rounds do not depend on observability.
