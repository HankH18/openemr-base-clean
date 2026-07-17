# AgentForge Clinical Co-Pilot — API Collection

Importable request collection for the `copilot` agent, in **two** formats so you
can use whichever tool you have:

- **Bruno** — the `*.bru` files + `bruno.json` in this directory. Open Bruno →
  *Open Collection* → point at `api-collection/`. Pick the **Local** or
  **Droplet** environment (top-right).
- **Postman** — `AgentForge.postman_collection.json`. Postman → *Import* → select
  that file. Set the collection variable `baseUrl` (defaults to
  `http://localhost:8000` — the port the local agent listens on, per the root
  `README.md` "Run locally" and `agent/Dockerfile`'s `CMD … --port 8000`).

Both point every request at a `{{baseUrl}}` variable and are kept in sync (the
same request set exists in both formats). Between them they cover **every path
the live app publishes** at `{{baseUrl}}/openapi.json` (24/24), matching the
committed contract in `../agent/openapi/week2.yaml`.

## Endpoints covered

| Request | Method + path | Notes |
|---|---|---|
| Health (liveness) | `GET /health` | unauthenticated |
| Ready (dependencies) | `GET /ready` | unauthenticated |
| Auth Status | `GET /v1/auth/status` | unauthenticated; reports `auth_mode` + `authenticated` |
| Auth Login | `GET /v1/auth/login` | smart mode → 302 (browser flow); disabled → 404 |
| Auth Callback | `GET /v1/auth/callback` | OAuth redirect target; not called by hand |
| Auth Me | `GET /v1/auth/me` | 200 identity when authed, else 401 |
| Auth Logout | `POST /v1/auth/logout` | 204; clears the session cookie |
| Chat (grounded drill-down) | `POST /v1/chat` | data route (see auth model) |
| Get Conversation | `GET /v1/conversations/{id}` | |
| Rounds Start | `POST /v1/rounds/start` | data route |
| Rounds Current | `GET /v1/rounds/current?clinician_id=` | data route |
| Rounds Advance | `POST /v1/rounds/advance` | data route |
| Rounds Jump | `POST /v1/rounds/jump` | data route |
| Rounds Refresh | `POST /v1/rounds/refresh` | data route |
| Rounds Alerts | `GET /v1/rounds/alerts?clinician_id=` | data route |
| Observations Series | `GET /v1/patients/{id}/observations?metric=&clinician_id=` | data route |
| Writes Propose | `POST /v1/writes` | **flag-gated** (503 unless `writeback_enabled`) |
| Writes Confirm | `POST /v1/writes/{idempotency_key}/confirm` | **flag-gated** (503 unless `writeback_enabled`) |

### Week 2 — document ingestion, evidence, status

| Request | Method + path | Notes |
|---|---|---|
| Document Upload (lab_pdf) | `POST /v1/documents` | multipart; `file` + `patient_id` required; captures `document_id` |
| Document Upload (intake_form) | `POST /v1/documents` | same path, `doc_type=intake_form` |
| Document Upload (medication_list) | `POST /v1/documents` | same path, `doc_type=medication_list` |
| Document Status (facts + citations) | `GET /v1/documents/{document_id}` | poll until `status="extracted"` |
| Document Page Image | `GET /v1/documents/{document_id}/pages/{page_no}` | raw `image/png` (bbox-overlay backdrop) |
| Chat (document + guideline evidence) | `POST /v1/chat` | evidence retrieval after an upload |
| Writes Propose from Document | `POST /v1/writes/propose-from-document/{document_id}` | **flag-gated**; intake→proposed-writes bridge |
| Status (metrics dashboard) | `GET /v1/status` | unauthenticated; no PHI |
| Status (root alias, local only) | `GET /status` | same handler, but **not reachable on the droplet** — see below |

**Document upload — the multipart fields** (per
`Body_upload_document_v1_documents_post` in the OpenAPI contract):

| Field | Required | Notes |
|---|---|---|
| `file` | **yes** | the scanned PDF; points at `../demo/sample_docs/*.pdf` (see below) |
| `patient_id` | **yes** | integer > 0 |
| `clinician_id` | no | integer > 0 — optional *on purpose* (see auth model) |
| `doc_type` | no | default `lab_pdf`; **closed set** `lab_pdf` \| `intake_form` \| `medication_list` |

`clinician_id` is optional on the upload and the document reads because in smart
mode identity comes from the cookie and the UI sends none — a *required* field
would 422 the upload before auth could run. In **disabled** mode an asserted
`clinician_id` is still required (400 without one). An unknown `doc_type` fails
loud with **400** rather than silently defaulting to `lab_pdf` — a mis-typed
upload would otherwise be extracted against the wrong schema.

### Sample documents

The three upload requests point at the committed samples in
`../demo/sample_docs/` — synthetic, fictional patient, **no real PHI**:

| Sample | `doc_type` | Feeds |
|---|---|---|
| `sample_lab_report.pdf` | `lab_pdf` | lab facts + citations; sepsis/AKI guideline evidence |
| `sample_intake_form.pdf` | `intake_form` | intake facts → the write-back bridge |
| `sample_medication_list.pdf` | `medication_list` | 6 meds → the densest input for the bridge |

- **Bruno** resolves `@file(../demo/sample_docs/…)` relative to this collection
  directory, so the uploads run as-is from a clone.
- **Postman** cannot attach a repo-relative path (a browser/security limit): the
  `src` is recorded as a hint, so **re-select the file in the Body tab** if it
  shows as missing.

## Auth model — smart mode vs disabled mode

Identity resolution is gated on the deployment's `auth_mode`:

- **`disabled` (Local dev, the default)** — no login, no session. Every data
  route takes the acting clinician from the **request** `clinician_id` (body
  field, **multipart form field**, or query string), exactly as before. This is
  the environment to exercise chat / rounds / observations / writes / documents
  as raw API calls from Bruno or Postman — **including the multipart upload**,
  which a raw client cannot drive against smart mode.

- **`smart` (deployed droplet)** — per-physician SMART App Launch login.
  Identity is resolved from a server-side session keyed by the **HttpOnly
  `af_session` cookie**. The interactive data routes (chat, rounds/\*, refresh,
  alerts, observations, writes, **documents/\***) return **401** without a valid
  session cookie, and **403** if a request-supplied `clinician_id` disagrees with
  the session. The login itself (`/v1/auth/login` → OpenEMR consent →
  `/v1/auth/callback`) is a **browser redirect flow** that sets the cookie, so
  raw API clients cannot complete it — see below.

**Which routes work without a session:** `GET /health`, `GET /ready`, the
`GET /v1/auth/status` probe, and **`GET /v1/status`** (the metrics dashboard
carries no PHI) are always unauthenticated. `GET /v1/auth/me` returns 401 until
you are logged in.

> **`GET /status` is local-only.** The app defines it as a true alias of
> `/v1/status`, but the droplet's edge (Caddy) reverse-proxies only
> `path /v1/* /health /ready /openapi.json /docs /docs/*` to the agent. `/status`
> is not in that matcher, so it falls through to the SPA catch-all and returns
> **200 `text/html`** — the web UI, not the JSON. Verified against the live
> droplet (`/status` → `text/html`; `/v1/status` → `application/json`). Use
> `/v1/status`; the alias is only useful against a Local instance (uvicorn
> direct, no Caddy).

**Authorization is separate from authentication.** Every patient-scoped route —
the document ones included — enforces the same rounding-list boundary: run
**Rounds Start** for that `patient_id` first, or you get **403 "Patient is not on
your rounding list"**. On the document reads the checks run in a deliberate
order: identity → **404** → **403**, so an unauthenticated caller cannot even
learn whether a document id exists.

### Exercising the data routes

- **Against the droplet (smart mode):** complete the SMART login in a **browser**
  at `https://agentforge.hankholcomb.com` (OpenEMR consent + the callback set the
  `af_session` cookie). The full web UI is the intended way to drive the data
  routes in smart mode. Bruno/Postman will replay a cookie captured in their
  cookie jar, but they cannot perform the interactive OpenEMR consent for you.
- **Against a local instance (disabled mode):** pick the **Local** environment;
  `clinician_id` in the request is authoritative and no login/cookie is needed.

### Write-back is flag-gated

`POST /v1/writes`, its confirm, and `POST /v1/writes/propose-from-document/{id}`
are inert unless `COPILOT_WRITEBACK_ENABLED=true`. Write-back is **OFF** on the
deployed droplet, so all three return **503** "Write-back is currently disabled".
To try them, run a local instance with the flag set. The confirm request is a
template: paste the `idempotency_key` and the `candidate` object exactly as
returned by *Writes Propose* (the candidate carries typed ids as
`{ "value": N }`), and the URL key must equal `candidate.idempotency_key`.

The **403** rounding-list check runs *before* the 503 flag gate, so feature
availability never leaks to an unauthorized caller.

**Document upload is not flag-gated** — with write-back off it falls back to the
derived-only uploader: ingestion + extraction still run locally, the source
document is simply never pushed to OpenEMR (`openemr_document_id` stays `null`).
So the upload → status → page → evidence flow is fully exercisable with
write-back off; only the propose step needs the flag.

## The full Week-2 flow (document → evidence → proposed write)

Run these **in order** against a **Local (disabled-mode)** instance — the one
environment where a raw API client can drive the whole thing end to end. Each
step hands the next one what it needs; `document_id` is captured automatically.

1. **Status (metrics dashboard)** — `GET /v1/status`, unauthenticated. Note
   `ingestion_count` so you can watch it move.
2. **Rounds Start** — **required first**, or every later step 403s. It is what
   authorizes this clinician for `{{patient_id}}` (the default `101` is already
   on the seeded list).
3. **Document Upload (intake_form)** — or `lab_pdf` / `medication_list`. Returns
   **202** `{document_id, status, correlation_id}` and **captures `document_id`**
   into the environment (Bruno `vars:post-response`; Postman test script), so no
   copy-paste is needed.
4. **Document Status (facts + citations)** — **poll** until `status` is
   `"extracted"` (real vision extraction takes a few seconds; `"failed"` is
   terminal). Inspect `extraction.facts[]` and `citations[]` — citations exist
   only for `supported` facts (fail-closed provenance).
5. **Document Page Image** — take a citation's `page_or_section` → set
   `{{page_no}}` → the raw PNG comes back. Draw that citation's `bbox` over it:
   that is the pixel-level provenance.
6. **Chat (document + guideline evidence)** — evidence retrieval. `claims[]`
   carry document-typed citations for facts from the upload; `guideline_evidence[]`
   is a separate labeled block (the two grounding surfaces never mix).
7. **Writes Propose from Document** — *(needs `COPILOT_WRITEBACK_ENABLED=true`)*
   the intake→proposed-writes bridge: `{document_id, count, proposals[]}`. Use an
   `intake_form` / `medication_list` upload — a `lab_pdf` yields no proposals
   (labs are not lists-backed writes).
8. **Writes Confirm** — paste one proposal's `idempotency_key` + `candidate`
   verbatim. The agent **structurally cannot** self-commit: the bridge only
   proposes, so the commit is always this separate human transaction.
9. Re-run **Status** — `ingestion_count` and `extraction_field_pass_rate` moved.

## Week-1 flow

1. **Rounds Start** — establishes the acuity-ranked round *and* authorizes the
   clinician to chat / read observations about those patients.
2. **Rounds Current / Advance / Jump / Refresh / Alerts** — walk and re-sync the
   round (deterministic, no LLM).
3. **Chat** — ask a grounded question about the current patient. Every claim is
   verified against a live FHIR re-fetch; ungroundable questions are withheld
   (fail-closed), never guessed.
4. **Observations Series** — drill into one metric's grounded trend line.
5. **Writes Propose → Writes Confirm** — (when `writeback_enabled`) the two-step
   propose/echo-back → explicit confirm gate for a physician direct-edit.
6. Note the **`X-Correlation-ID`** response header — it is the Langfuse trace id
   for that request (see `../OBSERVABILITY.md`).

## Environment variables

Both environments define:

| Variable | Default | Purpose |
|---|---|---|
| `baseUrl` | Local `http://localhost:8000` / Droplet `https://agentforge.hankholcomb.com` | every request targets it |
| `clinician_id` | `1` | acting clinician (authoritative in disabled mode) |
| `patient_id` | `101` | subject patient; must be on the round |
| `conversation_id` | `1` | for *Get Conversation* |
| `metric` | `Potassium` | humanized label for the observation series |
| `idempotency_key` | `write-demo-001` | the write-confirm template |
| `document_id` | `1` | seeds the document reads; **overwritten** by any upload |
| `page_no` | `1` | 1-based page for the page image |

In Postman these are **collection variables** (Variables tab) rather than a
separate environment file; set `baseUrl` there to switch between local and the
droplet.

> **Keep `environments/*.bru` to `vars` blocks only.** Bruno's *environment*
> grammar accepts `vars` / `vars:secret` / `color` and nothing else — a `docs { }`
> block or a `#` comment in an environment file is a **parse error**, and Bruno
> then silently loads no environment at all, leaving every `{{baseUrl}}`
> unresolved and the whole collection unrunnable. Per-environment prose belongs
> here in the README. (Request `.bru` files are different — `docs { }` is valid
> and expected in those.)

**Running it: deployed vs local.**

- **Deployed (droplet, `smart` mode)** — pick the **Droplet** environment. Only
  `/health`, `/ready`, `/v1/auth/status` and `/v1/status` answer a raw client;
  everything else needs the `af_session` cookie from the **browser** SMART login,
  which Bruno/Postman cannot perform for you. Drive the document flow from the
  web UI here, or replay a cookie captured in the client's cookie jar. Write-back
  is off, so the propose step returns 503.
- **Local (`disabled` mode)** — pick the **Local** environment. `clinician_id` in
  the request is authoritative, no login or cookie needed; this is where the full
  Week-2 flow above runs end to end. Set `COPILOT_WRITEBACK_ENABLED=true` on the
  local process to exercise the propose/confirm steps too.

The full OpenAPI schema is live at `{{baseUrl}}/docs` (Swagger UI) and
`{{baseUrl}}/openapi.json`; the committed contract is
`../agent/openapi/week2.yaml`.
