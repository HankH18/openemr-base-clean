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

The default stack includes the `caddy` public-ingress service — it has no
compose profile, so it starts with everything else. Caddy bind-mounts
`./Caddyfile` and `./agent/web/dist`, and **both are gitignored** (see
`.gitignore`), so on a fresh clone neither exists. Left alone, Docker
auto-creates them as empty directories and caddy crash-loops trying to parse a
directory as its config — which makes the `--wait` below exit nonzero. Create
the real files first: copy the committed HTTP-on-`:80` Caddyfile (the same file
§12's rollback restores) and build the SPA bundle caddy serves.

```bash
cp Caddyfile.example Caddyfile
cd agent/web && npm ci && npm run build && cd -   # -> agent/web/dist
```

Then bring the stack up:

```bash
docker compose -f docker-compose.deploy.yml --env-file .env up -d --wait
```

First boot pulls/builds the images and runs OpenEMR's install script — expect
~3 minutes. `--wait` blocks until the stack's containers report healthy.

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
CF="docker compose -f docker-compose.deploy.yml"
# OpenEMR publishes no host port and caddy (:80) does not route these paths, so
# smoke it from inside its own container — the agent reaches it the same way
# (http://openemr), which is why plain http on :80 serves both the UI and API.
$CF exec openemr curl -I  http://localhost/interface/login/login.php
$CF exec openemr curl -s  http://localhost/apis/default/fhir/metadata | head -c 400 ; echo
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

- **Google Safe Browsing "Dangerous site" flag (false positive; review pending, reported
  2026-07-19).** Google Safe Browsing flagged `agentforge.hankholcomb.com` as suspected phishing —
  a false positive triggered by a self-hosted OpenEMR SMART login (password form + clinical
  branding) on a fresh personal-domain subdomain. Nothing malicious is served (synthetic demo
  data only). A correction was filed via the Safe Browsing report-error form
  (`safebrowsing.google.com/safebrowsing/report_error/`); track/expedite via **Google Search
  Console → Security Issues → Request Review** for `hankholcomb.com`. Until it clears: click
  Chrome's **Details → "visit this unsafe site"**, use the demo video, or serve on an unflagged
  host (the flag is per-host). To reduce re-flagging, make the demo nature obvious and avoid
  mimicking a known brand's login page.
- **TLS.** Put Caddy or Traefik in front of the container on 443 and
  redirect 80. The compose file leaves 443 unpublished on purpose — a
  self-signed cert on 443 is worse than plain HTTP for a demo.
- **DNS.** Point a subdomain at the droplet's IP once you're happy with
  the setup, then update `SITE_ADDR_OATH` in `.env` and `up -d` again.
- **Backups.** See **§19 (Backup & recovery)** for the full treatment — artifact
  ownership, the manual procedure, recovery, and RPO/RTO. Two corrections to what
  this bullet used to say: the `db` volume is **not** the only crown jewel (`agent_db`
  holds the derived extractions, which OpenEMR cannot regenerate), and the cron this
  bullet hand-waved at **was never installed** — there is no scheduled backup today.
  The one-off demo dump, for reference:
  `docker run --rm -v openemr-base-clean_db:/data -v $PWD:/backup ubuntu
  tar czf /backup/db-$(date +%F).tgz -C /data .` — but prefer §19.3's logical dumps,
  which survive a DB image bump.
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

`/ready` is a **graded** readiness payload (Week 2): each dependency carries a
`status` (`ok` / `degraded` / `down`) alongside the `ok` boolean, so a
degraded-but-serving dependency is distinct from a hard failure. It probes
`document_store` + `pgvector` (the agent Postgres + vector extension),
`guideline_corpus` (the corpus is actually populated — `degraded` advisory when
empty, i.e. step 4 below was skipped), `embedder` + `reranker` (Voyage/Cohere, or
the keyless stubs — reported `degraded` advisory when running on the stub), and
`openemr_fhir` + `llm` + `langfuse`. A 503 means a **gating** dependency is down
(advisory ones — `langfuse`, `guideline_corpus`, the keyless stubs — never cause
a 503). `GET /status` returns the agent health aggregates (ingestion count,
extraction pass rate, eval-by-rubric over the **53-case fixture golden set** (the gate also
scores 9 baseline-free live cases → 62 total), p50/p95
latency, error rate), each labelled `measured:` (live agent-DB) or `recorded:`
(committed artifact) in its `metric_sources` block. `retrieval_hit_rate` is
reported **unavailable** — no retrieval telemetry is persisted, so the `0.0` is a
contract placeholder rather than a measurement (OBSERVABILITY.md §7.3).

> **Packaging note (Week 2):** the agent image installs the **tesseract** OCR
> binary (local, in-container OCR for document ingestion — PHI never leaves the
> deployment for bounding boxes), and `agent-postgres` runs the
> **`pgvector/pgvector:pg16`** image so the `vector` extension is available for
> dense guideline retrieval. Both are already wired in `agent/Dockerfile` and
> `docker-compose.deploy.yml`; no operator action beyond a rebuild
> (`docker compose ... build agent`).

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

**11.2 — Caddyfile** (put at repo root; replace the domain). The committed
`Caddyfile.example` / `Caddyfile.https.example` are the maintained sources —
they raise the request-body cap so scanned-PDF uploads (`POST /v1/documents`)
are not rejected by Caddy's small default:

```
copilot.example.com {
    encode gzip
    request_body { max_size 25MB }   # scanned-PDF uploads. NOTE: a byte cap does not
                                 # bound rasterization -- a 544-BYTE pdf can declare a
                                 # 60x60in page and render to 1.1 GB. The real guards
                                 # are the pixel-area and page-count caps in
                                 # agent/copilot/documents/raster.py, plus the agent
                                 # container's mem_limit.
    handle /v1/*   { reverse_proxy agent:8000 }
    handle /health { reverse_proxy agent:8000 }
    handle /ready  { reverse_proxy agent:8000 }
    handle         { root * /srv/dist ; file_server }
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
      - ./agent/web/dist:/srv/dist:ro
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

The registered SMART app redirect URI must be exactly
`${COPILOT_PUBLIC_BASE_URL}/v1/auth/callback`. CORS stays empty — the SPA and API are
same-origin behind Caddy, so cookies are first-party and no CORS middleware is needed.

**12.3 — Switch Caddy to the domain site.** The compose file already publishes `:443`
alongside `:80` (no compose edit needed), and `caddy_data` already persists the cert.
Copy the HTTPS variant over the live `Caddyfile` (gitignored), set your domain, and
redeploy just caddy + agent:

```bash
cd /root/openemr-base-clean
cp Caddyfile.https.example Caddyfile
#  - replace agentforge.example.com with your real domain
docker compose -f docker-compose.deploy.yml --env-file .env up -d caddy agent
```

Caddy provisions a Let's Encrypt cert on the first hit and auto-renews it; the cert
persists in `caddy_data`. Plain-HTTP `:80` requests are auto-redirected to https. There
is **no basic_auth guard** — per-physician SMART login is the access gate (in `smart`
mode the agent's data routes return `401` without an authenticated session), so users
hit only the OpenEMR login. (If you run HTTPS with `COPILOT_AUTH_MODE=disabled`, add a
`basic_auth` block back to the Caddyfile as a network guard — see `Caddyfile.https.example`.)
Verify: `curl -I https://agentforge.<your-domain>/` returns `200` and the cert is Let's
Encrypt (not `tls internal`).

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

**14.2 — DigitalOcean encrypted block-storage volume (operator step).** The DB images
(`pgvector/pgvector:pg16` for the agent store, MariaDB for OpenEMR) have no built-in TDE
and MariaDB engine-level encryption needs a keyfile+plugin, so the
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
> (`agent/COMPLIANCE.md`).

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
# 4. Verify (should still report document_store / openemr_fhir / llm ok).
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

> **As deployed (reference droplet) — this is live, not hypothetical.** The reference
> deployment at **`https://agentforge.hankholcomb.com`** already runs everything in this
> section: HTTPS via Caddy + Let's Encrypt (§12), `COPILOT_AUTH_MODE=smart` with the
> confidential SMART app client registered and enabled (§16.1–16.3), and the AgentForge
> login restyle (`custom/assets/css/agentforge-login.css`, autoloaded via
> `custom/assets/custom.yaml` and served through Caddy's `/custom/*` proxy). There is **no
> basic-auth guard** — the per-physician SMART sign-in is the sole access gate, and users
> sign in with their OpenEMR credentials (`OE_USER` / `OE_PASS`). The steps below are the
> generic runbook that produced it; a fresh deploy still defaults to `auth_mode=disabled`,
> so follow them against your own domain to reproduce it.

> **Read this before enabling.** SMART login is built end-to-end behind
> `COPILOT_AUTH_MODE` (default `disabled`): the `/v1/auth/*` routes + `/v1/auth/status`,
> the encrypted server-side session store, PKCE, token refresh, `fhirUser →
> ClinicianId` mapping, automatic-logoff TTL, the frontend "Sign in with OpenEMR"
> gate, the data-route identity cutover, **and the delegated-token cutover**. In
> `smart` mode:
> - **Identity** on every interactive route (`chat`, `rounds`, `observations`,
>   `writes`, `alerts`, `refresh`) comes from the authenticated **session** —
>   `401` without a valid session, `403` if a request asserts a different
>   `clinician_id`. The agent's own `audit_log` attributes every action to the
>   logged-in physician.
> - **Reads/writes use the physician's own delegated token**: chat, rounds-start,
>   and observations reads, plus writeback commits, call OpenEMR under the
>   logged-in physician's SMART token — so OpenEMR's *own native* audit also
>   attributes them to that physician (least-privilege, per-physician).
>
> Two endpoints deliberately keep the **system** token because they drive the
> shared poller machinery (`RefreshPipeline`), not a per-physician clinical read:
> `POST /v1/rounds/refresh` and `GET /v1/rounds/alerts` (their identity is still
> session-enforced; only the FHIR read runs as the system client). The background
> poller is likewise unchanged. Keep the network/ingress controls from §11/§12.

Prerequisites: §12 (HTTPS at a real domain — `Secure` cookies require TLS), §15
(the new agent image + migrations applied), and a **rebuilt web UI** so the
"Sign in with OpenEMR" gate is in the served bundle (`agent/web/dist` is built on
the box, not committed to git):

```bash
cd agent/web && npm ci && npm run build   # regenerates agent/web/dist (Caddy serves it)
cd -
```

**16.1 — Register the confidential SMART app client on the deployed OpenEMR.**
Mirrors §10.1 but for an `authorization_code` + `refresh_token` (login) client:

```bash
SITE=$(grep '^COPILOT_PUBLIC_BASE_URL=' .env | cut -d= -f2-)   # your https origin
mkdir -p secrets
docker compose -f docker-compose.deploy.yml run --rm --no-deps \
  -v "$PWD/secrets:/app/secrets" --entrypoint python agent \
  scripts/register_smart_app_client.py \
  --base-url "$SITE" --public-base-url "$SITE" --out /app/secrets/smart-app-client.json
```

The script derives the redirect URI as `${public-base-url}/v1/auth/callback` (which
must match §12.2) and writes the `client_id` + `client_secret` to
`secrets/smart-app-client.json` on the host — read the two values from that file
for §16.3. Add `--insecure` only for a self-signed dev cert.

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
# NOTE the missing COPILOT_ prefix on these two. It is not a typo: compose
# interpolates ${SMART_APP_CLIENT_ID} / ${SMART_APP_CLIENT_SECRET} from .env and
# passes them through AS COPILOT_SMART_APP_CLIENT_ID / _SECRET. Writing the
# prefixed names here means compose finds nothing, substitutes empty, and the
# container gets an EMPTY client secret. (This block used to say COPILOT_ on both;
# following it literally produced a silent login failure. It now refuses to boot
# instead — see ensure_smart_ready — but the names still have to be right.)
SMART_APP_CLIENT_ID=<printed client_id>
SMART_APP_CLIENT_SECRET=<printed client_secret>   # secrets-manager only; never commit
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

## 18. Ship the Week-2 multimodal build (document ingestion + graph + RAG)

The authoritative runbook to bring the **Week-2** surface live (document upload →
strict-schema extraction → hybrid RAG → source-grounded answer with citations →
supervisor/worker/critic graph). Supersedes §15 for a Week-2 deploy — it adds two
steps §15 omits (**guideline-corpus ingest** and the **graph flag**). All new
migrations (`0005` document-ingestion, `0006` guideline-RAG) are additive; the
`agent-postgres` image is already `pgvector/pgvector:pg16` and the agent
`Dockerfile` already installs Tesseract, so no operator action beyond the rebuild.

```bash
ssh root@agentforge.hankholcomb.com
cd /root/openemr-base-clean
git pull                                                          # pull the Week-2 code

# 1. (Optional) enable the multi-agent graph for serve-time chat. Without this the
#    inline agent+verify path still returns grounded answers + guideline_evidence;
#    with it, chat routes through supervisor -> intake-extractor/evidence-retriever
#    -> critic, with nested Langfuse spans + worker.handoff events.
grep -q '^COPILOT_CHAT_GRAPH_ENABLED=' .env \
  && sed -i 's/^COPILOT_CHAT_GRAPH_ENABLED=.*/COPILOT_CHAT_GRAPH_ENABLED=true/' .env \
  || echo 'COPILOT_CHAT_GRAPH_ENABLED=true' >> .env
#    Real hybrid RAG is optional: leave VOYAGE_API_KEY / COHERE_API_KEY unset to use
#    the deterministic keyless embedding/rerank stubs (retrieval still returns
#    grounded, cited guideline evidence). Set them in .env for real voyage-3.5
#    embeddings + rerank-v3.5. Anthropic key (already set) drives real Claude vision.

# 2. Rebuild the agent image from the pulled source (adds Tesseract, W2 packages).
docker compose -f docker-compose.deploy.yml build agent

# 3. Apply DB migrations (0005 document_ingestion + 0006 guideline_rag, which also
#    enables the `vector` extension; 0009 adds guideline_document.content_hash).
#    Additive; existing rows untouched.
docker compose -f docker-compose.deploy.yml run --rm -T --entrypoint alembic agent upgrade head

# 4. Ingest the guideline corpus into guideline_chunk. REQUIRED for the RAG half —
#    migrations only create empty tables, so hybrid retrieval returns nothing until
#    this runs. The corpus is baked into the image at /app/corpus (the script's
#    default); chunks + embeds it with the keyless stub embedder by default. The
#    `-T` disables the pseudo-TTY so the run does not swallow a piped/heredoc stdin.
#
#    Safe to re-run, and re-running APPLIES CORPUS EDITS: each source is skipped
#    only while its stored content_hash still matches the file, so a corrected
#    guideline is re-ingested automatically — no flag. Documents ingested before
#    migration 0009 carry no hash and are rebuilt once, then settle onto the cheap
#    skip path. Read the per-document lines: "skipped (unchanged)" vs
#    "re-ingested (content changed)" is the difference between the corpus being
#    current and it merely being present.
docker compose -f docker-compose.deploy.yml run --rm -T --no-deps \
  --entrypoint python agent scripts/ingest_guidelines.py

# 5. Rebuild the React UI bundle Caddy serves (upload control, provenance chips,
#    evidence overlay).
cd agent/web && npm ci && npm run build && cd -

# 6. Restart the agent (new image + env) and Caddy (new bundle).
docker compose -f docker-compose.deploy.yml --env-file .env up -d agent caddy

# 7. Verify graded readiness — document_store ok (gating); pgvector/embedder/
#    reranker may report `degraded`/`stub (keyless)` and that is expected & serving.
#    guideline_corpus must read `ok` with an `N chunks` detail: `degraded` +
#    "empty corpus" means step 4 did not take, and RAG will serve zero evidence.
docker compose -f docker-compose.deploy.yml exec agent \
  python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/ready').read().decode())"
```

**Smoke the Week-2 flow** (server-side, no browser login needed) — upload a fixture
lab PDF and confirm extraction + citations round-trip: hit `POST /v1/documents`
then `GET /v1/documents/{id}` (which returns the extraction's facts + citations; see the Postman/Bruno collection in
`api-collection/`, "Week 2" folder), or drive the browser flow at
`https://agentforge.hankholcomb.com` after SMART sign-in.

> **Rollback.** Additive and reversible. Code: `git checkout early-submission-failsafe`
> then rebuild the agent + web (that tag is the pre-Week-2 live state). Schema:
> `docker compose -f docker-compose.deploy.yml run --rm --entrypoint alembic agent
> downgrade 0004` drops the Week-2 tables. The prior agent image also remains in the
> local Docker cache until pruned.

---

## 19. Backup & recovery (what is protected, how to restore, RPO/RTO)

**Read the gap first.** There is **no scheduled backup on this deployment today.**
No cron unit, no timer, no snapshot policy, no backup container ships in
`docker-compose.deploy.yml`, and no backup script exists in this repo for our stack.
§9 offers a one-off `docker run … tar czf` volume-dump *demo* and says "on a cron is
enough" — that cron **was never installed**. So the honest current posture is:

> **Current RPO = total loss of the agent DB and the OpenEMR DB since deploy** (there
> is no restore point at all). **Current RTO = unbounded for PHI-bearing derived data**
> (it cannot be restored from something that was never captured).

Everything below is therefore split into **what is genuinely protected today** (the
repo-reproducible tier — real, and it covers more than you would expect) and **what an
operator must install before this handles real PHI** (the snapshot tier). Do not read
the RPO/RTO table as describing a running system; it describes the system **once §19.3
is installed**, and says so per row.

### 19.1 The four artifact classes and where they actually live

| Artifact | Authoritative store | Docker volume | Reproducible from repo? |
|---|---|---|---|
| **Source documents** (uploaded lab PDFs / intake forms) | **OpenEMR** (`POST /api/patient/:pid/document`) — the agent stores only `openemr_document_id` as a ref | `db` (MariaDB) + `sites` | **No** — clinician-supplied input |
| **Derived extractions** (`extraction`, `extracted_fact`, citations, `audit_log`) | **Agent DB** — append-only, agent-authoritative (labs are not API-writable, so these are *not* FHIR resources) | `agent_db` (Postgres) | **No** — output of a paid, non-deterministic vision call |
| **Derived FHIR records** (physician-**confirmed** write-backs: `medical_problem`, `allergy`, `medication`, vitals, encounters) | **OpenEMR** — written through the propose→confirm gate with the physician's own token, so OpenEMR owns and natively audits them | `db` (MariaDB) | **No** — clinical records |
| **Page-image render cache** (`document_page.image` bytea) | Agent DB, but explicitly a **re-derivable cache** | `agent_db` | **No, but re-derivable** — rasterize the source PDF again |
| **Eval golden set** (62 cases: 53 fixture + 9 live) + baseline + guideline corpus | **The git repo** | *none* | **Yes — fully** (see §19.2) |

Two consequences worth being blunt about:

- **The agent DB is the only home of the derived extractions.** Losing `agent_db` loses
  every extracted fact and its bbox provenance. They are *not* recoverable from OpenEMR
  — OpenEMR has the source PDF, not our parse of it. They are only *re-derivable* by
  re-running ingestion against a live Anthropic key, which costs money and (being a
  model call) will not reproduce byte-identical facts. Treat `agent_db` as
  crown-jewel-equal to `db`, which §9 currently does not.
- **`.env` is a single point of total loss.** `COPILOT_SESSION_ENC_KEY` (Fernet, §14.1)
  decrypts the physician SMART tokens at rest. **A volume backup without that key
  restores unreadable token rows** — physicians must re-authenticate, and any
  in-flight delegated session is gone. `.env` is gitignored by design; back it up to a
  secrets manager **separately from the volume dumps**, never into the same tarball.

### 19.2 The eval golden set is reproducible from the repo alone — verified

**This is a genuine "no backup required" claim, and it checks out.** The entire eval
gate is committed plain-text; a fresh `git clone` reconstitutes it with **zero**
dependency on any droplet, volume, or snapshot:

| Asset | Path | Committed |
|---|---|---|
| Gate cases (13) | `agent/evals/gate_dataset.jsonl` | ✅ tracked |
| Golden cases (40) | `agent/evals/golden_dataset.jsonl` | ✅ tracked |
| Regression baseline | `agent/evals/gate_baseline.json` | ✅ tracked |
| Rubric logic + runner | `agent/evals/rubrics.py`, `agent/evals/gate.py` | ✅ tracked |
| Guideline corpus (4 docs / 19 sections) | `agent/corpus/*.md` + `LICENSES.md` | ✅ tracked |
| Corpus ingest script | `agent/scripts/ingest_guidelines.py` | ✅ tracked |

Verify the claim in one command (from a clean clone, no droplet, no keys, no network):

```bash
cd agent && python evals/gate.py     # 62 cases (53 fixture + 9 live), 5 boolean rubrics → pass_rate 100, exit 0
```

The gate is **stubbed and LLM-free**, so it needs no API key and no database — which is
exactly why it survives losing everything else. The **guideline corpus** is equally
repo-reproducible: `guideline_chunk` rows are agent-DB-owned but are *derived*, and
`scripts/ingest_guidelines.py` deterministically rebuilds them from the committed
Markdown. Idempotency is **automatic, not a flag** — each source is skipped only while
its stored `guideline_document.content_hash` still matches the file, so re-running
never duplicates *and* an edited corpus file is re-ingested automatically; with the
stub embedder a from-scratch re-ingest is byte-identical. **RPO 0 / RTO ≈ 1 minute for
both.**

> **Two honest limits of this claim.**
> 1. "Reproducible from the repo alone" covers the eval set, rubrics, baseline, and
>    guideline corpus — the whole quality-defence apparatus. It does **not** cover
>    patient data, and nothing here should be read as implying otherwise.
> 2. **A plain re-ingest will not *upgrade* stub vectors to real ones — use `--force`.**
>    The skip is keyed on the corpus *text*, and swapping the embedder changes no text,
>    so re-running with `VOYAGE_API_KEY` newly set against an unchanged populated corpus
>    is a **no-op**: it will not re-embed. This is the one degradation a content hash
>    cannot see, and it is what `--force` is for — it rebuilds every source
>    unconditionally (`scripts/ingest_guidelines.py --force`), which is the supported way
>    to re-embed in place. You do **not** need to clear `guideline_chunk` /
>    `guideline_document` by hand:
>
>    ```bash
>    docker compose -f docker-compose.deploy.yml run --rm -T --no-deps \
>      --entrypoint python agent scripts/ingest_guidelines.py --force
>    ```
>
>    On a from-scratch restore, exporting `VOYAGE_API_KEY` **before** step 4 of §19.4
>    gets you real `voyage-3.5` vectors with no flag at all (cheap — one-time corpus
>    embed, §3d).
>
>    *Corrected 2026-07-17: this note previously claimed "there is no `--force` /
>    `--reset` flag — the only CLI argument is `--corpus-dir`". That was false;
>    `--force` has existed since the flag was added and `--help` lists it. Acting on
>    the old text meant hand-deleting rows in prod for a job the CLI already does.*

### 19.3 Manual backup procedure (install this — it is not running)

Logical dumps, not raw volume tars: they survive a Postgres/MariaDB image bump, which a
`/var/lib/postgresql/data` tarball does not. Run on the droplet in the repo root.

```bash
set -euo pipefail
BACKUP_DIR=/opt/agentforge-backups; STAMP=$(date -u +%F-%H%M)
mkdir -p "$BACKUP_DIR"
CF="docker compose -f docker-compose.deploy.yml --env-file .env"

# 1. Agent DB (derived extractions, facts, citations, audit_log, guideline chunks).
$CF exec -T agent-postgres pg_dump -U copilot -d copilot --format=custom \
  | gzip > "$BACKUP_DIR/agent-db-$STAMP.dump.gz"

# 2. OpenEMR DB (source documents + physician-confirmed FHIR write-backs).
$CF exec -T mariadb sh -c 'exec mariadb-dump -uroot -p"$MYSQL_ROOT_PASSWORD" \
  --single-transaction --routines --triggers openemr' \
  | gzip > "$BACKUP_DIR/openemr-db-$STAMP.sql.gz"

# 3. OpenEMR site assets (uploaded document blobs live here, not only in the DB).
docker run --rm -v "$(basename "$PWD")_sites":/data -v "$BACKUP_DIR":/backup ubuntu \
  tar czf "/backup/openemr-sites-$STAMP.tgz" -C /data .

# 4. Verify non-empty, then ship OFF the droplet — a backup on the box it protects
#    is not a backup (it dies with the droplet).
ls -lh "$BACKUP_DIR"/*"$STAMP"*
# e.g.: rclone copy "$BACKUP_DIR" do-spaces:agentforge-backups/  (encrypted remote)
```

**`.env` (separately, to a secrets manager — NOT into the tarball above):**
`COPILOT_SESSION_ENC_KEY`, `MYSQL_ROOT_PASSWORD`, `AGENT_POSTGRES_PASSWORD`,
`ANTHROPIC_API_KEY`, and any `VOYAGE_API_KEY` / `COHERE_API_KEY`.

**Automate it (the actual gap — one line):**

```bash
# /etc/cron.d/agentforge-backup — nightly 02:15 UTC; RPO becomes 24h.
15 2 * * * root cd /root/openemr-base-clean && /root/backup.sh >> /var/log/agentforge-backup.log 2>&1
```

A DigitalOcean **droplet snapshot** or an automated-backups subscription is the
belt-and-braces layer (whole-VM, weekly) — it protects against droplet loss, which the
above does not if the dumps never leave the box.

### 19.4 Recovery procedure

**Restore the agent DB** (derived extractions/facts/citations/audit):

```bash
CF="docker compose -f docker-compose.deploy.yml --env-file .env"
$CF stop agent                                    # stop writers first
gunzip -c /opt/agentforge-backups/agent-db-<STAMP>.dump.gz \
  | $CF exec -T agent-postgres pg_restore -U copilot -d copilot --clean --if-exists
$CF start agent
$CF exec agent python -c \
  "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/ready').read().decode())"
```

**Restore the OpenEMR DB + assets** (source documents, confirmed write-backs):

```bash
$CF stop openemr agent
gunzip -c /opt/agentforge-backups/openemr-db-<STAMP>.sql.gz \
  | $CF exec -T mariadb sh -c 'exec mariadb -uroot -p"$MYSQL_ROOT_PASSWORD" openemr'
docker run --rm -v "$(basename "$PWD")_sites":/data \
  -v /opt/agentforge-backups:/backup ubuntu \
  sh -c 'cd /data && tar xzf /backup/openemr-sites-<STAMP>.tgz'
$CF start openemr agent
```

**Rebuild the repo-reproducible tier** (no backup needed — this is §19.2 in practice):

```bash
git clone <fork-url> && cd openemr-base-clean          # eval set + corpus arrive with the clone
cd agent && python evals/gate.py                        # prove the gate is intact: 62 cases (53 fixture + 9 live), exit 0

# Re-seed guideline_chunk. Set VOYAGE_API_KEY in .env BEFORE this runs if you want real
# voyage-3.5 vectors: the ingest skips a document whose content_hash still matches, and
# an embedder swap changes no text, so a later plain re-run will NOT re-embed a populated
# corpus — that case needs `--force` (see §19.2). Keyless = stub vectors, which still
# serve grounded, cited evidence. (Corpus *edits* need no flag; they are detected.)
docker compose -f docker-compose.deploy.yml run --rm -T --no-deps \
  --entrypoint python agent scripts/ingest_guidelines.py
```

**Total droplet loss** — the order that matters: (1) rebuild the droplet and restore
`.env` from the secrets manager **first** (without `COPILOT_SESSION_ENC_KEY` the token
rows you are about to restore are undecryptable); (2) `git clone` the fork (§5 → §10);
(3) restore the two DB dumps; (4) re-run the corpus ingest; (5) rebuild the web bundle
(§18 step 5); (6) verify `/ready` and smoke the Week-2 flow (§18 step 7).

**Restore-test cadence.** A backup never restore-tested is a hypothesis. Restore the
agent dump into a scratch Postgres quarterly and assert row counts on `extraction` /
`extracted_fact` / `audit_log` — the append-only tables where silent truncation would
otherwise go unnoticed until it mattered.

### 19.5 RPO / RTO

Estimates, stated per class. **"Today" = the current no-backup reality; "With §19.3" =
once the nightly cron is installed.** RTO figures are for the single-droplet reference
deployment and assume the dumps are reachable.

| Artifact class | RPO today | RPO with §19.3 | RTO | Basis / limiting factor |
|---|---|---|---|---|
| **Eval golden set + rubrics + baseline** | **0** | 0 | **≈ 1 min** | Committed plain-text; `git clone` + `python evals/gate.py`. Needs no key, DB, or network — verified §19.2 |
| **Guideline corpus + chunks** | **0** | 0 | **≈ 1–2 min** | Committed Markdown + deterministic ingest script (idempotent by `source` natural key). Key must be set *before* the from-scratch run for real vectors — §19.2 limit 2 |
| **Derived extractions / facts / citations / audit** | **∞ — total loss** | **≤ 24 h** | **≈ 10–15 min** | `pg_restore` of a ~small custom-format dump + agent restart. *Not* re-derivable free: only a paid, non-deterministic re-ingest recreates them |
| **Page-image cache** | ∞ (but re-derivable) | ≤ 24 h | ≈ 10–15 min (with the dump) | Explicitly a re-derivable cache — rasterize the source PDF again if lost |
| **Source documents + confirmed FHIR write-backs** | **∞ — total loss** | **≤ 24 h** | **≈ 15–30 min** | `mariadb` logical restore + `sites` volume extract; largest dataset, so restore time dominates |
| **SMART session tokens** | ∞ | ≤ 24 h *(and useless without `.env`)* | ≈ 5 min | Fernet-encrypted; **`COPILOT_SESSION_ENC_KEY` is the hard dependency**. Worst case is not fatal — physicians re-authenticate |
| **Whole droplet** | ∞ | ≤ 24 h (dumps) / ≤ 7 d (DO snapshot) | **≈ 45–90 min** | Full §19.4 rebuild: droplet + `.env` + clone + 2 restores + ingest + web bundle + verify |

**Why 24 h and not tighter.** A nightly logical dump is the honest fit for this system's
write profile: ingestion is operator-driven and low-volume, and the highest-value rows
(`extracted_fact`, `audit_log`) are **append-only** — nothing overwrites history, so a
restore loses only the tail, never a mutation. Tightening RPO below 24 h means WAL
archiving / PITR (`wal-g` to object storage) or a managed Postgres with continuous
backup — that is the **1,000-user tier's** managed-Postgres step
(`COST_ANALYSIS.md` §7), not a single-droplet demo control, and pretending otherwise
would be the same kind of overclaim this section exists to retire.

**§164.312 note.** `audit_log` carries a **6-year retention floor** and the retention
sweep is **report-only** (no delete path) — so the audit trail only ever grows, and
backup sizing should assume it. A restore that silently loses audit rows is a
compliance problem, not just a data problem: that is why §19.4's restore test asserts
`audit_log` row counts specifically. See `agent/COMPLIANCE.md` §(b).
