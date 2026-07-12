# Deploy the OpenEMR fork to a DigitalOcean Ubuntu 22.04 droplet

Every step is a copy-paste block. It assumes:

- A fresh Ubuntu 22.04 x64 DigitalOcean droplet (the default $6/mo box is
  fine for a demo — CPU/RAM min 1 vCPU / 2 GB).
- You can SSH into it as `root` from your Mac (DigitalOcean sets this up
  when you paste your public key at droplet creation).
- Port 80 is reachable from the public internet. DigitalOcean droplets
  have no firewall by default — no `ufw` / cloud-firewall commands
  needed here.
- The repo lives at
  `https://labs.gauntletai.com/hankholcomb/openemr-base-clean` on the
  Gauntlet GitLab.

Anywhere you see `REPLACE-…`, substitute the real value.

---

## 1. SSH in from your Mac

On the Mac:

```bash
ssh root@REPLACE-DROPLET-IP
```

The rest of this document runs inside that SSH session.

## 2. Install Docker Engine and the Compose plugin

Docker's official apt repository — deterministic, unlike the
`get-docker.com` one-liner:

```bash
apt-get update
apt-get install -y ca-certificates curl gnupg git

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io \
                   docker-buildx-plugin docker-compose-plugin

systemctl enable --now docker
docker version
docker compose version
```

If `docker compose version` prints a version, Docker is ready.

## 3. Clone the fork

```bash
cd /root
git clone https://labs.gauntletai.com/hankholcomb/openemr-base-clean.git
cd openemr-base-clean
```

GitLab will prompt for a username + personal access token (or password)
because the repo may be private. This is the **only** step in this
runbook that needs credentials; enter them interactively — do not paste
them into files on the droplet.

If you plan to `git pull` for future updates and want to avoid retyping
the token every time, `git config --global credential.helper store` will
cache it in `~/.git-credentials` (plaintext — acceptable for a demo box,
not for a production one).

## 4. Configure secrets in `.env`

```bash
cp .env.deploy.example .env
```

Now open `.env` and set real values:

```bash
# Pick strong random passwords. `openssl rand -base64 24` works.
PW1=$(openssl rand -base64 24 | tr -d '=+/')
PW2=$(openssl rand -base64 24 | tr -d '=+/')
PW3=$(openssl rand -base64 24 | tr -d '=+/')

DROPLET_IP=$(curl -s ifconfig.me)   # or paste it in manually

sed -i \
  -e "s|SITE_ADDR_OATH=.*|SITE_ADDR_OATH=http://${DROPLET_IP}|" \
  -e "s|MYSQL_ROOT_PASSWORD=.*|MYSQL_ROOT_PASSWORD=${PW1}|" \
  -e "s|MYSQL_PASSWORD=.*|MYSQL_PASSWORD=${PW2}|" \
  -e "s|OE_PASS=.*|OE_PASS=${PW3}|" \
  .env

grep -E '^(SITE_ADDR_OATH|MYSQL_ROOT_PASSWORD|MYSQL_PASSWORD|OE_PASS)=' .env
```

Save `PW3` somewhere safe — that's the initial OpenEMR admin password.

`.env` is in `.gitignore`, so this file will never be committed.

## 5. Bring the stack up

```bash
docker compose -f docker-compose.deploy.yml --env-file .env up -d --wait
```

First boot pulls the images and runs OpenEMR's install script — expect
~3 minutes. `--wait` blocks until both containers report healthy.

If it fails on `--wait`, check logs:

```bash
docker compose -f docker-compose.deploy.yml logs --tail=200 openemr
docker compose -f docker-compose.deploy.yml logs --tail=200 mariadb
```

Common causes: `.env` values weren't loaded (double-check `--env-file .env`),
or the droplet is out of memory (1 GB is too small; use 2 GB+).

## 6. Seed the synthetic clinical dataset

From the repo root on the droplet:

```bash
COMPOSE_DIR=$(pwd) \
COMPOSE_FILE=docker-compose.deploy.yml \
MYSQL_SERVICE=mariadb \
MYSQL_ROOT_PASSWORD="$(grep '^MYSQL_ROOT_PASSWORD=' .env | cut -d= -f2-)" \
  scripts/seed/seed.sh
```

