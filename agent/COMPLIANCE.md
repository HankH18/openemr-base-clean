# AgentForge Clinical Co-Pilot — HIPAA Security Rule (§164.312) Technical-Safeguard Mapping

This document maps the AgentForge Clinical Co-Pilot service (`agent/`, package
`copilot`) to the **Technical Safeguards** of the HIPAA Security Rule
(45 CFR §164.312). It states, per safeguard, exactly what the software does
**today**, what is present in code but **disabled by default**, and what is
**not yet built**. It then enumerates the safeguards and obligations that
**only the deploying organization can satisfy** — the software cannot and does
not claim them.

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

### Status legend

| Label | Meaning |
|---|---|
| **Implemented** | Code is present and active in the default configuration. |
| **Implemented — gated OFF** | Code is present but disabled by a default-off flag; inert until an operator opts in. |
| **Config surface only** | Configuration knobs exist, but the enforcing code path is not yet built/wired. |
| **Operator / deployment step** | Depends on infrastructure or configuration outside the application code. |
| **Planned** | Designed in `agent/research/PRODUCTION_GRADE_PLAN.md`; no implementing code yet. |

---

## 2. §164.312 Technical-safeguard map

### Summary table

| §164.312 provision | Status | Where in code |
|---|---|---|
| (a)(1) Access control | **Partial** — fail-closed authorization gate implemented; per-physician login backbone **implemented, gated OFF**; data-route cutover pending | `copilot/auth/authorization.py`; `copilot/auth/service.py`; `config.py:auth_mode` (default `disabled`) |
| (a)(2)(i) Unique user identification | **Implemented — gated OFF** (SMART login backbone); request-supplied `clinician_id` still keys the data routes until the Phase-2 cutover | `copilot/auth/{service,identity,session}.py`, `api/routes/auth.py`, `api/deps.py`, `migrations/0004` |
| (a)(2)(iii) Automatic logoff | **Implemented — gated OFF** — idle + absolute TTL enforced in the session store | `copilot/auth/service.py:resolve_session`; `config.py:session_idle_seconds`, `session_absolute_seconds` |
| (a)(2)(iv) Encryption/decryption | **Implemented** — outbound TLS active; token-at-rest Fernet encryption **implemented, gated OFF** | `config.py:tls_verify` (default `True`); `copilot/auth/session.py:SessionCrypto`; `config.py:session_enc_key` |
| (b) Audit controls | **Implemented** — append-only trail; retention sweep implemented (report-only, 6-yr floor) | `memory/models.py:AuditLogRow`, `memory/repository.py:record_audit`, `api/middleware.py`, `memory/retention.py` |
| (c)(1) Integrity | **Implemented** (write path gated OFF) | `verification/core.py`, `verification/writes.py`, `fhir/write_client.py`, `writeback/service.py` |
| (d) Person or entity authentication | **Implemented — gated OFF** — SMART login delegated to OpenEMR; not enabled in the demo | `copilot/auth/service.py`, `fhir/auth.py:{SmartAppLaunchTokenProvider,SessionTokenProvider}`, `api/routes/auth.py` |
| (e)(1) Transmission security | **Partial** — outbound TLS + no-token-logging **implemented**; BFF token-never-in-browser **implemented, gated OFF**; browser HTTPS is a **deployment step** | `config.py:tls_verify`; `fhir/auth.py`; `copilot/auth/session.py`; Caddy (deploy) |

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

**The honest gap.** In the default configuration (`config.py:auth_mode`
defaults to `"disabled"`), the `clinician_id` that the authorization gate keys
on is **supplied by the request** (body/query), not established by an
authenticated login. The demo therefore trusts the caller's asserted identity.
This is a genuine limitation and is called out here plainly: **AgentForge does
not today authenticate the individual physician.** Background reads by the
poller run under a system (Backend Services) token and are non-interactive by
design (`worker/runtime.py`, audited as `poller.read` with no clinician).

**What is implemented but gated OFF.** The per-physician SMART login backbone
now exists in code (behind `auth_mode`, default `"disabled"`):
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

