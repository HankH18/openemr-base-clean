# WRITEBACK_PHASE1_PLAN.md — Physician Direct-Edit Write-Back (Phase 1)

**Project:** AgentForge Clinical Co-Pilot (`agent/`, package `copilot`)
**Status:** Implementation plan (file-level). No code written.
**Depends on:** `agent/research/PHYSICIAN_WRITEBACK.md` (feasibility brief)
**Scope of this doc:** Phase 1 ONLY — physician *direct-edit* (human-typed) of a **vital** or **medication** from the drill-down. Append-only, latest-wins, via OpenEMR's Standard REST API. No agent-mediated writes (Phase 2).

---

## 0. Grounding — what the code actually looks like today

Verified by reading the repo (not assumed):

- **Read client is deliberately minimal + read-only.** `copilot/fhir/client.py` exposes only `read` / `search` / `count_since`, with a one-401-retry loop and `Accept: application/fhir+json`. Its docstring explicitly says it must stay trivial so `verification` can reuse it. **Do not add a write path here.**
- **Two token providers exist** (`copilot/fhir/auth.py`): `SmartAppLaunchTokenProvider` (authorization_code, physician-delegated) and `BackendServicesTokenProvider` (client_credentials + `private_key_jwt`, `system/*.read`). Both cache in-memory, never log tokens.
- **The only working live credential on the droplet is the SYSTEM read client** (`agent/secrets/backend-client.json`): `grant_types: ["client_credentials"]`, `client_role: user`→enabled as `system`, `scope: "system/Patient.read … system/DiagnosticReport.read"`. It has **no user session and no write scope**.
- **Verification is the pattern to mirror.** `verification/core.py` = deterministic, non-promptable attribution + numeric-value gate; `verification/rules.py` = additive domain flags (critical_lab, reference_range, allergy/med conflict, med reconciliation) with reference-range bounds derived per-Observation; `verification/serve.py` = fail-closed serve-time re-fetch. `_to_result` computes `served / degraded / withheld`.
- **Audit is append-only and already the HIPAA §164.312(b) trail.** `AuditLogRow` (`memory/models.py`) has `correlation_id, clinician_id, patient_id, action, resources_returned, at`. `MemoryRepository.record_audit(...)` inserts one row per PHI touch. **There is no `entry_mode` column yet.**
- **Routes auto-mount.** `copilot.api.app.register_routers` imports every module under `copilot/api/routes/` and mounts any module-level `router`. A new route file requires **no edit to `app.py`**. Every route parses raw ids into `PatientId`/`ClinicianId` at the boundary and gates PHI on `is_authorized(cid, pid)` (rounding-list membership; fail-closed 403). `routes/observations.py` is the closest template (patient+clinician scoped, fail-closed, audits after success, fail-open on the audit write).
- **Frontend drill-down.** `ClaimList.tsx` renders each claim with a `claim-tools` span holding a `TrendChip` (lazy series fetch) and a `ProvenanceChip` (React-Aria `DialogTrigger`/`Popover`, echoes verbatim source value). `PatientHero.tsx` renders claim lists. The API seam is `api/client.ts` (`CopilotApi` interface) with a live `http.ts` adapter and a `mock.ts`; types in `api/types.ts`, response shaping in `normalize.ts`.

### OpenEMR write endpoints — confirmed against the repo

Standard REST routes (`_rest_routes_standard.inc.php`) and controllers:

- **Vital create:** `POST /apis/default/api/patient/{pid}/encounter/{eid}/vital`
  - ACL: `RestConfig::request_authorization_check($request, "encounters", "notes")`.
  - Body (all strings, `api_vital_request` schema in `EncounterRestController.php`): `bps, bpd, weight (lb), height (in), temperature (F), temp_method, pulse, respiration, note, waist_circ, head_circ, oxygen_saturation`.
  - `EncounterService::insertVital()` **strips any `id` to prevent IDOR — always creates a new record**, sets `authorized=1`, requires an existing `eid`. Returns `201` with `{vid, fid}`. **Append-only is enforced server-side.**
  - **Wrinkle:** a vital is attached to an **encounter**. The write path must resolve or create an `eid` (see §1.3).
