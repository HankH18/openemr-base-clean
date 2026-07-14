# MVP Build Plan — AgentForge Clinical Co-Pilot

**Audience:** a Claude Code agent driving this repo to the MVP checkpoint.
**Deadline:** MVP checkpoint — **Tuesday 11:59 PM CT** (AI interview 24h after submission).
**Fork:** `https://github.com/Gauntlet-HQ/openemr-base-clean` (OpenEMR 8.2 line).

---

## 0. Read this first — what "MVP" means here

Straight from the brief: **"The MVP is not a working agent. It is the foundation that makes a trustworthy agent possible."** Do **not** build the chatbot, verification engine, or poller for this checkpoint. Build the foundation, and set it up so it *compounds* — good architecture now saves double later.

There is a **hard gate**: the audit must be complete **before** any AI-layer work. Respect that ordering.

### MVP hard gates (all required at submission)
1. **GitHub repository** — forked from OpenEMR, with a setup guide, an architecture overview, and the deployed link.
2. **`./AUDIT.md`** — full audit, beginning with a ~500-word one-page summary of key findings.
3. **`./USERS.md`** — target user, workflow, and use cases (each with an explicit *why an agent*).
4. **`./ARCHITECTURE.md`** — the agent build plan, beginning with a ~500-word high-level summary.
5. **Deployed application** — publicly accessible; **you must submit the live URL**. Same infrastructure the final agent will deploy to.
6. **Demo video (3–5 min)** — *human-produced*; you prepare the script/checklist, you do not record it.

The five recommended stages map to these: Run Locally → Deploy → Audit (`AUDIT.md`) → Identify Users (`USERS.md`) → Plan the Agent (`ARCHITECTURE.md`).

---

## 1. Decisions already made — respect these, don't relitigate

Three planning docs already exist and should be placed at the repo root, then **validated against the running fork** (not rewritten): `AUDIT.md`, `USERS.md`, `ARCHITECTURE.md`. If they are not present in the repo, ask the operator for them before regenerating from scratch.

The load-bearing architecture decisions the foundation must be consistent with (full rationale in `ARCHITECTURE.md`):

- **User:** a **hospitalist** rounding on ~12 admitted patients before noon. One patient at a time, chat-only, paced by the doctor's "done" signal, with proactive soft-preempt on deterioration.
- **Integration (keystone):** a **separate Python service**; **all patient data via OpenEMR's FHIR/REST API** — never the raw 281-table schema, never an in-process PHP module.
- **Access:** two OAuth actors — **SMART App Launch** (interactive chat acts as the physician) and **SMART Backend Services** (`client_credentials`, `system/*.read`) for the background poller. **No read path bypasses OpenEMR's authorization.**
- **Runtime:** Python 3.12 · FastAPI · Pydantic v2 contracts · Pydantic AI agent loop · Claude (Anthropic).
- **State:** an **encrypted PostgreSQL 16** owned by the agent (memory files as JSONB + provenance pointers, sync bookkeeping, rounding cursor, conversation, append-only audit). Redis deferred.
- **Grounding:** memory-file-with-provenance; verification fails closed at synthesis + serve time. *(Built later — not at MVP.)*
- **Observability:** Langfuse + correlation IDs. *(Wired later — not at MVP.)*
- **Deployment:** Docker Compose on the **same infrastructure** as OpenEMR, behind a TLS reverse proxy; orchestrated later for scale.
- **UI:** React chat panel launched in-context via SMART App Launch. *(Built later — not at MVP.)*

---

## 2. Tasks (ordered)

Work top to bottom. Each task lists steps and an **acceptance check**. Tasks marked **[gate]** are required for the MVP submission; tasks marked **[foundation]** are not required for the gate but are cheap now and compound into Early Submission — do them if time allows, in order.

### Task 1 — Fork & run OpenEMR locally  **[gate: Stage 1]**
- Fork `Gauntlet-HQ/openemr-base-clean`; clone locally.
- Bring it up with the bundled Docker dev stack (`docker/development-easy`, admin/pass, ports 8300/9300). Prefer the `development-easy-redis` profile if you want Redis available for later.
- Confirm you can log in and load a patient.
- **Acceptance:** `docker compose … up` yields a reachable OpenEMR login; at least one patient record opens. Setup steps captured for the README (Task 8).

### Task 2 — Generate a realistic synthetic clinical dataset  **[gate: Stage 1 — CRITICAL]**
The shipped seed data is **demographics-only** (14 patients, no clinical content). You cannot audit or demo a "what changed overnight" agent against empty charts. This task is the linchpin of the MVP; do it before finalizing the audit's data-quality and performance sections.

- Produce realistic clinical data for a rounding-list-sized cohort (~12–20 inpatients). Two acceptable approaches:
  - **Synthea** (recommended for realism) to generate FHIR R4 patients, then load into OpenEMR via its FHIR/REST write endpoints where supported, falling back to a **SQL seed script** for resources the API won't accept; **or**
  - a **deterministic SQL/REST seed script** you write directly against OpenEMR's clinical tables.
