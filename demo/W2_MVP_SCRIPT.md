# Week-2 MVP demo script (3–5 min)

Covers exactly what the rubric asks the video to show: **document upload → extraction →
evidence retrieval → citations → eval results → observability.** Keep it to ~4 minutes.

## Before you hit record
- **Where to film:** the **live deployed app** is best for authenticity —
  **https://agentforge.hankholcomb.com** (chat graph **on**). It opens on the OpenEMR
  **SMART sign-in**; log in with the OpenEMR admin creds (`OE_USER`/`OE_PASS` from the
  droplet `.env` — see `ACCESS.md`), approve consent.
  - **Deterministic fallback:** the **offline mock UI** (`cd agent/web && npm run dev` →
    http://localhost:5173) reproduces the same Week-2 upload/extraction/citation flow with
    no login and fixed fixtures — smoother to film, zero network variance. Use this if the
    live login or live model latency gets in the way.
- Have two sample files ready: a **lab PDF** and an **intake form** (fixtures live under
  `agent/copilot/documents/` / `agent/evals/fixtures/`).
- Open a second tab on **Langfuse** (cloud.langfuse.com → *AgentForge-Gauntlet*) for the
  observability beat, and a terminal for the eval-gate beat.

## Shot list

**0:00–0:30 — Frame the problem.** "A hospitalist is prepping a follow-up. The chart has
structured OpenEMR data, but the recent signal is buried in a scanned lab PDF and a
front-desk intake form. Week 2 makes the agent *see* those, stay grounded, and prove it
with an eval gate." Show the patient open on the most-acute patient (Week-1 baseline in one
sentence — don't dwell).

**0:30–1:30 — Ingest two document types (Stage 1).** From the patient hero, **upload the lab
PDF**. Show the extraction result: strict-schema fields (test name, value, unit, reference
range, collection date, abnormal flag) each with a **citation**. Call out: "a value the
model can't locate on the page is flagged `supported=false`, never invented." Repeat briefly
with the **intake form** (chief concern, meds, allergies). Emphasize: *the schema is the
source of truth — raw vision output never bypasses validation.*

**1:30–2:00 — Click-to-source citation (Citation contract).** Click a provenance chip on a
document-derived claim → it opens the **scanned page with the bounding box drawn** over the
exact region the value came from. This is the "see exactly where it came from" moment.

**2:00–2:45 — Evidence retrieval + grounded answer (Stages 2–3).** In chat, ask a question
that needs guideline backing (e.g. *"Given this lactate and MAP, what does our sepsis
guideline recommend?"*). Show the answer separating **patient-record facts** (cited to the
chart) from **guideline evidence** (a labeled block, cited to the corpus). Mention the
**supervisor → intake-extractor / evidence-retriever → critic** graph routed this, with
hybrid RAG (sparse+dense → RRF → rerank) behind it.

**2:45–3:30 — Eval HARD GATE (Stage 4).** Cut to the terminal:
```bash
cd agent && python evals/gate.py                    # 62 cases (53 fixture + 9 live), 5 boolean rubrics → pass_rate 100, EXIT 0
python evals/gate.py --inject-regression            # pass_rate drops → BLOCKED, EXIT 1
```
Say: "62-case golden set (53 fixture + 9 live), boolean rubrics — `schema_valid`, `citation_present`,
`factually_consistent`, `safe_refusal`, `no_phi_in_logs`. It blocks a >5% regression, and
it's wired PR-blocking in **GitLab CI** (`agent:tests`). When a grader plants a regression,
this is what fails their build."

> **Say it accurately.** CI is the enforcing gate — say "CI", not "the pre-push hook". The
> committed hook (`.githooks/pre-push`) is *available* but **inert until a dev opts in** with
> `git config core.hooksPath .githooks` (unset by default). Claiming it blocks pushes today
> is an overclaim a grader can check in one command. Case count is **62** (53 fixture — 13
> `gate_dataset.jsonl` + 40 `golden_dataset.jsonl` — plus 9 live) — matches the terminal output on screen.

**3:30–4:00 — Observability (Requirement 7).** Switch to Langfuse: open the trace for the
chat turn you just ran, keyed by the **correlation ID** (also returned as the
`X-Correlation-ID` response header). Show the **nested spans** — supervisor span with the
worker invocations as children, retrieval + extraction sub-calls, latency per step, token
usage / cost. Close: "full multi-agent trace reconstructable from one correlation ID, and
**no raw PHI in logs** — verified by the `no_phi_in_logs` rubric in the same gate."

## After recording
- Upload (Loom/YouTube unlisted), then **update the video link** in `README.md`
  (deliverables table) and `demo/VIDEO.md` to the new Week-2 recording — the link there now
  points at the earlier iteration's cut.
