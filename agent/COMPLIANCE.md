# AgentForge Clinical Co-Pilot — HIPAA Security Rule (§164.312) Technical-Safeguard Mapping

This document maps the AgentForge Clinical Co-Pilot service (`agent/`, package
`copilot`) to the **Technical Safeguards** of the HIPAA Security Rule
(45 CFR §164.312). It states, per safeguard, exactly what the software does,
distinguishing two layers throughout: the **code default** (what a fresh deploy
does out of the box) and the **reference deployment** (the live droplet at
`https://agentforge.hankholcomb.com`, which opts several safeguards on). Where a
control is present in code but off by default, it says so; where the reference
deployment enables it, it says that too; and it flags what is **not yet built**.
It then enumerates the safeguards and obligations that **only the deploying
organization can satisfy** — the software cannot and does not claim them.

Every claim below is grounded in a named module/behavior in this repository.
Where a control is aspirational, flag-gated, or an operator responsibility, it
is labeled as such. Nothing here should be read as a certification of
compliance.

---

## 1. Scope and disclaimer

**AgentForge is software, not a covered entity.** It is a tool that a HIPAA
covered entity or business associate deploys inside its own environment,
alongside an OpenEMR instance it already operates. The software *implements
technical safeguards* that help a compliant deployment; **it does not, by
itself, make any deployment HIPAA-compliant.** Compliance is a property of the
whole environment — administrative policy, physical controls, executed
Business Associate Agreements (BAAs), workforce practices, and correct
operational configuration — most of which is outside this codebase and is the
deploying organization's responsibility (see §3).

Two facts about data flow shape everything below:

1. **AgentForge reads (and, when enabled, writes) PHI in OpenEMR exclusively
   through OpenEMR's FHIR and Standard REST APIs over OAuth2** — never by
   direct database access. OpenEMR remains the system of record and its own
   access controls and audit trail continue to apply.
2. **AgentForge sends PHI to the Anthropic Claude API** for chart synthesis
   and clinician chat (`copilot/config.py` — `anthropic_api_key`,
   `anthropic_base_url`, `anthropic_model_*`). This makes Anthropic a
   subprocessor handling PHI, which the deploying organization must cover by
   BAA (see §3).

### The reference deployment

A live reference deployment runs at `https://agentforge.hankholcomb.com` on a
DigitalOcean droplet. Two configuration facts about it drive the "reference
deployment" column throughout this map:

- **HTTPS is live.** Caddy terminates TLS with automatically-provisioned Let's
  Encrypt certificates (`Caddyfile.https.example`); plain-HTTP requests redirect
  to HTTPS. There is **no basic-auth guard** — the earlier bare-IP demo's
  `basic_auth` block has been removed.
- **Per-physician SMART login is live.** The agent runs with
  **`COPILOT_AUTH_MODE=smart`**, so the SMART session is the access gate:
  interactive data routes return `401` without an authenticated session (verified
  against the live service). Identity comes from the server-side session, not the
  request.

The crucial honest distinction: the **code default is still
`auth_mode=disabled`** (a fresh deploy has *no* login and trusts the
request-supplied identity — see `config.py:auth_mode`). The reference deployment
*opts the per-physician login stack on*. So each SMART-dependent safeguard below
is **implemented; enabled on the reference deployment; off by default in code**.

Two things remain off even on the reference deployment, and are kept honest
below: physician **write-back** (`writeback_enabled=false` ⇒ routes return `503`),
and **self-hosted** tracing — the reference deployment currently traces to
**Langfuse Cloud** (`us.cloud.langfuse.com`) with keys set, which makes Langfuse
a PHI-adjacent subprocessor requiring a BAA for production (or a switch to the
built-in self-hosted stack; see §3.1 and (b)).

### Status legend

