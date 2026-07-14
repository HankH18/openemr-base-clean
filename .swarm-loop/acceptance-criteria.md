# Acceptance criteria — the frozen "done" definitions (Week 2)

Each criterion = one or more pytest tests under `.swarm-loop/acceptance/<feature>/`; a criterion passes
only if all its tests pass. Feature-metric targets in `goals.json` equal these counts. Tests are
black-box, deterministic, LLM-free (StubAgent + respx + recorded fixtures; NO live API; NO tesseract
binary — OCR via recorded fixture tokens behind an OCR `Protocol` stub). **52 criteria across 8 feature
metrics.** Authored before implementation → the baseline is honestly all-failing.

## Harness contract (frozen with the goals)
- `run.py --feature X` prints (bare number, last stdout line) the count of passing criteria under
  `.swarm-loop/acceptance/X/`; `run.py --pass-rate` prints the overall %.
- `project_tests.py` (existing `agent/tests`+`agent/evals` pass %), `quality_count.py` (ruff+mypy count),
  `phi_check.py`, `web_check.py` — all bare-number-last-line.
- **Exit codes:** 0 = measured; 2 = usage error; **3 = ENVIRONMENT ERROR** (stale venv, missing dep/binary,
  empty scan corpus, failed scanner self-proof) with NO number printed — so env noise never reads as a
  regression. Every entry point self-syncs `agent/.venv` (`uv pip install --python .venv/bin/python -e '.[dev]'`)
  once on import-probe failure, then retries.
- `phi_check.py` exits 3 unless the corpus is non-empty with the expected event families
  {doc.ingest, extraction.run, guideline.retrieve, worker.handoff, verification.result} AND a planted-PHI
  sensitivity self-proof flags first. `web_check.py` runs `npm ci` if node_modules absent, then build + vitest.

---

## feat_verification (F1+F5) · target 8
1. Citation-union round-trip + back-compat (`FhirCitation|DocumentCitation|GuidelineCitation`; Week-1 claims rehydrate as fhir; byte-equal round-trip via repository serializers).
2. Strict extraction schemas reject malformed/partial `LabReport`/`IntakeForm`/`ExtractedFact` input (validation error, never coerced).
3. `MemoryRepository` CRUD accessors round-trip for source_document/document_page/extraction/extracted_fact/guideline_document/guideline_chunk on SQLite (JSONType + embedding_column fallback exercised).
4. Interim fail-closed: with only F1, verifier verifies fhir claims as before (existing suite green); document/guideline citations treated unverifiable → dropped (no crash, no false-verify).
5. Document grounding positive: value re-checked vs stored extracted_fact; requires supported=True + bbox/confidence ≥ threshold; agent-store authoritative.
6. Document grounding negatives: value mismatch / missing extraction / supported=False / below-threshold each drop the claim.
7. Guideline grounding positive+negative: quote-in-chunk verifies; absent quote or missing chunk drops.
8. Fail-closed end-to-end: mixed-claim answer keeps only verifiable; zero survivors → withheld (Week-1 contract intact).

## feat_ingestion (F2+F3+F4a) · target 8
1. Settings + pricing: `Settings` exposes W2 fields (voyage/cohere keys, OCR opts, doc confidence threshold, flags) with types/defaults; `pricing.py` resolves NONZERO rates for the vision model, voyage-3.5, rerank-v3.5.
2. Rasterization: fixture PDF → per-page images with width/height (pypdfium2), deterministic.
3. OCR Protocol stub-first: `StubOcr` replays fixture tokens; `build_ocr` selects stub when tesseract absent; whole suite passes with no binary.
4. Extraction schema-guarded + append-only: stubbed vision tool-forced JSON validates through strict schemas; invalid field rejected; re-ingest = NEW extraction row, priors intact.
5. Reconciliation: matched value → bbox + match_confidence; unmatched persists supported=False, flagged.
6. Pipeline persistence + audit: `attach_and_extract` writes source_document+document_page+extraction+extracted_fact w/ correlation_id + audit; status uploaded→extracting→extracted; mid-failure → status=failed, zero orphan facts.
7. `upload_document` happy path: multipart POST /api/patient/:pid/document (respx OpenEMR); openemr_document_id stored; content-hash dedupe → idempotent retry.
8. `upload_document` failure path: OpenEMR error → ingestion fails closed (status=failed, no extraction, error surfaced on status endpoint).

## feat_writeback (F4b) · target 3  [PROMOTED — 7-cycle budget]
1. `create_medical_problem` via OpenEmrWriteClient (respx OpenEMR Standard API) through the propose→confirm gate; committed write returns an id; audited `entry_mode=agent_proposed_physician_confirmed`.
2. `create_allergy` similarly through propose→confirm; committed + audited.
3. Gate enforced: `propose()` yields a typed ProposedWrite and does NOT commit; commit requires the explicit confirm step (agent structurally cannot self-commit); re-verify at commit; idempotent double-confirm.

