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

### Option B — Self-hosted (droplet)

Use this when PHI-adjacent trace data (patient/clinician ids in span metadata)
must stay on the org's own infra. The deploy compose ships a self-hosted
Langfuse stack **built in and off by default** — two services (`langfuse` +
its own `langfuse-postgres`, on a private `observability` network, never
published) gated behind the `observability` **profile**. A plain
`docker compose up` does not start them, so the default demo is unchanged.

The image is pinned to a **Langfuse v2 server** (`langfuse/langfuse:2`,
2.95.11) to match the SDK pin `langfuse>=2.55,<3`. v2 needs only Postgres —
v3 additionally requires ClickHouse + Redis + object storage (MinIO), which
is out of scope here; do not bump the major without adding them.

**1. Set the self-host secrets in the deploy `.env`** (they fail loud if left
blank — Postgres refuses an empty password, Langfuse refuses a missing
`NEXTAUTH_SECRET`/`SALT`):

```bash
ssh root@198.199.68.21
cd /root/openemr-base-clean
# openssl rand -base64 32  # generate a fresh value for each of the three:
#   LANGFUSE_POSTGRES_PASSWORD=...
#   NEXTAUTH_SECRET=...
#   SALT=...
nano .env
```

**2. Bring up the profile:**

```bash
docker compose -f docker-compose.deploy.yml --env-file .env \
  --profile observability up -d
```

**3. Create a project + mint keys via an SSH tunnel** (the UI is not publicly
exposed — recommended for least exposure). Uncomment the loopback `ports:`
block on the `langfuse` service in `docker-compose.deploy.yml`
(`127.0.0.1:3000:3000`, never `0.0.0.0`), recreate it, then from your Mac:

```bash
ssh -L 3000:127.0.0.1:3000 root@198.199.68.21
# open http://localhost:3000 → sign up (first user), create an organization +
# project → Settings → API Keys → Create → copy the pk-lf-… and sk-lf-… keys.
```

**4. Point the agent at the in-network instance.** Set these three in `.env`
(the host is the compose service name, reachable from the agent over the
`agent` network — no host port needed) and recreate the agent:

```bash
#   LANGFUSE_HOST=http://langfuse:3000
#   LANGFUSE_PUBLIC_KEY=pk-lf-...
#   LANGFUSE_SECRET_KEY=sk-lf-...
docker compose -f docker-compose.deploy.yml --env-file .env up -d --build agent
```

To stop the self-hosted stack without touching the demo:
`docker compose -f docker-compose.deploy.yml --profile observability down`
(the `langfuse_db` volume persists trace data across restarts).

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