**The remaining gap (Phase 2 — the route cutover).** The `current_clinician`
dependency is **not yet wired into the interactive data routes** (chat, rounds,
observations, writes). So even with `auth_mode=smart`, logging in establishes an
authenticated session, but those routes still read `clinician_id` from the
request until the Phase-2 cutover threads the session identity through them (see
`agent/research/PRODUCTION_GRADE_PLAN.md` §Phase 2). Additionally, SMART login
requires browser-facing HTTPS (Decision B) and an OpenEMR client registration
(operator step), and **it is not enabled in the current demo** (`auth_mode`
defaults to `disabled`). Unique per-physician identification on the data path,
and the corresponding native OpenEMR write attribution, become fully real only
once the cutover lands and `auth_mode=smart` is enabled over HTTPS.

> **Interim deployment guidance.** Until per-physician SMART login is wired into
> the data routes and enabled over HTTPS, treat AgentForge as a **single-tenant,
> trusted-network tool**: restrict network access to it (see §3, firewalling /
> ingress auth), and rely on OpenEMR's own user accounts and ACLs for individual
> accountability of the underlying record.

### (a)(2)(iii) Automatic logoff — *addressable*

**Implemented — gated OFF.** The idle and absolute session-lifetime knobs exist
(`config.py:session_idle_seconds`, default 1800 s / 30 min;
`session_absolute_seconds`, default 43200 s / 12 h) and are now **enforced** by
the session store: `AuthService.resolve_session` (`copilot/auth/service.py`)
rejects a session past its idle or absolute deadline (sliding-refresh on
activity), and logout revokes it. This machinery is inert until
`auth_mode=smart` is enabled over HTTPS. Until then, session expiration for the
underlying record is governed by OpenEMR's own logoff settings. Do not claim
automatic logoff as active for AgentForge until `auth_mode=smart` is enabled.

### (a)(2)(iv) Encryption and decryption — *addressable*

**Implemented — in transit (outbound).** All outbound calls from AgentForge to
OpenEMR verify TLS by default: `config.py:tls_verify` defaults to `True`, and
both the read client and the write client honor it
(`fhir/provider.py`, `fhir/write_client.py` — `verify=settings.tls_verify`).
`tls_verify=False` exists only for a local self-signed dev stack and must not be
used in production.

**Implemented — gated OFF — at rest (application layer).** The
highest-value secret (physician OAuth access + refresh tokens) is encrypted at
rest with a Fernet key (`config.py:session_enc_key`) via `SessionCrypto`
(`copilot/auth/session.py`); the `physician_session` table stores only Fernet
ciphertext (`LargeBinary`) and a `sha256` hash of the session cookie, never the
plaintext token or cookie. This is exercised by tests but inert until
`auth_mode=smart` is enabled, because no physician session is created until
login is on.

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
- **Distributed tracing (gated OFF by default).** `observability/factory.py`
  returns a real Langfuse tracer only when all three Langfuse env vars are set;
  otherwise a no-op. Traces can carry patient/clinician ids in span metadata,
  so the deployment plan calls for a **self-hosted** Langfuse to keep that data
  on the organization's own infrastructure (`PRODUCTION_GRADE_PLAN.md` §3);
  self-hosting is an operator step.

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
raises `WritebackDisabledError` unless explicitly enabled and configured).

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

**Implemented — gated OFF.** AgentForge delegates authentication of the
individual physician to OpenEMR via SMART App Launch (`authorization_code` +
PKCE), with an opaque, httpOnly, server-side session on the agent side. The
full login flow now exists — token-exchange provider
(`fhir/auth.py:SmartAppLaunchTokenProvider`), `/v1/auth/*` routes, session store,
and identity resolution (see (a)(2)(i)). It is inert by default: `auth_mode`
defaults to `"disabled"`, and enabling it requires browser HTTPS + an OpenEMR
client registration, plus the Phase-2 route cutover before the data routes
honor the session identity. **In the current demo the software does not
independently authenticate the end user**; it relies on the deploying
organization to place it behind network/ingress controls and on OpenEMR's
authentication for the record itself. This becomes an active control once
`auth_mode=smart` is enabled over HTTPS and the cutover lands.