- The data **must** include, because the agent and audit depend on it:
  - **Encounters** (`form_encounter`) — recent inpatient encounters per patient.
  - **Labs with reference ranges + abnormal flags** (`procedure_order` / `procedure_report` / `procedure_result`, populating `units`, `result`, `range`, `abnormal`) — the agent's domain-safety checks read these.
  - **Vitals trends** (`form_vitals`) over multiple timepoints.
  - **Medications in BOTH `lists` (type=medication) and `prescriptions`** — intentionally include at least one patient where the two disagree, to preserve the real reconciliation problem the verification layer must handle.
  - **Problems & allergies** (`lists`, typed) — include at least one allergy that conflicts with an active medication.
  - **Clinical notes** (SOAP / encounter notes) with some free-text.
  - **A scripted "overnight change"** on exactly one not-yet-seen patient (e.g., a new critical lab + a documented event) with a `date`/`lastUpdated` in the last few hours — this is what the deterioration-alert demo and later eval will hinge on.
- Keep the seed reproducible: commit the generator/script and a one-command reload. **Demo data only — never real PHI.**
- **Acceptance:** a documented, re-runnable command repopulates a cohort with all the data types above; spot-check via the FHIR API that `Observation`/`DiagnosticReport` carry ranges + abnormal flags and that meds appear in both stores.

### Task 3 — Complete & validate the audit  **[gate: Stage 3 → `AUDIT.md`]**
The static audit (security, architecture, data-quality, compliance) is already drafted. Your job: **validate each finding against the running fork** and **fill the performance section**, which was pending deployment.
- Re-confirm the static findings by inspection (SMART Backend Services present, FHIR `Provenance` implemented, dual authz schemes, polymorphic `lists` + `prescriptions` duplication, no FHIR Subscription/push).
- **Performance audit (now runnable):** capture baseline CPU/memory/throughput and p50/p95/p99 latency for representative FHIR reads against your seeded cohort; note the per-resource multi-call cost of assembling a full patient and confirm `_lastUpdated` + `_summary=count` change-detection queries work per resource type.
- Ensure `AUDIT.md` opens with the **~500-word summary** and leads with the demographics-only finding.
- **Acceptance:** `AUDIT.md` present at repo root, ≤~500-word summary first, all five audit dimensions covered, performance numbers filled from the live deployment, every static claim re-verified.

### Task 4 — Finalize the users doc  **[gate: Stage 4 → `USERS.md`]**
- Place `USERS.md` at the repo root; confirm it defines the hospitalist, the concrete pre-rounds workflow, and use cases UC-1…UC-7, **each with an explicit "why an agent."**
- Confirm every capability that `ARCHITECTURE.md` proposes traces back to a use case here (this doc is the source of truth).
- **Acceptance:** `USERS.md` present; no capability in `ARCHITECTURE.md` lacks a matching use case.

