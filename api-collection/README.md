# AgentForge Clinical Co-Pilot — API Collection

Importable request collection for the `copilot` agent, in **two** formats so you
can use whichever tool you have:

- **Bruno** — the `*.bru` files + `bruno.json` in this directory. Open Bruno →
  *Open Collection* → point at `api-collection/`. Pick the **Local** or
  **Droplet** environment (top-right).
- **Postman** — `AgentForge.postman_collection.json`. Postman → *Import* → select
  that file. Set the collection variable `baseUrl` (defaults to
  `http://localhost:8010`).

Both point every request at a `{{baseUrl}}` variable and are kept in sync (the
same request set exists in both formats).

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

## Auth model — smart mode vs disabled mode

Identity resolution is gated on the deployment's `auth_mode`:

- **`disabled` (Local dev, the default)** — no login, no session. Every data
  route takes the acting clinician from the **request** `clinician_id` (body
  field or query string), exactly as before. This is the environment to exercise
  chat / rounds / observations / writes as raw API calls from Bruno or Postman.

- **`smart` (deployed droplet)** — per-physician SMART App Launch login.
  Identity is resolved from a server-side session keyed by the **HttpOnly
  `af_session` cookie**. The interactive data routes (chat, rounds/\*, refresh,
  alerts, observations, writes) return **401** without a valid session cookie,
  and **403** if a request-supplied `clinician_id` disagrees with the session.
  The login itself (`/v1/auth/login` → OpenEMR consent → `/v1/auth/callback`) is
  a **browser redirect flow** that sets the cookie, so raw API clients cannot
  complete it — see below.

**Which routes work without a session:** `GET /health`, `GET /ready`, and the
`GET /v1/auth/status` probe are always unauthenticated. `GET /v1/auth/me` returns
401 until you are logged in.

### Exercising the data routes

- **Against the droplet (smart mode):** complete the SMART login in a **browser**
  at `https://agentforge.hankholcomb.com` (OpenEMR consent + the callback set the
  `af_session` cookie). The full web UI is the intended way to drive the data
  routes in smart mode. Bruno/Postman will replay a cookie captured in their
  cookie jar, but they cannot perform the interactive OpenEMR consent for you.
- **Against a local instance (disabled mode):** pick the **Local** environment;
  `clinician_id` in the request is authoritative and no login/cookie is needed.

### Write-back is flag-gated

`POST /v1/writes` and its confirm are inert unless `COPILOT_WRITEBACK_ENABLED=true`.
Write-back is **OFF** on the deployed droplet, so both return **503**
"Write-back is currently disabled". To try them, run a local instance with the
flag set. The confirm request is a template: paste the `idempotency_key` and the
`candidate` object exactly as returned by *Writes Propose* (the candidate carries
typed ids as `{ "value": N }`), and the URL key must equal
`candidate.idempotency_key`.

## Typical flow

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

Both environments define: `baseUrl`, `clinician_id`, `patient_id`,
`conversation_id`, `metric` (humanized label for the observation series, e.g.
`Potassium`), and `idempotency_key` (for the write confirm template). The full
OpenAPI schema is live at `{{baseUrl}}/docs` (Swagger UI) and
`{{baseUrl}}/openapi.json`.
