# OpenEMR Clinical Co-Pilot — Audit

Fork audited: `Gauntlet-HQ/openemr-base-clean` · OpenEMR **8.2.0-dev** · PHP 8.2 · MySQL/MariaDB
· FHIR R4 US Core 3.1.0 · `league/oauth2-server ^8.4`.

> **Scope note.** This audit covers the **static** dimensions (security, architecture,
> data-quality, compliance) established by reading the codebase directly. **Performance
> under load** (runtime latency, CPU/memory/throughput profiles, bottleneck traces) is
> marked *pending* until the fork is deployed and load-tested per MVP Stages 1–2; the
> methodology and expected hotspots are stated so the numbers can be filled in.

---

## Summary (~500 words)

The single most consequential finding is a **data-quality** one: the fork's shipped demo
data is **demographics-only**. A repo-wide search of every `.sql` file for inserts into
`form_encounter`, `form_vitals`, `procedure_result`, `procedure_report`, `prescriptions`, or
`lists` returns **nothing**; the only seed files are `example_patient_data.sql` (14 patient
demographic rows) and `example_patient_users.sql` (provider logins). A Clinical Co-Pilot that
synthesizes "what changed overnight" therefore has **nothing to synthesize** out of the box.
Before any agent work, a realistic clinical dataset must be generated — encounters, lab
results (with reference ranges and abnormal flags), medication lists, vitals trends, notes,
and at least one scripted "overnight change" to demonstrate the proactive-alert path. This is
the finding that most changes the plan, and the one that would have silently sunk a
build-first approach.

On the **security / architecture** side, the news is favorable. OpenEMR exposes three viable
data-access surfaces — a proprietary REST API (~96 routes), a **FHIR R4 US Core** API (30+
resources), and an in-process **Services layer** — and, critically, implements **OAuth2 with
SMART-on-FHIR scopes**, including **SMART Backend Services** (`client_credentials` grant +
`system/*.read` scopes, confirmed in `AuthorizationController` and `ScopeRepository`). This
means access control has a native, standards-based primitive: an external agent can query as
the physician (SMART App Launch) and let OpenEMR enforce visibility, and a background poller
can act as a legitimate, scoped *system* actor rather than forging a user session. The
integration architecture (see `W2_ARCHITECTURE.md`) is built directly on this.

A **verification/grounding** asset: FHIR **Provenance** is genuinely implemented
(`FhirProvenanceService.php`, ~485 lines, with full domain classes), so "this claim traces to
that record" is modeled by the platform, not invented. Lab results carry `range` and
`abnormal` columns, giving the agent's domain-constraint checks real source data to enforce
against. OpenEMR also ships a **Clinical Decision Rules** engine, but it is PHP/UI-bound and
CQM-oriented — a reference and production-integration target, not something an external
Python service can invoke at runtime. There is **no CDS Hooks** support and **no working FHIR
Subscription** endpoint (the `FHIRSubscription` classes exist but no route/service is wired),
so change detection must be **poll-based** for v1; event-driven is a build-it scale path.

Key **risk landmines**: (1) a **polymorphic `lists` table** stores problems, allergies, and
medications together by `type`, and medications *also* live in a separate `prescriptions`
table — two sources that can disagree, a real agent failure mode and a verification design
problem ("which med list is authoritative?"). (2) **Two authorization systems** coexist —
legacy GACL for the web UI and SMART scopes for the API — so an audit of "who can see what"
must reason about both. (3) The schema is large (**281 tables**), mixing modern PSR-4 code
with legacy procedural code; querying raw tables is a trap, and reading through the
Services/FHIR layer is the sane path.

---

## 1. Security audit

- **AuthN/AuthZ (API):** OAuth2 via `league/oauth2-server ^8.4`; SMART App Launch
  (authorization-code) and **SMART Backend Services** (`client_credentials`, `system/*.read`)
  both present (`src/RestControllers/AuthorizationController.php`,
  `src/Common/Auth/OpenIDConnect/Repositories/ScopeRepository.php`). Scope parsing in
  `src/RestControllers/SMART/ScopePermissionParser.php`; patient-context launch supported
  (`PatientContextSearchController`). **Opportunity:** authorization can be enforced by the
  platform, not re-implemented.
- **Dual authz surfaces (risk):** legacy **GACL** (`gacl/`, `src/Gacl`) governs the web UI;
  SMART scopes govern the API. Any access-control reasoning must cover both. The agent uses
  the API surface, so SMART scopes are the relevant boundary — but a full HIPAA access review
  must not forget GACL exists.
- **PHI exposure vectors:** the FHIR/REST APIs return PHI; access is only as safe as the
  token issued. The agent must (a) use short-lived physician-delegated tokens for chat and
  (b) confine the poller's `system/*.read` client to the *minimum* resource types.
