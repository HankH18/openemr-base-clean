# Interview Prep Notes — AgentForge Clinical Co-Pilot

Short answers to the questions the checkpoint interview probes. Backed by `AUDIT.md`,
`W2_ARCHITECTURE.md`, `USERS.md`, and the built agent (`agent/`) + UI (`agent/web/`).

## 1. The most important audit finding — and what building-first would have missed
**The shipped demo data is demographics-only.** A repo-wide search for inserts into
`form_encounter`, `form_vitals`, `procedure_result/report`, `prescriptions`, `lists` returns
nothing — just 14 demographic rows + provider logins. A "what changed overnight" agent would have
had **nothing to synthesize**, and the eval suite **no ground truth**. Building first, we'd have
shipped a confident-looking agent reasoning over empty charts and discovered it only at demo time.
Because the audit caught it, Stage 0 became **generate a realistic synthetic cohort** (encounters,
labs with reference ranges + abnormal flags, meds in both stores, a scripted overnight
deterioration) — and that dataset doubles as verification ground truth. A showstopper turned into
an asset.

## 2. How the audit changed the AI integration plan
- **Favorable auth findings let us lean on platform primitives.** OpenEMR implements OAuth2 with
  **SMART Backend Services** (`client_credentials` + `system/*.read`) and SMART App Launch. So we
  don't re-invent access control: chat reads **as the physician** (OpenEMR enforces visibility); the
  poller is a **scoped system actor**. No read path bypasses OpenEMR authorization.
- **FHIR `Provenance` is real** (`FhirProvenanceService.php`) and labs carry `range`/`abnormal` —
  so "this claim traces to that record" and the domain-safety checks are grounded in platform data,
  not invented.
- **No working FHIR Subscription** → change detection is **poll-based** for v1 (watermark +
  `_lastUpdated` count-gate + content-hash confirm), with event-driven as the documented scale path.
- **`lists` vs `prescriptions` double-storage** → medication reconciliation is a real verification
  problem we designed for (surface conflicts, don't silently merge).

## 3. Where the trust boundaries are, and how they're enforced
Three layers, deterministic-first:
1. **Two OAuth actors, no bypass.** Chat = short-lived physician-delegated token (SMART App Launch);
   poller = minimal `system/{Resource}.read` client-credentials grant. OpenEMR is the authorization
   authority.
2. **Serve-time re-check (UC-6).** Before any answer, the agent confirms the clinician is authorized
   for that patient (rounding-list membership) — else it refuses with a reason (HTTP 403). Broad read
   never becomes broad disclosure. *(Enforced in `copilot/auth/authorization.py`; test: chat about a
   patient off your list → 403.)*
3. **Deterministic verification gate the LLM can't talk past.** Every claim must carry a valid
   `source_ref` and every number/med-name must **exactly match** the cited record, re-fetched live at
   serve time. A prompt injected in a note field can steer the generator but still **fails
   attribution/value-match and is withheld**. This is the architectural answer, not a prompt plea.

## 4. What the agent does when a tool fails or a record is missing (fail-closed)
It **withholds rather than guesses**. If a cited resource can't be re-fetched, that claim fails
(treated as unverifiable) — never "assume true on error." All claims fail → the answer is `withheld`
with an honest "I can't confirm this — here's the source"; some fail → `degraded` (failing claims
dropped, the rest served). A fabricated or drifted numeric (live record ≠ what was said) is caught by
the value-match gate and withheld. *(See `copilot/verification/{core,serve}.py`; tests cover the
missing-resource, drift, and fabricated-number cases.)*

## 5. Quick "why an agent, not a dashboard?" (the USERS.md thesis)
A sorted list shows *fields*; it can't synthesize "creatinine 1.1→1.8 overnight, new fluids, watch
for AKI — see her first," maintain a paced patient-to-patient hand-off, defend its own ranking, or
proactively escalate a deteriorating not-yet-seen patient. Ranking + narrative synthesis + grounding
+ stateful pacing are agent behaviors. And the value isn't "it can chat" — it's that it changes
**what the physician walks into the room knowing**, on a foundation the buyer can trust.

## 6. Status honesty (what's real vs. operator-gated)
- **Real + tested deterministically:** rounds (start/current/advance + acuity ranking), grounded chat
  with serve-time verification, background refresh + deterioration alerts + memory persistence,
  authorization boundary, the React UI. 183+ tests green; 20/20 E2E acceptance.
- **Operator actions to go fully live:** `ANTHROPIC_API_KEY` (swaps the deterministic stub agent for
  live Claude), SMART client registrations (chat + backend-services), Langfuse creds, and the deploy.
  These need credentials and are intentionally not committed.