### Task 5 — Finalize the architecture plan  **[gate: Stage 5 → `ARCHITECTURE.md`]**
- Place `ARCHITECTURE.md` at the repo root; confirm the **~500-word summary** leads, and that the plan reflects the decisions in §1 (separate service, API-only, two OAuth actors, Postgres, verification fails-closed, poller change-gated, deploy same infra).
- Reconcile with what you learned running the fork; update any assumption that the live system contradicted (note the change, don't silently diverge).
- **Acceptance:** `ARCHITECTURE.md` present with summary first; the two assumptions flagged in it (`_lastUpdated` as a real per-resource filter; serve-time re-fetch latency) each have a note on how Task 6/foundation testing bears them out.

### Task 6 — Deploy the fork publicly  **[gate: Stage 2 → live URL]**
- Deploy the OpenEMR fork to a **publicly reachable** environment on the **same infrastructure the final agent will use** — a single cloud VM (e.g., EC2/Lightsail) running Docker Compose behind **Caddy or Traefik** for automatic TLS is the pragmatic MVP choice (swap for the operator's preferred host if specified). Not production-hardened, but live.
- Put OpenEMR and (later) the agent service on a shared internal network; only the reverse proxy is public.
- **Do not** commit secrets. Deployment credentials, TLS, DNS, and any account/secret entry are **operator actions** — prepare the config and commands, and hand off the steps that require credentials rather than performing them.
- **Acceptance:** a public HTTPS URL loads the deployed fork; the URL is recorded in the README and ready to paste into the submission.

### Task 7 — Foundation scaffolding  **[foundation]**
Cheap now, compounds into Early Submission. Do **not** implement agent logic — stubs only.
- Add an `agent/` service to the repo: **FastAPI (Python 3.12)** skeleton with **Pydantic v2** models stubbed, `GET /health` (liveness) and `GET /ready` (dependency checks: OpenEMR reachable, Postgres reachable — LLM/Langfuse added later) as separate endpoints, and a `pyproject.toml`/lockfile pinned. `uv` or `pip-tools`; `pip-audit` in CI later.
- Add a **PostgreSQL 16** service and an `agent` internal network to the compose stack.
- Add the migration tool (SQLAlchemy 2 + Alembic) with an empty baseline migration for the state model in `ARCHITECTURE.md` (memory_file, sync_state, last_seen, rounding_cursor, conversation, audit_log) — schema only, no logic.
- **Acceptance:** `docker compose up` starts OpenEMR + Postgres + the agent stub; `/health` returns liveness and `/ready` reports Postgres + OpenEMR reachability; no agent behavior implemented.

### Task 8 — README & setup guide  **[gate: repo]**
- Write the README: what the project is, local setup (from Task 1–2, including the one-command data reload), the deployed URL, and a short architecture overview linking to `ARCHITECTURE.md` / the diagram.
- **Acceptance:** a fresh clone can be brought up locally by following the README alone; deployed link present.

### Task 9 — Demo video script  **[gate support — human records]**
- Prepare a 3–5 min **shot list / script** (not the recording): the running deployed fork, the seeded clinical data (show a patient with the overnight change), a walk-through of the audit's headline finding and the architecture plan, and the "why an agent" framing. Save as `demo/SCRIPT.md`.
- **Acceptance:** `demo/SCRIPT.md` gives the operator a turnkey recording plan; flag clearly that recording + narration is the human's step.

### Task 10 — Keystone de-risking smoke test  **[foundation, high value]**
Validate the single biggest architecture assumption before Early Submission: that the agent can reach data the way the plan requires.
- Register a SMART client in OpenEMR; obtain a token via **SMART App Launch** (physician) and a **client_credentials** token with a `system/*.read` scope (Backend Services). Confirm a FHIR read succeeds under each, and that a physician token only returns that physician's patients.
- Confirm change detection: write a change to a seeded patient, then verify `GET /fhir/{Resource}?patient={id}&_lastUpdated=gt{ts}&_summary=count` reflects it.
- **Do not** enter or store secrets in the repo; surface any credential/registration step that needs the operator.
- **Acceptance:** a short `agent/smoke/README.md` records that both OAuth flows work, authorization scopes as expected, and `_lastUpdated` filtering catches a change — or documents precisely where it didn't, so the plan's fallback (DB fast-path for the poller) can be triggered.

---

## 3. Guardrails (apply throughout)

- **Don't build the agent.** No chatbot, verification, ranking, or poller logic for this checkpoint. Foundation and stubs only.
- **Audit before AI layer** — a hard gate; do not scaffold agent behavior before `AUDIT.md` is complete.
- **Demo data only.** Never load or fabricate real PHI. Treat all synthetic data as if under a signed BAA with a no-training guarantee.
- **API, not raw schema.** Any data access the foundation touches goes through OpenEMR's FHIR/REST layer, not direct SQL against the 281-table schema (seed scripts are the one allowed exception, and are clearly demo-only tooling).
- **Secrets never in the repo.** No credentials, tokens, TLS keys, or account passwords committed. Credential entry, account creation, and permission/OAuth-consent grants are **operator actions** — prepare and hand off, don't perform them.
- **HIPAA posture even now.** Encrypt the agent's Postgres volume; keep PHI (including synthetic) out of application logs (IDs + correlation IDs only).
- **Don't silently diverge from the plan.** If the running fork contradicts an assumption in `ARCHITECTURE.md`, update the doc and note the change; surface it, don't paper over it.

---

## 4. Definition of done (MVP)

- [ ] Fork runs locally via the README; one-command reload seeds a realistic clinical cohort (Task 1–2).
- [ ] Public HTTPS URL serves the deployed fork on the same infra the agent will use (Task 6).
- [ ] `AUDIT.md` — 5 dimensions, ~500-word summary first, performance numbers filled, findings re-verified (Task 3).
- [ ] `USERS.md` — hospitalist + workflow + UC-1…UC-7 with "why an agent" (Task 4).
- [ ] `ARCHITECTURE.md` — ~500-word summary first, consistent with §1, assumptions annotated (Task 5).
- [ ] README with setup guide, architecture overview, and deployed link (Task 8).
- [ ] `demo/SCRIPT.md` ready for the operator to record (Task 9).
- [ ] *(Foundation, if time)* agent stub with `/health` + `/ready`, Postgres + baseline migration, and a passing OAuth/`_lastUpdated` smoke test (Tasks 7, 10).

---

## 5. MVP interview prep (leave notes for the operator)

The interview 24h after submission will probe the audit and the plan. As you work, drop short notes in `NOTES.md` answering:
- The most important audit finding, and what building-first would have missed (lead with the demographics-only data).
- How the audit changed the AI integration plan.
- Where the trust boundaries are and how they're enforced (the two OAuth actors + serve-time re-check + no-bypass rule).
- What the agent will do when a tool fails or a record is missing (fail-closed / graceful degradation — per `ARCHITECTURE.md`).
