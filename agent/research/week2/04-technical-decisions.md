# Clinical Co-Pilot — Week 2 Technical Decisions

Brief record of the key technical decisions and what each one costs. Pulled from the Phase 4
options analysis; revisit an entry if its fragility flag fires. Each was a genuine fork with a
real alternative — non-decisions are omitted.

## 1. Multi-agent orchestration — hand-rolled supervisor
**Decision:** Build the supervisor + intake-extractor + evidence-retriever + critic by hand in a
new `copilot/graph/` package, each a Stub/Claude dual behind a `Protocol` + `build_*` factory, with
typed logged `Handoff` objects and nested Langfuse spans.
**Why:** Perfect fit with the existing hand-rolled Anthropic loop + Stub/Real pattern; the doc
explicitly permits "another inspectable orchestration framework" and grades *comprehensibility*.
**Benefits:** Zero external-telemetry (PHI) surface; trivially LLM-free CI via Stub twins; no
LangChain dependency-range / warning friction against the pinned Anthropic SDK + `filterwarnings=error`;
no framework churn; inspectability we fully own (typed handoffs + nested traces).
**Tradeoffs:** We build routing/state/tracing ourselves; "inspectable" is our responsibility to
prove; grading optics (no brand-name framework) — mitigated by a crisp rationale + the nested traces.
**Alternatives considered:** LangGraph (heavy, churny, PHI-telemetry lockdown needed); Pydantic AI
(fits Pydantic style but newer + its own model-client layer conflicts with the pinned SDK); OpenAI
Agents SDK (OpenAI-centric — poor fit for an all-Claude stack).
**Fragility flag:** If a grader hard-requires a named framework, reopen — LangGraph is the drop-in.

