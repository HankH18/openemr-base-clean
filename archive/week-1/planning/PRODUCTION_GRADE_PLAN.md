# PRODUCTION_GRADE_PLAN.md — Production-Grade AgentForge, Centered on Per-Physician SMART Login

**Project:** AgentForge Clinical Co-Pilot (`agent/`, package `copilot`; UI `agent/web`)
**Status:** File-level implementation plan. No application code written by this doc.
**Depends on / supersedes the deferred item in:** `agent/research/PHYSICIAN_WRITEBACK.md` §1.4/2.2, `agent/research/WRITEBACK_PHASE1_PLAN.md` §2.2 ("Phase-3 correct answer: per-physician SMART authorization_code")
**Centerpiece:** replace the hardcoded demo clinician (`CLINICIAN_ID=42`, no login) and the system-token-for-everything read model with a real per-physician SMART `authorization_code` login, then close the remaining production gaps (HTTPS, self-hosted Langfuse, encryption at rest, audit retention, compliance mapping).

---

## 0. Grounding — what the code actually is today (verified, not assumed)

- **All interactive reads use the SYSTEM token.** `chat/service.py`, `api/routes/observations.py`, and the rounds path all build their FHIR reader via `copilot.fhir.provider.build_fhir_client(settings)` → `build_token_provider(settings)`, which returns `BackendServicesTokenProvider` (`system/*.read`) when configured, else a `StaticTokenProvider` stub. There is **no per-request, per-physician token anywhere**. `SmartAppLaunchTokenProvider` exists in `copilot/fhir/auth.py` (authorization_code + refresh) but is referenced **only in tests** — it is wired to nothing.
- **`clinician_id` is a request input, not an identity.** Every interactive route accepts `clinician_id` in the body/query: `chat.py` (`ChatRequest.clinician_id`), `observations.py` (`Query`), `rounds.py` (`StartRequest`/`AdvanceRequest`/`JumpRequest`), `writes.py` (`ProposeRequest` + `WriteCandidate.clinician_id`). The frontend passes the hardcoded `CLINICIAN_ID = 42` (`web/src/census.ts`) into `useRounds`/`useChat`/`useAlerts` and every `api.*` call (`web/src/App.tsx`).
- **Authorization is a self-owned rounding-cursor gate.** `auth/authorization.py:is_authorized(cid, pid)` returns true iff the clinician has a persisted `rounding_cursor` whose `ordered_patient_ids` contains the patient. It keys purely on the integer `clinician_id` passed in — whoever the caller claims to be.
- **Write-back Phase 1 is already implemented and OFF.** `config.writeback_enabled=False`; `ResourceOwnerPasswordTokenProvider` (password grant, shared `copilot_writer` user), `fhir/write_client.py`, `writeback/service.py`, `api/routes/writes.py`, `domain/writes.py`, `verification/writes.py`, migration `0002_audit_entry_mode.py` all exist. `WRITEBACK_PHASE1_PLAN.md` §2.4 flags the **shared-user attribution gap** and names per-physician SMART as the fix — that is exactly this plan.
- **The poller keeps the system token and must.** `worker/runtime.py` builds its FHIR client from `RefreshPipeline._fhir_client()` (system token). It is a background, non-user action; it audits `poller.read` with a minted correlation id and no clinician. **This must not change.**
- **Infra:** repo-root `docker-compose.deploy.yml` (mariadb + openemr + agent-postgres + agent + caddy). Caddy is sole ingress on `:80`, **plain HTTP at a bare IP** (198.199.68.21), with a site-wide `basic_auth` guard (`Caddyfile.example`). `SITE_ADDR_OATH=http://<ip>`, `COPILOT_OAUTH_AUTHORIZE_URL`/`TOKEN_URL` present. Langfuse is **Cloud** today (`langfuse_host` → cloud.langfuse.com; SDK pinned `>=2.55,<3`). agent-postgres/mariadb are **not** published. DB is SQLAlchemy-async + Alembic (`0001_baseline`, `0002_audit_entry_mode`); `audit_log` already has `entry_mode`. No retention sweep exists.
- **Config discipline to mirror:** feature master-switches default OFF and inert (`poller_enabled`, `writeback_enabled`), builders fail-loud when misconfigured (`WritebackDisabledError`), routes auto-mount from `api/routes/` (no `app.py` edit), and "never log tokens/secrets" is a hard rule. Every new feature here follows the same discipline.

---

## 1. DECISION SET A — Per-physician SMART `authorization_code` login (the centerpiece)

### A.1 Topology: Backend-for-Frontend (BFF), token never touches the browser

**Recommendation: the agent backend is a *confidential* SMART client and the sole holder of the physician's OpenEMR token.** The SPA never sees an access/refresh token. The browser holds only an opaque, httpOnly session cookie that references a server-side session record.

