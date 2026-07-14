# BUILD_PLAN_TEMPORAL.md — Temporal Reasoning + Time-Series Drill-Down

AgentForge Clinical Co-Pilot. Phased, file-level implementation plan for three interrelated features that share one data foundation (a grounded timestamp on every source reference). Read-only-verified against the current tree.

## 0. Key findings that shape the plan

**Med timestamps already exist in FHIR; the agent just never surfaces them.**
- FHIR `MedicationRequest` is built **only** from the OpenEMR `prescriptions` table (`src/Services/FHIR/FhirMedicationRequestService.php`). `authoredOn` ← `prescriptions.date_added` (`populateAuthoredOn`, lines 438–443). The `lists` (`type='medication'`) rows do **not** surface as `MedicationRequest`.
- The seed (`scripts/seed/seed.sql`) already sets `date_added` on all 21 prescriptions — but every one is clustered at admission (`@T_MINUS_2D`/`@T_MINUS_3D`). So "what meds in the last 24 h?" honestly returns nothing, and there is no recent-order signal to demo.
- `copilot/agent/grounding.py::describe_resource` extracts only `medicationCodeableConcept.*` for a `MedicationRequest` — the timestamp is dropped before it reaches a claim. The `ClaudeAgent` sees the raw bundle (incl. `authoredOn`) in the tool result, so it *could* mention time in prose, but the emitted `Claim.source_ref` cannot ground it, and the deterministic `StubAgent` cannot filter by time at all.

**Observation effective time mapping (for Feature 3 series + grounding):**
- Lab Observations (`src/Services/FHIR/Observation/FhirObservationLaboratoryService.php`): `effectiveDateTime` ← `report_date` ← `procedure_report.date` (lines 185, 235–238). Effective time is at the **report** level — a per-metric series therefore needs **multiple reports** (distinct `date_report`), not multiple result rows in one report.
- Vitals Observations (`.../FhirObservationVitalsService.php`, lines 638–644): `effectiveDateTime` ← `form_vitals.date`. Each `form_vitals` row = one timepoint; each vital column becomes its own Observation. Vitals therefore **already** carry a 3-point series per metric (admission / +1 d / recent).

**Verification is deterministic and value-keyed.** `copilot/verification/core.py::_verify_claim` checks (a) `source_ref` resolves to a re-fetched resource, (b) `extract_field_value(resource, ref.field) == ref.value`, (c) every numeric literal in `claim.text` appears in the resource's flattened text. `FhirReference.last_updated` exists but is **never verified** and never populated today — precedent for a non-gating temporal field, but the task wants the new timestamp grounded, so we will verify it via a shared extractor.

**Route auto-mount:** any module under `copilot/api/routes/` exposing a module-level `router` is mounted automatically (`app.py::register_routers`) — a new endpoint needs no `app.py` edit.

**Manual claim (de)serialization is the sharp edge.** `copilot/memory/repository.py::_claim_to_json` / `_claim_from_json` serialize `source_ref` field-by-field (they do **not** use `model_dump`). A new `FhirReference` field silently drops on the rounds-card DB round-trip unless these two functions are updated.

---

## Phase 0 — Shared foundation: a grounded timestamp on every source_ref

This unblocks Features 1 and 2 and is reused by 3. Land it first, alone.

**`copilot/domain/primitives.py`** — add to `FhirReference`:
```python
timestamp: datetime | None = Field(
    default=None,
    description="Clinically meaningful time of the cited resource — authoredOn "
    "(MedicationRequest) or effectiveDateTime (Observation). Grounded, re-checked "
    "on live re-fetch; NOT part of the value-match gate.",
)
```
Keep `value` semantics exactly as-is. `last_updated` stays (record-mutation time); `timestamp` is the clinical time the physician cares about — deliberately distinct.