## 2. Document extraction — OCR + Claude vision, reconciled
**Decision:** Local Tesseract OCR (word-level boxes) + Claude vision for strict-schema extraction,
reconciled by matching each extracted value to OCR tokens (bbox + match-score confidence; unmatched
= flagged unsupported).
**Why:** The bounding-box overlay is a hard requirement and Claude vision doesn't emit reliable
coordinates; OCR supplies accurate boxes *and* independent corroboration.
**Benefits:** Accurate overlay; "extraction without invention" enforced by corroboration; a real
confidence signal; the reconciliation doubles as the deterministic document-grounding anchor for
the verifier; all PHI stays in-container.
**Tradeoffs:** Two-stage pipeline + reconciliation logic to build and test; OCR errors on poor scans
(mitigated: flag, don't invent).
**Alternatives considered:** Claude-vision-only (unreliable boxes, self-reported confidence, no
independent check); cloud Document AI (extra PHI-to-vendor surface, cost, ops — overkill since we
control the demo docs).
**Fragility flag:** If real-world scan quality tanks OCR match rates, revisit a cloud Document-AI
provider (with a BAA) for the boxes.

## 3. Citation & verification — `{fhir | document | guideline}` union, agent-store grounding
**Decision:** Generalize the citation to a discriminated union; extend the fail-closed verifier with
a **document** path (re-check value vs. the stored schema-validated extraction, bbox ≥ confidence
threshold) and a **guideline** path (quote-in-chunk). Grounding for document facts is
**agent-store-authoritative**.
**Why:** Verified against the live route maps — **labs/Observations are read-only in both the FHIR
and Standard APIs**, so lab-PDF values cannot become FHIR resources to re-fetch. The stored,
schema-validated extraction is the only defensible re-check target.
**Benefits:** Preserves the fail-closed invariant across all three source types; keeps the trust
boundary in deterministic code; no fragile write-then-reground dance.
**Tradeoffs:** A confidence threshold to tune; a schema migration for the claim union (backward-compat
via defaults).
**Alternatives considered:** Write extractions into OpenEMR as FHIR first and reuse the gate
unchanged — infeasible (no Observation write, no DocumentReference read path) and risks the
"duplicate/untraceable records" the doc warns against.
**Fragility flag:** If OpenEMR ever gains Observation write + a read-back path, the FHIR-authoritative
model becomes viable and cleaner — revisit.

## 4. Storage & data authority — OpenEMR owns source docs; agent DB owns derived facts (+ confirmed write-back)
**Decision:** Store the source document in OpenEMR (`POST /api/patient/:pid/document`, readable back
as a FHIR `DocumentReference`); keep extractions/citations/corpus/audit agent-store-authoritative and
append-only; additionally allow **physician-confirmed** write-back of intake-derived
meds/allergies/problems via the existing propose→confirm gate.
**Why:** "One source of truth per data type, no silent overwrites"; the doc requires source-doc
storage and derived-fact persistence; the source-doc upload alone already demonstrates a real
OpenEMR write with typed contract + correlation-ID + audit + traceability.
**Benefits:** Clean authority split; no duplicate/untraceable records; the stronger round-trip story
via the reserved `agent_proposed_physician_confirmed` entry mode; reuses the Week 1 write pipeline.
**Tradeoffs:** Reopens the gated-OFF write path (enable flag, extend the write client to
problems/allergies, more security surface + enablement steps + build).
**Alternatives considered:** Source-doc-only (read-only ingestion) — simpler/safer but a weaker
data-authority narrative; the user chose the fuller write-back.
**Fragility flag:** Write-back stays behind `writeback_enabled` + physician confirmation; if the
enablement/attribution story slips, fall back to source-doc-only for the demo.

## 5. Hybrid RAG — pgvector + FTS + RRF, Voyage embeddings + Cohere rerank
**Decision:** pgvector on the existing `agent-postgres` + Postgres full-text (sparse) fused with RRF,
Voyage `voyage-3.5` embeddings, Cohere `rerank-v3.5`. Corpus = repo files + ingest script; vectors
precomputed/cached.
**Why:** Highest retrieval quality (Voyage is Anthropic's recommended embedder; Cohere rerank is
named in the doc); pgvector reuses the existing DB (no new service) and the `JSONType` dual-dialect
pattern for the SQLite test path.
**Benefits:** Strong relevance; one datastore; corpus reproducible from the repo (backup req); public
corpus + de-identified queries keep PHI exposure minimal.
**Tradeoffs:** Two external vendors + two secrets + two BAAs; rerank latency feeds the retrieval SLO;
model-version/pricing drift; Anthropic has no embeddings API so a non-Claude embedder is unavoidable.
**Alternatives considered:** Cohere embed+rerank (one vendor, lower quality); local embed+rerank
(zero egress but heavy torch stack vs. the lean container).
**Fragility flag:** Vendor pricing/model-version change or a deprecation → re-evaluate; the
Protocol-wrapped retriever/reranker makes the swap contained.

## 6. Eval gate + CI — two-tier (deterministic blocking, live non-blocking)
**Decision:** The PR-blocking gate runs stubbed/deterministic (50 cases, 5 boolean rubrics, baseline
+ >5%/threshold regression, git hook + CI job, an injected-regression self-proof); a separate
live-model quality run is non-blocking.
**Why:** The doc's HARD GATE (graders inject a regression → CI must fail) requires determinism, and
the doc forbids live API in CI integration tests; the Week 1 stub runner already exits nonzero on
failure.
**Benefits:** No keys/cost/flakiness in the gate; protects the safety invariants (schema, citation,
grounding, refusal, PHI) on every PR; live tier still measures real model quality.
**Tradeoffs:** The blocking gate tests deterministic glue + fixtures, not live model quality — that
is deliberately measured in the separate tier.
**Alternatives considered:** Live-model blocking gate (needs CI keys, costs $, flaky against the 5%
threshold, violates the "no live API in CI" rule).
**Fragility flag:** If stubbed fixtures drift from real model behavior, the live tier is the
early-warning signal — keep it running.

## 7. Operational dashboard — Langfuse + hand-rolled status page
**Decision:** Keep Langfuse (v2) for LLM traces/cost; add an agent-served status page reading
agent-DB aggregates + eval artifacts.
**Why:** Langfuse is LLM-observability-focused (weak on error rate/latency/queue); a hand-rolled page
fits the house style and directly answers "a grader sees health at a glance."
**Benefits:** Lean, reproducible, demo-friendly, no new infra on the single droplet.
**Tradeoffs:** We build the panel; not as feature-rich as Grafana alerting.
**Alternatives considered:** Prometheus+Grafana (best production optics, two more containers +
config); Langfuse-only (insufficient operational health coverage).
**Fragility flag:** At real multi-tenant scale, graduate to Prometheus/Grafana.

## 8. Bounding-box overlay — hand-rolled SVG over the page image
**Decision:** Render the page PNG (already produced for OCR) as a backdrop with absolutely-positioned
SVG rectangles from the normalized bbox; click a citation → scroll + highlight.
**Why:** Inputs are scanned (image) PDFs, so pdf.js's text layer is moot; page PNGs + OCR boxes
already exist.
**Benefits:** Exact grain-fit (hand-rolled SVG + React Aria + CSS tokens), zero new deps, uniform
for both doc types, theme-aware + accessible.
**Tradeoffs:** We render page images server-side (already doing it for OCR) and ship them; no native
PDF text selection (not needed).
**Alternatives considered:** pdf.js (heavy, Vite worker setup, text layer moot); react-pdf/annotation
lib (heavy UI dep clashing with the stated taste).
**Fragility flag:** None material.

## 9. Deploy — pgvector swap on the existing droplet (default choice)
**Decision (made for you — challenge if you disagree):** Swap `agent-postgres` to
`pgvector/pgvector:pg16`; add Voyage/Cohere secrets to the gitignored `.env`; new `/v1/documents`
route auto-mounts (Caddy already proxies `/v1/*`, only a `request_body max_size` bump needed); cache
page images in the agent DB (source bytes authoritative in OpenEMR).
**Why:** Lowest-friction add on the single-droplet topology; no new datastore service.
**Benefits:** Minimal ops delta; one DB; transactional with the rest of the agent state.
**Tradeoffs:** Page-image bytes in Postgres won't scale to large volumes.
**Alternatives considered:** Dedicated vector DB (Qdrant/Chroma) + MinIO object store — noted as the
scale path, not built.
**Fragility flag:** At document-heavy scale, move page images to MinIO/S3 and vectors to a dedicated
store.

## 10. Intake schema — align extraction to OpenEMR record types (NEXT PHASE — Early Submission)

**Status:** DONE (Early Submission) via **approach (A) — category-tag**. Each intake fact now
carries a typed OpenEMR `category` so it maps 1:1 to the record it round-trips to. (MVP had shipped
generic, strictly-validated, per-fact-cited `ExtractedFact` rows with a free-form `field_path` — all
required intake fields extracted + cited, but untagged.)

**As built:** `IntakeCategory` StrEnum (`demographic | chief_complaint | medication | allergy |
medical_problem | family_history`) + `IntakeFact(ExtractedFact)` with a required `category`;
`IntakeForm.facts: list[IntakeFact]` (lab `LabReport.facts` stays plain `ExtractedFact`, so lab
extraction is byte-for-byte unchanged). Persisted via a nullable `extracted_fact.category` column
(migration `0007`, additive/reversible). The vision tool-schema for intake now requires `category`,
so the model tags every fact; the extraction prompt names the six homes. Frozen harness stayed 12/12
(cycle 7). Live-verified: real Claude vision tags demographics/chief-concern/medications/allergies/
family-history correctly. **Still open (next):** wire an auto-propose bridge from categorized intake
facts → `WriteKind` write candidates (allergy/medication/medical_problem) through the existing
propose→confirm gate — the category makes this a typed lookup instead of a `field_path` heuristic.

**Plan (confirmed direction):** reshape the intake extraction so every fact maps to where OpenEMR
actually stores it, so intake round-trips cleanly and the allergy/medication/problem facts flow
straight into the existing write-back path. OpenEMR storage reference:

| Intake field | OpenEMR storage | Key columns |
|---|---|---|
| Demographics | `patient_data` (1 row/patient) | `fname, lname, mname, DOB, sex, street, city, state, phone_home, email` |
| Chief concern | `form_encounter.reason` | `reason` |
| Current medications | `lists` `type='medication'` | `title, begdate, enddate, diagnosis` |
| Allergies | `lists` `type='allergy'` | `title, begdate, reaction` |
| Medical problems | `lists` `type='medical_problem'` | `title, begdate, diagnosis` |
| Family history | `history_data` (1 row/patient) | `history_*` free-text columns |

Note OpenEMR unifies meds/allergies/problems in one type-discriminated `lists` table; our
`domain/writes.py` (`MedicationWrite`/`AllergyWrite`/`MedicalProblemWrite`) already mirrors those
columns exactly.

**Two implementation levels (pick at build time):**
- **(A) Category-tag (lower risk):** add an `IntakeCategory` enum — `demographic | chief_complaint |
  medication | allergy | medical_problem | family_history` — to each intake fact, mapping 1:1 to
  `lists.type` / `patient_data` / `form_encounter.reason` / `history_data`. Keeps the strict schema +
  per-fact bbox citations; likely needs a small `extracted_fact.category` migration (0007) + repo
  serializer + vision/stub extraction + gate-fixture updates.
- **(B) Full typed sections (fuller match):** restructure `IntakeForm` into typed sub-models
  (`demographics`, `medications[]`, `allergies[]`, `family_history[]`) mirroring the OpenEMR columns;
  reworks the reconciliation/provenance flow so each structured field carries its own bbox citation.

**Why deferred:** MVP was already deployed + submission-ready; the change touches frozen-green code,
the eval gate (`feat_ingestion`), the extraction path, fixtures, and requires a redeploy — not worth
the regression risk on the MVP deadline for a field-tagging refinement. Do it as a guarded change
(re-run the eval gate + acceptance suite; redeploy) in the Early Submission window.
**Fragility flag:** family history in `history_data` is free-text per-patient columns, not a
`lists`-style row set — mapping it structurally is the least clean of the six; scope it last.