- **Secrets:** the agent introduces new sensitive material (poller client secret, LLM API
  key) — must live in a secrets manager, never in code or logs.
- **Prompt injection (new surface):** free-text note fields become an injection vector once
  an LLM reads them. Mitigation is architectural (deterministic verification gate; PHI treated
  as data, never instructions) — see `W2_ARCHITECTURE.md` §Security.

## 2. Architecture audit

- **Shape:** PHP 8.2 monolith, ~8,700 files. Modern code PSR-4 under `src/` (`OpenEMR\`
  namespace); legacy procedural code in `library/` and `interface/`. Laminas MVC + Symfony
  components; Twig/Smarty templates.
- **Data-access layers (3):** REST (`apis/`, ~96 routes), FHIR R4 US Core (`_rest_routes_fhir_*`),
  and `src/Services/*` (`PatientService`, `EncounterService`, `ObservationLabService`,
  `VitalsService`, `ProcedureService`, `MedicationPatientIssueService`, `ListService`,
  `UserService`, …). These are genuinely swappable behind an integration boundary.
- **Integration points:** a clean event dispatcher (`src/Core/Kernel.php`, `src/Events/`) and
  a custom-module system (`interface/modules/custom_modules`) exist for in-tree integration;
  an internal `BackgroundService` mechanism exists for monolith-side cron tasks.
- **Deployment:** Docker profiles under `docker/` including `development-easy` (admin/pass,
  ports 8300/9300), `development-easy-redis`, and `production`.

## 3. Data-quality audit  *(highest-impact section)*

- **Demo data is demographics-only.** `example_patient_data.sql` = 14 patient rows;
  `example_patient_users.sql` = provider logins. **No** encounters, vitals, labs, meds,
  problems, or notes anywhere in SQL (verified repo-wide). → **Action: generate a synthetic
  clinical dataset** before agent work; it doubles as eval ground truth.
- **Medication double-storage.** Meds appear in `lists` (`type='medication'`) *and* in
  `prescriptions` (separate table). These can disagree → agent must pick an authoritative
  source and verification must handle conflicts.
- **Polymorphic `lists` table.** Problems/allergies/medications share one table keyed by a
  `type varchar` — easy to mis-join; read via the typed Services/FHIR layer, not raw SQL.
- **Positive:** `procedure_result` carries `units`, `result`, `range`, `abnormal` — real
  reference-range and abnormal-flag data for domain-constraint checks.

## 4. Compliance & regulatory audit (HIPAA)

- **PHI everywhere:** patient data via API and any derived artifact (memory files,
  conversation history) is PHI → encryption at rest + TLS in transit are mandatory; PHI must
  never appear in application logs (log IDs + correlation ID only).
- **Audit logging:** HIPAA requires access logging. OpenEMR logs its own access; the **agent
  must add its own audit trail** (who queried what patient, when, what was returned) keyed by
  correlation ID.
- **BAA:** per the brief, treat all LLM-provider traffic as covered by a signed BAA with a
  no-training guarantee; use **demo data only**. The poller's broad read is the largest PHI
  movement — isolate and audit it.
- **Retention/immutability:** conversation history and memory files need a defined retention
  policy; audit rows should be append-only. Design the agent's datastore assuming a retention
  rule will land on it.

## 5. Performance audit  *(pending deployment — methodology + expected hotspots)*

- **Not yet measured** (requires MVP Stages 1–2 deployed + load tests). To be captured:
  baseline CPU/memory/throughput, p50/p95/p99 latency at 10 and 50 concurrent users.
- **Expected constraints affecting agent latency:**
  - FHIR reads are per-resource → assembling a full patient = several round-trips. Mitigate
    with change-gating (only re-pull changed patients) and `_revinclude` where supported.
  - The large schema and legacy query paths mean raw-DB access is a bottleneck trap; the
    Services/FHIR layer is the intended path.
  - LLM synthesis latency dominates the *briefing* path (acceptable — it's async/prep);
    interactive drill-down must be tuned for low-seconds and is the latency-critical path.
- **Change-detection efficiency (verified capability):** FHIR `_lastUpdated` (185 refs,
  `meta.lastUpdated` carrier) + `_summary=count` / `_count` support enable cheap
  "did-anything-change" queries, so cost/latency scale with **change rate**, not
  patient-count × poll-frequency.

---

## Most important finding & its impact on the AI plan

The demographics-only demo data. Skipping the audit and building first would have produced an
agent with no clinical substance to reason over and an eval suite with no ground truth. Because
the audit surfaced it early, the plan front-loads **synthetic clinical data generation** as
Stage 0 of the build, and reuses that dataset as verification ground truth — turning a
showstopper into an asset. The favorable auth findings (SMART Backend Services, Provenance,
abnormal/range flags) then let the architecture lean on platform primitives instead of
re-inventing access control and grounding.