Rationale: (a) the OAuth token grants live PHI access and must be treated like the highest-value secret — the existing "tokens never logged / never leave the server" rule extends to "never sent to the browser"; (b) a confidential client keeps `client_secret` server-side where OpenEMR can enforce it; (c) it centralizes refresh/logout/revocation; (d) it fits the current same-origin Caddy topology (SPA and API on one origin) so `SameSite=Lax` cookies just work with no cross-site cookie complexity.

**Rejected alternatives:**
- *Public client + PKCE in the SPA, token in JS memory/localStorage* — exposes a PHI-scoped token to XSS; rejected.
- *Stateless JWT session with claims in the cookie* — cannot safely carry the OAuth refresh token; rejected in favor of an opaque server session.

### A.2 The login flow (exact sequence)

```
Browser (SPA)                 Agent backend (BFF)                 OpenEMR
   | no/invalid session cookie
   |-- GET /v1/auth/login -------------->|
   |                                    |  mint state + PKCE (verifier/challenge) + nonce
   |                                    |  persist login-txn (server-side, short TTL)
   |<-- 302 to authorize ---------------|
   |-------------------------------------------------------------->| /oauth2/default/authorize
   |                                                               |  ?response_type=code&client_id&redirect_uri
   |                                                               |   &scope&state&code_challenge&code_challenge_method=S256&aud
   |            (physician logs in + consents at OpenEMR)          |
   |<--------------------------------- 302 code+state ------------ |
   |-- GET /v1/auth/callback?code&state ->|
   |                                    |  validate state; exchange code (PKCE verifier + client_secret)  --> OpenEMR /token
   |                                    |  receive access+refresh+id_token; extract fhirUser/sub
   |                                    |  map fhirUser -> ClinicianId (clinician table, auto-provision)
   |                                    |  create session; store ENCRYPTED tokens; Set-Cookie httpOnly
   |<-- 302 to SPA root -----------------|
   |-- GET /v1/auth/me (cookie) -------->|  { clinician_id, display_name, expires_at }
   |<-- identity -----------------------|
   |  ... all subsequent /v1/* calls carry the cookie; clinician resolved server-side ...
   |-- POST /v1/auth/logout ------------>|  delete session; (optional) token revoke; clear cookie
```

**This is a *standalone* SMART launch** (not an EHR launch), so there is no `launch` parameter and no patient launch context — the app owns its own patient list (the rounding cursor). `aud` = the FHIR base URL (OpenEMR requires it).

### A.3 Session mechanism — DECISION

**Recommendation: opaque server-side session + httpOnly cookie.**
- Cookie: name `af_session`; value = `secrets.token_urlsafe(32)`; attributes `HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=<idle TTL>`. `Secure` **requires** HTTPS → this is why Decision Set B (HTTPS) is a hard prerequisite for enabling SMART.
- `SameSite=Lax` is correct: the only cross-site entry is the top-level GET redirect from OpenEMR to `/v1/auth/callback`, which Lax permits; all API calls are same-origin.
- CSRF: same-origin + `SameSite=Lax` + require `Content-Type: application/json` on state-changing POSTs already blocks classic CSRF. **Add a double-submit CSRF token** (`/v1/auth/me` returns a CSRF token the SPA echoes in an `X-CSRF-Token` header on POSTs) as defense-in-depth. The OAuth `state` parameter covers the callback leg.
- Idle + absolute TTL: idle (e.g. 30 min, "automatic logoff" per §164.312(a)(2)(iii)) and absolute (e.g. 12 h). Sliding refresh of the cookie on activity.

### A.4 Where the physician's OpenEMR token lives — DECISION

**Recommendation: a new `physician_session` table in agent-postgres, storing the token material encrypted at the application layer.** Never in the browser, never in a log.

