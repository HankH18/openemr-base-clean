# Backlog — Week 2 multimodal evidence agent (FINAL, user-approved, 7 cycles)

Atomic features → goals.json metric buckets. Frozen "done" definitions in `.swarm-loop/acceptance-criteria.md`
(52 criteria). Phase 0 (migrations 0005/0006 + models + pgvector) DONE (f2de171). Frozen constraints:
fail-closed verification; no PHI in logs/traces/evals; schema-as-source-of-truth; LLM-free CI.

Legend: **metric** · **deps** · **owns** (file-ownership for wave planning; see codebase-map hazards).

## F1 — Citation union + schemas + repo accessors · metric: feat_verification
Discriminated union `Claim.source_ref {fhir|document|guideline}` (back-compat default fhir); strict
`LabReport`/`IntakeForm`/`ExtractedFact` schemas; repository (de)serializers for the union; **MemoryRepository
CRUD accessors** for all Phase-0 tables; **minimal FhirCitation verifier integration** (non-fhir → unverifiable/
fail-closed interim, keeps repo green). deps: none · owns: domain/primitives.py, domain/contracts.py, new
domain/documents.py, memory/repository.py

## F2 — Config + pricing · metric: feat_ingestion (crit 1)
Settings W2 fields (voyage/cohere keys, OCR opts, doc confidence threshold, flags); pricing rows for vision +
voyage-3.5 + rerank-v3.5. deps: none · owns: config.py, observability/pricing.py

## F3 — Doc extraction pipeline + attach_and_extract · metric: feat_ingestion
pypdfium2 raster → **OCR Protocol (StubOcr fixture tokens; real tesseract only in Docker)** → Claude-vision
(Stub/Real, tool-forced JSON, strict schemas) → reconcile (bbox+confidence; unmatched=supported=False) → persist
append-only. deps: F1,F2, **deps-task** (pypdfium2/Pillow/pytesseract hard-pinned — own sequenced task) · owns:
new copilot/documents/, pyproject.toml (deps-task only)

## F4a — Write client: document upload · metric: feat_ingestion (REQUIRED)
`upload_document` via Standard REST `POST /api/patient/:pid/document`; store openemr_document_id; content-hash
dedupe. deps: F1 · owns: fhir/write_client.py (upload only)

## F4b — Write client: problems/allergies write-back · metric: feat_writeback  [PROMOTED, 7-cycle budget]
medical_problem + allergy writes through the existing propose→confirm gate; entry_mode
agent_proposed_physician_confirmed; agent cannot self-commit. deps: F1, F4a · owns: fhir/write_client.py
(problems/allergies), domain/writes.py, writeback/service.py

## F5 — Verifier extension: document + guideline grounding · metric: feat_verification  (HIGHEST RISK)
document path (re-check vs stored extracted_fact + bbox≥threshold, agent-store authoritative) + guideline path
(quote-in-chunk); preserve fail-closed. deps: F1 · owns: verification/core.py, serve.py, rules.py

## F6 — Hybrid RAG + rerank · metric: feat_rag
Corpus files + `scripts/ingest_guidelines.py`; Voyage embed (Stub/Real, httpx, 1024-d, cached); Postgres FTS +
pgvector + RRF; Cohere rerank (Stub/Real) w/ fused-order fallback; `deidentify()` before egress. deps: F1,F2 ·
owns: new copilot/rag/, scripts/ingest_guidelines.py, corpus/

## F7 — Supervisor + workers + critic graph · metric: feat_graph
New copilot/graph/ (supervisor + intake-extractor + evidence-retriever + critic), Stub/Real Protocol+factory,
typed logged handoffs; **span nesting** (edit observability/base.py + langfuse_backend.py flat→parent/child) is
F7's named sub-task; 7-key observability fields; graph returns same VerificationResult (chat-service override
stays). deps: F3,F5,F6 · owns: new copilot/graph/, observability/base.py, observability/langfuse_backend.py