## feat_rag (F6) · target 6
1. Corpus + reproducible ingest: in-repo corpus files w/ license metadata; `scripts/ingest_guidelines.py` chunks+persists; re-run idempotent (no dup chunks).
2. Embeddings behind Protocol: Voyage Stub/Real, 1024-d, precomputed+cached vectors at query time (zero network in tests); pgvector-PG/JSON-SQLite fallback exercised.
3. Hybrid retrieve + RRF: Postgres-FTS sparse + dense fused with RRF; ordering verified vs hand-computed fixture.
4. Rerank + fallback: Cohere Stub reorders; reranker absence/failure → fused-order fallback (logged), never errors.
5. De-identified egress: `deidentify()` scrubs the query; planted identifiers never appear in stub-captured outbound payloads.
6. Typed evidence contract: top-K carry chunk_id+section, typed as guideline evidence (not patient facts); empty retrieval → explicit no-evidence, no fabricated citation.

## feat_graph (F7) · target 7
1. Supervisor routing: doc-in-scope→intake-extractor; guideline-need→evidence-retriever; both/neither correct; iteration cap → safe "insufficient grounded information" withhold.
2. Typed logged handoffs: each transition emits `Handoff{from_agent,to_agent,reason,payload}` into the captured artifact, in order.
3. Stub/Real workers keyless: supervisor+both workers+critic behind Protocol+factory; full stub graph runs with no key.
4. Critic gate: a drafted claim without a machine-readable citation is rejected deterministically; verifier stays in path (critic augments).
5. Span nesting (edit observability/base.py + langfuse_backend.py flat→parent/child): worker spans are CHILDREN of the supervisor span (ids asserted in captured trace); trace reconstructs from correlation_id alone.
6. Contract preservation: graph returns same VerificationResult to chat service; "no grounded claims → withheld" override still lives+fires in chat/service.py (no hot-path migration).
7. Observability fields: a stubbed E2E run's captured artifact contains all 7 keys — tool/handoff sequence, latency, tokens, cost, retrieval hits, extraction confidence, eval outcome.

## feat_api (F8) · target 9  [+status page]
1. `POST /v1/documents` multipart → 202 {document_id,status,correlation_id}; auth required; RBAC rounding-list gate → 403 off-list.
2. Status + page endpoints: `GET /v1/documents/{id}` returns status/facts/citations; `GET .../pages/{n}` serves page image; correct 404s.
3. Evidence separation: evidence endpoint / extended chat carries guideline evidence as a separate labeled block, never mixed into patient-fact claims.
4. OpenAPI sync + contract tests: normalized diff of app-generated schema vs committed `agent/openapi/week2.yaml` is clean; contract tests assert live shapes match.
5. Graded `/ready`: probes document_store/pgvector/embedder/reranker; degraded dep reflected in graded payload; `/health` stays liveness-only.
6. JSON logging wired: dictConfig active; a captured record parses as JSON w/ correlation_id; records feed the phi_check corpus.
7. SLO/alerts artifact (structural): stubbed run emits a latency-report artifact w/ numeric p50/p95 for doc-ingestion + evidence-retrieval; `OBSERVABILITY.md` has SLO defs + Week-2 alert defs w/ response actions (required-section check). No pass/fail on p95 values.
8. Packaging file-check (deterministic): agent Dockerfile installs tesseract; docker-compose.deploy.yml uses pgvector/pgvector:pg16; Caddy request-body size ≥ upload limit. Actual deploy = operator step.
9. **Status page** (agent-served, Decision 7): a `/status` (or `/v1/status`) endpoint/asset returns the health aggregates — ingestion count, extraction field-level pass rate, retrieval hit rate, worker routing decisions, eval pass/fail per category, p50/p95, error rate — read from the agent DB + eval artifacts (structural: keys present, values well-typed).

## feat_frontend (F9) · target 6  [pulled to C3]
1. Build: `npm run build` succeeds (web_check `npm ci` first if node_modules absent).
2. Vitest infra: vitest configured (new) + executes the unit suite.
3. ProvenanceChip variants: fhir/document/guideline render distinct labeled variants.
4. Overlay geometry: normalized [x,y,w,h] → SVG rect coords for given image dims; scaling/aspect cases correct.
5. Upload flow: FileTrigger component posts multipart to /v1/documents (mocked); 202 + error states handled.
6. Citation adapter: API Citation union → UI model incl. fail-safe fallback for unknown source_type (no crash).

## feat_evalgate (F10a C1/C2 + F10b C3–C4) · target 5  [pinned]
1. Golden set ≥50 with ≥8 per rubric (incl. adversarial safe_refusal + planted-PHI no_phi_in_logs). [F10b]
2. Runner emits all 5 booleans per case (schema_valid, citation_present, factually_consistent, safe_refusal, no_phi_in_logs), stubbed/LLM-free. [F10a]
3. Baseline + >5% relative regression → nonzero exit. [F10a]
4. Injected-regression self-proof: harness injects a known regression (fault-injection flag / doctored baseline) and asserts the gate exits nonzero. [F10a]
5. Enforcement wiring: a git hook invokes the gate pre-push AND the existing `.gitlab-ci.yml` `agent:tests` job invokes it (NOT GitHub Actions); GitLab branch-protection = documented operator step. [F10a]