The seed is idempotent: every row it writes carries `external_id='SEED'`
(or a fixed `pid` range for patients), and the script wipes those rows
before re-inserting, so re-running is safe. The final output should be a
summary table:

```
patients      15
encounters    15
vitals        45
problems      15
allergies      6
meds (lists)  25
meds (rx)     21
lab orders    21
lab reports   21
lab results   45
critical      7
soap notes    15
pnotes         1
```

## 7. Smoke checks

From the droplet:

```bash
curl -I  http://localhost/interface/login/login.php
curl -s  http://localhost/apis/default/fhir/metadata | head -c 400 ; echo
```

The first should return `HTTP/1.1 200 OK`; the second should be JSON
starting with `{"resourceType":"CapabilityStatement",…`.

From your Mac:

```bash
open http://REPLACE-DROPLET-IP/
```

Log in with `admin` and the `OE_PASS` value from `.env`. Pull up patient
**Oren Novak** (pid 1015) — his chart should show the overnight troponin
rise (`0.02 → 2.34 ng/mL` in the last 2 hours) and a new RN progress
note.

## 8. Update / redeploy loop

Pulling code updates from GitLab:

```bash
cd /root/openemr-base-clean
git pull
docker compose -f docker-compose.deploy.yml pull
docker compose -f docker-compose.deploy.yml --env-file .env up -d --wait
```

Re-seeding after a code / data change:

```bash
COMPOSE_DIR=$(pwd) COMPOSE_FILE=docker-compose.deploy.yml MYSQL_SERVICE=mariadb \
MYSQL_ROOT_PASSWORD="$(grep '^MYSQL_ROOT_PASSWORD=' .env | cut -d= -f2-)" \
  scripts/seed/seed.sh
```

Full teardown (destructive — wipes the DB volume):

```bash
docker compose -f docker-compose.deploy.yml --env-file .env down -v
```

## 9. Follow-ups explicitly deferred

- **TLS.** Put Caddy or Traefik in front of the container on 443 and
  redirect 80. The compose file leaves 443 unpublished on purpose — a
  self-signed cert on 443 is worse than plain HTTP for a demo.
- **DNS.** Point a subdomain at the droplet's IP once you're happy with
  the setup, then update `SITE_ADDR_OATH` in `.env` and `up -d` again.
- **Backups.** The `db` named volume is the crown jewel. For a demo,
  `docker run --rm -v openemr-base-clean_db:/data -v $PWD:/backup ubuntu \
  tar czf /backup/db-$(date +%F).tgz -C /data .` on a cron is enough.
- **Rebuilding the image.** If you ever need to build a custom image
  instead of using `openemr/openemr:flex`, the dev compose file at
  `docker/development-easy/docker-compose.yml` documents the extra env
  vars (composer token, etc.) that the build path needs. Do not copy
  those into the deploy compose — they are dev-only and would ship
  credentials to production.

---

## 10. Deploy the Clinical Co-Pilot agent