**`copilot/agent/grounding.py`** — add a deterministic shared helper (mirrors the role of `extract_field_value`):
```python
def extract_temporal(resource: Mapping[str, Any]) -> str | None:
    """authoredOn for MedicationRequest; effectiveDateTime → issued for Observation."""
```
Reads via `extract_field_value` so it agrees byte-for-byte with a live re-fetch. Have `describe_resource` (or a thin sibling `describe_with_time`) also return the temporal string so callers can populate `source_ref.timestamp` from the *same* fetched resource.

**`copilot/verification/core.py::_verify_claim`** — after the value/numeric checks, add: if `ref.timestamp is not None`, re-derive the temporal value from the re-fetched resource with the **same** `extract_temporal` and require ISO-equality; on mismatch return `value_match=False` with a temporal reason (fail-closed). Critical rule: `ref.timestamp is None` ⇒ skip the check entirely, so every existing timestamp-less claim is untouched.

**`copilot/memory/repository.py`** — extend `_claim_to_json` (write `timestamp` as `.isoformat()` or `None`) and `_claim_from_json` (parse with `datetime.fromisoformat` when present; default `None` for older rows). Backward-compatible.

Risk: if grounding populates `timestamp` but verification derives it differently, claims get withheld. Mitigation: one shared `extract_temporal`, used by both sides; `None` short-circuits.

Tests to update: `tests/test_verification_core.py` (add a timestamp-match + timestamp-drift case), `tests/test_repository.py` (round-trip a claim carrying `timestamp`).

---

## Phase 1 — Feature 1: medication timestamps for temporal reasoning

Depends on Phase 0.

**`copilot/agent/grounding.py`** — in the `MedicationRequest`/Observation claim-build path, set `source_ref.timestamp = extract_temporal(resource)`. `claim_text` stays value-only (do **not** inject dates into text — that would force year/month/day numeric literals through the numeric gate).

**`copilot/agent/stub.py`** and **`copilot/agent/claude.py`** — when constructing `FhirReference(...)`, pass `timestamp=...` from the same fetched resource (both already have the resource in hand: stub in its loop, claude via `describe_resource` in `_build_answer`).

**`copilot/agent/claude.py::_SYSTEM_PROMPT`** — one sentence: `MedicationRequest.authoredOn` and `Observation.effectiveDateTime` are present in tool results and may be used to answer time-relative questions; the system still fills `source_ref` (including the timestamp) from the cited resource, so grounding holds. (No change to the JSON claim shape the model emits.)

**`copilot/rounds/summary.py`** — `_observation_claim` and the non-observation claim builder set `source_ref.timestamp`. `build_change_claims` already reasons over `_effective`; no logic change, just the extra grounded field on emitted claims.

**Seed (`scripts/seed/seed.sql`)** — add a small number of **recently-authored** prescriptions (`date_added = @T_MINUS_2H` / `@T_MINUS_18H`) so "last 24 h" returns real rows — e.g. pt 1015 heparin/nitro started `@T_MINUS_2H`; pt 1004 a pressor titration; pt 1003 an insulin adjustment. New `id`s continue the `50xx` range with matching `cc…` UUIDs; the existing `uuid_registry` backfill SELECT and the `external_id='SEED'` DELETE already cover them (re-runnable, no cleanup edit).

Verification impact: med claims now carry `timestamp = authoredOn`; the re-fetched prescription re-derives the same `authoredOn`, so served/withheld behavior is unchanged for honest data and correctly withholds on temporal drift.

---

## Phase 2 — Feature 2: show the record's timestamp in the drill-down

Depends on Phase 0's contract; parallelizable with Phase 1.

**`agent/web/src/api/types.ts`** — `SourceRef` gains `timestamp?: string | null;`.

**`agent/web/src/api/normalize.ts::normalizeSourceRef`** — read `timestamp` tolerantly (string or null; never `fail()` on absence).

**`agent/web/src/fmt.ts`** — add `fmtStamp(iso: string): string` (date + `HH:MM`, `'—'` on invalid) reusing the existing `Date` guards.