`physician_session` columns:
- `session_id` (PK, the opaque cookie value, or a hash of it — store a hash so a DB leak doesn't yield live cookies; recommend storing `sha256(cookie)`).
- `clinician_id` (BIGINT, FK to `clinician`).
- `access_token_enc`, `refresh_token_enc` (BYTEA, Fernet/AES-GCM ciphertext).
- `access_expires_at`, `scope`, `fhir_user`.
- `created_at`, `last_used_at`, `absolute_expires_at`, `revoked` (bool).

Encryption: symmetric key from `COPILOT_SESSION_ENC_KEY` (secrets-manager/env; 32-byte urlsafe). Use `cryptography.fernet` (already a transitive dep via authlib's stack; add `cryptography` explicitly if needed). This is the concrete "encryption at rest for the crown-jewel secret" win (see Decision Set E).

### A.5 Token refresh + logout — DECISION

**A new `SessionTokenProvider(TokenProvider)`** (in `copilot/fhir/auth.py`) satisfies the existing `TokenProvider` protocol so `FhirClient` and `OpenEmrWriteClient` consume it **unchanged**:
- `get_token(force)`: load the session row; if the cached access token `is_fresh()` and not `force`, decrypt+return; else use the stored `refresh_token` to call OpenEMR `/token` (reuse `SmartAppLaunchTokenProvider._refresh` logic), **persist the rotated token back** to `physician_session` (OpenEMR rotates refresh tokens), and return. On refresh failure → raise `TokenAcquisitionError` (the client's one-retry then 401 surfaces as a re-login prompt).
- Because it persists on refresh, it needs a DB session factory injected — provide it via a small callable rather than importing `session_scope` for testability.

**Logout** (`POST /v1/auth/logout`): mark session `revoked`, best-effort POST to OpenEMR's token-revocation endpoint (`/oauth2/default/revoke`) for both tokens, clear the cookie. **Login** initial exchange reuses `SmartAppLaunchTokenProvider` (extended for PKCE — see A.8) as a one-shot code exchanger at the callback.

### A.6 Clinician identity — from `fhirUser`, mapped to `ClinicianId` — DECISION

The tables that key on clinician (`rounding_cursor`, `audit_log`, `last_seen`, `conversation`) all use an **integer** `clinician_id`. OpenEMR's `fhirUser` is a Practitioner reference/UUID. **Recommendation: a `clinician` mapping table that mints a stable integer surrogate** so none of the int-keyed tables change:

`clinician` columns: `id` (BIGINT PK autoincrement = the `ClinicianId.value`), `fhir_user` (unique), `openemr_username`, `display_name`, `npi` (nullable), `created_at`, `last_login_at`.

On callback: look up by `fhir_user`; if absent, insert (auto-provision) and use the new `id`. This replaces the hardcoded `42`. The demo identity (42) stays valid because it is just another integer row when auth is off.

`is_authorized` and the rounding cursor now key on the **session-resolved** `ClinicianId` (real physician), not a body param — so a physician can only round/chat/write about patients on **their own** established list, and OpenEMR independently enforces which patients that `user/` token may read. Double-gated, least-privilege.

### A.7 Scopes — least privilege — DECISION

Register the SMART app with, and request at authorize time:

```
openid fhirUser offline_access
user/Patient.read user/Observation.read user/MedicationRequest.read
user/MedicationStatement.read user/Condition.read user/AllergyIntolerance.read
user/Encounter.read user/DiagnosticReport.read
api:oemr user/vital.crus user/encounter.crus user/medication.cruds
```

- `openid fhirUser` → identity (who is logging in). `offline_access` → refresh token.
- Enumerated `user/*.read` (not `user/*.*`) → interactive reads under the physician's own ACLs, mirroring the poller's system read set but user-context.
- `api:oemr` + `user/vital.crus user/encounter.crus user/medication.cruds` → the Standard-API write surface (`WRITEBACK_PHASE1_PLAN.md` §2.1 confirms only `user/` scopes exist there). **One authorization_code token carries both `api:fhir` reads and `api:oemr` writes**, so the same per-physician token serves reads AND writes — this is what retires the password grant (Decision Set F).
- New config `COPILOT_SMART_SCOPES` (space-separated) so the scope set is operable without a code change, mirroring `backend_services_scopes`.

### A.8 Client registration in OpenEMR — confidential + PKCE — DECISION

- **Confidential client** (the BFF holds the secret): `token_endpoint_auth_method: client_secret_basic` (or `_post`), `grant_types: [authorization_code, refresh_token]`.
- **PKCE required** (OpenEMR supports S256; belt-and-braces even for a confidential client). Extend `SmartAppLaunchTokenProvider` to send `code_verifier` on `_exchange_code` (add a `code_verifier: str | None` field). This is a small, backward-compatible mod.
- **redirect_uris MUST be the HTTPS agent callback** and exact-match: `https://agentforge.<domain>/v1/auth/callback`. (OpenEMR validates redirect URIs strictly; a scheme/host mismatch = silent failure, same failure mode `DEPLOY.md` warns about for `SITE_ADDR_OATH`.)
- **Registration:** add `scripts/register_smart_app_client.py` (sibling to `register_backend_client.py`) that dynamic-registers the confidential auth-code client with the scope set + redirect URI and prints `client_id` + the SQL to enable it (`is_enabled=1, client_role='user'`). Store `client_id` in `COPILOT_SMART_APP_CLIENT_ID` (already exists) and the secret in `COPILOT_SMART_APP_CLIENT_SECRET` (new, secrets-manager only).

### A.9 How this closes the write-back gaps

- **Removes the password-grant shortcut entirely.** Writes stop using `ResourceOwnerPasswordTokenProvider` + the shared `copilot_writer` user; they use the logged-in physician's `api:oemr user/*.cruds` token. OpenEMR attributes the write to the **real physician** in its own native audit (`created_by`/`user`) — closing the shared-user attribution gap flagged in `WRITEBACK_PHASE1_PLAN.md` §2.4 and the unique-user-identification requirement (§164.312(a)(2)(i)).
- `audit_log.entry_mode` (`human_direct`) + the real `clinician_id` remain the co-pilot's attribution surface; now it is corroborated by OpenEMR-native per-physician attribution.

---

## 2. DECISION SET B — HTTPS at a real domain (Caddy auto-HTTPS)

**Prerequisite for SMART** (Secure cookies + OAuth-over-TLS + HTTPS redirect URIs).

- **Operator action (not code):** create a DNS **A record** `agentforge.<domain> → <droplet-ip>`. That is the only non-code step for TLS.
- **Caddyfile (code/config):** replace the `:80 { ... }` site label with `agentforge.<domain> { ... }`. Caddy then provisions a Let's Encrypt cert automatically on first hit (ACME HTTP-01 needs inbound :80 + :443). Keep the existing `basic_auth` guard during cutover (it can be dropped once SMART login gates the app), keep the `@api`/SPA routing.
- **Compose (`docker-compose.deploy.yml`):** publish `443:443` in addition to `80:80` on the `caddy` service (keep `80` for ACME + redirect). `caddy_data` volume already persists certs.
- **Config that must all become the https origin (single source of truth):**
  - `.env` / compose: `SITE_ADDR_OATH=https://agentforge.<domain>` (drives OpenEMR's advertised issuer + redirect validation + the JWT `aud` for backend services).
  - New: `COPILOT_PUBLIC_BASE_URL=https://agentforge.<domain>` (used to build the exact `redirect_uri` for authorize/exchange and the post-login redirect).
  - CORS: **keep same-origin** (SPA + API both served by Caddy) → `cors_allow_origins` stays empty, no CORS middleware, and cookies are first-party. If a split origin is ever needed, CORS must switch to a specific origin + `allow_credentials=True` (never `*` with credentials).
  - The SMART app's registered redirect URI (Decision A.8) must equal `${COPILOT_PUBLIC_BASE_URL}/v1/auth/callback`.
- **Outbound TLS:** `tls_verify=True` for OpenEMR calls (already the default). Inside the compose network the agent may still call `http://openemr` internally (fine); only the *browser-facing* and *issuer* URLs must be HTTPS.

---

## 3. DECISION SET C — Self-hosted Langfuse on the droplet

Goal: PHI-adjacent trace data (patient/clinician ids in span metadata) never leaves the org's infra.

- **Compose:** add two services on a new internal `observability` network (not published):
  - `langfuse-postgres` (its **own** Postgres 16, separate volume `langfuse_db` — do not share agent-postgres).
  - `langfuse` (the Langfuse server image, pinned by digest; env: `DATABASE_URL` → langfuse-postgres, `NEXTAUTH_SECRET`, `SALT`, `NEXTAUTH_URL`). Newer Langfuse also needs ClickHouse + Redis + object storage; **pin to a v2 server image** to match the code's `langfuse>=2.55,<3` SDK and keep the footprint to just Postgres. (If a v3 server is desired, that is a larger add — ClickHouse/Redis/MinIO — and a separate decision; v2 self-host is the minimal PHI-containment win.)
- **Config (no code change):** point the agent at it via env — `LANGFUSE_HOST=http://langfuse:3000` (internal) — and set `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` to keys minted in the self-hosted project. `observability/factory.py` already turns tracing on iff all three are set; nothing in code changes.
- **Ingress:** optionally expose the Langfuse UI behind Caddy at `agentforge.<domain>/langfuse` (or a separate subdomain) behind its own auth; or keep it SSH-tunnel-only. Recommend tunnel-only for least exposure.
- **Docs:** update `agent/LANGFUSE_SETUP.md` "Part 1 — Option B (self-hosted)" and `ACCESS.md` (Langfuse row) to point at the droplet instance.

---

## 4. DECISION SET D — Encryption at rest

Layered; be explicit about **what the software does** vs **what the operator must do**.

- **Application-layer (code — this plan):** encrypt the highest-value secret — the physician OAuth tokens in `physician_session` (Decision A.4) — with `COPILOT_SESSION_ENC_KEY` (Fernet). This is genuinely customer-controlled and does not depend on the platform.
- **Postgres/MariaDB posture (config/operator):**
  - `postgres:16-alpine` has no built-in TDE; MariaDB supports data-at-rest encryption but needs a keyfile + plugin config. For a single-VM demo the realistic control is **disk-level**, not engine-level.
- **DO droplet reality (operator action, documented — NOT claimed by the app):**
  - DigitalOcean encrypts droplet disks at the platform/data-center level, but that is **not customer-managed** and should not be over-claimed.
  - The achievable customer-managed win: attach a **DigitalOcean Block Storage volume (encrypted at rest by DO)** and relocate the Docker data-root or the DB named volumes (`db`, `agent_db`, `langfuse_db`) onto it. Operator step; document in `DEPLOY.md`.
  - Stronger (operator, heavier): LUKS full-disk encryption on the droplet. Document as an option, not a default.
- **Deliverable:** a "Encryption at rest" section in `COMPLIANCE.md` + `DEPLOY.md` stating the three layers and which are operator-owned.

---

## 5. DECISION SET E — Audit retention (§164.312(b), 6-year minimum)

- **Policy (documented):** `audit_log` rows are retained **≥ 6 years** and are **never** deleted before then; conversation PHI (`conversation`/`message`) has a separate, shorter clinical retention.
- **Mechanism (code):**
  - Add an index on `audit_log.at` (migration) for efficient range scans.
  - New `copilot/memory/retention.py` + `scripts/audit_retention_sweep.py`: a sweep that (a) **refuses** to delete any `audit_log` row younger than `COPILOT_AUDIT_RETENTION_YEARS` (default 6), (b) optionally purges/*archives* rows older than that to cold storage (export to file/object store before delete), (c) separately purges `conversation`/`message` older than `COPILOT_CHAT_RETENTION_DAYS`. Fail-safe: default config deletes nothing (retention >= 6y with no upper archive target = no-op), so enabling the sweep can never accidentally destroy the trail.
  - Optionally schedule it like the poller (flag-gated background task) or leave it as a cron-invoked script (recommended: script + documented cron, simplest and least risk).
- Reaffirm append-only: `record_audit` only inserts; no update/delete path exists in `MemoryRepository`. Keep it that way.

---

## 6. DECISION SET F — Retire the password-grant write path

Once Decision Set C (route cutover) lands and writes ride the per-physician token:
- `writeback/service.py:_write_client()` builds the write client from the **session** token provider (api:oemr scopes) instead of `build_write_client(settings)` (password grant).
- Deprecate `ResourceOwnerPasswordTokenProvider`, `build_write_token_provider`, and the `write_username`/`write_password`/`write_client_id`/`write_client_secret` config (keep the flag `writeback_enabled`). Delete the dedicated `copilot_writer` OpenEMR user and disable password grant in OpenEMR (`OPENEMR_SETTING_oauth_password_grant` → off).
- `WriteCandidate.clinician_id` / `ProposeRequest.clinician_id` are validated against the session identity (mismatch → 403); the write is attributed to the real physician natively.

---

## 7. DECISION SET G — `COMPLIANCE.md`

Structure (new file `agent/COMPLIANCE.md` or repo-root `COMPLIANCE.md`):

1. **Scope + disclaimer:** AgentForge is *software*, not a covered entity; it is a tool a covered entity/BA deploys. It implements technical safeguards; it does not by itself make a deployment HIPAA-compliant.
2. **§164.312 technical-safeguard map (what the app now does):**
   - **(a)(1) Access control / (a)(2)(i) Unique user identification:** per-physician SMART login → `clinician` table surrogate + `audit_log.clinician_id`; OpenEMR ACL enforcement on the `user/` token; rounding-cursor authorization gate.
   - **(a)(2)(iii) Automatic logoff:** session idle + absolute TTL.
   - **(a)(2)(iv) Encryption/decryption:** OAuth tokens encrypted at rest (Fernet); TLS in transit.
   - **(b) Audit controls:** append-only `audit_log` (read + write trail, `entry_mode`), correlation IDs, self-hosted Langfuse traces, 6-year retention sweep.
   - **(c)(1) Integrity:** append-only clinical writes, deterministic verification gate, post-write read-back, no hard delete; FHIR `entered-in-error` semantics for reversal.
   - **(d) Person/entity authentication:** delegated auth to OpenEMR via SMART; opaque server session.
   - **(e)(1) Transmission security:** Caddy Let's Encrypt HTTPS; `tls_verify` outbound; tokens never in the browser.
3. **What the DEPLOYING ORG must own (explicitly NOT claimed by the software):**
   - **BAAs:** with **Anthropic** (PHI is sent to the Claude API for synthesis/chat — the org must sign Anthropic's BAA and enable **zero-data-retention**) and with **DigitalOcean** (hosting PHI). Also any Langfuse/other subprocessors.
   - **§164.308 administrative safeguards:** risk analysis, workforce training, sanction policy, access management/provisioning, contingency plan/backups, incident response, breach notification.
   - **§164.310 physical safeguards:** facility/device controls (delegated to DO + org policy).
   - **Operational config:** OpenEMR user ACLs, strong admin credentials, DO encrypted volume, firewalling, key rotation, log review cadence.

---

## 8. PHASED SEQUENCE

Ordering principle: **HTTPS is a hard prerequisite for enabling SMART**; the SMART *backbone* can be built in parallel with infra work; the *route cutover* is the risky part and is staged behind a flag so the current no-login demo keeps working byte-for-byte until deliberately switched.

### Phase 0 — Infra hardening (parallelizable; low app-logic risk)
Can run concurrently; none touches the auth re-architecture. **B (HTTPS) must complete before Phase 2 cutover is enabled.**
- **0a HTTPS/domain** (Decision B). Operator DNS + Caddyfile/compose/config.
- **0b Self-hosted Langfuse** (Decision C). Compose + env.
- **0c Encryption-at-rest posture** (Decision D). Docs + operator; the app-side token encryption lands in Phase 1.
- **0d Audit-retention sweep + index** (Decision E).

### Phase 1 — SMART session backbone (backend, additive, `auth_mode=disabled` default → inert)
Everything here is new code and new tables; nothing existing calls it yet, so the demo is unchanged.
- Config, `clinician` + `physician_session` tables + migration, `SessionTokenProvider`, PKCE on `SmartAppLaunchTokenProvider`, token encryption, `AuthService`, `/v1/auth/*` routes, `current_clinician` dependency (with disabled-mode fallback).

### Phase 2 — Interactive auth cutover (the re-architecture; flag-gated)
Thread the per-physician token + session identity into every interactive route/service. When `auth_mode=disabled` behavior is identical to today; when `smart`, identity comes from the session and the per-physician token is used.

### Phase 3 — Frontend login
Login gate, session/identity hook, drop hardcoded `CLINICIAN_ID` (flag-driven), `credentials: 'include'`, 401→re-login, logout, CSRF header.

### Phase 4 — Retire password-grant writes (Decision F).

### Phase 5 — `COMPLIANCE.md` + doc finalization (Decision G) + `ACCESS.md`/`DEPLOY.md` updates.

**Dependency graph:** 0a → (enable) 2/3; 1 → 2 → 3 → 4; 0b/0c/0d independent; 5 drafted anytime, finalized last.

---

## 9. FILES TO ADD/CHANGE (per phase)

### Phase 0
| File | New/Mod | Purpose |
|---|---|---|
| `Caddyfile.example` (+ droplet `Caddyfile`) | mod | site label `:80`→`agentforge.<domain>`; keep `@api`/SPA routing + guard |
| `docker-compose.deploy.yml` | mod | caddy publishes `443:443`; add `langfuse` + `langfuse-postgres` services, `langfuse_db` volume, `observability` network; agent env `LANGFUSE_HOST=http://langfuse:3000` |
| `.env.deploy.example` | mod | `SITE_ADDR_OATH=https://…`, `COPILOT_PUBLIC_BASE_URL`, Langfuse self-host keys, `NEXTAUTH_SECRET`/`SALT`, `COPILOT_AUDIT_RETENTION_YEARS` |
| `agent/copilot/config.py` | mod | `public_base_url`, `audit_retention_years`, `chat_retention_days` |
| `agent/copilot/memory/retention.py` | **new** | retention policy + sweep (never deletes audit < 6y) |
| `agent/scripts/audit_retention_sweep.py` | **new** | invocable sweep (cron) |
| `agent/migrations/versions/0003_audit_at_index.py` | **new** | index `audit_log(at)` |
| `agent/LANGFUSE_SETUP.md`, `DEPLOY.md`, `ACCESS.md` | mod | self-host + HTTPS + encrypted-volume operator steps |

### Phase 1 (SMART backbone — additive)
| File | New/Mod | Purpose |
|---|---|---|
| `agent/copilot/config.py` | mod | `auth_mode` (`disabled`\|`smart`, default `disabled`), `smart_app_client_secret`, `smart_scopes`, `session_enc_key`, `session_cookie_name`, `session_idle_seconds`, `session_absolute_seconds` |
| `agent/copilot/fhir/auth.py` | mod | add `code_verifier` (PKCE) to `SmartAppLaunchTokenProvider`; add `SessionTokenProvider` (store-backed refresh+persist) |
| `agent/copilot/auth/session.py` | **new** | crypto helpers (Fernet), cookie build/parse, session id hashing |
| `agent/copilot/auth/identity.py` | **new** | id_token/userinfo → `fhirUser`/`sub`; map/auto-provision `ClinicianId` |
| `agent/copilot/auth/service.py` | **new** | `AuthService`: `begin_login` (state+PKCE), `complete_login` (exchange+persist), `logout`, `resolve_session` |
| `agent/copilot/api/routes/auth.py` | **new** | `GET /v1/auth/login`, `GET /v1/auth/callback`, `GET /v1/auth/me`, `POST /v1/auth/logout` (auto-mounted) |
| `agent/copilot/api/deps.py` | **new** | `current_clinician` FastAPI dependency: session→`ClinicianId` when `smart`; body/query fallback when `disabled` |
| `agent/copilot/memory/models.py` | mod | `ClinicianRow`, `PhysicianSessionRow`, `LoginTxnRow` (or reuse session table for txn) |
| `agent/copilot/memory/repository.py` | mod | CRUD for clinician mapping + session (get/create/rotate/revoke) |
| `agent/migrations/versions/0004_clinician_and_sessions.py` | **new** | new tables |
| `agent/scripts/register_smart_app_client.py` | **new** | register confidential auth-code client + redirect URI + scopes |
| `agent/tests/test_auth_session.py`, `test_auth_routes.py`, `test_identity_mapping.py`, `test_session_token_provider.py` | **new** | PKCE/state, exchange, refresh-persist, cookie, auto-provision, disabled-mode fallback |

### Phase 2 (cutover — flag-gated)
| File | New/Mod | Purpose |
|---|---|---|
| `agent/copilot/fhir/provider.py` | mod | `build_fhir_client_for_session(settings, token_provider)` + `build_write_client_for_session(...)`; keep system-token builders for the poller |
| `agent/copilot/chat/service.py` | mod | accept a per-request token provider; use it for reads+verifier |
| `agent/copilot/rounds/service.py` | mod | reads via per-physician token; cursor keyed on session clinician |
| `agent/copilot/writeback/service.py` | mod | write client from session token (retire password path in Phase 4) |
| `agent/copilot/api/routes/{chat,rounds,observations,writes,alerts,refresh}.py` | mod | resolve clinician via `current_clinician`; ignore/validate body `clinician_id`; pass session token into services |
| `agent/tests/test_*_route.py` | mod | add authenticated-session cases; assert disabled-mode unchanged |

### Phase 3 (frontend)
| File | New/Mod | Purpose |
|---|---|---|
| `agent/web/src/state/useAuth.ts` | **new** | fetch `/v1/auth/me`; `{clinicianId, displayName, status}`; login/logout |
| `agent/web/src/components/LoginGate.tsx` | **new** | "Sign in with OpenEMR" → `window.location = /v1/auth/login`; render app only when authed |
| `agent/web/src/App.tsx` | mod | wrap in `LoginGate`; source clinician id from `useAuth` (flag: fall back to `CLINICIAN_ID` when auth disabled) |
| `agent/web/src/census.ts` | mod | `CLINICIAN_ID` demo-only; identity now from session |
| `agent/web/src/api/http.ts` | mod | `credentials: 'include'`; `X-CSRF-Token` on POSTs; 401→trigger re-login |
| `agent/web/src/api/client.ts` | mod | drop `clinicianId` args where server-resolved (or keep as no-op for mock parity) |
| `agent/web/src/components/TopBar.tsx` | mod | show logged-in physician + Logout |

### Phase 4 / 5
| File | New/Mod | Purpose |
|---|---|---|
| `agent/copilot/config.py`, `fhir/auth.py`, `fhir/provider.py`, `writeback/service.py` | mod | remove password-grant provider/config; writes on session token |
| `docker-compose.deploy.yml` / OpenEMR setting | mod | disable password grant; drop `copilot_writer` |
| `COMPLIANCE.md` | **new** | §164.312 map + org-owned section |

---

## 10. CONTRACTS

- `GET /v1/auth/login` → `302` to OpenEMR authorize (sets short-lived login-txn cookie).
- `GET /v1/auth/callback?code&state` → validate `state`; exchange; `302` to SPA; `Set-Cookie: af_session=…; HttpOnly; Secure; SameSite=Lax`. Errors (bad state, exchange fail) → `302` to `/?login_error=…`, never a 500 leaking detail.
- `GET /v1/auth/me` → `200 {clinician_id, display_name, fhir_user, expires_at, csrf_token}` when authed; `401` otherwise.
- `POST /v1/auth/logout` → `204`; clears cookie + revokes.
- `current_clinician` dependency → `ClinicianId`. `smart` mode: from session or `401`. `disabled` mode: from body/query (exactly today).
- `SessionTokenProvider.get_token(force)` → fresh `OAuthToken`, persisting rotations; raises `TokenAcquisitionError` on unrecoverable refresh failure.
- Interactive routes under `smart` mode: **ignore** any `clinician_id` in the body except to reject a mismatch with the session (`403`).

---

## 11. RISKS & MITIGATIONS

- **Auth re-architecture touches every interactive route + the frontend + breaks the no-login demo.** *Mitigation:* the `auth_mode` flag (default `disabled`) makes Phases 1–2 inert; `current_clinician` has an explicit disabled-mode fallback to today's body/query id; ship behind the flag, enable only after HTTPS + registration are verified. Add a test matrix that runs the existing route tests in **both** modes.
- **Token exposed to the browser.** *Mitigation:* BFF pattern — token only in the encrypted server session; cookie is opaque + httpOnly; token never serialized to any response.
- **CSRF on state-changing POSTs.** *Mitigation:* same-origin + `SameSite=Lax` + JSON content-type + double-submit CSRF token.
- **Redirect-URI / issuer mismatch** (the silent-failure class `DEPLOY.md` already warns about). *Mitigation:* single `COPILOT_PUBLIC_BASE_URL` builds both the registered redirect URI and the authorize/exchange `redirect_uri`; a readiness check asserts they match.
- **Refresh-token rotation races** (two concurrent requests both refresh). *Mitigation:* refresh under a per-session DB row lock (`SELECT … FOR UPDATE`) or single-flight; OpenEMR refresh-token rotation means the loser must re-read, not reuse.
- **Identity spoof via body `clinician_id`.** *Mitigation:* in `smart` mode the body id is authorization-irrelevant; session is the sole source of truth; mismatch → 403.
- **Poller accidentally gaining a user/write scope.** *Mitigation:* poller keeps `BackendServicesTokenProvider`; the session/write builders are only reachable from the interactive request path (same invariant `WRITEBACK_PHASE1_PLAN.md` §2.4 already enforces).
- **Self-hosted Langfuse version drift** (SDK v2 vs server v3 needing ClickHouse/Redis). *Mitigation:* pin the server to a v2 image to match the `langfuse<3` SDK; a v3 migration is a separate, larger decision.
- **Losing the audit trail via the new sweep.** *Mitigation:* sweep hard-refuses to delete `audit_log` younger than 6y; default config is a no-op.
- **Cookie `Secure` before HTTPS exists** → login silently fails on plain HTTP. *Mitigation:* enforce ordering — SMART cannot be enabled until Phase 0a lands; a startup check refuses `auth_mode=smart` when `public_base_url` is not https.

---

## 12. OPERATOR / USER ACTIONS (not code)

1. **DNS:** create A record `agentforge.<domain> → <droplet-ip>`; ensure inbound :80 + :443.
2. **OpenEMR SMART app registration:** run `scripts/register_smart_app_client.py` against `https://agentforge.<domain>`; enable the client (`is_enabled=1`); record `client_id` + `client_secret` into `.env` (`COPILOT_SMART_APP_CLIENT_ID`/`_SECRET`). Ensure each physician has an OpenEMR user with correct ACLs (`encounters/notes`, `patients/med` for writers).
3. **Set the https origin everywhere:** `SITE_ADDR_OATH` + `COPILOT_PUBLIC_BASE_URL` = `https://agentforge.<domain>`; redeploy.
4. **Self-hosted Langfuse:** bring up the containers; create a project; mint keys into `.env`.
5. **Encryption at rest:** attach a DO **encrypted block volume**, relocate DB/Docker volumes onto it; generate + store `COPILOT_SESSION_ENC_KEY`.
6. **Audit retention:** set `COPILOT_AUDIT_RETENTION_YEARS=6`; schedule `audit_retention_sweep.py` (cron) + a backup job.
7. **Disable password grant** and remove the `copilot_writer` user after Phase 4.
8. **BAAs & governance (org-owned):** sign **Anthropic** BAA + enable zero-data-retention; sign **DigitalOcean** BAA; complete §164.308 risk analysis, workforce training, contingency/backup, incident-response.

---

## 13. PARALLELIZATION & MODEL-TIERING

- **Parallel:** Phase 0a/0b/0c/0d are mutually independent. Phase 1 (backbone) can be built alongside Phase 0. Phase 5 (COMPLIANCE.md draft) anytime.
- **Sequential:** 0a(HTTPS) → enable 2/3; 1 → 2 → 3 → 4.
- **Senior/careful work (do not delegate to a cheap model):** `SessionTokenProvider` + refresh-rotation, PKCE/state handling, the route cutover and `current_clinician` semantics, token encryption, and the write-attribution change — these are the security-load-bearing pieces. Delegate them to Opus at high effort.
- **Cheaper-model-suitable:** Caddyfile/compose/env edits, the Langfuse compose block, the frontend login-gate/hook boilerplate and `credentials: 'include'` wiring, test scaffolding, the retention sweep script, and the COMPLIANCE.md prose (from this outline).