| Label | Meaning |
|---|---|
| **Implemented** | Code is present and active in the default configuration. |
| **Implemented — gated OFF** | Code is present but disabled by a default-off flag; inert until an operator opts in. |
| **Live on reference deployment** | Code is present and **off by default in code**, but the reference deployment (`agentforge.hankholcomb.com`) opts it on — so it is an **active control there**. A fresh deploy must enable it. |
| **Config surface only** | Configuration knobs exist, but the enforcing code path is not yet built/wired. |
| **Operator / deployment step** | Depends on infrastructure or configuration outside the application code. |
| **Planned** | Designed in `agent/research/PRODUCTION_GRADE_PLAN.md`; no implementing code yet. |

---

## 2. §164.312 Technical-safeguard map

### Summary table

| §164.312 provision | Status | Where in code |
|---|---|---|
| (a)(1) Access control | **Live on reference deployment** — fail-closed authorization gate + per-physician SMART login + data-route identity enforcement + delegated per-physician tokens for interactive reads/writes (smart mode); enabled on the reference deployment, off by default in code (`auth_mode=disabled`) | `copilot/auth/*`; `api/deps.py:resolve_acting_context`; `fhir/provider.py:build_*_for_session`; `config.py:auth_mode` |
| (a)(2)(i) Unique user identification | **Live on reference deployment** — SMART login; in smart mode every data route takes identity from the session (401 no session / 403 on mismatch), not the request; enabled on the reference deployment, off by default in code | `copilot/auth/{service,identity,session}.py`, `api/routes/{auth,chat,rounds,observations,writes}.py`, `api/deps.py`, `migrations/0004` |
| (a)(2)(iii) Automatic logoff | **Live on reference deployment** — idle + absolute TTL enforced in the session store; active under smart mode on the reference deployment, off by default in code | `copilot/auth/service.py:resolve_session`; `config.py:session_idle_seconds`, `session_absolute_seconds` |
| (a)(2)(iv) Encryption/decryption | **Implemented** — outbound TLS active by default; token-at-rest Fernet encryption **live on the reference deployment** (smart mode), off by default in code | `config.py:tls_verify` (default `True`); `copilot/auth/session.py:SessionCrypto`; `config.py:session_enc_key` |
| (b) Audit controls | **Implemented** — append-only trail; retention sweep implemented (report-only, 6-yr floor); distributed tracing active on the reference deployment via Langfuse **Cloud** (subprocessor — BAA required) | `memory/models.py:AuditLogRow`, `memory/repository.py:record_audit`, `api/middleware.py`, `memory/retention.py`, `observability/factory.py` |
| (c)(1) Integrity | **Implemented** (physician write path still gated OFF — `writeback_enabled=false` ⇒ 503, on the reference deployment too) | `verification/core.py`, `verification/writes.py`, `fhir/write_client.py`, `writeback/service.py` |
| (d) Person or entity authentication | **Live on reference deployment** — SMART login delegated to OpenEMR; enabled on the reference deployment (`auth_mode=smart`), off by default in code | `copilot/auth/service.py`, `fhir/auth.py:{SmartAppLaunchTokenProvider,SessionTokenProvider}`, `api/routes/auth.py` |
| (e)(1) Transmission security | **Live on reference deployment** — outbound TLS + no-token-logging **implemented** (default on); browser-facing **HTTPS live** (Caddy + Let's Encrypt); BFF token-never-in-browser active under smart mode; all off by default in code / a fresh deploy must stand up HTTPS | `config.py:tls_verify`; `fhir/auth.py`; `copilot/auth/session.py`; `Caddyfile.https.example` (deploy) |

The subsections below give the precise, honest detail behind each row.

---

### (a)(1) Access control / (a)(2)(i) Unique user identification

**What is implemented today.** A serve-time authorization gate exists in
`copilot/auth/authorization.py`. `is_authorized(clinician_id, patient_id)`
returns true only if the clinician has a persisted **rounding cursor** whose
`ordered_patient_ids` contains the patient; a clinician with no cursor has an
empty authorized set and is refused. This is **fail-closed** by construction
(no cursor ⇒ no access). Reads via the Standard/FHIR API are additionally
constrained by whatever token the service holds and by OpenEMR's own ACLs.

**The code default (honest limitation of a fresh deploy).** In the default
configuration (`config.py:auth_mode` defaults to `"disabled"`), the
`clinician_id` that the authorization gate keys on is **supplied by the request**
(body/query), not established by an authenticated login. A fresh deploy therefore
trusts the caller's asserted identity — **out of the box AgentForge does not
authenticate the individual physician.** This is the code default; the reference
deployment overrides it (below). Background reads by the poller run under a
system (Backend Services) token and are non-interactive by design regardless of
auth mode (`worker/runtime.py`, audited as `poller.read` with no clinician).

**What is implemented and enabled on the reference deployment.** The
per-physician SMART login backbone exists in code (behind `auth_mode`, default
`"disabled"`) and is **turned on for the reference deployment**
(`COPILOT_AUTH_MODE=smart`):
`SmartAppLaunchTokenProvider` (`copilot/fhir/auth.py`) implements the
physician-delegated `authorization_code` + `refresh_token` exchange with **PKCE**;
`SessionTokenProvider` (same file) refreshes and re-encrypts the token on the
fly; the `/v1/auth/login|callback|me` + logout routes (`api/routes/auth.py`), the
server-side session store (`clinician` / `physician_session` / `login_txn` tables,
migration `0004`), Fernet token encryption + opaque hashed session cookies
(`copilot/auth/session.py`), `fhirUser`→`ClinicianId` auto-provisioning
(`copilot/auth/identity.py`), and a `current_clinician` dependency
(`copilot/api/deps.py`) are all built and unit-tested. A startup guard
(`AuthService.ensure_smart_ready`) refuses `auth_mode=smart` unless
`public_base_url` is `https://…` and the session key + client id are set.

**Data-route enforcement (Phase 2 — done).** In `smart` mode the interactive
routes (chat, rounds, observations, writes, and the clinician-scoped alerts /
refresh) now take the acting clinician from the authenticated session via
`api/deps.py:resolve_acting_clinician` — `401` without a valid session, `403` if
a request tries to assert a different `clinician_id`. The session is
authoritative; the request-supplied id can no longer be trusted. Because the
reference deployment runs `auth_mode=smart`, this per-physician identity
enforcement is **active on the data path there** (verified live: interactive
routes return `401` without a session), and the agent's own `audit_log`
attributes each action to the logged-in physician.

**Delegated per-physician tokens (done).** Interactive reads (chat, rounds-start,
observations) and writeback commits now call OpenEMR under the logged-in
physician's *own* delegated SMART token (`fhir/provider.py:build_*_for_session`),
so OpenEMR's *own native* audit — not just the agent's `audit_log` — attributes
them to the individual physician (least-privilege). Two endpoints intentionally
retain the system token because they drive the shared poller machinery
(`RefreshPipeline`), not a per-physician clinical read: `POST /v1/rounds/refresh`
and `GET /v1/rounds/alerts` (their *identity* is still session-enforced). The
background poller is likewise system-token by design.

**On the reference deployment this is enablement-complete.** The reference
deployment has stood up the two operator prerequisites — browser-facing HTTPS
(Caddy + Let's Encrypt, `Caddyfile.https.example`) and an OpenEMR SMART-client
registration — and runs with `auth_mode=smart`, so per-physician identity
enforcement is **live** there. A **fresh deploy is still login-less**
(`auth_mode` defaults to `disabled` in code) until an operator performs the same
enablement.

> **Deployment guidance.** The reference deployment runs SMART login over HTTPS,
> so it authenticates each physician individually. **A fresh deploy that leaves
> `auth_mode=disabled` does not** — until SMART login is enabled over HTTPS, treat
> such a deploy as a **single-tenant, trusted-network tool**: restrict network
> access to it (see §3, firewalling / ingress auth), and rely on OpenEMR's own
> user accounts and ACLs for individual accountability of the underlying record.

### (a)(2)(iii) Automatic logoff — *addressable*

**Live on the reference deployment (off by default in code).** The idle and
absolute session-lifetime knobs exist (`config.py:session_idle_seconds`, default
1800 s / 30 min; `session_absolute_seconds`, default 43200 s / 12 h) and are
**enforced** by the session store: `AuthService.resolve_session`
(`copilot/auth/service.py`) rejects a session past its idle or absolute deadline
(sliding-refresh on activity), and logout revokes it. Because the reference
deployment runs `auth_mode=smart` over HTTPS, this machinery is **active there**.
On a fresh deploy with `auth_mode=disabled` it is inert (no physician sessions
exist), and session expiration for the underlying record is governed by OpenEMR's
own logoff settings. Do not claim automatic logoff as active for a deploy that
has not enabled `auth_mode=smart`.

### (a)(2)(iv) Encryption and decryption — *addressable*

**Implemented — in transit (outbound).** All outbound calls from AgentForge to
OpenEMR verify TLS by default: `config.py:tls_verify` defaults to `True`, and
both the read client and the write client honor it
(`fhir/provider.py`, `fhir/write_client.py` — `verify=settings.tls_verify`).
`tls_verify=False` exists only for a local self-signed dev stack and must not be
used in production.

**Live on the reference deployment (off by default in code) — at rest
(application layer).** The highest-value secret (physician OAuth access +
refresh tokens) is encrypted at rest with a Fernet key
(`config.py:session_enc_key`) via `SessionCrypto` (`copilot/auth/session.py`);
the `physician_session` table stores only Fernet ciphertext (`LargeBinary`) and a
`sha256` hash of the session cookie, never the plaintext token or cookie. Because
the reference deployment runs `auth_mode=smart`, physician sessions are created
and this encryption is **active there**. On a fresh deploy with
`auth_mode=disabled` it is inert (exercised by tests only), because no physician
session is created until login is on.

**Operator / deployment step — at rest (data store).** Encryption at rest for
the agent's own PostgreSQL and for OpenEMR's MariaDB is **not** provided by the
application. On a single-VM deployment the realistic control is disk/volume
level (e.g. a DigitalOcean encrypted Block Storage volume hosting the DB
volumes, or LUKS full-disk encryption). This is an operator responsibility
(see §3). DigitalOcean's platform-level disk encryption is **not
customer-managed** and is deliberately not over-claimed here.

### (b) Audit controls — *required*

**Implemented.** This is the strongest safeguard in the current build.

- **Append-only audit trail.** `audit_log` (`memory/models.py:AuditLogRow`) is
  written only through `MemoryRepository.record_audit`
  (`memory/repository.py`), which performs an **INSERT only** — there is no
  update or delete path for audit rows anywhere in the codebase. The trail is
  therefore append-only by construction, not merely by policy.
- **Reads are trailed.** Every PHI read produces a row: interactive chat
  (`chat/service.py`, action `"chat"`, recording exactly the FHIR resources the
  answer cited), the observation series endpoint
  (`api/routes/observations.py`, action `"observations.series"`), and each
  background poller tick (`worker/runtime.py`, action `"poller.read"`). Audit
  writes are **fail-open** — a broken audit write is logged and swallowed so it
  can never turn a served read into an error — a deliberate trade-off that
  favors clinical availability; monitor audit-write failures operationally.
- **Writes are trailed with attribution.** The write path records
  `write_proposed`, `write_committed`, and `write_failed`
  (`writeback/service.py`), each carrying `entry_mode` (`"human_direct"` in the
  current phase) via the nullable `audit_log.entry_mode` column
  (`memory/models.py`, migration `0002_audit_entry_mode.py`).
- **Correlation IDs.** `CorrelationIdMiddleware` (`api/middleware.py`) binds a
  validated `X-Correlation-ID` per request and echoes it on the response;
  background ticks mint their own. Every audit row stores the correlation id,
  so a trail entry can be tied back to the originating request or tick.
- **Distributed tracing (active on the reference deployment via Langfuse
  Cloud).** `observability/factory.py` returns a real Langfuse tracer only when
  all three Langfuse env vars are set; otherwise a no-op. The reference
  deployment **has those keys set and points at Langfuse Cloud**
  (`us.cloud.langfuse.com`), so tracing is **live** there. Traces can carry
  patient/clinician ids in span metadata, which makes **Langfuse Cloud a
  PHI-adjacent subprocessor** — a production deployment must cover it by BAA
  (see §3.1) or switch to the **self-hosted** Langfuse stack, which is **built
  into the deploy compose and off by default** (`docker-compose.deploy.yml`,
  `agent/LANGFUSE_SETUP.md`, `PRODUCTION_GRADE_PLAN.md` §3) to keep trace
  metadata on the organization's own infrastructure. On a fresh deploy with no
  keys, tracing is a no-op.

**Retention — implemented (report-only) with a hard 6-year floor.**
`config.py:audit_retention_years` defaults to **6** (the HIPAA §164.312(b)
documentation-retention floor) and `chat_retention_days` defaults to **0**
(never purge chat PHI unless an operator opts in). An operator-invoked sweep
(`copilot/memory/retention.py`, CLI `scripts/audit_retention_sweep.py`, default
`--dry-run`; index `ix_audit_log_at` via migration `0003`) enforces this
**fail-safe**: (1) there is **no `DELETE` statement against `audit_log` anywhere
in the codebase** — the sweep's audit deletion count is hard-coded `0` and gated
on a cold-storage archive target that is not wired in, so no configuration value
can purge audit rows; (2) a hard `HIPAA_AUDIT_FLOOR_YEARS = 6` constant means
even a misconfigured sub-6-year `audit_retention_years` can never even *report* a
younger row as eligible (a `below_floor` warning is logged instead). Chat purge
is separate and opt-in (`chat_retention_days > 0`). The current effective
behavior for the audit trail therefore remains "retain everything" — the safe
default — while the sweep provides the enforced floor and the seam for a future
archive-then-delete step. Backups and long-term archival of the audit trail are
an operator responsibility (see §3).

### (c)(1) Integrity — *required (addressable mechanism to authenticate ePHI)*

**Implemented** (with the physician write path gated OFF by default —
`config.py:writeback_enabled` defaults to `False`; `fhir/provider.py`
raises `WritebackDisabledError` unless explicitly enabled and configured, and the
write routes return `503` while it is off — `api/routes/writes.py`. **This
remains off on the reference deployment too**: write-back is the one safeguard
the reference deployment has *not* opted on).

- **Deterministic read-side verification gate.** `verification/core.py`
  (`Verifier`) checks every synthesized claim against the source FHIR resource:
  attribution (the cited resource must exist), verbatim value match, numeric-
  literal presence, and temporal grounding. The gate is **not promptable** (a
  claim injected via free text still has to cite a real resource and match its
  value) and **fail-closed**: if no claim verifies the result is `withheld`; a
  mixed result is `degraded` (only proven claims survive). Fabrications fail
  attribution or value match.
- **Deterministic write-side verification gate.** `verification/writes.py`
  (`verify_write`) validates a typed `WriteCandidate` against a closed set of
  metrics (exhaustive `match`, no `default`), enforces unit sanity (mismatch is
  a hard block), and checks physiologic plausibility. It is pure and
  non-promptable; no free text reaches OpenEMR.
- **Confirmed, append-only writes.** `fhir/write_client.py` treats a write as
  successful **only** on an explicit `201` (create) / `200` (update) whose body
  carries a parseable id; anything ambiguous — non-2xx, unparseable body,
  transport error/timeout — raises `OpenEmrWriteError` and the write is treated
  as **FAILED, never assumed committed**. Writes are append-only (a new form /
  list row); there is **no hard delete** — a reversal is a *compensating
  append* (`retract_medication` end-dates the record).
- **Propose → confirm with re-verification and read-back.**
  `writeback/service.py` re-runs the identical deterministic verification on
  confirm (a tampered re-send cannot slip through), commits append-only, then
  performs a post-write read-back to log any value mismatch. Idempotency keys
  guard against double-submitted confirms.

### (d) Person or entity authentication — *required*

**Live on the reference deployment (off by default in code).** AgentForge
delegates authentication of the individual physician to OpenEMR via SMART App
Launch (`authorization_code` + PKCE), with an opaque, httpOnly, server-side
session on the agent side. The full login flow exists — token-exchange provider
(`fhir/auth.py:SmartAppLaunchTokenProvider`), `/v1/auth/*` routes, session store,
and identity resolution (see (a)(2)(i)) — and the **reference deployment runs it
with `auth_mode=smart` over HTTPS**, so physicians authenticate individually
through OpenEMR before reaching any data route (verified live: `/v1/auth/me`
returns `401` without a session). It is **off by default in code**: `auth_mode`
defaults to `"disabled"`, and enabling it requires browser HTTPS + an OpenEMR
client registration. **On a fresh deploy that leaves the default, the software
does not independently authenticate the end user** and relies on the deploying
organization's network/ingress controls plus OpenEMR's own authentication for
the record.

### (e)(1) Transmission security — *required*

**Implemented — outbound.**
- Outbound TLS verification is on by default (`config.py:tls_verify=True`),
  covering AgentForge→OpenEMR and AgentForge→Anthropic transport.
- **Secrets are never logged.** Token providers hold passwords/secrets with
  `repr=False` and the code follows a hard "never log tokens/secrets" rule
  (`fhir/auth.py`); the write client logs status codes, never token material or
  raw server messages.

**Live on the reference deployment — browser edge.** HTTPS termination for
browser-facing traffic is provided by the deployment's reverse proxy (Caddy with
automatic Let's Encrypt certificates) and requires a real domain + DNS — an
operator step (`Caddyfile.https.example`, `PRODUCTION_GRADE_PLAN.md` §2). **The
reference deployment now runs HTTPS at `https://agentforge.hankholcomb.com`**
(Caddy + Let's Encrypt; verified live — HTTP/2 served with a valid certificate,
plain HTTP redirects to HTTPS), and the earlier bare-IP demo's **basic-auth guard
has been removed**: the per-physician SMART session is the access gate instead
(`Caddyfile.https.example` ships with no `basic_auth` block, on the rationale
that in `smart` mode unauthenticated requests already get `401`). Browser-facing
HTTPS must still be stood up on any fresh deploy before it is exposed to real PHI
or before per-physician SMART login (whose `Secure` cookies require TLS) can be
enabled. The "token never touches the browser" (Backend-for-Frontend) property
**is implemented** in the SMART login backbone — the physician's OpenEMR token
lives only in the encrypted server-side `physician_session`, and the browser
holds only an opaque hashed cookie — and is **active on the reference deployment**
because it runs `auth_mode=smart`. On a fresh deploy with `auth_mode=disabled` no
physician token exists in the browser because no browser-side token flow exists
at all.

---

## 3. What the deploying organization must own (NOT claimed by the software)

The following are **outside the software's control** and are required for a
compliant deployment. AgentForge makes none of these claims on the
organization's behalf.

### 3.1 Business Associate Agreements (and subprocessors)

- **Anthropic — required.** AgentForge sends PHI to the Anthropic Claude API for
  synthesis and chat. The organization **must execute Anthropic's BAA and
  enable zero-data-retention (ZDR)** for the account/keys AgentForge uses.
  Without a signed BAA + ZDR, sending PHI to the API is not permissible. The
  API key is injected by the operator (`config.py:anthropic_api_key`);
  `anthropic_base_url` may be pointed at an approved gateway if required.
- **DigitalOcean (or the chosen host) — required.** If PHI-bearing services
  (OpenEMR, the agent DB, backups) run on DigitalOcean, the organization **must
  execute DigitalOcean's BAA** covering the hosting.
- **Langfuse Cloud — currently in use on the reference deployment.** The
  reference deployment traces to **Langfuse Cloud** (`us.cloud.langfuse.com`)
  with keys set, and trace span metadata can include patient/clinician ids. A
  production deployment that keeps Langfuse Cloud **must execute a Langfuse BAA**;
  the alternative is the **self-hosted** Langfuse stack shipped (off by default)
  in `docker-compose.deploy.yml` (see `agent/LANGFUSE_SETUP.md`), which keeps
  trace metadata on the organization's own infrastructure.
- **Voyage AI + Cohere — required *if keyed* (Week-2 retrieval).** When
  `VOYAGE_API_KEY` / `COHERE_API_KEY` are set, the clinician's **retrieval query
  text** leaves the deployment to Voyage (embedding) and Cohere (rerank). Keyless
  is the default and the deterministic stubs make **zero outbound calls**, so an
  unkeyed deploy has no Voyage/Cohere egress at all. Every query is routed through
  the `deidentify()` choke point first (`copilot/rag/retriever.py:150`) — a single,
  real, architecturally-enforced control. **But state its limit honestly:** the
  scrub is a deterministic regex pass (`copilot/rag/deidentify.py`), not a model.
  It removes structured identifiers by shape (email, SSN, dates, phone, 5+-digit
  runs such as MRNs) and *label-gated* names (`Patient: <Name>`, `deidentify.py:50-53`),
  and it **does not remove an arbitrary free-text name** a clinician types into a
  question. It is therefore **not de-identification in the §164.514 Safe Harbor
  sense**, and a keyed deployment must not treat it as such. An organization
  enabling these keys **must execute Voyage and Cohere BAAs** (or route to an
  approved gateway), exactly as for any other PHI-capable subprocessor — the
  scrub reduces, but does not eliminate, the possibility that identifying text
  reaches them. Document **images and full chart PHI never go to Voyage/Cohere**;
  they go only to Anthropic (above), which is BAA-covered.
- **Any other subprocessor** that can see PHI or PHI-adjacent metadata —
  including any monitoring/log aggregation — must be covered by a BAA or must not
  receive PHI.

### 3.2 §164.308 Administrative safeguards

Entirely organization-owned; the software neither provides nor substitutes for
any of these:

- Security risk analysis and risk management.
- Workforce training, sanction policy, and termination/access-revocation
  procedures.
- Access management and provisioning (who gets an OpenEMR account, with which
  ACLs; who may reach AgentForge).
- Contingency plan: **data backup**, disaster recovery, and emergency-mode
  operation — including **long-term backup and archival of the append-only
  audit trail** (the app retains but does not back up).
- Incident response and breach notification procedures.
- Business Associate management and periodic evaluation.

### 3.3 §164.310 Physical safeguards

Facility access, device and media controls, and workstation security are
delegated to the hosting provider (per its BAA and SOC-2/attestations) and to
the organization's own physical policies. AgentForge asserts nothing here.

### 3.4 Operational configuration (deployment hardening)

- **OpenEMR user accounts and ACLs** are the authoritative access control for
  the underlying record; provision least-privilege accounts, especially for any
  future write-capable users.
- **Strong administrative credentials**; rotate default credentials.
- **Encryption at rest for the data stores** — attach a DigitalOcean encrypted
  Block Storage volume (or use LUKS) and relocate the DB volumes onto it; the
  application does not encrypt the database.
- **Network/ingress controls** — do not publish the databases; restrict access
  to the agent. On a deploy that leaves `auth_mode=disabled`, gate ingress with a
  reverse-proxy auth guard and/or IP allow-listing; once per-physician SMART
  login is enabled (as on the reference deployment) the SMART session is the
  access gate and the basic-auth guard is removed.
- **HTTPS at a real domain** before exposing PHI (§2 (e)(1)) — live on the
  reference deployment; still required on any fresh deploy.
- **Langfuse** — if tracing is enabled, either **self-host** (the off-by-default
  stack in `docker-compose.deploy.yml`) to keep trace metadata on the
  organization's infrastructure, or **execute a Langfuse BAA** if using Langfuse
  Cloud (the reference deployment currently uses Cloud — see §3.1).
- **Secret management** — supply `anthropic_api_key`, OAuth client secrets, and
  (for SMART login, which is live on the reference deployment) `session_enc_key`
  via a secrets manager; **rotate keys** on a defined cadence; never commit
  secrets.
- **Audit-log review cadence** and monitoring of audit-write failures (audit
  writes are fail-open by design).
- **Retention** — the audit sweep (`scripts/audit_retention_sweep.py`) is
  report-only by default and never deletes audit rows; schedule it via cron for
  visibility, keep `audit_retention_years ≥ 6`, and configure backups and any
  lawful long-term archival operationally (the app retains but does not back up).

---

## 4. Honest summary

**Strong and real on every deploy (including the code default):** append-only
audit trail with correlation IDs and per-read/per-write trailing (§164.312(b));
deterministic, non-promptable, fail-closed verification of both reads and writes,
append-only writes with confirmed-only success and no hard delete
(§164.312(c)(1)); outbound TLS verification on by default and a strict
no-secrets-in-logs rule (§164.312(e)(1)); a fail-closed authorization gate.

**Live on the reference deployment (`agentforge.hankholcomb.com`), off by default
in code:** the per-physician SMART login stack is **enabled** there
(`auth_mode=smart` over HTTPS) — authentication (d), unique user identification
(a)(2)(i), automatic-logoff enforcement (a)(2)(iii), Fernet encryption of tokens
at rest (a)(2)(iv), and the Backend-for-Frontend "token-never-in-browser"
property. In `smart` mode identity is enforced on every interactive route (401
without a session, verified live) AND interactive reads/writes ride the
physician's own delegated token, so OpenEMR's native audit attributes them to the
individual physician. Browser-facing HTTPS (Caddy + Let's Encrypt) is live and
the old basic-auth guard is removed. **All of this is off by default in code**
(`auth_mode` defaults to `disabled`): a fresh deploy authenticates no individual
user until an operator enables SMART login over HTTPS with an OpenEMR SMART-client
registration (see `DEPLOY.md` §15–16 / `agent/research/PRODUCTION_GRADE_PLAN.md`).

**Still off, even on the reference deployment (kept honest):** the physician
write-back path (`writeback_enabled=False` ⇒ routes return `503`) is the one
safeguard not opted on anywhere. Distributed tracing is **on** at the reference
deployment but points at **Langfuse Cloud** — a PHI-adjacent subprocessor that
requires a BAA for production, or a switch to the built-in (off-by-default)
self-hosted stack. The audit-retention sweep remains report-only (6-yr floor, no
delete path).

**Bounded, not eliminated (Week-2 retrieval egress):** the `deidentify()` choke
point is a genuine architectural control — one function, one place, every
retrieval query through it — and the **default keyless deploy sends nothing to
Voyage/Cohere at all**. But the scrub is shape-based (structured identifiers +
label-gated names) and **misses a free-text name typed into a question**, so a
*keyed* deployment must not present it as Safe Harbor de-identification and must
carry Voyage/Cohere BAAs (§3.1). Closing the gap needs an NER pass or a
per-request chart-name denylist — neither is built.

**Never the software's to claim:** the BAAs (Anthropic + ZDR, Langfuse Cloud,
hosting, and Voyage/Cohere if keyed), all §164.308 administrative safeguards,
§164.310 physical safeguards, and the operational hardening in §3.4.
</content>
</invoke>
