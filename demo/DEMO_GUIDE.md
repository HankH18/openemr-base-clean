# AgentForge Clinical Co-Pilot — Demo Feature Guide

A recording companion for the current iteration of the app. Every feature below is
grounded in the code; where a feature is built but switched off in the live demo, it
says so plainly. Glance at this while you film.

> **Two things to internalize before you hit record.**
> 1. **The live demo opens with a real login.** Per-physician SMART-on-FHIR sign-in is
>    **LIVE** (`COPILOT_AUTH_MODE=smart` on the droplet): loading the URL lands you on the
>    OpenEMR SMART sign-in — no basic-auth prompt, no bare IP. Sign in with your **OpenEMR
>    admin credentials** (`OE_USER` / `OE_PASS` from the droplet `.env`), approve consent,
>    and you're in the app authenticated as that clinician — identity, reads, and any writes
>    run under your own delegated token. (The offline mock instead uses a hardcoded demo
>    clinician, `Dr. N. Ellery — Hospitalist`, id `42`; `agent/web/src/census.ts`.)
> 2. **The core promise is "grounded, verified, sickest-first."** Every claim cites a
>    real record; the agent withholds rather than guesses; the queue is ranked by acuity,
>    not room number.

---

## 1. What it is (the pitch)

AgentForge is a **hospitalist rounding co-pilot** built on an OpenEMR fork. Instead of a
dashboard, it opens on your **most acute** patient first, gives a **grounded, source-cited
chart summary** plus "what changed overnight," and answers drill-down questions in chat —
where a **fail-closed verification layer** re-checks every claim against the live record and
**withholds** an answer it can't prove rather than hallucinate. It reads patient data
**only** through OpenEMR's FHIR/REST API over OAuth, so no read path bypasses OpenEMR's own
authorization. The audience is a physician asking "could I actually rely on this on the
ward?" — the answer the demo earns is: grounded, access-controlled, and honest about
uncertainty.

---

## 2. Access

| | Live public demo | Offline mock (fully reproducible) |
|---|---|---|
| **URL** | https://agentforge.hankholcomb.com | http://localhost:5173 (after `npm run dev`) |
| **Access** | per-physician **SMART sign-in** (OpenEMR admin creds — see `ACCESS.md`) | none |
| **Backend** | real agent + Claude + OpenEMR FHIR | in-browser mock, no backend |
| **Census** | 15-patient census (`census.ts`) | 5-patient cohort (`cohort.ts`) |

- **Live URL + login:** open **`https://agentforge.hankholcomb.com`** (HTTPS via Caddy +
  Let's Encrypt — a valid cert, so no browser warning). There is **no basic-auth prompt and
  no bare IP** anymore. The first and only gate is the **OpenEMR SMART sign-in**: sign in
  with your **OpenEMR admin credentials** (`OE_USER` / `OE_PASS` from the droplet's
  gitignored `.env` — get the value from the operator; it is not in git), approve the
  consent screen, and you land in the app authenticated as that clinician. This is a **real
  delegated SMART-on-FHIR login to a genuine EHR** — identity, reads, and any writes run
  under your own delegated token. `COPILOT_AUTH_MODE=smart` is set on the droplet.
- **The login page is on-brand:** an AgentForge restyle (`custom/assets`) hides the
  "Patient Login" button and the "Email if required" field, paints the sign-in button in
  the AgentForge accent, and sets an **"AgentForge — Clinical Co-Pilot"** wordmark above
  "Sign In" — while staying recognizably OpenEMR, which nicely reinforces the
  real-delegated-auth story. Upgrade-safe; no core OpenEMR file is edited.
- **Live data is seeded and verified** (checked 2026-07-12): the droplet's OpenEMR holds the
  full 15-patient census (54 lab results, 25 medications), and the temporal / trend / acuity
  features were validated against it. If the live site ever shows the "record service is
  unreachable" gate, the **offline mock** is a byte-for-byte reproducible fallback
  (`cd agent/web && npm install && npm run dev`).
- **This guide's walkthrough is written for the LIVE census** (Marcus Webb / June Okafor /
  Denise Alvarez, etc.), because that seed is where the patients, starter questions, and the
  deterioration alert all line up. The mock cohort differs — see the **"Offline mock
  differences"** note at the end of §4.