**`agent/web/src/components/ProvenanceChip.tsx`** — add a `<div><dt>Recorded</dt><dd>{fmtStamp(source.timestamp)}</dd></div>` row in the `<dl class="prov-meta">`, rendered only when `source.timestamp` is present. Keep the existing "Recorded value" row (`source.value`) unchanged — verification semantics intact.

Serialization already flows: the chat route uses `source_ref.model_dump(mode="json")` (auto-includes `timestamp`); the rounds route returns the Pydantic `PatientCard` (FastAPI serializes it). The only manual path — the DB round-trip — is fixed in Phase 0.

Risk: any DOM/snapshot test on `ProvenanceChip` gains a row (additive). No CSS token needed; reuse `.prov-meta`.

---

## Phase 3 — Feature 3: enriched seed + time-series line chart

### 3a — Enriched seed (mechanical; ideal cheap-model subagent task)

**`scripts/seed/seed.sql`** — add **multiple dated readings per metric** for showcase patients, each new lab reading = one `procedure_order` (7xxx) + one `procedure_report` (8xxx, distinct `date_report` → distinct `effectiveDateTime`) + one `procedure_result` (9xxxx):
- pt 1015 serial troponin: extend the existing baseline(`-1D`)+overnight(`-2H`) to 4 points (`-1D`, `-18H`, `-6H`, `-2H`: 0.02 → 0.03 → 0.8 → 2.34).
- pt 1008 serial creatinine (AKI trend 3.1 → 2.7 → 2.4 across `-2D`/`-1D`/`-2H`).
- pt 1014 serial sodium (118 → 122 → 124 → 126).
- pt 1004 serial lactate (4.2 → 2.8 → 1.9).
- Optionally add a 4th vitals timepoint for 1–2 patients (vitals series already exist).

This is highly templated: the INSERT column lists, `dd…`/`ee…`/`fc…` UUID patterns, `external_id='SEED'`, and the `uuid_registry`/`procedure_order_code` backfill SELECTs all already generalize to new rows. **Delegate to a cheaper model**, constrained by: keep id ranges contiguous, keep `range`/`abnormal`/`units` self-consistent with existing rows, keep every value a plausible monotone-ish trend. A reviewer only checks id/UUID uniqueness and date ordering.

### 3b — Data-path decision: **new endpoint, not a fattened claim** (recommended)

Add **`GET /v1/patients/{patient_id}/observations?metric=<label>`** rather than extending the card/claim payload.

Reasoning:
- The `Claim`/card contract is deliberately a point-in-time snapshot: exactly one claim per metric (`build_summary_claims`), and verification depends on the invariant that a claim has **one** `source_ref` with **one** `(field, value)`. A full per-metric series is many `(value, timestamp)` pairs — folding it into `Claim` breaks that 1:1 invariant, bloats every rounds payload, and complicates the frozen verification gate.
- A separate, lazily-fetched endpoint keeps charting orthogonal to the verified-claim contract, reuses the existing `FhirClient` + `extract_temporal`/`_effective`, and lets each point stay independently grounded (`resource_id` + `value` + `timestamp`) so the chart is as auditable as a claim.
- It mirrors the auto-mount pattern and reuses `is_authorized(clinician_id, patient_id)` for the same fail-closed access rule the chat path uses.

**New file `copilot/api/routes/observations.py`** — `router` with the GET; resolves `ClinicianId`/`PatientId`, enforces `is_authorized` (403 otherwise), searches `Observation`, groups by humanized metric label (reuse `grounding.describe_resource` + `summary._group_observations`/`_effective`), and returns a series. Because the query is patient-scoped and authorization-gated, no cross-patient leak.

Response shape:
```jsonc
{
  "patient_id": 1015,
  "metric": "Troponin I",
  "unit": "ng/mL",
  "reference_range": { "low": 0.0, "high": 0.04 },   // when present
  "points": [
    { "resource_id": "…", "value": "0.02", "timestamp": "2026-07-…Z", "abnormal": "" },
    { "resource_id": "…", "value": "2.34", "timestamp": "2026-07-…Z", "abnormal": "vhigh" }
  ]
}
```
Fail-closed: a point with no groundable value/timestamp is dropped; an unknown metric returns an empty `points` list (never fabricated). Points are sorted oldest→newest for plotting.

