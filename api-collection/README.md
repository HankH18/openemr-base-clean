# AgentForge Clinical Co-Pilot — API Collection

Importable request collection for the `copilot` agent, in **two** formats so you
can use whichever tool you have:

- **Bruno** — the `*.bru` files + `bruno.json` in this directory. Open Bruno →
  *Open Collection* → point at `api-collection/`. Pick the **Local** or
  **Droplet** environment (top-right).
- **Postman** — `AgentForge.postman_collection.json`. Postman → *Import* → select
  that file. Set the collection variable `baseUrl` (defaults to
  `http://localhost:8010`).

Both point every request at a `{{baseUrl}}` variable.

## Endpoints covered

| Request | Method + path |
|---|---|
| Health (liveness) | `GET /health` |
| Ready (dependencies) | `GET /ready` |
| Chat (grounded drill-down) | `POST /v1/chat` |
| Get Conversation | `GET /v1/conversations/{id}` |
| Rounds Start | `POST /v1/rounds/start` |
| Rounds Current | `GET /v1/rounds/current?clinician_id=` |
| Rounds Advance | `POST /v1/rounds/advance` |
| Rounds Jump | `POST /v1/rounds/jump` |

## Typical flow

1. **Rounds Start** — establishes the acuity-ranked round *and* authorizes the
   clinician to chat about those patients.
2. **Rounds Current / Advance / Jump** — walk the round (deterministic, no LLM).
3. **Chat** — ask a grounded question about the current patient. Every claim is
   verified against a live FHIR re-fetch; ungroundable questions are withheld
   (fail-closed), never guessed.
4. Note the **`X-Correlation-ID`** response header — it is the Langfuse trace id
   for that request (see `../OBSERVABILITY.md`).

Request/response bodies use `{clinician_id, patient_id | patient_ids, message}`
as documented per request. The full OpenAPI schema is live at
`{{baseUrl}}/docs` (Swagger UI) and `{{baseUrl}}/openapi.json`.