### (e)(1) Transmission security — *required*

**Implemented — outbound.**
- Outbound TLS verification is on by default (`config.py:tls_verify=True`),
  covering AgentForge→OpenEMR and AgentForge→Anthropic transport.
- **Secrets are never logged.** Token providers hold passwords/secrets with
  `repr=False` and the code follows a hard "never log tokens/secrets" rule
  (`fhir/auth.py`); the write client logs status codes, never token material or
  raw server messages.

**Operator / deployment step — browser edge.** HTTPS termination for
browser-facing traffic is provided by the deployment's reverse proxy (Caddy
with automatic Let's Encrypt certificates) and requires a real domain + DNS —
an operator step (`PRODUCTION_GRADE_PLAN.md` §2). The current reference
deployment runs plain HTTP behind a basic-auth guard at a bare IP, so
**browser-facing HTTPS is not yet live** and must be stood up before the
service is exposed to real PHI or before per-physician SMART login (whose
`Secure` cookies require TLS) can be enabled. The "token never touches the
browser" (Backend-for-Frontend) property **is implemented** in the SMART login
backbone — the physician's OpenEMR token lives only in the encrypted
server-side `physician_session`, and the browser holds only an opaque hashed
cookie — but is inert until `auth_mode=smart` is enabled; today no physician
token exists in the browser because no browser-side token flow exists.

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
- **Any other subprocessor** that can see PHI or PHI-adjacent metadata —
  including a hosted Langfuse instance if used instead of self-hosting, and any
  monitoring/log aggregation — must be covered by a BAA or must not receive PHI.

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
  to the agent (and, until per-physician login is enabled, gate ingress with
  the reverse-proxy auth guard and/or IP allow-listing).
- **HTTPS at a real domain** before exposing PHI (§2 (e)(1)).
- **Self-host Langfuse** if tracing is enabled, to keep trace metadata on the
  organization's infrastructure.
- **Secret management** — supply `anthropic_api_key`, OAuth client secrets, and
  (when SMART ships) `session_enc_key` via a secrets manager; **rotate keys**
  on a defined cadence; never commit secrets.
- **Audit-log review cadence** and monitoring of audit-write failures (audit
  writes are fail-open by design).
- **Retention** — the audit sweep (`scripts/audit_retention_sweep.py`) is
  report-only by default and never deletes audit rows; schedule it via cron for
  visibility, keep `audit_retention_years ≥ 6`, and configure backups and any
  lawful long-term archival operationally (the app retains but does not back up).

---

## 4. Honest summary

**Strong and real today:** append-only audit trail with correlation IDs and
per-read/per-write trailing (§164.312(b)); deterministic, non-promptable,
fail-closed verification of both reads and writes, append-only writes with
confirmed-only success and no hard delete (§164.312(c)(1)); outbound TLS
verification on by default and a strict no-secrets-in-logs rule
(§164.312(e)(1)); a fail-closed authorization gate.

**Implemented but disabled by default (inert until an operator opts in):** the
per-physician SMART login backbone — authentication (d), unique user
identification (a)(2)(i), automatic-logoff enforcement (a)(2)(iii), Fernet
encryption of tokens at rest (a)(2)(iv), and the Backend-for-Frontend
"token-never-in-browser" property — all behind `auth_mode` (default
`disabled`); the physician write-back path (`writeback_enabled=False`); the
report-only audit-retention sweep (report-only, 6-yr floor, no delete path); and
Langfuse tracing (no keys ⇒ no-op).

**The remaining gap before per-physician identity is live on the data path — do
not claim as live yet:** the Phase-2 route cutover (wiring `current_clinician`
into chat/rounds/observations/writes so those routes read identity from the
session rather than the request body), browser-facing HTTPS, and the OpenEMR
SMART-client registration. All three are specified in
`agent/research/PRODUCTION_GRADE_PLAN.md`. Until they land and `auth_mode=smart`
is enabled, the demo authenticates no individual user.

**Never the software's to claim:** the BAAs (Anthropic + ZDR, hosting), all
§164.308 administrative safeguards, §164.310 physical safeguards, and the
operational hardening in §3.4.
</content>
</invoke>
