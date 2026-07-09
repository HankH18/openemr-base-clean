# Demo Script — AgentForge Clinical Co-Pilot (3–5 min)

**You (the human) record and narrate this.** Everything below is a turnkey shot list and
suggested narration. The Rounds Co-Pilot UI runs standalone on the seeded demo cohort — no
backend or API key required — so the demo is fully reproducible.

## Pre-flight (before recording)
- `cd agent/web && npm install && npm run dev` → open the printed localhost URL (Chrome, ~1440px wide).
- Optional split-screen: the deployed OpenEMR fork (login admin/pass) to show the system of record.
- The UI opens on **Ernest Vaughn (MRN 1001)** — acuity **9.1**, critical troponin. Have a second
  tab in dark mode ready if you want to show the theme (top-bar toggle).
- **Demo data only. No real PHI.**

## Shot list

**0:00–0:30 — The problem (talking head or over the OpenEMR chart).**
> "A hospitalist rounds on ~12 patients before noon. Every morning they reconstruct what changed
> overnight, chart by chart, under a fixed clock. The scarce resource is pre-rounds time — and the
> real risk is sequencing: the patient who deteriorated overnight can sit unexamined in the middle
> of an alphabetical queue."

**0:30–1:15 — The audit headline (why we didn't build first).**
> "Before writing a line of agent code, we audited the fork. The single most consequential finding
> was a data-quality one: the shipped demo data is **demographics-only** — 14 patients, zero
> encounters, labs, or meds. A 'what changed overnight' agent would have had nothing to reason over.
> So Stage 0 was generating a realistic synthetic clinical cohort — which doubles as our eval ground
> truth." *(Show AUDIT.md summary; optionally the seeded labs via the FHIR API.)*

**1:15–2:15 — The Co-Pilot opens on the sickest patient first (UC-1).**
- Show the rounds view: Ernest Vaughn, **acuity 9.1**, "Ranked here: critical troponin."
> "It doesn't hand you a dashboard. It opens on your **most acute** patient, with a grounded summary
> and — first — *what changed since you last saw him*: troponin rose 0.4 → **0.9** overnight, heparin
> started per ACS protocol. Every line cites its source record." *(Point at the `OBSERVATION trop-1001`
> provenance chips on the right of each claim.)*

**2:15–3:15 — Grounded drill-down + the trust story (UC-2, UC-7).**
- In "Ask the chart," click **"Latest troponin?"** → a green **VERIFIED — SERVED** answer with the
  cited value.
- Then ask **"What did the brain MRI show?"** → a **WITHHELD — NO SOURCE FOUND** refusal (dashed red,
  honest language).
> "Ask a follow-up and every claim is checked against the live record at serve time. Ask about
> something not in the chart — an MRI we never did — and it **refuses**: 'I can't confirm that… rather
> than guess, I'm withholding an answer.' In a clinical setting a confident hallucination can harm a
> patient, so the system is built to withhold, not guess."

**3:15–4:15 — Proactive deterioration alert (UC-5) + hand-off (UC-3).**
- Wait for / show the census rail flipping **Lillian Cho** to a red **ALERT** (a not-yet-seen patient
  crossing the critical threshold), and the jump offer.
> "While you round, a background loop re-checks charts. When a patient you *haven't seen yet*
> deteriorates, it interrupts to **offer** a jump — you decide. Hit **Done** and it advances to the
> next patient by acuity, not by room number."

**4:15–5:00 — Why an agent, and the trust boundary (for the buyer).**
> "Why an agent and not a sorted list? Ranking + narrative synthesis + grounding + a paced, stateful
> hand-off are agent behaviors a static view can't do. And it's built for the buyer's bar — 'could a
> physician rely on this?': reads happen through OpenEMR's own OAuth (physician-delegated for chat, a
> scoped system actor for the poller), a **deterministic** verification gate the LLM can't talk past,
> and an append-only audit trail. Grounded, access-controlled, and honest about uncertainty."

## Notes for the recorder
- Recording + narration is the human step; this file is the plan.
- If showing the live API instead of the mock: set `VITE_API_BASE_URL` to the running agent service
  (see `agent/web/README.md`) and start the agent with `ANTHROPIC_API_KEY` set.
- Keep total runtime 3–5 min; the four beats to land are: **audit-first**, **sickest-first + grounded**,
  **withholds rather than guesses**, **proactive + physician-in-control**.