- **Medication create:** `POST /apis/default/api/patient/{pid}/medication`
  - ACL: `("patients", "med")`. `ListRestController::post()` forces `type=medication`, `pid`, validates, `insert()` (always a new list row). Returns `201` with `{id}`.
  - Body (`api_medication_request`, required `title, begdate`): `title` (drug text), `begdate` (YYYY-MM-DD), `enddate` (nullable), `diagnosis` (`<codetype>:<code>`, optional). **No free-text note/dose field** — dosing lives in `POST /api/prescription` (deferred).
- Both routes key on the **integer `pid`** (matches `PatientId.value`), not the FHIR UUID. The FHIR read side maps int→UUID via `fhir_patient_id_template`; the write side does not need that mapping.

---

## 1. Decision 1 — Write transport: `OpenEmrWriteClient`

**Recommendation: a brand-new client, not an extension of `FhirClient`.** Reasons: (a) different base (`/apis/default/api` vs `/apis/default/fhir`); (b) different content type (`application/json`, not `application/fhir+json`); (c) different auth surface (user-context `api:oemr` token, not the system read token); (d) keeping `FhirClient` minimal is load-bearing for verification reuse.

### 1.1 Shape

New file `copilot/fhir/write_client.py`:

- Constructor mirrors `FhirClient`: `base_url` (`…/apis/default/api`), a `TokenProvider` (the **write** provider — §2), injectable `httpx.AsyncClient`, `timeout`, `verify`. Same one-401-retry-with-forced-refresh loop.
- **Typed methods, DTO in / DTO out** (no raw dicts at call sites):
  - `resolve_or_create_encounter(pid: PatientId) -> str` — GET the patient's encounters; reuse the most recent encounter dated today if present, else `POST /api/patient/{puuid}/encounter` to create a minimal "Co-Pilot bedside entry" encounter. (This step gets its own audit sub-row.)
  - `create_vital(pid, eid, vital: VitalWrite) -> CommittedWrite`
  - `create_medication(pid, med: MedicationWrite) -> CommittedWrite`
  - `retract_medication(pid, mid) -> CommittedWrite` — the reversibility path (PUT `enddate`/inactive). Present on the client but **not surfaced in the Phase-1 UI**; used for compensating writes.
- Metric→payload mapping lives here (closed, exhaustive `match` on `WritableMetric`, no `default`, per CLAUDE.md enum discipline):
  - `heart_rate → pulse`, `spo2 → oxygen_saturation`, `systolic_bp → bps`, `diastolic_bp → bpd`, `respiratory_rate → respiration`, `temperature → temperature` (°F), `weight → weight` (lb), `height → height` (in). A single-metric write posts a new vitals form with only that column populated.

### 1.2 Error handling / fail-closed on write

- Success is **only** an explicit `201` (create) or `200` (update) with a parseable id in the body (`vid`/`fid` for vitals, `id` for meds). Anything else raises `OpenEmrWriteError`.
- Map and surface: `400/422` (validation) → return the OpenEMR validation message to the confirm response; `403` (scope/ACL) → "not permitted"; `409` (version conflict; only relevant to future PUT — support `If-Match`/ETag then); `401` → one forced-refresh retry, else fail.
- **A write whose success cannot be confirmed is treated as FAILED** (timeout, ambiguous body, unparseable id) — logged, audited `write_failed`, never assumed committed.
- Every method returns a typed `CommittedWrite(resource_kind, new_id, encounter_id, committed_at)` or raises. No silent success.

### 1.3 Append-only "new record = latest"

- Vitals: guaranteed by `insertVital` (strips id, new form). The reading's clinical time is "now" (server timestamps the form). The poller's change-gate (`count_since` / `_lastUpdated=gt…`) then re-synthesizes the memory file, so the co-pilot's view reflects the write automatically.
- Meds: guaranteed by `ListRestController::insert` (new list row). Latest-wins by `begdate`/creation.
- **Idempotency:** each candidate carries a client-generated `idempotency_key`; the confirm endpoint (§3) refuses to double-commit the same key, so a retried/double-clicked confirm cannot create a duplicate.
- **Read-back (closing the loop):** after commit, re-`read`/`search` the new resource through the existing **read** `FhirClient` and confirm the value round-trips (reusing the read-side trust machinery as a post-write integrity check). A mismatch is logged and surfaced but does not delete the write (append-only).