New Pydantic models can live in `contracts.py` (e.g. `ObservationSeriesPoint`, `ObservationSeries`) or locally in the route module.

### 3c — Charting decision: **hand-rolled inline SVG**, no dependency (recommended)

`agent/web/package.json` has zero chart libraries and a deliberately spare tree (react, react-dom, react-aria-components, fontsource). Recommend a hand-rolled SVG line chart over adding Recharts/visx/chart.js:
- A single-series time chart is ~120 lines of SVG (time x-scale, value y-scale, `<polyline>` + point `<circle>`s, a reference-range band, sparse axis ticks). No transitive-dependency cost, no Vite/build churn, no fighting a library's default styles.
- It matches the editorial / React-Aria aesthetic exactly by consuming the existing design tokens: `var(--accent)` for the line, `var(--critical)`/`var(--warning)` for out-of-range points, `var(--line)`/`--line-soft` for axes/grid, `var(--ok-soft)` for the reference band. Fully theme-aware in light/dark via CSS custom properties (per `styles/tokens.css`), and honors `prefers-reduced-motion` (the app already gates animation on it).
- Accessibility: `role="img"` + an `aria-label` summarizing the trend ("Troponin I: 0.02 → 2.34 ng/mL over 22 h, rising"), a `<title>`/`<desc>`, and a visually-hidden `<table>` of the points for screen readers.
- (Consult the **dataviz** skill for palette/mark discipline before writing chart code.)

**New file `agent/web/src/components/MetricChart.tsx`** — props `{ series: ObservationSeries }`; pure SVG; no external deps.

