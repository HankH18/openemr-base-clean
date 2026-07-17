# Clinical Co-Pilot — Users & Use Cases

> This document is the source of truth for `ARCHITECTURE.md`. Every agent capability
> specified there traces back to a use case defined here.

---

## Customer & problem summary (~500 words)

**Who we're building for.** The user is a **hospitalist**: an inpatient physician who
rounds on roughly a dozen admitted patients before noon. Each morning they inherit a
patient list from the overnight team and must reconstruct, patient by patient, what
happened overnight — new labs, vitals trends, imaging and consult results, medication
changes, and events like a rapid response or a fall — before they walk into the room.
The *buyer* is different from the user: it is the health system's CMIO or CTO, who
decides whether a tool is safe and trustworthy enough to put in front of clinicians.
The user chooses the tool day to day; the buyer's bar — "could a physician safely rely
on this?" — is the standard everything is built to.

**The problem.** The hospitalist's scarce resource is *pre-rounds time*. Assembling the
overnight picture means opening each chart in turn and scanning dense notes, lab flowsheets,
and med lists under a fixed clock, because rounds start at a set time. The cost of the
status quo is threefold: time lost to chart archaeology instead of clinical reasoning;
**sequencing risk** — a patient who deteriorated overnight may sit unexamined in the middle
of a chart-review queue while the physician works alphabetically or by room; and cognitive
load that compounds across twelve patients. This is worth solving now because the raw
material (structured EHR data) already exists — the gap is synthesis and prioritization,
not data capture.

**How this solves it.** The Clinical Co-Pilot is a conversational agent embedded in
OpenEMR that maintains a continuously-updated, provenance-backed **memory file** for each
patient on the list. It does not present a dashboard. At the start of the day it opens on
the physician's **most important patient**, gives a grounded chart summary and the changes
since they last saw that patient, and then lets them **drill down by asking follow-up
questions**. When the physician says they are done, the agent advances to the next patient
by acuity. A background process re-processes any chart that changes every 5–15 minutes, so
if a not-yet-seen patient deteriorates mid-rounds, the agent **proactively interrupts** to
offer jumping to them — the physician decides. Every claim is traceable to a specific record;
unverifiable claims are withheld, not guessed.

**Why it matters.** In a clinical setting, a confidently stated hallucination doesn't just
erode trust — it can harm a patient. The value of this system is not that it can chat about
a patient; it is that it changes *what the physician walks into the room knowing*, and does
so on a foundation the buyer can trust: grounded, access-controlled, observable, and honest
about uncertainty. When it works, the hospitalist rounds sickest-first with the overnight
story already in hand, and spends reclaimed minutes on the patient instead of the chart.

---

## Target user (narrow)

**A hospitalist rounding on ~12 admitted patients before noon.**

- **Panel:** ~12 inpatients on a defined rounding list for the day.
- **Clock:** hard — rounds begin at a fixed time; pre-rounds prep is ~30–60 minutes.
- **Environment:** multi-user hospital. The hospitalist sees *their* patients; nurses and
  supervised residents have different access. Authorization is real, not cosmetic.
- **Tolerance for agent behavior:** low tolerance for confident wrong answers; high value on
  prioritization and "what changed." Prefers a verified refusal over a plausible guess.

Explicitly *not* building for: the primary-care 90-second-between-rooms case, the ED
overnight-intake case, nurses, or patient-facing use. Those are different workflows with
different data and latency needs; mixing them dilutes the design.

---

## The workflow (where the agent enters the day)

1. **Before (7:00–8:00 AM):** the hospitalist opens their list, reads overnight sign-out,
   and would otherwise open each chart one by one to assemble the overnight picture.
2. **Agent entry:** instead, they open the Co-Pilot. It presents the **highest-acuity /
   most-changed patient first**, with a grounded summary + "since you last saw them" updates.
3. **Interaction:** they ask follow-ups on that patient (drill-down), then signal **"done."**
4. **Advance:** the agent presents the next patient by ranking, and repeats.
5. **Interrupt (as needed):** if the background loop detects a not-yet-seen patient
   deteriorating during rounds, the agent **offers** to jump to them; the physician decides.
6. **After:** the physician rounds sickest-first, already knowing the overnight story, and
   spends reclaimed time on reasoning and the patient.

The doctor's **"done"** signal is also the *last-seen* marker: "updates since you last saw
them" means changes since they last completed that patient.

---

## Use cases (each with *why an agent*)

| # | Use case | What the agent does | Why an agent (not a dashboard / sorted list) |
|---|----------|---------------------|----------------------------------------------|
| **UC-1** | **Start-of-day top patient** | Opens on the highest-acuity/most-changed patient with a grounded chart summary + overnight/since-last-seen changes. | A sorted list shows *fields*; it can't synthesize "creatinine 1.1→1.8 overnight, new IV fluids, watch for AKI — see her first." Ranking + narrative synthesis + grounding is the agent's job. |
| **UC-2** | **Conversational drill-down** | Multi-turn, tool-chained Q&A on one patient ("why is she on vanc?", "show the trend behind that"), every claim source-attributed. | Follow-up questions with maintained context and tool chaining over meds/labs/notes is inherently conversational; a static view can't answer the *next* question. |
| **UC-3** | **Guided patient-to-patient hand-off** | On "done," advances to the next patient by ranking and re-presents. Maintains a rounding cursor. | "Walk me through my patients, wait while I round, hand me the next when I'm ready" is a stateful, paced interaction no dashboard replicates. |
| **UC-4** | **Interrogate the ranking** | Explains *why* a patient is ranked where they are and shows the underlying evidence. | Makes the prioritization itself questionable and auditable — a ranked list can't defend its own order. |
| **UC-5** | **Proactive deterioration alert (soft preempt)** | When a not-yet-seen patient's chart changes materially mid-rounds, interrupts between patients to offer a jump; physician decides. | Proactive, human-in-the-loop escalation from continuous monitoring is an agent behavior; a static view requires the doctor to notice. |
| **UC-6** | **Authorization boundary** | Refuses queries about patients not on this physician's authorized list. | The correct behavior is a *refusal with a reason* — a security-relevant conversational response, and an explicit eval target. |
| **UC-7** | **Graceful uncertainty** | When it can't verify enough to answer, says so and surfaces the raw source to check, rather than guessing. | Honest, contextual uncertainty communication is conversational; the alternative (a confident wrong field) is the exact failure the project exists to prevent. |

Each capability specified in `ARCHITECTURE.md` maps to one or more of UC-1…UC-7. Capabilities
that do not map to a use case above are out of scope for v1.