---

## 2. Decision 2 — AUTH / attribution (the hard one)

### 2.1 Finding: the system token structurally cannot write here

The Standard API (`api:oemr`) advertises **only `user/` context scopes** (`Documentation/api/AUTHORIZATION.md` §"Standard API Scopes"): `user/vital.crus`, `user/medication.cruds`, `user/encounter.crus`, etc. **There are no `system/` context scopes for the Standard API.** Additionally, every write handler calls `RestConfig::request_authorization_check($request, …)`, which resolves an **OpenEMR user's ACLs** — a `client_credentials` token has no user to check. Therefore:

> The backend-services SYSTEM client cannot perform these writes even if granted `api:oemr` — it can obtain no `user/` scope and presents no user/ACL context. Using it is both impossible *and* would violate the read-only-poller invariant. **Reject this option outright.**

### 2.2 Recommendation for Phase 1: Resource-Owner Password grant against a dedicated OpenEMR user

Concrete, works on this droplet:

1. **Register a NEW confidential client** (separate from the read poller — never reuse it): `grant_types: [password, refresh_token]`, `client_role: user`, `scope: "openid offline_access api:oemr user/vital.crus user/encounter.crus user/medication.cruds"`. (Extend `scripts/register_backend_client.py` or a sibling `register_write_client.py`.)
2. **Enable password grant** in OpenEMR: Admin → Config → Connectors → "Enable OAuth2 Password Grant".
3. **Create a dedicated OpenEMR user** for co-pilot writes with the `encounters/notes` + `patients/med` ACLs, named to signal mediated entry (e.g. `copilot_writer`, display "AgentForge Co-Pilot (on behalf of clinician)").
4. **New token provider** `ResourceOwnerPasswordTokenProvider` in `copilot/fhir/auth.py` (mirrors the existing dataclasses): `grant_type=password`, `user_role=users`, `username`/`password`, `client_id` (+`client_secret`), caches + refreshes via `refresh_token`. Reuses `_parse_token_response`.

**Phase-3 correct answer (documented, deferred):** per-physician SMART **authorization_code** (`SmartAppLaunchTokenProvider` already exists) so OpenEMR attributes each write to the actual logged-in physician. Deferred because Dr. Ellery is a demo identity with no OAuth login.

### 2.3 Attribution — how the write is tied to the physician

Two layers, because there is no FHIR `Provenance.create`:

- **Co-pilot-native (authoritative for us):** every write records an `audit_log` row with `clinician_id` = the real physician's `ClinicianId` (Dr. Ellery), `action = write_proposed / write_committed / write_failed`, and a **new `entry_mode` column** = `human_direct`, plus the resulting resource id in `resources_returned`. This is the true physician attribution surface for Phase 1.
- **OpenEMR-native:** the write is attributed to the dedicated `copilot_writer` user (native `created_by`/`user`/audit). For **vitals**, also stamp the physician into the `note` field (e.g. "Entered via AgentForge Co-Pilot direct-edit; clinician_id=…"). Medications have no note field in this API schema, so med attribution lives in the co-pilot audit_log + the dedicated user only.

### 2.4 Security trade-offs to flag

- The app now holds a **writable** credential (the write client secret + the `copilot_writer` password) — materially higher value than the read-only key. It MUST live in the secrets manager, never git, never logged (extend the existing "never log tokens" rule to these creds).
- **Shared-user attribution gap:** because a single OpenEMR user backs all writes, OpenEMR's *native* trail cannot distinguish which physician made each edit; only the co-pilot `audit_log` can. This is a compliance gap to close in Phase 3 (per-physician SMART accounts). State it explicitly.
- Password grant is "not recommended for production" per OpenEMR docs — acceptable for a demo droplet, flagged as tech-debt.
- **Hard invariant reaffirmed:** the write provider/client is constructed **only in the interactive request path**, never in the background lifespan; the poller's system token never gains a write scope.