The base stack already builds `agent` + `agent-postgres`. The agent additionally
needs a SMART **Backend Services** OAuth client (to read OpenEMR's FHIR API) and a
few secrets. Do this **after** OpenEMR is up + seeded (steps 5–6). All commands run
on the droplet in the repo root.

**10.1 — Register the Backend Services client on the deployed OpenEMR.**
The agent image ships the registration helper + its deps. Create the key dir and
run it against your public URL (`$SITE_ADDR_OATH` from `.env`):

```bash
mkdir -p secrets
SITE=$(grep '^SITE_ADDR_OATH=' .env | cut -d= -f2-)
docker compose -f docker-compose.deploy.yml run --rm --no-deps \
  -v "$PWD/secrets:/app/secrets" --entrypoint python agent \
  scripts/register_backend_client.py --base-url "$SITE" --out-key /app/secrets/backend-key.pem
```

If `$SITE` is HTTPS with a self-signed cert, add `--insecure`. The command prints a
`CLIENT_ID` and the private key lands in `./secrets/backend-key.pem`.

**10.2 — Enable the client** (new clients are disabled + role `user`; Backend
Services needs enabled + `system`):

```bash
ROOT_PW=$(grep '^MYSQL_ROOT_PASSWORD=' .env | cut -d= -f2-)
docker compose -f docker-compose.deploy.yml exec -T -e MYSQL_PWD="$ROOT_PW" \
  mariadb mariadb -uroot openemr \
  -e "UPDATE oauth_clients SET is_enabled=1, client_role='system' WHERE client_id='REPLACE-CLIENT-ID';"
```

**10.3 — Fill the agent secrets in `.env`** (new keys are in `.env.deploy.example`):
set `BACKEND_SERVICES_CLIENT_ID` to the printed id, `ANTHROPIC_API_KEY` to your key,
leave `BACKEND_KEY_PATH=./secrets/backend-key.pem`, and — only if demoing the seeded
cohort — `FHIR_PATIENT_ID_TEMPLATE=a1000000-0000-0000-0000-{pid:012d}`. The
`BACKEND_SERVICES_SCOPES` default already matches what the helper registers.

**10.4 — Bring up the agent** and verify:

```bash
docker compose -f docker-compose.deploy.yml --env-file .env up -d agent
docker compose -f docker-compose.deploy.yml exec agent \
  python -c "import urllib.request,json; print(urllib.request.urlopen('http://localhost:8000/ready').read().decode())"
```

`/ready` should report `postgres`, `openemr_fhir`, and `llm` ok. A 503 lists the
failing dependency. (`llm` needs `ANTHROPIC_API_KEY`; the FHIR probe confirms the
agent can reach OpenEMR.)

> **OAuth audience gotcha (already handled in compose):** the agent POSTs to the
> internal `http://openemr` token endpoint, but the signed-JWT `aud` must equal the
> issuer OpenEMR advertises = `SITE_ADDR_OATH/oauth2/default/token`. The compose sets
> `COPILOT_OAUTH_AUDIENCE` from `SITE_ADDR_OATH` for you. If tokens fail with
> `invalid_client`, that mismatch is the first thing to check.

## 11. Serve the React chat UI (+ TLS) — Caddy

The agent is internal-only by design; front it (and serve the built UI) with a
reverse proxy. This needs a **DNS name** for automatic TLS (IP-only can use
`tls internal`, a self-signed cert). **Prepared, not yet run against your droplet —
adjust the domain.**

**11.1 — Build the UI** (same-origin: the proxy serves the app and proxies the API,
so no `VITE_API_BASE_URL` is needed):

```bash
cd agent/web && npm ci && npm run build   # -> agent/web/dist
```

**11.2 — Caddyfile** (put at repo root; replace the domain):

```
copilot.example.com {
    encode gzip
    handle /v1/*   { reverse_proxy agent:8000 }
    handle /health { reverse_proxy agent:8000 }
    handle /ready  { reverse_proxy agent:8000 }
    handle         { root * /srv/web ; file_server }
}
```

**11.3 — Add Caddy to the stack** (a compose fragment; join the `agent` network,
mount `agent/web/dist` + the Caddyfile, publish 80/443):

```yaml
  caddy:
    image: caddy:2
    restart: always
    ports: ["80:80", "443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - ./agent/web/dist:/srv/web:ro
      - caddy_data:/data
    networks: [agent]
# add `caddy_data: {}` under top-level volumes.
```

Point DNS at the droplet, `docker compose up -d caddy`, and the UI is at
`https://copilot.example.com` calling the agent same-origin (no CORS needed). For an
IP-only demo, swap the site label for `:443` + add `tls internal`, or serve on `:80`.

_This replaces the deferred-TLS follow-up in §9 for the agent/UI surface._

---

## 12. Cut over to HTTPS at a real domain (opt-in)

The default deploy runs plain HTTP on `:80` at the bare droplet IP (`Caddyfile.example`).
That keeps working unchanged. **HTTPS is a hard prerequisite for per-physician SMART
login** (`Secure` cookies, OAuth-over-TLS, https redirect URIs). When you have a domain,
cut over with the steps below — nothing here changes until you deliberately swap the
Caddyfile.

**12.1 — DNS + firewall (operator, not code).** Create an A-record and make sure both
ACME ports are reachable:

```
agentforge.<your-domain>.   A   <droplet-ip>
```

DigitalOcean droplets have no firewall by default, so `:80` and `:443` are already open.
If you added a cloud firewall or `ufw`, allow inbound **80 and 443** (Caddy needs `:80`
for the ACME HTTP-01 challenge + the http→https redirect, and `:443` for TLS).

**12.2 — Set the https origin everywhere (single source of truth).** In `.env`, set BOTH
to the *same* https origin — a mismatch is the silent SMART/redirect-URI failure class
this runbook already warns about for `SITE_ADDR_OATH` (§10):

```bash
SITE_ADDR_OATH=https://agentforge.<your-domain>          # OpenEMR's advertised issuer + redirect validation + JWT aud
COPILOT_PUBLIC_BASE_URL=https://agentforge.<your-domain> # builds the SMART redirect_uri + post-login redirect
```

The registered SMART app redirect URI (see PRODUCTION_GRADE_PLAN.md §A.8) must be exactly
`${COPILOT_PUBLIC_BASE_URL}/v1/auth/callback`. CORS stays empty — the SPA and API are
same-origin behind Caddy, so cookies are first-party and no CORS middleware is needed.

**12.3 — Switch Caddy to the domain site.** The compose file already publishes `:443`
alongside `:80` (no compose edit needed), and `caddy_data` already persists the cert.
Copy the HTTPS variant over the live `Caddyfile` (gitignored), set your domain + a fresh
password hash, and redeploy just caddy + agent:

```bash
cd /root/openemr-base-clean
cp Caddyfile.https.example Caddyfile
#  - replace agentforge.example.com with your real domain
#  - replace the password hash:
docker run --rm caddy caddy hash-password --plaintext 'YOUR-PASSWORD'
docker compose -f docker-compose.deploy.yml --env-file .env up -d caddy agent
```

Caddy provisions a Let's Encrypt cert on the first hit and auto-renews it; the cert
persists in `caddy_data`. Plain-HTTP `:80` requests are auto-redirected to https. The
`@api`/SPA routing and the `basic_auth` guard are identical to the `:80` file — only the
site label changed. Verify: `curl -I https://agentforge.<your-domain>/` returns `200` and
the cert is Let's Encrypt (not `tls internal`).

> **Rollback:** `cp Caddyfile.example Caddyfile` and re-run the `up -d caddy` line to
> return to the bare-IP `:80` demo. Certs in `caddy_data` are untouched.

## 13. Self-hosted Langfuse on the droplet (opt-in)

By default observability points at Langfuse **Cloud** (or is off entirely). To keep
PHI-adjacent trace data on the org's own infra, bring up the self-hosted stack — two
services gated behind the `observability` compose profile, so a plain `docker compose up`
never starts them and the default demo is unchanged:

```bash
docker compose -f docker-compose.deploy.yml --env-file .env \
  --profile observability up -d
```

Set `LANGFUSE_POSTGRES_PASSWORD`, `NEXTAUTH_SECRET`, and `SALT` in `.env` first (they fail
loud if empty). Then create a project in the self-hosted UI (reach it via SSH tunnel —
it is **not** publicly exposed), mint a public/secret key pair, and set
`LANGFUSE_HOST=http://langfuse:3000` + the two keys in `.env`, then recreate the agent.
The pinned image is a **Langfuse v2** server (matches the `langfuse>=2.55,<3` SDK; v3 would
need ClickHouse/Redis/object storage — out of scope). Full runbook + tunnel command:
`agent/LANGFUSE_SETUP.md` → "Self-hosted (droplet)".

## 14. Encryption at rest

Three layers, with an explicit split between **what the software does** and **what the
operator must own**. Do not over-claim the platform layer.

**14.1 — Application-layer token encryption (software).** When per-physician SMART login
is enabled (`COPILOT_AUTH_MODE=smart`), the physicians' OpenEMR OAuth access/refresh tokens
— the crown-jewel secret — are encrypted at rest with a Fernet key before being stored in
the agent database, and are never sent to the browser or written to a log. This is
genuinely customer-controlled and independent of the hosting platform. Generate the key
and put it in `.env` as `COPILOT_SESSION_ENC_KEY` (secrets-manager only; never commit):

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**14.2 — DigitalOcean encrypted block-storage volume (operator step).** `postgres:16-alpine`
has no built-in TDE and MariaDB engine-level encryption needs a keyfile+plugin, so the
realistic single-VM control is **disk-level**. The achievable customer-managed win: attach
a **DigitalOcean Block Storage volume** (DO encrypts these at rest) and relocate the DB
named volumes (`db`, `agent_db`, and — if you enabled it — `langfuse_db`) or the whole
Docker data-root (`/var/lib/docker`) onto it. This is an operator action, not something the
compose file can do for you. (Stronger and heavier: LUKS full-disk encryption on the
droplet — document as an option, not a default.)

**14.3 — DO platform disk encryption (do NOT over-claim).** DigitalOcean encrypts droplet
disks at the platform/data-center level, but that is **not customer-managed** and must not
be presented as a customer-controlled safeguard. Only 14.1 (token encryption) and 14.2
(encrypted block volume) are controls the deploying org actually holds keys to / manages.

> Compliance framing (technical-safeguard map, BAAs, operator-owned administrative and
> physical safeguards) is tracked separately in the compliance write-up
> (PRODUCTION_GRADE_PLAN.md §7 / `COMPLIANCE.md`).

## 15. Redeploy the agent with new backend code (rebuild + migrate)

The `agent` image is **built from source** and DB migrations are **not** run
automatically at container start — they are applied explicitly. After a code
pull that changes `agent/` (as of `a060e42`: the SMART-login backbone + the
audit-retention sweep — all inert while `COPILOT_AUTH_MODE=disabled` and
`COPILOT_WRITEBACK_ENABLED=false`), rebuild the image and apply the additive
migrations. **This is safe to run on the live demo: the new code changes no
existing behavior.**

```bash
cd /root/openemr-base-clean
# 1. Rebuild only the agent image from the pulled source.
docker compose -f docker-compose.deploy.yml build agent
# 2. Apply DB migrations. Additive only: an audit_log(at) index (0003) plus the
#    clinician / physician_session / login_txn tables (0004) — all UNUSED until
#    SMART login is enabled, so no existing row is touched.
docker compose -f docker-compose.deploy.yml run --rm --entrypoint alembic agent upgrade head
# 3. Restart the agent on the new image.
docker compose -f docker-compose.deploy.yml --env-file .env up -d agent
# 4. Verify (should still report postgres / openemr_fhir / llm ok).
docker compose -f docker-compose.deploy.yml exec agent \
  python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/ready').read().decode())"
```

The new `/v1/auth/*` routes return `404` and writes return `503` until their
flags are set, so the demo behaves identically to before the rebuild.

> **Rollback.** The migrations are additive and fully reversible without data
> loss: `docker compose -f docker-compose.deploy.yml run --rm --entrypoint alembic
> agent downgrade 0002` drops the new index + tables. To revert the code,
> re-checkout the prior commit's `agent/` tree and rebuild. The previous image
> also remains in the local Docker cache until pruned.

## 16. Enable per-physician SMART login (prerequisites + current limitation)

> **Read this before enabling.** SMART login is now built end-to-end behind
> `COPILOT_AUTH_MODE` (default `disabled`): the `/v1/auth/*` routes + `/v1/auth/status`,
> the encrypted server-side session store, PKCE, token refresh, `fhirUser →
> ClinicianId` mapping, automatic-logoff TTL, the frontend "Sign in with OpenEMR"
> gate, **and the data-route cutover**. In `smart` mode the interactive routes
> (`chat`, `rounds`, `observations`, `writes`, `alerts`, `refresh`) take the
> clinician identity from the authenticated **session** — `401` without a valid
> session, `403` if a request tries to assert a different `clinician_id`. So
> turning `COPILOT_AUTH_MODE=smart` on gives real per-physician login **and**
> identity enforcement on the data path, and the agent's own `audit_log`
> attributes every action to the logged-in physician.
>
> **The one remaining gap is the delegated-token cutover** (the deferred Phase-2
> bonus): interactive reads/writes still use the shared system / Backend-Services
> token, so OpenEMR's *own native* audit does not yet attribute them to the
> individual physician — the agent-side audit does. Least-privilege per-physician
> tokens + OpenEMR-native attribution land when that cutover ships
> (PRODUCTION_GRADE_PLAN.md §Phase 2). Keep the network/ingress controls from
> §11/§12 regardless.

Prerequisites: §12 (HTTPS at a real domain — `Secure` cookies require TLS) and
§15 (the new agent image + migrations applied).

**16.1 — Register the confidential SMART app client on the deployed OpenEMR.**
Mirrors §10.1 but for an `authorization_code` + `refresh_token` (login) client:

```bash
SITE=$(grep '^COPILOT_PUBLIC_BASE_URL=' .env | cut -d= -f2-)   # your https origin
docker compose -f docker-compose.deploy.yml run --rm --no-deps \
  --entrypoint python agent scripts/register_smart_app_client.py \
  --base-url "$SITE" --redirect-uri "$SITE/v1/auth/callback"
```

It prints a `client_id` and `client_secret`. The redirect URI must exactly equal
`${COPILOT_PUBLIC_BASE_URL}/v1/auth/callback` (§12.2).

**16.2 — Enable the client** (new clients are disabled + role `user`, which is
what SMART login needs — do NOT promote it to `system`):

```bash
ROOT_PW=$(grep '^MYSQL_ROOT_PASSWORD=' .env | cut -d= -f2-)
docker compose -f docker-compose.deploy.yml exec -T -e MYSQL_PWD="$ROOT_PW" \
  mariadb mariadb -uroot openemr \
  -e "UPDATE oauth_clients SET is_enabled=1 WHERE client_id='REPLACE-CLIENT-ID';"
```

Ensure each physician has an OpenEMR user with the right ACLs for what they will
do (read; and, for writers, `encounters`/`patients-med`).

**16.3 — Set the agent env and restart:**

```bash
COPILOT_AUTH_MODE=smart
COPILOT_SMART_APP_CLIENT_ID=<printed client_id>
COPILOT_SMART_APP_CLIENT_SECRET=<printed client_secret>   # secrets-manager only; never commit
COPILOT_SESSION_ENC_KEY=<Fernet key from §14.1>
COPILOT_PUBLIC_BASE_URL=https://agentforge.<your-domain>  # already set in §12.2
# then:
docker compose -f docker-compose.deploy.yml --env-file .env up -d agent
```

The agent refuses to boot in `smart` mode unless `COPILOT_PUBLIC_BASE_URL` is
`https://…` and the session key + client id are present — a deliberate guard so
`Secure` cookies can never be issued over plain HTTP. Verify login by opening
`https://agentforge.<your-domain>/v1/auth/login` — it should redirect to
OpenEMR's authorize screen.

## 17. Business Associate Agreements & governance (organization-owned)

These are **not** the software's to satisfy and must be handled by the deploying
organization before real PHI flows (full detail: `agent/COMPLIANCE.md` §3):

- **Anthropic BAA + zero-data-retention.** The agent sends PHI to the Claude API
  for synthesis and chat. Execute Anthropic's BAA and enable ZDR for the
  API key the agent uses (`COPILOT_ANTHROPIC_API_KEY`). Without both, sending PHI
  to the API is not permissible.
- **DigitalOcean BAA** (or the chosen host's) covering the droplet + volumes.
- **Any other PHI-touching subprocessor** (a hosted Langfuse instead of the
  self-hosted §13, log aggregation, monitoring) — covered by BAA or must not
  receive PHI.
- **§164.308 / §164.310** administrative and physical safeguards (risk analysis,
  workforce training, contingency/backup, incident response, facility controls)
  — organization policy, outside this codebase.
