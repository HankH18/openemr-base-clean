# Physician Write-Back — Feasibility & Architecture Brief

**Project:** AgentForge Clinical Co-Pilot (`agent/`, package `copilot`)
**Status:** Research / design deliverable only. No application code changed.
**Author:** Staff engineering feasibility review
**Date:** 2026-07-11

---

## Executive summary

Today the co-pilot is strictly **read-only**: `copilot/fhir/client.py` exposes
`read` / `search` / `count_since` and nothing else, and the whole trust model
(grounding → deterministic verification → fail-closed) exists to guarantee the
agent can only *report* what the record already says. The proposed feature —
letting a physician **update** records, either by direct field edit (A) or via
the chat agent (B) — inverts that boundary. This brief assesses it and
recommends a conservative design that extends, rather than abandons, the
existing ethos.

**Four load-bearing findings:**

1. **OpenEMR's FHIR API cannot write the resources this feature needs.** The
   FHIR route map in this repo
   (`apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php`) defines **34
   readable resources but only 3 writable ones** — `POST`/`PUT` exist for
   `Patient`, `Practitioner`, and `Organization` only. `Observation`,
   `MedicationRequest`, `Condition`, `AllergyIntolerance`, `Encounter`,
   `DiagnosticReport` are **GET-only**. This is corroborated by an OpenEMR
   community thread where `MedicationRequest.create` returns **404** on 7.0.3.
   **Clinical write-back therefore cannot go through the FHIR endpoints the
   current `FhirClient` talks to.**

2. **Clinical writes must use OpenEMR's *Standard* REST API** (`/apis/default/api/…`,
   scope `api:oemr` + ACL), which *does* support `POST`/`PUT` for **vitals,
   medications/prescriptions, allergies, medical problems (conditions), and
   encounters**. Even here there is a real gap: **lab-result Observations
   (LOINC panels) have no create endpoint** in either API — only *vitals* are
   directly writable. Phase 1 scope must be constrained accordingly.

3. **The feature is a near-perfect mirror of the read-side trust model.** The
   codebase already grounds values in deterministic code rather than the model
   (`copilot/agent/grounding.py`), gates every claim through a non-promptable
   deterministic verifier (`copilot/verification/core.py`), fails closed
   (`copilot/verification/serve.py`), applies range/plausibility domain rules
   (`copilot/verification/rules.py`), and keeps an append-only audit trail
   (`AuditLogRow` in `copilot/memory/models.py`). The safe write path is the
   same pattern run in reverse: a **write-confirmation gate** where the agent
   only ever *proposes* a typed, range-checked candidate and the physician
   confirms the exact structured record before commit.

4. **The primary concern — the agent writing incorrect records — is best
   answered architecturally, not behaviourally.** The recommendation is that
   the agent be *structurally incapable* of committing a write: its tool loop
   can emit a candidate but has no access to the commit endpoint, which
   requires a separate physician-initiated confirmation. A hallucinated value
   is then caught at the mandatory human echo-back, and even if committed is
   fully reversible because every write is append-only and attributed.

**Recommended rollout:** Phase 1 = direct edit only (human types the value),
append-only + provenance + audit, vitals/meds/allergies/problems. Phase 2 =
agent *proposes only* with mandatory physician confirmation. Labs and FHIR-native
`Provenance` writes deferred until OpenEMR exposes the endpoints.

---

## 1. FHIR write-back mechanics

### 1.1 How "create a resource" works in FHIR

FHIR distinguishes two operations that both look like "editing a value," and the
distinction is exactly the user's (A) model:

- **Create a new resource** (`POST /[type]`): the server assigns a *new logical
  id* and returns `201 Created` with a `Location` header and the resource's
  first `meta.versionId`. A *new measurement of the same metric* — e.g. a new
  potassium result — is a **new `Observation`** with a later `effectiveDateTime`.
  Querying "the latest potassium" then sorts by `effectiveDateTime`/`_lastUpdated`
  and returns the newest. **This is precisely the user's "new record with the
  current timestamp becomes the latest value" (append-only, latest-wins).**