---

## 3. Decision 3 — the confirmation gate (propose → review → confirm → commit)

Mirrors the read-side gate, run in reverse. Two server calls so **commit is always a distinct, human-initiated transaction** (and so Phase 2 can reuse the confirm step):

1. **Parse, by code, into a closed candidate** (`POST /v1/writes`). The request carries `clinician_id, patient_id, kind (vital|medication), metric, raw_value, unit`. Server code — not any model — parses `raw_value` into a typed candidate over the **closed `WritableMetric` enum**. Unparseable value, unknown metric, or a unit that does not match the metric's expected unit ⇒ **hard block** (400 with the specific violation). No free text ever reaches the DB (meds: `title` is a picked/echoed drug string, not prose).
2. **Deterministic write-verification pass** (`verification/writes.py`, sibling to `core.py`): enum membership, unit sanity, and **plausibility bounds** from a new small closed per-metric table (absolute physiologic min/max). For a *human direct-edit* (path A), out-of-range is a **SOFT warning** — surfaced, overridable — so a genuine critical value is still recordable. (Phase 2 agent proposals will hard-block out-of-range; the module takes a `mode` param now to make that a one-line change later.)
3. **Structured echo-back.** The propose response returns the exact record to be written — `kind, metric, value, unit, effective_time="now"` — plus an explicit `"This creates a NEW record dated now; it does not overwrite prior values."` and any range warning. The frontend renders this as a confirmation card, not prose. This is where a fat-finger is caught by a human. Audit `write_proposed` here.
4. **Explicit confirm as a separate transaction** (`POST /v1/writes/{idempotency_key}/confirm`), initiated by the physician's second click. Stateless design: the confirm body re-sends the identical candidate + `idempotency_key`; the server re-runs the same deterministic parse+verify and refuses if the echo-back would differ. No new candidate table required.
5. **Commit + audit + read-back.** Confirm → `OpenEmrWriteClient` (write token) → on `201`, audit `write_committed` (`entry_mode=human_direct`, `clinician_id`, resource id) → read-back via `FhirClient`. On any failure, audit `write_failed` and return the error; **never silent overwrite, never assume success.**

`WriteService` (new `copilot/writeback/service.py`) orchestrates this, mirroring `chat/service.py`. Both routes gate on `is_authorized(cid, pid)` (403 fail-closed) exactly like chat/observations — no cross-patient write is possible.

---

## 4. Decision 4 — Files (phased, file-level)

### Phase 1a — foundation (no user-facing change)

| File | New/Mod | Purpose |
|---|---|---|
| `agent/copilot/domain/writes.py` | **new** | `WritableMetric` StrEnum (heart_rate, spo2, systolic_bp, diastolic_bp, respiratory_rate, temperature, weight, height), `WriteKind`, `WriteEntryMode` (`human_direct`; `agent_proposed_physician_confirmed` reserved), `VitalWrite`, `MedicationWrite`, `ProposedWrite`, `WriteCandidate`, `CommittedWrite`, `WriteVerdict`. Frozen Pydantic, mirroring `contracts.py`/`primitives.py`. |
| `agent/copilot/verification/writes.py` | **new** | Deterministic write-verification gate: enum membership + unit sanity + closed per-metric plausibility table; `verify_write(candidate, mode) -> WriteVerdict`. Reuses `rules.py` range helpers where applicable. |
| `agent/copilot/fhir/auth.py` | mod | Add `ResourceOwnerPasswordTokenProvider` (grant_type=password + refresh). |
| `agent/copilot/fhir/write_client.py` | **new** | `OpenEmrWriteClient` (Standard API base, user token): `resolve_or_create_encounter`, `create_vital`, `create_medication`, `retract_medication`; typed `CommittedWrite`/raise; error mapping; idempotency. |
| `agent/copilot/fhir/provider.py` | mod | `build_write_token_provider(settings)` + `build_write_client(settings)`; guard so it is never built from the poller path. |
| `agent/copilot/config.py` | mod | `writeback_enabled=False` (master flag, default OFF), `write_client_id`, `write_client_secret`, `write_username`, `write_password`, `write_scopes`, `write_api_base_url`. |
| `agent/copilot/memory/models.py` | mod | Add nullable `entry_mode: Mapped[str \| None]` to `AuditLogRow`. |
| `agent/copilot/memory/repository.py` | mod | `record_audit(..., entry_mode: str \| None = None)`. |
| `agent/migrations/versions/0002_audit_entry_mode.py` | **new** | Alembic: `op.add_column("audit_log", sa.Column("entry_mode", sa.String(32), nullable=True))`; downgrade drops it. |