---

## 3. Feature walkthrough

Each feature: **(a)** what it is · **(b)** what to click/type · **(c)** the on-camera
one-liner · **(d)** the patient/data that makes it land.

### 3.1 The ranked rounding queue (sickest-first)

- **(a)** On start, the agent reads every chart, scores acuity deterministically from the
  patient's FHIR observations (critical band 8.0–10.0, warning 3.5–6.5, normal 1.0), and
  opens on the highest score. The left **"Rounding order"** rail shows rank numerals, name,
  bed, a one-line condition, and a status chip (`Now` / `Up next` / `Upcoming` / `Seen` /
  `Alert`), plus an `N of M seen` progress count. Ranking is code, never an LLM guess —
  `agent/copilot/rounds/ranking.py`.
- **(b)** Just load the app. The loading gate reads "Ranking your list. Reading 15 charts —
  sickest first." Then it lands on the top card. Point at the rail; click any row to jump.
- **(c)** *"It doesn't hand you a dashboard — it opens on your most acute patient, and the
  order is a reproducible acuity ranking, not alphabetical."*
- **(d)** Opens on **Marcus Webb (MRN 1003, Bed 03-A)** — DKA, acuity ~9.0, ranked for
  "glucose 386, potassium 5.7, bicarbonate 12." The card's **"Ranked here:"** line names the
  exact driving labs.

### 3.2 The grounded per-patient chart summary

- **(a)** The patient hero shows identity + an **acuity meter** with its `rank_reason`, a
  **freshness tag** ("as of HH:MM · N min ago", with a "Stale — re-check advised" flag when
  old), a **"Since you last saw her"** overnight-changes section, and a **"Chart summary"**
  section — **one row per metric** (labs/vitals collapsed to their latest reading with a
  trend suffix), everything else once. Labels are **humanized** ("MedicationRequest" →
  "Medication"; "oxygen_saturation" → "Oxygen Saturation") while acronyms survive verbatim
  (WBC, BUN, aPTT). Duplicate medication rows are **de-duplicated** (a bare "Hydromorphone."
  is dropped in favor of the full "Hydromorphone 0.5 mg IV q4h PRN pain."). Built in
  `agent/copilot/rounds/summary.py`; mirrored in `agent/web/src/labels.ts`.
- **(b)** Read the open card top-to-bottom. Note the "Chart summary N records" count and the
  one-row-per-metric layout.
- **(c)** *"Every line is one clinical fact, in the words a doctor uses — collapsed to the
  latest value per metric, so it reads like a hand-off, not a data dump."*
- **(d)** **Marcus Webb (1003)** — a rich DKA picture (glucose, potassium, bicarbonate,
  active insulin/fluids) so the summary has real content to show.

### 3.3 Provenance chips + fail-closed verification (the trust proof)

- **(a)** Every claim carries a **✓ provenance chip** naming the source resource type
  (never a raw UUID); clicking it opens a popover with the **recorded value**, the
  **recorded timestamp**, and "Quoted verbatim from the source record"
  (`ProvenanceChip.tsx`). Underneath, a deterministic gate (`verification/core.py`) re-checks
  each claim against a **live FHIR re-fetch**: the cited resource must exist (attribution),
  the value must match verbatim, every numeric literal in the text must appear in the source,
  and any timestamp must re-derive exactly. If **no** claim verifies → **withheld**; mixed →
  **degraded** (only proven claims survive); all pass → **served**. The gate is **not
  promptable** — a fact injected via a note field still has to cite a real resource and match
  its value.
- **(b)** Click a provenance chip on any claim to show the exact `(resource, value,
  timestamp)`. Then go to chat (§3.4) for the withheld proof point.
- **(c)** *"Every claim traces to a record — click the chip and there's the exact value and
  timestamp. And it's re-verified against the live chart at serve time, by deterministic code
  the model can't talk past."*
- **(d)** Any card — the chips are everywhere. The **withheld** counter-example lives in
  chat, next.

### 3.4 Grounded chat + the "no source → withheld" moment

- **(a)** "Ask the chart" answers a free-text question about the current patient; the header
  reads *"Cited from the record, or withheld — never guessed."* Each answer is tagged
  **Verified — served** (green), **Degraded — re-check incomplete** (amber), or **Withheld —
  no source found** (red), with source chips on every cited claim and a `corr <id>` footer
  (see §3.9). Per-patient **starter-question chips** sit above the input
  (`agent/web/src/suggestions.ts`).