- **Update an existing resource** (`PUT /[type]/[id]`): overwrites the resource
  *in place*, incrementing `meta.versionId`; the prior version is retained
  server-side in `_history` for audit/integrity and is never silently lost. Use
  this for **correcting** a specific record, not for recording a new reading.
  ([FHIR versioning](https://build.fhir.org/versioning.html),
  [Resource.meta](https://www.hl7.org/fhir/resource.html))

For the co-pilot's metrics, the **new-resource / append-only** model (A's design)
is the clinically correct default: a new vital sign or lab value is a *new
observation*, not a mutation of the old one. In-place `PUT` is reserved for
corrections.

**Amendments in FHIR are a status transition, never a delete.** `Observation.status`
carries a controlled vocabulary: `registered → preliminary → final`, then
`amended` (modified after final, incl. new info), `corrected` (error fix),
`cancelled`, and `entered-in-error` (withdrawn after release). To retract an
erroneous record you set `status = entered-in-error` — **you never `DELETE`**.
([FHIR R4 Observation](https://hl7.org/fhir/R4/observation.html),
[ObservationStatus](https://hapifhir.io/hapi-fhir/apidocs/hapi-fhir-structures-r4/org/hl7/fhir/r4/model/Observation.ObservationStatus.html))
The same append-then-supersede logic applies to `MedicationRequest`
(`status` = `active | stopped | entered-in-error | …`, `authoredOn` as the
timestamp).

### 1.2 Provenance — attributing who/what created the data

FHIR's `Provenance` resource is the native mechanism for "who or what created
this, and when." Key elements ([FHIR R4 Provenance](http://hl7.org/fhir/R4/provenance.html),
[US Core Basic Provenance](https://build.fhir.org/ig/HL7/US-Core/basic-provenance.html)):

- `Provenance.target` → the resource created/updated.
- `Provenance.recorded` → timestamp of the action.
- `Provenance.agent.who` → the responsible party.
- `Provenance.agent.type` → the *role*: **`author`** (created the info) vs
  **`transmitter`** / `assembler`.

Crucially, US Core lets you make **human-entered vs machine-entered
distinguishable** by *what `agent.who` references*: a `Practitioner` /
`PractitionerRole` for a human author, vs a `Device` for a software system.
This maps directly onto the feature's requirement that agent-entered data be
distinguishable from physician-entered data:

- **Direct edit (A):** one `author` agent → the physician (`Practitioner`).
- **Agent-mediated (B):** *two* agents on the same Provenance — a `Device`
  (the co-pilot software) as the **author/proposer**, and the physician
  (`Practitioner`) as the **verifier/attester** who confirmed it. The record
  then carries a permanent, machine-readable "AI-proposed, human-confirmed"
  fingerprint.

**Gap:** OpenEMR exposes `Provenance` for **read** but does not expose
`Provenance.create`. Until it does, the "AI-proposed vs human-entered"
distinction must be recorded (a) in the co-pilot's own `audit_log` (a new
`entry_mode` field), and (b) in whatever author/comment fields the target
OpenEMR entry supports (e.g. the med/vital comment). Treat FHIR-native
`Provenance` write as a Phase-3 upgrade.

### 1.3 OpenEMR's actual write support (verified against this repo)

Grounded in `apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php` and
`apis/routes/_rest_routes_standard.inc.php`:

**FHIR API (`/apis/default/fhir/…`, scope `api:fhir`): effectively read-only for
clinical data.** Distinct route counts in the FHIR map: **64 GET keys across 34
resource types, exactly 3 POST and 3 PUT** —

| Operation | Resources with a route |
|-----------|------------------------|
| `POST /fhir/…` (create) | `Patient`, `Practitioner`, `Organization` |
| `PUT /fhir/…/:uuid` (update) | `Patient`, `Practitioner`, `Organization` |
| `GET` (read/search) | 34 resources incl. Observation, MedicationRequest, Condition, AllergyIntolerance, Encounter, DiagnosticReport, MedicationStatement, Immunization, Procedure |

The clinical resources the co-pilot reads today are **all GET-only over FHIR**.
An OpenEMR 7.0.3 CapabilityStatement lists `MedicationRequest` with only
`search-type` + `read`, and `MedicationRequest.create` returns 404
([community thread](https://community.open-emr.org/t/help-enabling-write-flows-medicationrequest-create-documentreference-create-on-openemr-7-0-3/26006)).

**Standard REST API (`/apis/default/api/…`, scope `api:oemr` + OpenEMR ACL):
this is where clinical writes live.** Verified `POST` routes include:

| Endpoint | Creates | Maps to co-pilot metric |
|----------|---------|-------------------------|
| `POST /api/patient/:pid/encounter/:eid/vital` | Vital signs | Vital-sign Observations (HR, SpO₂, temp, BP) |
| `POST /api/patient/:pid/medication` | Medication list entry | MedicationRequest / MedicationStatement |
| `POST /api/prescription` | Prescription | MedicationRequest |
| `POST /api/patient/:puuid/allergy` | Allergy | AllergyIntolerance |
| `POST /api/patient/:puuid/medical_problem` | Problem list entry | Condition |
| `POST /api/patient/:puuid/encounter` | Encounter | Encounter |

with matching `PUT …/:id` update routes. These handlers enforce OpenEMR's own
ACLs, e.g. `RestConfig::request_authorization_check($request, "encounters",
"notes")` for vitals and `("patients", "med")` for medications — a **different
authorization surface** from the FHIR `api:fhir` scope.

**Hard gap — lab results.** Neither API exposes a create for *lab-result*
`Observation`s (LOINC panels such as troponin/potassium map to
`procedure_result`, which has no write route). Only **vitals** are writable as
Observation-like data. **Phase 1 must exclude labs**; supporting them requires
OpenEMR to expose a `procedure_result` write, or a custom controller — out of
scope for a conservative rollout.

### 1.4 Write scopes required

- **Clinical writes (Standard API):** a **physician-delegated** token with
  `api:oemr` and the ACL categories above. This must come from the SMART App
  Launch (authorization-code) provider — `SmartAppLaunchTokenProvider` in
  `copilot/fhir/auth.py` — so OpenEMR attributes the write to the physician and
  enforces *their* access rights.
- **FHIR-writable resources (Patient/Practitioner/Org):** SMART v2 `.cruds`
  syntax, e.g. `user/Patient.c` (create) / `user/Patient.u` (update); the v1
  equivalent is `user/Patient.write`. Valid suffixes are a subset of the
  in-order string `.cruds` (`c`=create, `r`=read, `u`=update, `d`=delete,
  `s`=search); OpenEMR advertises `permission-v1` backwards-compat.
  ([SMART App Launch v2 scopes](https://hl7.org/fhir/smart-app-launch/scopes-and-launch-context.html),
  [scopes-v2 wiki](https://github.com/smart-on-fhir/smart-on-fhir.github.io/wiki/scopes-v2))
- **The background poller MUST stay read-only.** `BackendServicesTokenProvider`
  (`system/*.read`, per `copilot/fhir/provider.py`) must never be granted a
  write scope. Writes are always physician-attributed and interactive — never
  a system/background action.

### 1.5 What must be added to the read-only client

`copilot/fhir/client.py` is deliberately minimal — its docstring notes it is
"trivial to reuse from `verification` re-fetches," and serve-time verification
depends on that. **Do not overload it with a write path.** Instead:

- **Add a new, separate `OpenEmrWriteClient`** targeting the Standard API base
  (`/apis/default/api`), constructed with a **physician-delegated
  `TokenProvider`** (SMART App Launch), never the system token. It exposes
  typed methods — `create_vital(...)`, `create_medication(...)`,
  `create_allergy(...)`, `create_problem(...)`, and `retract(...)` (the
  `entered-in-error` / status-transition path) — each taking a typed DTO, not a
  raw dict.
- **Error handling / fail-closed on write:** treat only an explicit `201`/`200`
  with a parseable resource id + version as success. Capture and surface `400`/
  `422` (validation), `409` (version conflict — support optimistic concurrency
  via `If-Match`/`ETag` on updates), `403` (scope/ACL). A write whose success
  cannot be *confirmed* is treated as failed and logged; never assume success
  on a timeout or an ambiguous body. Every method returns a typed
  `CommittedWrite` (resource type, new id, version, timestamp) or raises.
- **Idempotency:** each candidate carries a client-generated idempotency key so
  a retried/double-confirmed commit cannot create a duplicate record.
- **Read-back verification (closing the loop):** after a successful commit,
  re-`read` the new resource by id through the existing `FhirClient` and confirm
  the value round-trips. This reuses the read-side trust machinery as a
  post-write integrity check — *the system can prove what it wrote by reading it
  back* — and the poller's change-gate (`count_since`, `_lastUpdated=gt…`) then
  naturally re-synthesizes the memory file so the co-pilot's view reflects the
  write.

---

## 2. Clinical data-integrity & medico-legal best practices

The legal medical record is an evidentiary artifact. The governing principle is
**you never alter or delete an original entry — you append a dated, attributed
correction and preserve the original.**

- **Never overwrite / never delete.** Accepted practice uses dated, signed
  **addenda** that preserve the original entry alongside the correction;
  altering or deleting original entries creates evidence-spoliation exposure.
  ([Frier Levitt — EMR audit trails](https://www.frierlevitt.com/articles/understanding-emr-audit-trails-importance-and-implications-for-medical-record-alteration/),
  [Amendments & corrections](https://www.autonotes.ai/compliance/amendments-and-corrections-to-the-medical-record/))
  In FHIR terms this is `status = amended | corrected | entered-in-error` plus a
  new superseding resource — **never a hard delete**.
- **HIPAA §164.526** governs amendment of PHI (the correction is *linked* to,
  not substituted for, the original);
  **HIPAA §164.312(b)** mandates audit controls that record activity in systems
  holding ePHI, with a **6-year minimum retention**.
  ([45 CFR 164.526](https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-C/part-164/subpart-E/section-164.526),
  [HIPAA §164.312(b) audit trails](https://www.frierlevitt.com/articles/understanding-emr-audit-trails-importance-and-implications-for-medical-record-alteration/))
  The co-pilot already cites §164.312(b) for its read trail in
  `copilot/chat/service.py` — writes must extend the same trail.
- **Versioning preserves originals.** FHIR servers keep every prior version
  (`meta.versionId`, `_history`), superseded but available for audit/integrity.
  ([FHIR versioning](https://build.fhir.org/versioning.html))
- **Attribution must distinguish human from machine.** `Provenance.agent`
  referencing `Device` (software) vs `Practitioner` (human) makes AI-entered
  data machine-distinguishable from physician-entered data
  ([US Core Basic Provenance](https://build.fhir.org/ig/HL7/US-Core/basic-provenance.html)).
  Until OpenEMR exposes `Provenance.create`, record the distinction in the
  co-pilot's `audit_log` and the entry's comment fields.
- **Reversibility.** Because writes are append-only and attributed, any
  erroneous write is undone by a *compensating* write (set the new record
  `entered-in-error`; the prior record remains latest), never a destructive
  rollback. The audit trail records both the write and its reversal.

**Requirements checklist for any write path:**

1. Original preserved + superseding record (append-only). 2. Version history
retained. 3. Timestamped. 4. Attributed (human vs AI distinguishable). 5.
Full audit trail (6-yr retention). 6. Reversible via compensating write. 7.
No hard delete — ever.

---

## 3. Agent-write safety architecture (the core concern)

> **Design invariant: the agent never writes autonomously.** It emits a
> structured, typed, range-checked *candidate*; the physician reviews the exact
> structured record and explicitly confirms; only then does a separate,
> human-initiated transaction commit. The agent's tool loop has **no access to
> the commit endpoint** — it is structurally incapable of writing.

This is the read-side trust model run in reverse. The mapping is exact:

| Read side (exists today) | Write side (proposed) |
|--------------------------|-----------------------|
| `grounding.py`: code, not the model, fills each `(field, value)` | Code, not the model, builds the typed write candidate from a **closed metric set** |
| `verification/core.py`: non-promptable attribution + value-match gate | **Write-verification gate**: enum membership + unit + range/plausibility, non-promptable |
| `verification/rules.py`: range / critical / reference-range checks | Reuse the *same* thresholds as pre-commit plausibility checks |
| `serve.py`: fail-closed — unverifiable ⇒ `withheld` | Fail-closed — unparseable / out-of-range / unconfirmed ⇒ **blocked**, never committed |
| `AuditLogRow`: append-only read trail | Append-only **write** trail: `write_proposed / write_confirmed / write_committed / write_failed` |
| `FhirReference` typed source pointer | `ProposedWrite` / `CommittedWrite` typed DTOs |

### 3.1 The write-confirmation gate (step by step)

1. **Parse, don't validate — NL → typed candidate, by code.** The physician's
   natural-language instruction is parsed into a typed `ProposedWrite` DTO over
   a **closed, enumerated metric set** (a `WritableMetric` `StrEnum` — e.g.
   `heart_rate`, `spo2`, `temperature`, `systolic_bp`, plus writable med/allergy
   /problem kinds), each with a required unit, a numeric value, and an
   `effective_time`. If the instruction cannot be parsed into that closed set
   with a unit and a numeric value, the proposal is **refused** — the direct
   mirror of "no `source_ref`, no claim." **No free text ever reaches the DB.**
   Use exhaustive `match` on the metric enum (no `default`) so a new writable
   metric cannot be silently mishandled — the same discipline CLAUDE.md mandates
   for enums.

2. **Deterministic write-verification pass** (a new
   `copilot/verification/writes.py`, sibling to `core.py`): enum membership
   (metric ∈ writable set), unit sanity (unit matches the metric's expected
   unit), and **range / physiologic-plausibility** checks reusing the thresholds
   already encoded in `verification/rules.py` (reference ranges, critical
   high/low). A candidate that fails any check is **blocked**, never
   auto-corrected. Out-of-range candidates are surfaced with the specific
   violation ("38.0 °C is above the reference range").

3. **Structured echo-back.** The physician is shown the **exact record that will
   be written** — metric, value, unit, effective time, and an explicit
   "*this creates a NEW record dated now (does not overwrite prior values)*" —
   plus any range warning. Rendered as a confirmation card, not as agent prose.
   This is the moment a hallucinated or mis-parsed value is caught by a human.

4. **Explicit physician confirmation, as a separate transaction.** The agent's
   chat turn *only* produces the candidate. Commit is a **distinct HTTP call**
   (`POST /v1/writes/{candidate_id}/confirm`) initiated by the physician's click,
   authenticated with the physician's session — carrying the candidate's
   idempotency key. The agent cannot call it; there is no tool for it.

5. **Commit + provenance + audit.** The commit goes through `OpenEmrWriteClient`
   with the **physician's delegated token** (attributed to the physician in
   OpenEMR). Provenance is tagged **agent-proposed + physician-confirmed** (two
   agents: `Device` proposer, `Practitioner` confirmer — recorded in `audit_log`
   `entry_mode` today, FHIR `Provenance` later). An `audit_log` row is written at
   each transition (`write_proposed`, `write_confirmed`, `write_committed`, or
   `write_failed`) with the correlation id, the candidate, and the resulting
   resource id/version.

### 3.2 Guardrails against hallucinated values

- **No free-text-to-DB.** Value/unit/metric must survive a deterministic parse
  into the closed candidate shape; unparseable ⇒ refuse.
- **Range / plausibility gate.** Reuse `rules.py` thresholds; out-of-physiologic-
  range candidates are blocked pending explicit human action.
- **Mandatory structured echo-back + confirmation.** No silent commit; the human
  always sees the exact structured value.
- **Capability isolation.** The agent has a `propose_write` tool but **no**
  commit tool; the commit endpoint requires a physician confirmation token. A
  compromised or hallucinating agent cannot reach the DB.
- **Idempotency key** prevents double-writes on retry/double-confirm.
- **Reversibility.** Any committed write is undone by a compensating
  `entered-in-error` write (append-only); the original is untouched.
- **Read-back check.** Post-commit re-read confirms the value round-tripped;
  a mismatch is logged and surfaced.

### 3.3 Direct edit (A) vs agent-mediated (B)

**(A) Direct edit — lower risk (human types the value).** No NL interpretation;
the value comes from the physician's own input. Safeguards:

- Structured, typed input field (numeric + unit + metric picked from the closed
  set) — never a free-text box.
- **Soft** range warning: out-of-range is flagged but the physician may confirm
  and override (a genuine critical value must be recordable).
- Append-only new-record semantics + echo-back ("creates a new record dated now").
- Provenance `entry_mode = human_direct`; `audit_log` write row.
- Residual risk is a fat-finger, mitigated by unit/range sanity + echo-back.

**(B) Agent-mediated — higher risk (agent interprets NL).** The agent can
mis-parse ("5.4" vs "5.14"), pick the wrong metric, or fabricate. Safeguards =
everything in (A), **plus**:

- The value is **re-extracted deterministically** from the parse and re-shown;
  it is *never* taken from the agent's prose.
- Ambiguous parses force disambiguation before a candidate is even offered.
- **Hard** block on range violations: an out-of-range *agent* proposal cannot be
  soft-confirmed — the physician must **type the value themselves** (falling
  back to path A) to record it. The agent may propose plausible values only.
- Provenance carries both agents (`Device` proposer + `Practitioner` confirmer);
  `entry_mode = agent_proposed_physician_confirmed`.
- The design deliberately **collapses (B) into (A) at the confirmation step**:
  the physician must still see and confirm the exact structured value, so the
  agent's role is reduced to "draft the form," never "submit the form." A
  hallucinated value that survives to the card is caught by the same human
  echo-back that protects (A).

---

## 4. Phased implementation recommendation

**Phase 0 — foundation (no user-facing change).**
- Typed DTOs: `WritableMetric` (`StrEnum`), `ProposedWrite`, `CommittedWrite` in
  `copilot/domain/` (mirroring `contracts.py`/`primitives.py`).
- New `audit_log` action types + an `entry_mode` column (Alembic migration under
  `agent/migrations/`).
- `copilot/verification/writes.py`: the deterministic write-verification gate
  (enum + unit + range), reusing `rules.py` thresholds. Unit-tested like
  `tests/test_verification_core.py`.
- `OpenEmrWriteClient` against the Standard API, taking a **physician-delegated**
  token provider (`SmartAppLaunchTokenProvider`). Poller/system token never
  gains write scope.
- New `WriteAction` enum (`proposed / confirmed / committed / blocked / failed`)
  paralleling `VerificationAction`.

**Phase 1 — direct edit only.**
- **Scope:** vitals, medications/prescriptions, allergies, problems (the writable
  Standard-API surface). **Labs explicitly excluded** (no endpoint).
- **UI:** editable structured field in the record drill-down; echo-back "creates
  a new record dated now"; soft range warning; explicit Save.
- **Server:** a `POST /v1/records` (or per-type) route → write-verification gate
  → `OpenEmrWriteClient.create_*` → `audit_log` `write_committed`
  (`entry_mode=human_direct`) → post-commit read-back.
- **Provenance:** human author (recorded in `audit_log` + entry comment).
- **Tests:** DB-backed write happy-path + range-warning + ACL-denied + version-
  conflict, mirroring the existing suite.

**Phase 2 — agent proposes only, mandatory confirmation.**
- Add a `propose_write` tool to `ClaudeAgent` (`copilot/agent/claude.py`) that
  returns a typed candidate and **cannot commit**.
- Chat renders the candidate as a confirmation card (echo-back + range flags).
- Separate `POST /v1/writes/{id}/confirm` commits via the Phase-1 path with
  `entry_mode=agent_proposed_physician_confirmed`; hard block on out-of-range
  agent proposals (force fallback to direct entry).
- Extend the eval suite (`agent/evals/`) with **adversarial write cases**:
  hallucinated value, wrong unit, wrong metric, out-of-range, ambiguous NL —
  asserting each is blocked or forced to human re-entry, never silently
  committed.
- Add write metrics to observability (`copilot/observability/`) mirroring
  `record_verification`.

**Phase 3 — deferred upgrades (only when OpenEMR supports them).**
- FHIR-native `Provenance.create` when exposed (replaces the `audit_log`-only
  attribution surrogate).
- Lab-result Observation writes if/when OpenEMR exposes `procedure_result`
  writes or a custom controller.
- Reconciliation-aware writes that respect the existing `medication_reconciliation`
  domain rule; batch/transaction writes.

---

## Sources

- OpenEMR route maps (this repo, verified):
  `apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php`,
  `apis/routes/_rest_routes_standard.inc.php`
- [OpenEMR FHIR write-flow limitation — community thread (MedicationRequest.create → 404)](https://community.open-emr.org/t/help-enabling-write-flows-medicationrequest-create-documentreference-create-on-openemr-7-0-3/26006)
- [OpenEMR FHIR_README.md](https://github.com/openemr/openemr/blob/master/FHIR_README.md)
- [FHIR R4 Observation](https://hl7.org/fhir/R4/observation.html) · [ObservationStatus (HAPI)](https://hapifhir.io/hapi-fhir/apidocs/hapi-fhir-structures-r4/org/hl7/fhir/r4/model/Observation.ObservationStatus.html)
- [FHIR versioning](https://build.fhir.org/versioning.html) · [Resource / meta](https://www.hl7.org/fhir/resource.html)
- [FHIR R4 Provenance](http://hl7.org/fhir/R4/provenance.html) · [US Core Basic Provenance](https://build.fhir.org/ig/HL7/US-Core/basic-provenance.html)
- [SMART App Launch v2 scopes](https://hl7.org/fhir/smart-app-launch/scopes-and-launch-context.html) · [scopes-v2 wiki](https://github.com/smart-on-fhir/smart-on-fhir.github.io/wiki/scopes-v2)
- [45 CFR §164.526 — Amendment of PHI](https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-C/part-164/subpart-E/section-164.526)
- [HIPAA §164.312(b) audit trails / EMR alteration (Frier Levitt)](https://www.frierlevitt.com/articles/understanding-emr-audit-trails-importance-and-implications-for-medical-record-alteration/)
- [Amendments & corrections to the medical record](https://www.autonotes.ai/compliance/amendments-and-corrections-to-the-medical-record/)

## Grounding — files reviewed in this repo

- `agent/copilot/fhir/client.py` — read-only client (`read`/`search`/`count_since`); no write path.
- `agent/copilot/fhir/auth.py`, `agent/copilot/fhir/provider.py` — SMART App Launch (physician-delegated) vs Backend Services (`system/*.read`) token providers.
- `agent/copilot/verification/core.py`, `rules.py`, `serve.py` — deterministic gate, domain/range rules, fail-closed serve-time re-verify.
- `agent/copilot/agent/grounding.py`, `agent/copilot/agent/claude.py` — code-built source refs; agent proposes, code grounds.
- `agent/copilot/domain/primitives.py`, `contracts.py` — typed primitives, `FhirReference`, `VerificationAction`.
- `agent/copilot/memory/models.py`, `repository.py` — `AuditLogRow` (append-only trail), `record_audit`.
- `agent/copilot/chat/service.py`, `agent/copilot/api/routes/chat.py` — serve-time orchestration, §164.312(b) read-audit, authz boundary.
- `agent/web/src/components/ProvenanceChip.tsx`, `ClaimList.tsx`, `agent/web/src/api/types.ts` — drill-down / provenance UI surface.