### Phase 1b — the write path

| File | New/Mod | Purpose |
|---|---|---|
| `agent/copilot/writeback/service.py` (+ `__init__.py`) | **new** | `WriteService.propose(...)` and `WriteService.commit(...)`: authz already done at route; parse → `verify_write` → echo-back / audit `write_proposed`; commit → write client → audit `write_committed`/`write_failed` → read-back. |
| `agent/copilot/api/routes/writes.py` | **new** | `POST /v1/writes` (propose) + `POST /v1/writes/{idempotency_key}/confirm` (commit). `is_authorized` 403 gate; auto-mounted; module-level `router`. |

### Phase 1c — frontend (React-Aria, in the drill-down)

| File | New/Mod | Purpose |
|---|---|---|
| `agent/web/src/components/EditRecordDialog.tsx` | **new** | React-Aria `DialogTrigger`/`Dialog`: typed numeric input + **locked unit** + fixed metric label (never a free-text box); propose→echo-back card→"Confirm & save"; soft range-warning banner; "creates a new record dated now" copy. Mirrors `ProvenanceChip`/`TrendChip` structure. |
| `agent/web/src/components/ClaimList.tsx` | mod | Add an "Edit" chip in `claim-tools` (next to Trend/Provenance) for claims whose metric is writable. |
| `agent/web/src/labels.ts` | mod | `writableMetric(claim): WritableMetric \| null` mapping. |
| `agent/web/src/api/types.ts` | mod | `WriteCandidate`, `CommittedWrite`, propose/confirm request/response types. |
| `agent/web/src/api/client.ts` | mod | Add `proposeWrite` / `confirmWrite` to `CopilotApi`. |
| `agent/web/src/api/http.ts` | mod | Live adapter for the two endpoints. |
| `agent/web/src/api/mock.ts` | mod | Mock adapter (so the demo works offline). |
| `agent/web/src/api/normalize.ts` | mod | Shape the propose/confirm responses. |

### Phase 1d — tests (mirroring the existing suite)

| File | New/Mod | Purpose |
|---|---|---|
| `agent/tests/test_write_client.py` | **new** | Payload shapes (vital column mapping, med title/begdate); 201 id parsing; **error handling** (400/403/409/timeout → fail-closed, never assumed-success); idempotency; retract. |
| `agent/tests/test_verification_writes.py` | **new** | Enum membership; unit sanity; **range-rejection test** (hard block on unparseable/wrong-unit/unknown-metric; soft warning on out-of-physiologic-range for `human_direct`). |
| `agent/tests/test_writes_route.py` | **new** | Propose→confirm happy path; **403 unauthorized** (not on rounding list); **audit test** (`write_proposed` + `write_committed` rows carry `entry_mode=human_direct`, `clinician_id`, resource id); append-only (no overwrite of prior); read-back check; double-confirm idempotency. |
| `agent/tests/test_fhir_auth.py` | mod | `ResourceOwnerPasswordTokenProvider` exchange + refresh. |
| `agent/tests/test_migrations.py` | mod | Assert `audit_log.entry_mode` exists after upgrade. |

---

## 5. Decision 5 — Live-test strategy (safe write against the droplet)

Feature stays **OFF by default** (`COPILOT_WRITEBACK_ENABLED=false`) so the running demo is unaffected until deliberately enabled.

