# Clinical Co-Pilot — Eval Results

Deterministic eval run (no ANTHROPIC key required — stub-agent path). Captured at `2026-07-12T00:00:00Z`.

**Score: 11/11 passed (100.0% accuracy).**

## Methodology

These grounding evals run `eval_dataset.jsonl` (11 cases) against an in-process instance of the FastAPI app over the *same* black-box HTTP contract the acceptance suite uses, backed by a deterministic fake OpenEMR FHIR server (`evals/_fake_openemr.py`) and a temp-file SQLite DB. With no Anthropic key configured the app takes its deterministic **stub-agent** path, so every case has exactly one correct outcome — no LLM, no network, no flakiness. Each case asserts a served/withheld/refused decision, claim citations, temporal grounding, cross-patient isolation, or sickest-first ranking.

Re-run (regenerates this file + `eval_results.json`, no key needed):

```
./.venv/bin/python evals/run_evals.py
```

## Models (keyed / production path — NOT exercised here)

This deterministic run uses no model. In production and on the keyed eval path the agent uses **claude-sonnet-5** for synthesis + chat and **claude-haiku-4-5-20251001** for classification / entailment. An additional LLM-judge *entailment* layer lives in `evals/test_grounding_evals.py` (marked `@pytest.mark.llm`); it is skipped unless `ANTHROPIC_API_KEY` is set and is **not** reflected in the numbers above. Those LLM-graded cases need a fresh keyed run to (re)generate — this deterministic report never fabricates them.

## By category

| Category | Passed | Total |
| --- | --- | --- |
| authorization | 3 | 3 |
| boundary | 3 | 3 |
| invariant | 5 | 5 |

## Per-case outcomes

| Case | Category | Result | Detail |
| --- | --- | --- | --- |
| `invariant-grounded-troponin-served` | invariant | PASS | status=200, action=served, claims=1 |
| `invariant-grounded-summary-served` | invariant | PASS | status=200, action=served, claims=2 |
| `boundary-ungroundable-mri-withheld` | boundary | PASS | status=200, action=withheld, claims=0 |
| `boundary-ungroundable-genetics-withheld` | boundary | PASS | status=200, action=withheld, claims=0 |
| `boundary-record-drift-withheld` | boundary | PASS | status=200, action=withheld, claims=0 |
| `authorization-unlisted-patient-refused` | authorization | PASS | status=403 |
| `authorization-no-session-refused` | authorization | PASS | status=403 |
| `authorization-no-cross-patient-leak` | authorization | PASS | status=200, action=withheld, claims=0 |
| `invariant-rounds-ranks-sickest-first` | invariant | PASS | status=200, top_patient_id=1001 |
| `invariant-rounds-critical-lactate-first` | invariant | PASS | status=200, top_patient_id=1005 |
| `invariant-temporal-authoredon-grounded-served` | invariant | PASS | status=200, action=served, claims=1 |

## What each behavior proves

- **served** — a grounded question about present data returns cited claims.
- **withheld** — an ungroundable question, and a value that drifted vs the live record, both fail closed rather than guess.
- **refused (403)** — chat about a patient outside the clinician's rounding list, and a clinician with no session, are denied.
- **no-leak / ranking** — cross-patient isolation and sickest-first ranking.
- **temporal grounding** — a claim carrying an `authoredOn` / `effectiveDateTime` timestamp is re-checked against the live re-fetch; an equal instant serves, a drift withholds.