- **(b)** On Marcus Webb, click the starter chip **"Latest glucose?"** → a **served** answer
  citing the value with a source chip. Then click **"Any MRI report?"** → a **withheld**
  refusal ("I can't confirm that from this patient's record… Rather than guess, I'm
  withholding an answer.").
- **(c)** *"Ask about something real and it cites the record. Ask about an MRI we never did,
  and it refuses instead of inventing one — in a clinical setting a confident hallucination
  can harm a patient, so it withholds."*
- **(d)** **"Any MRI report?"** is the built-in last chip for **every** patient (confirmed in
  `suggestions.ts`) precisely because **no MRI exists anywhere in the seed** — it's a
  guaranteed, repeatable withheld answer. Use it on any patient.

### 3.5 Temporal Q&A (grounded `authoredOn`)

- **(a)** Time-scoped questions are grounded on real record timestamps: a MedicationRequest's
  `authoredOn`, an Observation's `effectiveDateTime`/`issued` (`agent/copilot/agent/
  grounding.py` → `extract_temporal`). The verification gate then **re-derives the same
  instant** from a live re-fetch and **fails closed on temporal drift** (`verification/
  core.py`).
- **(b)** Type a free-form temporal question, e.g. **"Which medications were started in the
  last 24 hours?"** on a med-heavy patient. The answer cites each order with its recorded
  time.
- **(c)** *"Time-scoped questions are grounded on the record's own timestamps — and the same
  timestamp has to survive a live re-check, or the claim is dropped."*
- **(d)** **Marcus Webb (1003)** (active DKA orders). **Live only** — this needs the real
  Claude agent + FHIR timestamps; the offline mock answers by keyword and may withhold a
  free-form temporal phrasing.

### 3.6 Per-metric trend chart + drill-down timestamps

- **(a)** Any **numeric Observation** claim gets a **"Trend"** chip; clicking it lazily
  fetches that metric's full series and draws a hand-rolled inline **SVG line chart** — shaded
  reference band, out-of-range points colored by severity, the endpoint value labeled, first/
  last time ticks, and a screen-reader data table (`MetricChart.tsx`; series endpoint
  `agent/copilot/api/routes/observations.py`). Each plotted point stays independently grounded
  (`resource_id` + verbatim value + timestamp).
- **(b)** Jump to **Denise Alvarez (MRN 1015)** in the rail, then click the chat chip **"Show
  the troponin trend"** (or the **"Trend"** chip on the troponin claim in the summary).
- **(c)** *"When a number has a history, one click plots it — grounded point by point, with
  the out-of-range readings flagged."*
- **(d)** **Denise Alvarez (1015)** — NSTEMI, troponin **2.34** ng/mL, the seed's rising
  troponin series. (In the offline mock the troponin trend lives on Ernest Vaughn 1001.)

### 3.7 Metric indicators: value-movement arrow + satisfaction-scaled name

- **(a)** Every metric row carries two grounded indicators, both **read from the record**,
  never invented:
  - **Metric-name color — a green→amber→red satisfaction scale** from the record's own
    abnormal flag: `normal` (in range) → **green**, `warning` (H/L/high/low) → **amber**,
    `critical` (HH/LL/vhigh/vlow/critical_*) → **red**. Non-observation claims (meds,
    conditions) stay default ink.
  - **Value-movement arrow** after the value — drawn from the structured `value_direction`
    (not parsed from text): **↑** if the value rose vs the prior reading, **↓** if it fell,
    **—** if there's no prior or no change. Its **color** comes from the range-relative
    `trend_direction`: **green when moving toward the reference range** (`improving`), **red
    when moving away** (`worsening`), neutral for steady / no range / no prior (and always
    for `—`). Color always ships **paired with the glyph**, so it is never the sole signal.
    Logic in `summary.py` (`_classify_severity`, `_classify_trend`) and `ClaimList.tsx`.
  - **Acuity meter band** — ≥7.5 critical, ≥4 guarded, <4 routine (`fmt.ts`); unchanged.
- **(b)** Point at a critical lab (red name) vs an in-range one (green name), and at a
  worsening ↑ (red) vs a value moving back toward range (green ↓ or ↑) — say what each means.
- **(c)** *"The name tells you where the value sits — green in range, amber caution, red
  critical. The arrow tells you which way it's moving — green toward the safe range, red
  away from it. Both come straight from the record's own flags and prior readings."*
- **(d)** **Marcus Webb (1003)** (critical DKA labs — red names, worsening arrows) or
  **June Okafor (1004)** after the alert (critical lactate).

### 3.8 The deterioration alert + rerank (physician-in-control)

- **(a)** A not-yet-seen patient whose risk spikes mid-round is surfaced as a **non-modal red
  "Deterioration" banner** offering a jump — the physician decides. Accepting reorders the
  queue: the current patient stays pinned at top, the alerting patient is hoisted just below,
  and seen patients sink to the bottom (`App.tsx` `displayOrder`). *Demo mechanism:* the real
  detector is a change-gated background poller that's impractical to drive live, so the
  **"Re-check charts"** top-bar button deterministically raises the alert the poller would
  otherwise surface (client-side; `App.tsx` `DEMO_ALERT`).
- **(b)** Click **"Re-check charts"** (top bar). A status line reads "N charts re-checked · 1
  flagged · HH:MM" and the red banner appears. Click **"Jump to June"** to accept (or **"Stay
  with current patient"** to decline) — watch June rise in the rail with an `Alert` chip.
- **(c)** *"It surfaces a patient you haven't seen yet who's crossing a critical threshold —
  and offers a jump. You decide. The list re-ranks by acuity, not by room number."*
- **(d)** **June Okafor (MRN 1004)** — "Sepsis watch." The alert: *"New lactate 4.2 mmol/L —
  critical high (reference 0.5–2.0). Concern for septic shock — acuity now 9.3."* Her live card
  actually carries that critical lactate, so jumping lands on a card that corroborates the
  banner. The round opens on Marcus (1003), so June is always unseen when the alert fires.

### 3.9 Physician write-back (propose → confirm, append-only) — BUILT, NOT SHOWN in the UI

- **(a)** A propose→confirm write-back path is fully built. Numeric **writable-vital** claims
  (heart rate, SpO₂, systolic/diastolic BP, respiratory rate, temperature, weight, height —
  `labels.ts` `WRITABLE_METRICS`) *can* carry an **"Edit"** affordance whose flow is a two-step
  gate: **Review change** proposes the edit and the server returns a **structured echo-back**
  (unit locked, never agent prose), then **Confirm & save** commits it as a **NEW dated
  record** — append-only, prior values untouched (`EditRecordDialog.tsx`; routes
  `agent/copilot/api/routes/writes.py`; gate `verification/writes.py`). Labs are never editable.
- **(b)** Nothing to click on camera — the affordance is **not present**.
- **(c)** *"The chart is read-only in this build. A propose-then-confirm, locked-unit,
  append-only write-back is built and tested, but it's held back for the roadmap — the demo
  only reads."*
- **(d)** **The direct-edit "Edit" affordance is removed from the UI by product decision** —
  neither displayed nor functional, in **either** the live app or the offline mock: `App.tsx`
  simply does not thread the `proposeWrite`/`confirmWrite` callbacks into `ClaimList`, so its
  `canEdit` stays false and no chip ever renders. The propose→confirm code and
  `EditRecordDialog` are **retained intact for the roadmap** (re-enabling is a one-line
  change). **Backend write-back also remains OFF** (`writeback_enabled` defaults to `False`);
  a write that reached the server would return an honest **"Record write-back is disabled on
  this deployment. Nothing was written."** So the story is simply: this build reads, it does
  not write.

### 3.10 Observability + audit (for the buyer)

- **(a)** Every chat answer shows a **`corr <id>`** footer — the request's correlation id,
  also returned as the `X-Correlation-ID` response header (`api/middleware.py`). Every PHI read
  and every write appends a row to an **append-only audit trail** (no UPDATE/DELETE path
  exists; `memory/repository.py`), and when Langfuse keys are set each request is a **trace**
  keyed by that same correlation id.
- **(b)** After running a chat turn, point at the `corr …` line. (Optional, off-camera: open
  the Langfuse project and find the matching trace by that id.)
- **(c)** *"Every request carries a correlation id, every read and write is trailed
  append-only, and each is traceable end-to-end for audit."*
- **(d)** Any chat turn. Langfuse is a bonus deliverable (`OBSERVABILITY.md`); if creds aren't
  set it's a silent no-op — don't promise a trace you can't show.

---

## 4. Suggested recording flow (~3–5 min)

A coherent storyline that strings the features together. Narrate the italic one-liners above.

1. **Sign in for real** — load **`https://agentforge.hankholcomb.com`**; you hit the
   AgentForge-styled **OpenEMR SMART sign-in** (no basic-auth). Sign in with the OpenEMR admin
   credentials and approve consent. Call it out: *"This is a real delegated SMART-on-FHIR login
   to a genuine EHR — I'm authenticated as this clinician, and every read runs under my own
   token."* *(§2)*
2. **Open on the ranked queue** — the app loads; call out the loading line ("Reading 15
   charts — sickest first") and that it lands on **Marcus Webb, DKA, acuity ~9.0**, not a
   dashboard. *(§3.1)*
3. **Read the grounded card** — walk the acuity reason, the one-row-per-metric summary, and
   "since you last saw her." *(§3.2)*
4. **Prove the provenance** — click a **✓** chip to reveal the exact recorded value +
   timestamp; say it's re-verified live. *(§3.3)*
5. **Ask a real question, then an impossible one** — **"Latest glucose?"** → *served* with a
   citation; **"Any MRI report?"** → *withheld*. This is the trust money-shot. *(§3.4)* Add a
   temporal ask ("meds in the last 24 hours") if on the live agent. *(§3.5)*
6. **Show a trend + the indicator language** — jump to **Denise Alvarez (1015)**, open the
   **troponin trend**, and explain the metric name's green→amber→red scale and the movement
   arrow (green toward range, red away). *(§3.6, §3.7)*
7. **Trigger the deterioration rerank** — click **"Re-check charts,"** the **June Okafor**
   banner appears (lactate 4.2, acuity 9.3); **Jump to June**, watch the queue re-rank; land on
   her corroborating card. *(§3.8)*
8. **(Optional) Read-only stance** — note there's **no Edit affordance anywhere**: a full
   propose→confirm, append-only write-back is built and tested but held back for the roadmap,
   and backend write-back is off. This build reads, it does not write. *(§3.9)*
9. **Close on the promise** — "grounded, access-controlled, honest about uncertainty — it never
   guesses," and point at the `corr` id for auditability. *(§3.10)*

> **Offline mock differences (if you film localhost instead of live).** The mock is a
> **5-patient cohort** and **opens on Ernest Vaughn (MRN 1001, rising troponin)**, not Marcus
> Webb. Its **native** deteriorating patient is **Lillian Cho (1005, sepsis/lactate)**, surfaced
> after ~12 s or on "Re-check charts." Note the top bar shows a **"Demo data"** tag in mock mode.
> The starter questions and trend series map to the 5 mock patients (troponin on 1001, lactate/
> heart-rate on 1005). The mock has **no SMART sign-in** — it opens straight on the cohort — and
> the **Edit affordance is absent here too** (removed app-wide; the propose→confirm write-back
> code still exists but nothing renders it). Narrate the mock's names if you record there.

---

## 5. Production-grade architecture (built — now partly live)

Real, tested hardening code. On the reference droplet (`agentforge.hankholcomb.com`)
**per-physician SMART login, delegated tokens, token-at-rest encryption, and HTTPS are now
LIVE**; write-back and self-hosted Langfuse remain opt-in. A fresh deploy still defaults to the
simpler off state. Full HIPAA §164.312 mapping in `agent/COMPLIANCE.md`; design in
`agent/research/PRODUCTION_GRADE_PLAN.md`. You can speak to all of this honestly:

- **Per-physician SMART login** — an OpenEMR-delegated `authorization_code` + PKCE flow, an
  opaque httpOnly server-side session, `fhirUser → ClinicianId` auto-provisioning, and
  idle/absolute session timeouts (automatic logoff). **LIVE on the reference droplet**
  (`COPILOT_AUTH_MODE=smart`); a fresh deploy defaults to `disabled`. In `smart` mode **every
  data route takes identity from the session** (401 no session / 403 on a mismatched
  `clinician_id`) — the request can no longer assert who it is.
- **Delegated per-physician tokens** — in `smart` mode interactive reads/writes call OpenEMR
  under the **logged-in physician's own** SMART token, so **OpenEMR's native audit** attributes
  each action to that individual (least-privilege) — **active on the droplet now**. The
  background poller keeps a scoped system (Backend Services) token by design.
- **Token-at-rest encryption** — physician OAuth tokens are Fernet-encrypted in the session
  store (`SessionCrypto`); the browser holds only an opaque hashed cookie (a
  Backend-for-Frontend "token never touches the browser" property). **Active on the droplet**
  now that `auth_mode=smart`.
- **Append-only audit + 6-yr retention** — the audit trail has **no UPDATE/DELETE path**; the
  retention sweep is report-only with a hard 6-year floor and **no delete statement against
  `audit_log` anywhere in the codebase**. Correlation ids tie every row to its request.
- **Self-hostable Langfuse** — tracing is a no-op without keys; the deploy compose can bring up
  a **self-hosted** Langfuse (SSH-tunnel access) to keep PHI-adjacent trace metadata on the
  org's own infrastructure. Opt-in.
- **HTTPS** — automatic Let's Encrypt TLS is **live** on the reference deploy
  (`agentforge.hankholcomb.com`, `Caddyfile.https.example`), with SMART login as the access
  gate and **no basic-auth guard**. A fresh deploy can still opt into plain HTTP on a bare IP.
- **Honest boundary (`COMPLIANCE.md`)** — AgentForge **implements technical safeguards**;
  it does **not** by itself make a deployment HIPAA-compliant. The BAAs (Anthropic + ZDR,
  hosting), administrative and physical safeguards, and operational hardening are the deploying
  **organization's** responsibility. Say this out loud — it reads as maturity, not weakness.

---

## 6. Talking points / FAQ

**Why read through OpenEMR's API instead of forking logic into it?**
The co-pilot is a **separate Python service** (`agent/`) that touches PHI **only** through
OpenEMR's FHIR/REST API over OAuth. OpenEMR stays the system of record; its access controls and
audit trail keep applying; and no read path bypasses its authorization. That's also what makes
per-physician delegated tokens (native OpenEMR attribution) possible.

**How does grounding/verification actually stop hallucination?**
Two layers. First, **claims are constructed from the fetched resource** — the LLM picks which
resources are relevant, but deterministic code fills the exact `(field, value)` pair, so a claim
is built to survive the gate. Second, a **non-promptable deterministic gate** re-checks every
claim against a live re-fetch (attribution + verbatim value + numeric-literal presence +
temporal grounding) and is **fail-closed**: no proof → withheld, partial → degraded. A
fabrication fails attribution or value-match every time.

**What's synthetic vs. real?**
The **clinical data is synthetic** (a generated cohort; the shipped OpenEMR demo data was
demographics-only, so there'd be nothing to reason over — this is documented in the audit). The
**agent, verification, ranking, chat, and the OpenEMR FHIR path are real**. The display roster
(names/beds in `census.ts`) is UI identity mapped onto real seeded patient ids. **No real PHI,
ever.**

**What does "production-grade" mean here — is any of it vaporware?**
It's **built and unit-tested code** (see §5), not slideware — 285 backend tests, a green E2E
acceptance suite, an eval dataset, a cost analysis, and a HIPAA technical-safeguard mapping. On
the reference droplet the operator-enablement steps are **already done**: SMART login is on
(`COPILOT_AUTH_MODE=smart`), a confidential SMART client is registered, and HTTPS is live — so
the delegated-auth story is real, not hypothetical. What genuinely remains off is **write-back**
(built, not shown); self-hosted Langfuse is opt-in.

**Why an agent and not just a sorted list?**
A static view can't do acuity ranking **plus** grounded narrative synthesis **plus**
serve-time verification **plus** a paced, stateful, physician-in-control hand-off (open →
summarize → answer → surface a deterioration → advance). Those are agent behaviors.

**Is the doctor ever out of the loop?**
No. The deterioration alert is a **non-modal offer** ("Jump" or "Stay") — never a forced
navigation — and write-back is **propose → confirm** against a locked echo-back, append-only,
and **off in this build** (the Edit affordance isn't even shown — the chart is read-only).