Preconditions: register the write client, enable password grant, create/enable the `copilot_writer` OpenEMR user with `encounters/notes` + `patients/med` ACLs, set the write env vars in `.env.local` (gitignored). Verify the token first with a `grant_type=password` curl and confirm the returned `scope` contains `api:oemr user/vital.crus` (proves the whole auth chain before touching data).

Test **only against a scratch/seed patient that is not a demo hero patient**:

1. Baseline read via FHIR (current Observation set for that patient UUID).
2. `resolve_or_create_encounter(pid)` → get an `eid`.
3. `create_vital` with an in-range value (e.g. `heart_rate=72`) via a small read-safe `scripts/writeback_smoke.py`.
4. **Read back via FHIR** Observation search by the patient UUID; confirm the new HR appears with effective time ≈ now and value 72 (round-trip proven).
5. Confirm append-only: prior vitals unchanged; a new form row was created.
6. Negative controls:
   - Same write with the **SYSTEM read token** → expect `401/403` (proves the poller token cannot write).
   - Out-of-range value → soft-warn path returns a warning, still commits on explicit confirm.
   - Unparseable value / wrong unit → hard-blocked at propose.

Rollback: writes are append-only and land on a scratch patient, so they are harmless; optionally mark a test med inactive via `retract_medication`. **Never mutate demo hero patients.**

---

## 6. Decision 6 — Risks & how Phase 1 stays safe

**Medico-legal:**
- **Append-only** is enforced server-side (`insertVital` strips `id`; `ListRestController::insert` always creates). No `PUT`/`DELETE` in the Phase-1 create path. Latest-wins by timestamp.
- **Provenance gap:** no FHIR `Provenance.create`; mitigated by co-pilot `audit_log` (`entry_mode` + real `clinician_id`), the dedicated OpenEMR user, and the vital `note` stamp. **Flagged:** shared-user attribution is a compliance gap; per-physician SMART is required for production (Phase 3).
- **Reversibility:** a bad write is undone by a *compensating* append (mark med inactive / a corrected new reading), never a destructive delete; both the write and its reversal are audited.
- **6-yr audit retention** (§164.312(b)) already applies to `audit_log`; writes extend the same trail.

**Operational / safety:**
- **Fat-finger:** typed numeric input + locked unit + plausibility warning + mandatory structured echo-back + explicit confirm.
- **Wrong-patient:** the write routes are patient+clinician scoped and gated by `is_authorized` (rounding-list membership), identical to reads — no cross-patient write.
- **Secret exposure:** writable creds are higher-value → secrets-manager only, never logged; poller token stays read-only; write client never built in the background path.
- **Demo corruption:** `writeback_enabled` default OFF + scratch-patient testing.

**Why Phase 1 is intrinsically safe:** the human types the value (no NL/agent interpretation), over a closed metric set, through a deterministic parse+verify, with echo-back and an explicit separate confirm transaction, committed append-only with full audit and a post-write read-back, behind the same authorization gate as reads — and **no agent commit path exists** (that is Phase 2, and even then commit will remain a separate physician-initiated transaction the agent cannot call).

---

## Summary

- **Transport:** new `OpenEmrWriteClient` on the Standard REST API (`POST …/vital`, `POST …/medication`), separate from the minimal read-only `FhirClient`; typed DTOs, strict 201-only success, idempotency, read-back.
- **Auth:** the SYSTEM token *cannot* write (Standard API has only `user/` scopes and needs a user/ACL session). Recommend a **new password-grant client + a dedicated `copilot_writer` OpenEMR user**; attribute the real physician in the co-pilot `audit_log` via a new `entry_mode` column. Flag the shared-user attribution gap and the writable-secret risk; per-physician SMART authorization_code is the Phase-3 fix.
- **Confirm gate:** `POST /v1/writes` (parse→verify→echo-back, audit `write_proposed`) then a distinct `POST /v1/writes/{idempotency_key}/confirm` (commit→audit `write_committed`/`write_failed`→read-back); soft range-warning for human direct-edit; append-only, never silent overwrite; `is_authorized` 403 gate.
- **Files:** enumerated new/modified backend, frontend, migration, and tests (including the required range-rejection test and audit test).