**Drill-down wiring:**
- **`agent/web/src/components/ClaimList.tsx`** — for `Observation` claims, render a "View trend" affordance (a React-Aria `DialogTrigger` + `Popover`/`Dialog`, matching `ProvenanceChip`'s pattern) that lazy-fetches the series and renders `MetricChart`. The metric key is the humanized label already derived for the claim.
- **`agent/web/src/api/client.ts`** — widen `CopilotApi` with `observations(clinicianId, patientId, metric): Promise<ObservationSeries>`.
- **`agent/web/src/api/http.ts`** — implement via `get('/v1/patients/${patientId}/observations?metric=${encodeURIComponent(metric)}')` + a `normalizeObservationSeries` in `normalize.ts`.
- **`agent/web/src/api/mock.ts`** + **`agent/web/src/api/cohort.ts`** — add a per-metric series to the mock cohort (`CohortPhase` gains an optional `series: Record<string, ObservationSeriesPoint[]>`) and implement `observations()` in the mock adapter, so the chart demos without a live backend.
- **`agent/web/src/api/types.ts`** — add `ObservationSeries` / `ObservationSeriesPoint` interfaces.
- **`agent/web/src/styles/app.css`** — a small `.metric-chart` block (line/point/axis/band classes bound to tokens).

Optionally add a chat suggestion ("Show me the troponin trend" / "What meds were started in the last 24 hours?") in `agent/web/src/suggestions.ts` for the demo.

---

## Sequencing & parallelization

1. **Phase 0 (foundation)** — solo, must land first (contract + shared extractor + verification + repository round-trip).
2. After Phase 0, **Phase 1 (backend temporal)** and **Phase 2 (web chip timestamp)** run in **parallel** (different files; both consume the contract).
3. **Phase 3a (seed enrichment)** has no code dependency — start it in parallel immediately (cheap-model subagent), but it gates the *demo* of 3b/3c.
4. **Phase 3b (series endpoint)** depends on Phase 0's `extract_temporal` and the FHIR client (exists); parallel with Phase 2.
5. **Phase 3c (chart UI)** depends on 3b (endpoint) + 3a (data) + Phase 2's normalize plumbing; land last.

Subagent split:
- **Cheap/mechanical:** Phase 3a bulk seed rows (templated INSERTs); the `MetricChart.tsx` SVG scaffolding from a fixed spec; `normalize`/mock series wiring.
- **Higher-judgment:** Phase 0 verification change (fail-closed correctness); the series endpoint grounding; the `_SYSTEM_PROMPT` wording.

---

## Cross-cutting risks

- **Verification withholding (highest risk).** Grounding and verification must derive `timestamp` from the *identical* `extract_temporal`, and a `None` timestamp must skip the check. Any divergence silently withholds real claims. Add explicit tests: timestamp-match served, timestamp-drift withheld, timestamp-absent unaffected.
- **Eval fakes / fixtures lack temporal fields.** `agent/evals/_fake_openemr.py` (`_obs`/`_med`) and `agent/evals/fixtures/__init__.py` (`observation`/`medication_request`) emit only `meta.lastUpdated` — no `effectiveDateTime`/`authoredOn`. With the `None`-skips rule this is *safe* (no regression), but to **exercise** temporal behavior you must add optional `effective`/`authored_on` params there and consider a new eval case in `agent/evals/eval_dataset.jsonl` ("meds started in the last 24 h" → served with a recent med). The existing `cited_value: "0.9"` / withheld / ranking cases are unaffected. Confirm the pytest suite uses `agent/tests` + `agent/evals` fakes, **not** the frozen `.swarm-loop/acceptance/_fake_openemr.py` (treat that tree as read-only).
- **Snapshot/round-trip tests.** `tests/test_summary.py` builds Observations with `effectiveDateTime`, so its claims will now carry a non-`None` `timestamp`; assertions on `.text` and `.source_ref.value/resource_id` still hold, but any full-`source_ref`-equality or serialized-JSON assertion needs updating. `tests/test_repository.py` must round-trip the new field. `tests/test_grounding_evals.py` (in `agent/evals/`) may assert claim shape.
- **CopilotApi seam widening.** Adding `observations()` touches the shared interface (`client.ts`) plus both adapters (`http.ts`, `mock.ts`) and the mock data (`cohort.ts`); keep it optional/lazy so nothing above the API seam breaks.
- **Lab series granularity.** Because lab `effectiveDateTime` is report-level, a lab time-series requires multiple *reports*, not multiple result rows — the seed enrichment must add report rows, or the chart shows a single point. Vitals already satisfy this.
- **Do not inject dates into `claim.text`.** Date digits would be pulled by the numeric-literal gate and must then appear verbatim in the resource — brittle. The timestamp rides in `source_ref.timestamp` only.

---

### Critical files for implementation
- `agent/copilot/domain/primitives.py`
- `agent/copilot/agent/grounding.py`
- `agent/copilot/verification/core.py`
- `scripts/seed/seed.sql`
- `agent/web/src/components/ProvenanceChip.tsx`

(Also central, one tier down: `agent/copilot/memory/repository.py`, a new `agent/copilot/api/routes/observations.py`, and a new `agent/web/src/components/MetricChart.tsx`.)

---

## Summary

The three features rest on **one foundation**: a grounded, verified `FhirReference.timestamp` (Phase 0) carrying `authoredOn` (MedicationRequest ← `prescriptions.date_added`, already seeded) / `effectiveDateTime` (Observation ← report/vitals date). Feature 1 populates it in grounding and adds recently-authored seed meds; Feature 2 surfaces it in `ProvenanceChip`; Feature 3 enriches the seed with multi-point per-metric series and adds a **new `GET /v1/patients/{id}/observations?metric=` endpoint** (recommended over fattening the claim, to protect the verification 1-claim-1-value invariant) feeding a **hand-rolled, token-themed SVG `MetricChart`** (recommended over a chart dependency). The dominant risk is verification: grounding and verification must share the exact temporal extractor, and a `None` timestamp must skip the gate so nothing regresses.