## F8 — HTTP endpoints + OpenAPI + graded /ready + JSON logging + status page + packaging · metric: feat_api
routes/documents.py (POST /v1/documents, status, pages, evidence); committed openapi/week2.yaml + contract tests;
graded /ready deps; wire JSON logging; agent-served status page (health aggregates); SLO/alerts artifact +
OBSERVABILITY.md sections; packaging (Dockerfile tesseract, compose pgvector image, Caddy body-size). deps:
F3,F6,F7 · owns: api/routes/documents.py, api/readiness.py, openapi/, observability/logging.py, api/routes/status.py,
Dockerfile, docker-compose.deploy.yml, Caddyfile*, OBSERVABILITY.md, DEPLOY.md

## F9 — Frontend · metric: feat_frontend  [pulled to C3]
React Aria FileTrigger upload; extend ProvenanceChip (doc/fhir/guideline); hand-rolled SVG-over-image bbox
overlay; reuse MetricChart; **vitest setup (new)**; citation-union adapter w/ unknown-type fallback. deps: F8
(contracts) · owns: agent/web/src/**, agent/web/package.json, vitest config

## F10a — Eval gate scaffold · metric: feat_evalgate  [EARLY, C1/C2]
Runner emits all 5 booleans/case; baseline + >5% regression → nonzero; injected-regression self-proof; git hook +
extend `.gitlab-ci.yml agent:tests` (NOT GitHub Actions); seed ~10-15 cases. deps: F1 · owns: evals/, git-hook
script, .gitlab-ci.yml

## F10b — Grow golden set · metric: feat_evalgate  [rolling C3–C4]
Grow evals/eval_dataset.jsonl to ≥50 cases, ≥8/rubric (incl adversarial safe_refusal + planted-PHI). deps: F3,F5 ·
owns: evals/eval_dataset.jsonl, fixtures

## 7-cycle plan (sequencing; refined each cycle from the analysis)
- **C1** wave0: F1, F2, F3-deps-task (sequenced enablers). wave1: F3-core, F4a, F6-corpus, F10a.
- **C2**: F5 (verifier), F6 (retriever), F7 (graph + span-nesting), F4b (write-back).
- **C3**: F8 (endpoints/OpenAPI/ready/logging/status/packaging), F9 (overlay+vitest+ProvenanceChip), F10b start,
  **quality-review task** (cycle-3 cadence).
- **C4**: F9 finish (upload wiring), F10b finish (→50), observability/SLO polish, fix regressing/stalled metrics.
- **C5–C7**: convergence — fix regressions/stalls, harden, live-verify; C6 quality-review; protect at-target metrics.
Never parallelize two tasks sharing a file (codebase-map hotspots: config.py, primitives/contracts, models/
repository, verification/core, pyproject deps, web contract files — single-writer per cycle).

## CLOSEOUT — converged cycle 3/7 (12/12 targets), see reports/final-report.md
- Cycles 3 (F8+F9+F10b) + 4 (graph-into-chat completeness) + 5 (guarded cleanup) landed on main;
  all 12 metrics green at c3, c4, and c5. Worktrees + task/* branches cleaned up. HEAD ba8adb7.
- **DEFERRED quality backlog:**
  1. ~~Repository-gateway consolidation~~ **DONE (cycle 5)**: the 5 inline by-id/by-hash/by-source
     select() fetches (verification/serve.py, graph/intake_extractor.py, documents/pipeline.py dedupe,
     rag/retriever.py, rag/ingest.py) now route through MemoryRepository accessors (07b1868 added
     accessors; ba8adb7 wired call sites + dropped dead SELECTs). ~12 scoped aggregates/sweeps/cursor
     (status.py, retention.py, worker/runtime.py) left as-is — not gateway erosion.
  2. ~~_ISO_DATE_RE dedup~~ **DONE (a31ede6)**: one ISO_DATE_RE + is_iso_date() in domain/primitives.py.
  3. Real* graph workers realness vs stubs — **STILL DEFERRED**; needs a keyed env; investigate, don't force.
