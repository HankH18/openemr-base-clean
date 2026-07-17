# Demo Script — AgentForge Clinical Co-Pilot (3–5 min)

> **Superseded — [`DEMO_GUIDE.md`](DEMO_GUIDE.md) is the current recording companion.**
> It has the feature-by-feature walkthrough grounded in the code, with the exact on-screen
> specifics (opening patient, provenance chips, trend chart, the "Re-check charts"
> deterioration trigger, write-back gate) and which beats are live vs. mock. This file is
> kept for the high-level narration arc only; where the two disagree, DEMO_GUIDE.md wins.

**You (the human) record and narrate this.** Film the **live** deployment (real Claude,
real OpenEMR OAuth + FHIR) or the fully-offline mock fallback noted at the end.

## Access (before recording)
- Open **https://agentforge.hankholcomb.com** in Chrome (~1440px wide). Access is
  **per-physician SMART login** — sign in with an OpenEMR physician account (demo credentials
  handed off separately; see [`../ACCESS.md`](../ACCESS.md)).
- The UI opens on the sickest patient (**Marcus Webb — MRN 1003, Bed 03-A**, acuity ~9.0,
  ranked for critical DKA labs) and advances by acuity, not room number.
- Dark-mode toggle is top-right if you want to show the theme.
- **Synthetic demo data only. No real PHI.** Give the first load ~30–60s (it synthesizes
  the cohort live via Claude on first `rounds/start`).

## Shot list

**0:00–0:30 — The problem (talking head or over the OpenEMR chart).**
> "A hospitalist rounds on ~12 patients before noon. Every morning they reconstruct what
> changed overnight, chart by chart, under a fixed clock. The scarce resource is pre-rounds
> time — and the real risk is sequencing: the patient who deteriorated overnight can sit
> unexamined in the middle of an alphabetical queue."

**0:30–1:15 — The audit headline (why we didn't build first).**
> "Before writing a line of agent code, we audited the fork. The most consequential finding
> was a data-quality one: the shipped demo data is demographics-only — patients with zero
> encounters, labs, or meds. A 'what changed overnight' agent would have had nothing to
> reason over. So Stage 0 was generating a realistic synthetic clinical cohort — which
> doubles as our eval ground truth." *(Show AUDIT.md; optionally seeded labs via the API.)*

**1:15–2:15 — The Co-Pilot opens on the sickest patient first (UC-1).**
- Show the rounds view: **Marcus Webb, acuity 9.0**, "Ranked here: Critical — potassium
  5.7 mEq/L, glucose 386 mg/dL, bicarbonate 12 mEq/L."
> "It doesn't hand you a dashboard. It opens on your most acute patient — a DKA picture in
> active treatment — with a grounded chart summary. Every line cites the exact source
> record." *(Point at the ✓ provenance chip beside each claim — clicking one reveals the
> exact source resource, recorded value, and timestamp; chips name the resource type, not a
> raw UUID.)*

**2:15–3:15 — Grounded drill-down + the trust story (UC-2, UC-7).**
- In "Ask the chart," ask **"What are the most concerning labs, and are any critical?"** → a
  green **VERIFIED — SERVED** answer citing glucose 386 (HH), bicarbonate 12 (LL),
  potassium 5.7 — each with a source chip.
- Then ask **"What did the brain MRI show?"** → a **WITHHELD** refusal (honest language).
> "Every claim is re-checked against the live record at serve time. Ask about something not
> in the chart — an MRI we never did — and it refuses: 'I can't confirm that from this
> patient's record,' rather than guess. In a clinical setting a confident hallucination can
> harm a patient, so the system withholds instead of guessing."

**3:15–4:15 — Proactive deterioration + hand-off (UC-5, UC-3).**
- Click **Re-check charts** (top bar) to raise the alert, then point at the **DETERIORATION**
  banner: **June Okafor** — a patient you *haven't seen yet* — flagged for a critically-high
  lactate (4.2). Show the **Jump to June** offer.
> "The co-pilot surfaces a not-yet-seen patient who's crossing a critical threshold and
> offers a jump — you decide. Hit **Done** and it advances by acuity, not by room number."

**4:15–5:00 — Why an agent, the trust boundary, and observability (for the buyer).**
> "Why an agent and not a sorted list? Ranking + narrative synthesis + grounding + a paced,
> stateful hand-off are agent behaviors a static view can't do. And it's built for the
> buyer's bar — 'could a physician rely on this?': reads happen through OpenEMR's own OAuth
> (a scoped system actor), a deterministic verification gate the LLM can't talk past, and
> every request is traced in Langfuse by a correlation ID for audit. Grounded,
> access-controlled, and honest about uncertainty."
- *(Optional: show the Langfuse trace for the chat you just ran — the `chat` span + its
  `verification.result` served/withheld event.)*

## Notes for the recorder
- Recording + narration is the human step; this file is the plan.
- The four beats to land: **audit-first**, **sickest-first + grounded**, **withholds rather
  than guesses**, **proactive + physician-in-control** (+ observability for the buyer).
- Access + credentials for both environments: see `ACCESS.md`.
- **Offline mock fallback** (no backend/API key, fully reproducible): `cd agent/web && npm
  install && npm run dev`, open the printed localhost URL. Note the mock cohort differs — it
  opens on **Ernest Vaughn (MRN 1001, troponin)** and the deteriorating patient is **Lillian
  Cho** — so narrate those names if you film the mock instead of the live site.
