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
